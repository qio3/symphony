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
    ):
        self._symphony = symphony
        self._github = github
        self._test_environment = test_environment
        self._supervisor = supervisor
        self._state_store = state_store
        self._worker_limit = worker_limit
        self._canonical_ref = canonical_ref
        self._cache_seconds = cache_seconds
        self._builder = SnapshotBuilder()
        self._cache_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        stored_sources = self._state_store.read().get("last_good_sources")
        self._last_good_sources = deepcopy(stored_sources) if isinstance(stored_sources, dict) else {}

    def snapshot(self, *, fresh: bool = False) -> dict[str, Any]:
        if fresh:
            return self._refresh()

        with self._cache_lock:
            now = time.monotonic()
            cached = self._cached
            expired = cached is not None and now - self._cached_at >= self._cache_seconds

        if cached is None:
            return self._refresh()
        if expired:
            self._start_refresh()
        return cached

    def invalidate(self) -> None:
        with self._cache_lock:
            self._cached_at = 0.0
        self._start_refresh()

    def _refresh(self) -> dict[str, Any]:
        with self._refresh_lock:
            return self._collect_and_cache()

    def _start_refresh(self) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            return
        threading.Thread(
            target=self._refresh_from_acquired_lock,
            name="owner-control-snapshot-refresh",
            daemon=True,
        ).start()

    def _refresh_from_acquired_lock(self) -> None:
        try:
            self._collect_and_cache()
        finally:
            self._refresh_lock.release()

    def _collect_and_cache(self) -> dict[str, Any]:
        value = self._collect()
        with self._cache_lock:
            self._cached = value
            self._cached_at = time.monotonic()
        return value

    def _collect(self) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        sources: dict[str, dict[str, Any]] = {}
        changed = False
        refreshed_at = datetime.now(timezone.utc).isoformat()

        try:
            service = self._supervisor.status()
            sources["supervisor"] = _fresh_source(refreshed_at)
        except Exception as error:
            service = {"live": False, "status": "unknown", "reason": str(error)}
            failures.append(_source_failure("supervisor", error))
            sources["supervisor"] = _unavailable_source(error)

        try:
            runtime = self._symphony.state()
            changed = self._remember_source("runtime", runtime, refreshed_at) or changed
            sources["runtime"] = _fresh_source(refreshed_at)
        except Exception as error:
            runtime, sources["runtime"] = self._stale_or_unavailable("runtime", _empty_runtime(), error)
            failures.append(_source_failure("runtime", error))

        try:
            project = self._github.project_snapshot()
            canonical = self._github.canonical(self._canonical_ref)
            github_value = {"project": project, "canonical": canonical}
            changed = self._remember_source("github", github_value, refreshed_at) or changed
            sources["github"] = _fresh_source(refreshed_at)
        except Exception as error:
            github_value, sources["github"] = self._stale_or_unavailable(
                "github",
                {
                    "project": {"items": []},
                    "canonical": {"sha": None, "url": None, "ref": self._canonical_ref},
                },
                error,
            )
            project = github_value["project"]
            canonical = github_value["canonical"]
            failures.append(_source_failure("github", error))

        try:
            test = self._test_environment.deployment()
            changed = self._remember_source("test", test, refreshed_at) or changed
            sources["test"] = _fresh_source(refreshed_at)
        except Exception as error:
            test, sources["test"] = self._stale_or_unavailable(
                "test", {"sha": None, "url": None}, error
            )
            failures.append(_source_failure("test", error))

        if changed:
            self._state_store.update({"last_good_sources": deepcopy(self._last_good_sources)})

        snapshot = self._builder.build(
            service=service,
            intake_active=self._state_store.intake_active(),
            worker_limit=self._worker_limit,
            runtime=runtime,
            project=project,
            canonical=canonical,
            test=test,
        )
        snapshot["failures"] = failures
        snapshot["sources"] = sources
        snapshot["stale"] = any(source.get("status") != "fresh" for source in sources.values())
        snapshot["refreshed_at"] = refreshed_at
        return snapshot

    def _remember_source(self, name: str, value: Any, confirmed_at: str) -> bool:
        stored = {"confirmed_at": confirmed_at, "value": deepcopy(value)}
        changed = self._last_good_sources.get(name, {}).get("value") != stored["value"]
        self._last_good_sources[name] = stored
        return changed

    def _stale_or_unavailable(
        self,
        name: str,
        empty_value: Any,
        error: Exception,
    ) -> tuple[Any, dict[str, Any]]:
        stored = self._last_good_sources.get(name)
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


def _fresh_source(confirmed_at: str) -> dict[str, Any]:
    return {"status": "fresh", "confirmed_at": confirmed_at, "error": None}


def _unavailable_source(error: Exception) -> dict[str, Any]:
    return {"status": "unavailable", "confirmed_at": None, "error": str(error)}
