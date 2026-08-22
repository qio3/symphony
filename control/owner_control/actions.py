from __future__ import annotations

import threading
import time
from typing import Any, Callable, Protocol

from .state_store import StateStore


class ActionError(ValueError):
    pass


class Lifecycle(Protocol):
    def set_status(self, issue: int, status: str) -> None: ...
    def add_label(self, issue: int, label: str) -> None: ...
    def comment(self, issue: int, body: str) -> None: ...
    def close_issue(self, issue: int) -> None: ...


class Supervisor(Protocol):
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
        with self._lock:
            result = self._execute_locked(action, params)
            self._after_action()
            return result

    def _execute_locked(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "pause":
            self._state_store.set_intake_active(False)
            return {"status": "accepted", "intake": {"active": False}}
        if action == "resume":
            self._state_store.set_intake_active(True)
            return {"status": "accepted", "intake": {"active": True}}
        if action == "restart":
            self._state_store.update({"expected_service_restart_until": time.time() + 120})
            try:
                service = self._supervisor.restart()
            except Exception:
                self._state_store.update({"expected_service_restart_until": 0})
                raise
            return {"status": "accepted", "service": service}
        if action not in {"run", "accept", "rework"}:
            raise ActionError(f"unsupported action: {action}")

        issue_number = self._issue_number(params.get("issue"))
        snapshot = self._snapshot_provider()
        issue = snapshot.get("issues", {}).get(str(issue_number))
        if not isinstance(issue, dict):
            raise ActionError(f"issue #{issue_number} is not in the control snapshot")

        if action == "run":
            return self._run(issue_number, issue)
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

    def _accept(
        self, issue_number: int, issue: dict[str, Any], snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        status = str(issue.get("status", "")).casefold()
        if status not in {"ready for acceptance", "done"}:
            raise ActionError("accept requires Ready for Acceptance")
        if status == "ready for acceptance" and not snapshot.get("test", {}).get("synced"):
            raise ActionError("accept refused because TEST is not synced to canonical")
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
