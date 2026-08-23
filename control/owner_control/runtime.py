from __future__ import annotations

import threading
import time
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
        failures = []
        try:
            service = self._supervisor.status()
        except Exception as error:
            service = {"live": False, "reason": str(error)}
            failures.append(_source_failure("supervisor", error))
        try:
            runtime = self._symphony.state()
        except Exception as error:
            runtime = _empty_runtime()
            service = {**service, "live": False, "reason": str(error)}

        try:
            project = self._github.project_snapshot()
            canonical = self._github.canonical(self._canonical_ref)
        except Exception as error:
            project = {"items": []}
            canonical = {"sha": None, "url": None, "ref": self._canonical_ref}
            failures.append(_source_failure("github", error))

        try:
            test = self._test_environment.deployment()
        except Exception as error:
            test = {"sha": None, "url": None}
            failures.append(_source_failure("test", error))

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
        return snapshot


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
