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
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def snapshot(self, *, fresh: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if not fresh and self._cached is not None and now - self._cached_at < self._cache_seconds:
                return self._cached
            value = self._collect()
            self._cached = value
            self._cached_at = now
            return value

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
            self._cached_at = 0.0

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
