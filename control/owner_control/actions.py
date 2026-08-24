from __future__ import annotations

import threading
import time
from typing import Any, Callable, Protocol

from .state_store import StateStore


_SERVICE_ACTION_TIMEOUT_SECONDS = 120


class ActionError(ValueError):
    pass


class Lifecycle(Protocol):
    def set_status(self, issue: int, status: str) -> None: ...
    def add_label(self, issue: int, label: str) -> None: ...
    def comment(self, issue: int, body: str) -> None: ...
    def close_issue(self, issue: int) -> None: ...


class Supervisor(Protocol):
    def start(self) -> dict[str, Any]: ...
    def stop(self) -> dict[str, Any]: ...
    def restart(self) -> dict[str, Any]: ...


class ActionService:
    """Serial, typed actions shared by HTTP and Telegram adapters."""

    def __init__(
        self,
        *,
        snapshot_provider: Callable[[], dict[str, Any]],
        lifecycle: Lifecycle,
        supervisor: Supervisor,
        state_store: StateStore,
        after_action: Callable[[], None] = lambda: None,
    ):
        self._snapshot_provider = snapshot_provider
        self._lifecycle = lifecycle
        self._supervisor = supervisor
        self._state_store = state_store
        self._after_action = after_action
        self._lock = threading.Lock()

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise ActionError("another action is already in progress")
        try:
            result = self._execute_locked(action, params)
            try:
                self._after_action()
            finally:
                if action in {"start_service", "restart", "stop_service"}:
                    self._state_store.update(
                        {
                            "service_action_in_progress": None,
                            "service_action_in_progress_until": 0,
                        }
                    )
            return result
        finally:
            self._lock.release()

    def _execute_locked(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "pause":
            self._state_store.set_intake_active(False)
            return {"status": "accepted", "intake": {"active": False}}
        if action == "resume":
            snapshot = self._snapshot_provider()
            self._require_fresh_sources(snapshot, "runtime", "github")
            self._state_store.set_intake_active(True)
            return {"status": "accepted", "intake": {"active": True}}
        if action in {"start_service", "restart"}:
            snapshot = self._snapshot_provider()
            self._require_fresh_sources(snapshot, "supervisor")
            action_deadline = time.time() + _SERVICE_ACTION_TIMEOUT_SECONDS
            self._state_store.update(
                {
                    "expected_service_restart_until": action_deadline,
                    "expected_service_stop": False,
                    "service_action_in_progress": action,
                    "service_action_in_progress_until": action_deadline,
                }
            )
            try:
                service = (
                    self._supervisor.start()
                    if action == "start_service"
                    else self._supervisor.restart()
                )
            except Exception:
                self._state_store.update(
                    {
                        "expected_service_restart_until": 0,
                        "service_action_in_progress": None,
                        "service_action_in_progress_until": 0,
                    }
                )
                raise
            return {"status": "accepted", "service": service}
        if action == "stop_service":
            snapshot = self._snapshot_provider()
            self._require_fresh_sources(snapshot, "supervisor", "runtime")
            running_workers = self._running_workers(snapshot)
            confirmation = params.get("confirm_running_workers")
            if running_workers > 0 and (
                type(confirmation) is not int or confirmation != running_workers
            ):
                raise ActionError(
                    f"stop_service requires confirm_running_workers to match {running_workers}"
                )
            self._state_store.update(
                {
                    "expected_service_stop": True,
                    "service_action_in_progress": action,
                    "service_action_in_progress_until": (
                        time.time() + _SERVICE_ACTION_TIMEOUT_SECONDS
                    ),
                }
            )
            try:
                service = self._supervisor.stop()
            except Exception:
                self._state_store.update(
                    {
                        "expected_service_stop": False,
                        "service_action_in_progress": None,
                        "service_action_in_progress_until": 0,
                    }
                )
                raise
            return {"status": "accepted", "service": service}
        if action not in {"run", "lease", "accept", "rework"}:
            raise ActionError(f"unsupported action: {action}")

        issue_number = self._issue_number(params.get("issue"))
        snapshot = self._snapshot_provider()
        self._require_fresh_sources(snapshot, "github")
        if action == "accept":
            self._require_fresh_sources(snapshot, "test")
        issue = snapshot.get("issues", {}).get(str(issue_number))
        if not isinstance(issue, dict):
            raise ActionError(f"issue #{issue_number} is not in the control snapshot")

        if action == "run":
            return self._run(issue_number, issue)
        if action == "lease":
            return self._lease(issue_number, issue)
        if action == "accept":
            return self._accept(issue_number, issue, snapshot)
        return self._rework(issue_number, issue, params.get("reason"))

    def _run(self, issue_number: int, issue: dict[str, Any]) -> dict[str, Any]:
        if str(issue.get("state", "OPEN")).upper() != "OPEN":
            raise ActionError("run requires an open issue")
        if str(issue.get("status", "")).casefold() not in {"backlog", "ready for ai"}:
            raise ActionError("run requires Backlog or Ready for AI")
        self._lifecycle.set_status(issue_number, "Ready for AI")
        if "symphony" not in {str(label).casefold() for label in issue.get("labels", [])}:
            self._lifecycle.add_label(issue_number, "symphony")
        return {"status": "accepted", "action": "run", "issue": issue_number}

    def _lease(self, issue_number: int, issue: dict[str, Any]) -> dict[str, Any]:
        if not self._state_store.intake_active():
            raise ActionError("lease requires active intake")
        if type(issue.get("number")) is not int or issue.get("number") != issue_number:
            raise ActionError("lease requires a canonical issue number")
        if str(issue.get("state", "OPEN")).upper() != "OPEN":
            raise ActionError("lease requires an open issue")
        if str(issue.get("status", "")).casefold() != "ready for ai":
            raise ActionError("lease requires Ready for AI")
        if "symphony" not in {str(label).casefold() for label in issue.get("labels", [])}:
            self._lifecycle.add_label(issue_number, "symphony")
        return {"status": "accepted", "action": "lease", "issue": issue_number}

    def _accept(
        self, issue_number: int, issue: dict[str, Any], snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        status = str(issue.get("status", "")).casefold()
        if status not in {"ready for acceptance", "done"}:
            raise ActionError("accept requires Ready for Acceptance")
        if status == "ready for acceptance" and not snapshot.get("test", {}).get("synced"):
            raise ActionError("accept refused because TEST is not synced to canonical")
        issue_test = issue.get("test")
        if (
            status == "ready for acceptance"
            and isinstance(issue_test, dict)
            and issue_test.get("synced") is not True
        ):
            raise ActionError("accept refused because Issue TEST is not synced")
        if status == "ready for acceptance":
            self._lifecycle.set_status(issue_number, "Done")
        if str(issue.get("state", "OPEN")).upper() != "CLOSED":
            self._lifecycle.close_issue(issue_number)
        return {"status": "accepted", "action": "accept", "issue": issue_number}

    def _rework(self, issue_number: int, issue: dict[str, Any], reason: Any) -> dict[str, Any]:
        if str(issue.get("status", "")).casefold() != "ready for acceptance":
            raise ActionError("rework requires Ready for Acceptance")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ActionError("rework reason is required")
        self._lifecycle.comment(issue_number, f"Owner requested rework: {normalized_reason}")
        self._lifecycle.set_status(issue_number, "Ready for AI")
        if "symphony" not in {str(label).casefold() for label in issue.get("labels", [])}:
            self._lifecycle.add_label(issue_number, "symphony")
        return {"status": "accepted", "action": "rework", "issue": issue_number}

    @staticmethod
    def _issue_number(value: Any) -> int:
        normalized = str(value or "").strip().lstrip("#")
        if not normalized.isdigit() or int(normalized) <= 0:
            raise ActionError("a positive issue number is required")
        return int(normalized)

    @staticmethod
    def _running_workers(snapshot: dict[str, Any]) -> int:
        workers = snapshot.get("workers", {})
        if not isinstance(workers, dict):
            return 0
        value = workers.get("running", 0)
        return value if isinstance(value, int) and value > 0 else 0

    @staticmethod
    def _require_fresh_sources(snapshot: dict[str, Any], *names: str) -> None:
        sources = snapshot.get("sources")
        for name in names:
            source = sources.get(name) if isinstance(sources, dict) else None
            if not isinstance(source, dict) or source.get("status") != "fresh":
                raise ActionError(f"action requires fresh {name} state")
