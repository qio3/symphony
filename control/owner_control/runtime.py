from __future__ import annotations

import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .snapshot import SnapshotBuilder
from .state_store import StateStore


class SnapshotService:
    def __init__(
        self,
        *,
        symphony: Any,
        github: Any,
        test_environment: Any,
        supervisor: Any,
        state_store: StateStore,
        worker_limit: int,
        canonical_ref: str,
        cache_seconds: float = 5.0,
        github_cache_seconds: float = 60.0,
        github_retry_seconds: float = 60.0,
    ):
        self._symphony = symphony
        self._github = github
        self._test_environment = test_environment
        self._supervisor = supervisor
        self._state_store = state_store
        self._worker_limit = worker_limit
        self._canonical_ref = canonical_ref
        self._cache_seconds = cache_seconds
        self._github_cache_seconds = github_cache_seconds
        self._github_retry_seconds = github_retry_seconds
        self._builder = SnapshotBuilder()
        self._cache_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._source_lock = threading.RLock()
        self._github_refresh_lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._github_cached_at = 0.0
        self._github_retry_at = 0.0
        self._github_last_error: Exception | None = None
        self._containment_cache: dict[tuple[str, str], bool] = {}
        self._control_generation = 0
        stored_sources = self._state_store.read().get("last_good_sources")
        self._last_good_sources = deepcopy(stored_sources) if isinstance(stored_sources, dict) else {}

    def snapshot(self, *, fresh: bool = False) -> dict[str, Any]:
        if fresh:
            return self._refresh(force_github=True)

        with self._cache_lock:
            now = time.monotonic()
            cached = self._cached
            expired = cached is not None and now - self._cached_at >= self._cache_seconds

        if cached is None:
            return self._refresh()
        if expired:
            self._start_refresh()
        return cached

    def cached_snapshot(self) -> dict[str, Any]:
        """Return only an already-collected snapshot without touching any source."""
        with self._cache_lock:
            if self._cached is None:
                raise RuntimeError("owner-control snapshot is not ready")
            return self._cached

    def invalidate(self) -> None:
        refreshed_at = datetime.now(timezone.utc).isoformat()
        control_state = self._state_store.read()
        try:
            service = _service_with_intent(
                self._supervisor.status(),
                control_state,
                now=time.time(),
            )
            supervisor_source = _fresh_source(refreshed_at)
        except Exception as error:
            service = {"live": False, "status": "unknown", "reason": str(error)}
            supervisor_source = _unavailable_source(error)
        with self._source_lock:
            self._github_cached_at = 0.0
            self._github_retry_at = 0.0
            self._github_last_error = None
        with self._cache_lock:
            self._control_generation += 1
            if self._cached is not None:
                self._cached = self._patched_cached_snapshot(
                    self._cached,
                    intake_active=bool(control_state.get("intake_active", True)),
                    service=service,
                    supervisor_source=supervisor_source,
                    control_transition=_control_transition_active(control_state),
                    refreshed_at=refreshed_at,
                )
            self._cached_at = 0.0
        self._start_refresh(force_github=True)

    def _refresh(self, *, force_github: bool = False) -> dict[str, Any]:
        with self._refresh_lock:
            return self._collect_and_cache(force_github=force_github)

    def _start_refresh(self, *, force_github: bool = False) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            return
        threading.Thread(
            target=self._refresh_from_acquired_lock,
            args=(force_github,),
            name="owner-control-snapshot-refresh",
            daemon=True,
        ).start()

    def _refresh_from_acquired_lock(self, force_github: bool) -> None:
        try:
            self._collect_and_cache(force_github=force_github)
        finally:
            self._refresh_lock.release()

    def _collect_and_cache(self, *, force_github: bool = False) -> dict[str, Any]:
        while True:
            with self._cache_lock:
                generation = self._control_generation
            value = self._collect(force_github=force_github)
            with self._cache_lock:
                if generation != self._control_generation:
                    force_github = True
                    continue
                self._cached = value
                self._cached_at = time.monotonic()
                return value

    def _collect(self, *, force_github: bool = False) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        sources: dict[str, dict[str, Any]] = {}
        changed = False
        refreshed_at = datetime.now(timezone.utc).isoformat()
        control_state = self._state_store.read()

        try:
            service = _service_with_intent(
                self._supervisor.status(), control_state, now=time.time()
            )
            sources["supervisor"] = _fresh_source(refreshed_at)
        except Exception as error:
            service = {"live": False, "status": "unknown", "reason": str(error)}
            failures.append(_source_failure("supervisor", error))
            sources["supervisor"] = _unavailable_source(error)

        if _service_is_transitioning(service) or _service_is_confirmed_stopped(service):
            runtime = self._inactive_runtime_projection()
            last_runtime = self._stored_source("runtime")
            sources["runtime"] = {
                "status": "stale",
                "confirmed_at": last_runtime.get("confirmed_at") if isinstance(last_runtime, dict) else None,
                "error": _inactive_runtime_reason(service),
            }
        else:
            try:
                runtime = self._symphony.state()
                changed = self._remember_source("runtime", runtime, refreshed_at) or changed
                sources["runtime"] = _fresh_source(refreshed_at)
                recovered_intent: dict[str, Any] = {}
                if _restart_expected(control_state) and service.get("live") is True:
                    recovered_intent["expected_service_restart_until"] = 0
                if (
                    bool(control_state.get("expected_service_stop"))
                    and service.get("live") is True
                ):
                    recovered_intent["expected_service_stop"] = False
                if control_state.get("service_action_in_progress"):
                    recovered_intent.update(
                        {
                            "service_action_in_progress": None,
                            "service_action_in_progress_until": 0,
                        }
                    )
                if recovered_intent:
                    self._state_store.update(recovered_intent)
            except Exception as error:
                if _restart_expected(control_state) and service.get("live") is True:
                    service = {**service, "status": "starting"}
                    runtime = self._inactive_runtime_projection()
                    stored_runtime = self._stored_source("runtime")
                    sources["runtime"] = {
                        "status": "stale" if isinstance(stored_runtime, dict) else "unavailable",
                        "confirmed_at": (
                            stored_runtime.get("confirmed_at")
                            if isinstance(stored_runtime, dict)
                            else None
                        ),
                        "error": str(error),
                    }
                else:
                    runtime, sources["runtime"] = self._stale_or_unavailable(
                        "runtime", _empty_runtime(), error
                    )
                failures.append(_source_failure("runtime", error))

        github_value, sources["github"], github_error = self._github_projection(
            force=force_github,
            refreshed_at=refreshed_at,
        )
        project = github_value["project"]
        canonical = github_value["canonical"]
        if github_error is not None:
            failures.append(_source_failure("github", github_error))

        try:
            test = self._test_environment.deployment()
            changed = self._remember_source("test", test, refreshed_at) or changed
            sources["test"] = _fresh_source(refreshed_at)
        except Exception as error:
            test, sources["test"] = self._stale_or_unavailable(
                "test", {"sha": None, "url": None}, error
            )
            failures.append(_source_failure("test", error))

        project = self._with_test_containment(project, test)

        if changed:
            self._persist_last_good_sources()

        snapshot = self._builder.build(
            service=service,
            intake_active=self._state_store.intake_active(),
            worker_limit=self._state_store.worker_limit(self._worker_limit),
            worker_max=self._worker_limit,
            runtime=runtime,
            project=project,
            canonical=canonical,
            test=test,
            quarantines=self._state_store.quarantines(),
        )
        snapshot["failures"] = failures
        snapshot["sources"] = sources
        snapshot["stale"] = any(source.get("status") != "fresh" for source in sources.values())
        snapshot["refreshed_at"] = refreshed_at
        snapshot["history"] = self._state_store.record_status_sample(
            {
                "recorded_at": refreshed_at,
                "counts": {
                    key: snapshot["counts"].get(key, 0)
                    for key in (
                        "ready_for_ai",
                        "running",
                        "blocked",
                        "ready_for_acceptance",
                        "done",
                    )
                },
                "workers": {
                    key: snapshot["workers"].get(key, 0)
                    for key in ("running", "limit")
                },
            }
        )
        snapshot["infrastructure"] = self._infrastructure_projection(
            service=service,
            refreshed_at=refreshed_at,
        )
        return snapshot

    def _infrastructure_projection(
        self,
        *,
        service: dict[str, Any],
        refreshed_at: str,
    ) -> dict[str, Any]:
        hosts: list[dict[str, Any]] = []
        stale = False
        if service.get("live") is False and service.get("status") not in {"unknown", None}:
            hosts = [
                {
                    "name": "Local Symphony",
                    "kind": "local",
                    "status": "stopped",
                    "cpu_percent": 0.0,
                    "memory_percent": 0.0,
                    "memory_usage": None,
                    "runners_busy": 0,
                    "runners_total": 0,
                    "jobs": [],
                }
            ]
        else:
            metrics = getattr(self._supervisor, "metrics", None)
            if callable(metrics):
                try:
                    hosts = [metrics()]
                    self._state_store.update({"last_infrastructure_hosts": hosts})
                except Exception:
                    stored = self._state_store.read().get("last_infrastructure_hosts")
                    hosts = deepcopy(stored) if isinstance(stored, list) else []
                    stale = bool(hosts)
        history = self._state_store.record_infrastructure_sample(
            {
                "recorded_at": refreshed_at,
                "hosts": {
                    str(host.get("name") or "Host"): {
                        "cpu_percent": host.get("cpu_percent"),
                        "memory_percent": host.get("memory_percent"),
                    }
                    for host in hosts
                },
            }
        )
        return {
            "hosts": hosts,
            "queued_jobs": 0,
            "alerts": 1 if stale else 0,
            "stale": stale,
            "history": history,
        }

    def _with_test_containment(
        self, project: dict[str, Any], test: dict[str, Any]
    ) -> dict[str, Any]:
        enriched = deepcopy(project)
        deployed_sha = test.get("sha") if isinstance(test, dict) else None
        for item in enriched.get("items") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").casefold() != "ready for acceptance":
                continue
            pull_request = item.get("pr")
            merge_sha = (
                pull_request.get("merge_sha")
                if isinstance(pull_request, dict)
                else None
            )
            issue_test = {
                **(test if isinstance(test, dict) else {}),
                "merge_sha": merge_sha,
                "contains_merge": None,
            }
            if deployed_sha and merge_sha:
                cache_key = (str(deployed_sha), str(merge_sha))
                try:
                    contains = self._containment_cache.get(cache_key)
                    if contains is None:
                        contains = bool(
                            self._github.commit_contains(deployed_sha, merge_sha)
                        )
                        self._containment_cache[cache_key] = contains
                    issue_test["contains_merge"] = contains
                except Exception as error:
                    issue_test["containment_error"] = str(error)
            item["test"] = issue_test
        return enriched

    def _github_projection(
        self,
        *,
        force: bool,
        refreshed_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any], Exception | None]:
        if force:
            return self._refresh_github(refreshed_at)

        now = time.monotonic()
        with self._source_lock:
            stored = deepcopy(self._last_good_sources.get("github"))
            cached_at = self._github_cached_at
            retry_at = self._github_retry_at
            last_error = self._github_last_error

        has_stored_value = isinstance(stored, dict) and "value" in stored
        if (
            has_stored_value
            and cached_at > 0
            and now - cached_at < self._github_cache_seconds
        ):
            return (
                deepcopy(stored["value"]),
                _fresh_source(stored.get("confirmed_at") or refreshed_at),
                None,
            )

        if retry_at > now and last_error is not None:
            value, source = self._stale_or_unavailable(
                "github",
                self._empty_github(),
                last_error,
            )
            return value, source, last_error

        if has_stored_value:
            self._start_github_refresh()
            if last_error is not None:
                value, source = self._stale_or_unavailable(
                    "github",
                    self._empty_github(),
                    last_error,
                )
                return value, source, last_error
            confirmed_at = stored.get("confirmed_at") or refreshed_at
            return (
                deepcopy(stored["value"]),
                _fresh_source(confirmed_at)
                if cached_at > 0
                else {
                    "status": "stale",
                    "confirmed_at": confirmed_at,
                    "error": "refreshing GitHub after Owner Control startup",
                },
                None,
            )

        return self._refresh_github(refreshed_at)

    def _start_github_refresh(self) -> None:
        if not self._github_refresh_lock.acquire(blocking=False):
            return
        threading.Thread(
            target=self._refresh_github_from_acquired_lock,
            name="owner-control-github-refresh",
            daemon=True,
        ).start()

    def _refresh_github_from_acquired_lock(self) -> None:
        try:
            self._fetch_github(datetime.now(timezone.utc).isoformat())
        finally:
            self._github_refresh_lock.release()
            with self._cache_lock:
                self._cached_at = 0.0

    def _refresh_github(
        self,
        refreshed_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any], Exception | None]:
        with self._github_refresh_lock:
            return self._fetch_github(refreshed_at)

    def _fetch_github(
        self,
        refreshed_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any], Exception | None]:
        try:
            value = {
                "project": self._github.project_snapshot(reconcile_intake=True),
                "canonical": self._github.canonical(self._canonical_ref),
            }
        except Exception as error:
            with self._source_lock:
                self._github_cached_at = 0.0
                self._github_retry_at = time.monotonic() + self._github_retry_seconds
                self._github_last_error = error
            stale, source = self._stale_or_unavailable(
                "github",
                self._empty_github(),
                error,
            )
            return stale, source, error

        changed = self._remember_source("github", value, refreshed_at)
        with self._source_lock:
            self._github_cached_at = time.monotonic()
            self._github_retry_at = 0.0
            self._github_last_error = None
        if changed:
            self._persist_last_good_sources()
        return value, _fresh_source(refreshed_at), None

    def _empty_github(self) -> dict[str, Any]:
        return {
            "project": {"items": []},
            "canonical": {"sha": None, "url": None, "ref": self._canonical_ref},
        }

    @staticmethod
    def _patched_cached_snapshot(
        cached: dict[str, Any],
        *,
        intake_active: bool,
        service: dict[str, Any],
        supervisor_source: dict[str, Any],
        control_transition: bool,
        refreshed_at: str,
    ) -> dict[str, Any]:
        snapshot = deepcopy(cached)
        snapshot["intake"] = {
            "active": intake_active,
            "status": "active" if intake_active else "paused",
        }
        snapshot["service"] = deepcopy(service)
        sources = snapshot.setdefault("sources", {})
        sources["supervisor"] = deepcopy(supervisor_source)
        supervisor_known = supervisor_source.get("status") == "fresh"
        inactive = supervisor_known and (
            control_transition
            or _service_is_transitioning(service)
            or _service_is_confirmed_stopped(service)
        )
        if inactive:
            previous_runtime = sources.get("runtime") or {}
            sources["runtime"] = {
                "status": "stale",
                "confirmed_at": previous_runtime.get("confirmed_at"),
                "error": _inactive_runtime_reason(
                    service, control_transition=control_transition
                ),
            }
            snapshot["running"] = []
            snapshot.setdefault("workers", {})["running"] = 0
            snapshot.setdefault("counts", {})["running"] = 0
            snapshot.setdefault("owner_view", {}).setdefault("work_items", []).clear()
        snapshot["stale"] = any(
            source.get("status") != "fresh"
            for source in sources.values()
            if isinstance(source, dict)
        )
        snapshot["refreshed_at"] = refreshed_at
        return snapshot

    def _inactive_runtime_projection(self) -> dict[str, Any]:
        stored = self._stored_source("runtime")
        if isinstance(stored, dict) and isinstance(stored.get("value"), dict):
            runtime = deepcopy(stored["value"])
        else:
            runtime = _empty_runtime()
        runtime["running"] = []
        return runtime

    def _stored_source(self, name: str) -> dict[str, Any] | None:
        with self._source_lock:
            stored = self._last_good_sources.get(name)
            return deepcopy(stored) if isinstance(stored, dict) else None

    def _remember_source(self, name: str, value: Any, confirmed_at: str) -> bool:
        stored = {"confirmed_at": confirmed_at, "value": deepcopy(value)}
        with self._source_lock:
            changed = self._last_good_sources.get(name, {}).get("value") != stored["value"]
            self._last_good_sources[name] = stored
            return changed

    def _persist_last_good_sources(self) -> None:
        with self._source_lock:
            stored_sources = deepcopy(self._last_good_sources)
        self._state_store.update({"last_good_sources": stored_sources})

    def _stale_or_unavailable(
        self,
        name: str,
        empty_value: Any,
        error: Exception,
    ) -> tuple[Any, dict[str, Any]]:
        stored = self._stored_source(name)
        if isinstance(stored, dict) and "value" in stored:
            return deepcopy(stored["value"]), {
                "status": "stale",
                "confirmed_at": stored.get("confirmed_at"),
                "error": str(error),
            }
        return deepcopy(empty_value), _unavailable_source(error)


def _empty_runtime() -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "running": [],
        "retrying": [],
        "blocked": [],
        "codex_totals": {},
        "rate_limits": None,
    }


def _source_failure(source: str, error: Exception) -> dict[str, Any]:
    message = f"{source} snapshot unavailable: {error}"
    normalized = str(error).casefold()
    unrecoverable = isinstance(error, (PermissionError, ValueError)) or "http 401" in normalized or "http 403" in normalized
    return {
        "fingerprint": f"{source}:{type(error).__name__}:{'auth' if unrecoverable else 'transient'}",
        "message": message,
        "unrecoverable": unrecoverable,
    }


def _service_with_intent(
    service: dict[str, Any],
    control_state: dict[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    projected = deepcopy(service)
    live = projected.get("live") is True
    expected_stop = bool(control_state.get("expected_service_stop"))
    expected_restart_until = float(
        control_state.get("expected_service_restart_until") or 0
    )
    action_in_progress = _active_service_action(control_state, now=now)
    if expected_stop and live and action_in_progress == "stop_service":
        projected["status"] = "stopping"
    elif (
        expected_restart_until > now
        and not expected_stop
        and (not live or action_in_progress in {"start_service", "restart"})
    ):
        projected["status"] = "starting"
    elif not live and not projected.get("status"):
        projected["status"] = "exited"
    return projected


def _control_transition_active(control_state: dict[str, Any]) -> bool:
    return bool(
        _active_service_action(control_state, now=time.time())
    ) or _restart_expected(control_state)


def _active_service_action(control_state: dict[str, Any], *, now: float) -> str:
    action = str(control_state.get("service_action_in_progress") or "").casefold()
    deadline = float(control_state.get("service_action_in_progress_until") or 0)
    return action if action and deadline > now else ""


def _restart_expected(control_state: dict[str, Any]) -> bool:
    return float(control_state.get("expected_service_restart_until") or 0) > time.time()


def _service_is_transitioning(service: dict[str, Any]) -> bool:
    return str(service.get("status") or "").casefold() in {
        "created",
        "restarting",
        "starting",
        "stopping",
    }


def _service_is_confirmed_stopped(service: dict[str, Any]) -> bool:
    return service.get("live") is False and str(service.get("status") or "").casefold() not in {
        "unknown",
        "created",
        "restarting",
        "starting",
        "stopping",
    }


def _inactive_runtime_reason(
    service: dict[str, Any], *, control_transition: bool = False
) -> str:
    status = str(service.get("status") or "").casefold()
    if status in {"created", "restarting", "starting"}:
        return "Symphony service is starting"
    if status == "stopping":
        return "Symphony service is stopping"
    if control_transition and service.get("live") is True:
        return "Symphony runtime is refreshing after a service action"
    return "Symphony service is stopped"


def _fresh_source(confirmed_at: str) -> dict[str, Any]:
    return {"status": "fresh", "confirmed_at": confirmed_at, "error": None}


def _unavailable_source(error: Exception) -> dict[str, Any]:
    return {"status": "unavailable", "confirmed_at": None, "error": str(error)}
