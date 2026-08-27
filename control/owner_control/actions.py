from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .state_store import StateStore


_SERVICE_ACTION_TIMEOUT_SECONDS = 120
_SYSTEM_QUARANTINE_LABEL = "symphony:quarantined"
_OWNER_WAITING_LABEL = "ждёт-владельца"
_QUARANTINE_REASON_MAX_BYTES = 512
_ACTION_IDEMPOTENCY_SECONDS = 10
_ANSI_ESCAPE_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class ActionError(ValueError):
    pass


class RetryableActionError(ActionError):
    """An internal runtime action can be retried after the control snapshot settles."""


class Lifecycle(Protocol):
    def set_status(self, issue: int, status: str) -> None: ...
    def add_label(self, issue: int, label: str) -> None: ...
    def remove_label(self, issue: int, label: str) -> None: ...
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
        fresh_snapshot_provider: Callable[[], dict[str, Any]] | None = None,
        lifecycle: Lifecycle,
        supervisor: Supervisor,
        state_store: StateStore,
        after_action: Callable[[], None] = lambda: None,
    ):
        self._snapshot_provider = snapshot_provider
        self._fresh_snapshot_provider = fresh_snapshot_provider or snapshot_provider
        self._lifecycle = lifecycle
        self._supervisor = supervisor
        self._state_store = state_store
        self._after_action = after_action
        self._lock = threading.Lock()
        self._recent_results: dict[str, tuple[float, dict[str, Any]]] = {}
        self._active_operation_key: str | None = None

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise ActionError("another action is already in progress")
        try:
            idempotent_action = action in {
                "run",
                "accept",
                "rework",
                "start_service",
                "stop_service",
                "restart",
            }
            action_key = (
                json.dumps([action, params], sort_keys=True, ensure_ascii=False)
                if idempotent_action
                else None
            )
            recent = self._recent_results.get(action_key) if action_key else None
            if recent is not None and time.monotonic() - recent[0] < _ACTION_IDEMPOTENCY_SECONDS:
                return dict(recent[1])
            persistent_key = action_key if action in {"run", "accept", "rework"} else None
            persisted = self._state_store.action_result(persistent_key) if persistent_key else None
            if persisted is not None:
                return persisted
            self._active_operation_key = persistent_key
            try:
                result = self._execute_locked(action, params)
                if persistent_key:
                    self._state_store.complete_action(persistent_key, result)
            finally:
                self._active_operation_key = None
            if action_key:
                self._recent_results[action_key] = (time.monotonic(), dict(result))
            self._recent_results = {
                key: value
                for key, value in self._recent_results.items()
                if time.monotonic() - value[0] < _ACTION_IDEMPOTENCY_SECONDS
            }
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

    def execute_internal(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one fixed runtime-only action; it is not exposed to owner adapters."""
        if not self._lock.acquire(blocking=False):
            raise ActionError("another action is already in progress")
        try:
            result = self._execute_internal_locked(action, params)
            self._after_action()
            return result
        finally:
            self._lock.release()

    def _execute_locked(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "pause":
            self._state_store.set_intake_active(False)
            return {"status": "accepted", "intake": {"active": False}}
        if action == "resume":
            snapshot = self._fresh_snapshot_provider()
            self._require_fresh_sources(snapshot, "runtime", "github")
            systemic_gate = snapshot.get("systemic_gate")
            if isinstance(systemic_gate, dict) and systemic_gate.get("blocked"):
                raise ActionError(
                    str(systemic_gate.get("reason") or "systemic delivery gate is blocked")
                )
            self._state_store.set_intake_active(True)
            return {"status": "accepted", "intake": {"active": True}}
        if action == "set_workers":
            snapshot = self._snapshot_provider()
            workers = snapshot.get("workers") if isinstance(snapshot, dict) else None
            maximum = workers.get("maximum") if isinstance(workers, dict) else None
            limit = params.get("limit")
            if type(maximum) is not int or maximum <= 0:
                raise ActionError("worker maximum is unavailable")
            if type(limit) is not int or not 1 <= limit <= maximum:
                raise ActionError(f"worker limit must be between 1 and {maximum}")
            self._state_store.set_worker_limit(limit, maximum=maximum)
            return {
                "status": "accepted",
                "workers": {"limit": limit, "maximum": maximum},
            }
        if action in {"start_service", "restart"}:
            snapshot = self._fresh_snapshot_provider()
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
            snapshot = self._fresh_snapshot_provider()
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
        snapshot = (
            self._snapshot_provider()
            if action == "lease"
            else self._fresh_snapshot_provider()
        )
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

    def _execute_internal_locked(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action not in {"complete_run", "quarantine_before_run"}:
            raise ActionError(f"unsupported internal action: {action}")

        issue_number = self._issue_number(params.get("issue"))
        if action == "complete_run":
            try:
                snapshot = self._fresh_snapshot_provider()
                self._require_fresh_sources(snapshot, "github", "runtime")
            except ActionError as error:
                raise RetryableActionError(str(error)) from error
            except Exception as error:
                raise RetryableActionError(
                    f"complete_run requires fresh control state: {error}"
                ) from error
            issue = snapshot.get("issues", {}).get(str(issue_number))
            return self._complete_run(issue_number, issue, snapshot)

        snapshot = self._fresh_snapshot_provider()
        self._require_fresh_sources(snapshot, "github")

        issue = snapshot.get("issues", {}).get(str(issue_number))
        if not isinstance(issue, dict):
            raise ActionError(f"issue #{issue_number} is not in the control snapshot")

        self._require_canonical_open_quarantine_issue(issue_number, issue)
        return self._quarantine_before_run(issue_number, issue, params.get("reason"))

    def _run(self, issue_number: int, issue: dict[str, Any]) -> dict[str, Any]:
        if str(issue.get("state", "OPEN")).upper() != "OPEN":
            raise ActionError("run requires an open issue")
        persisted_quarantine = self._state_store.quarantine_for(issue_number)
        fixed_quarantine_marker = self._has_label(issue, _SYSTEM_QUARANTINE_LABEL)
        status = str(issue.get("status", "")).casefold()
        if status not in {"backlog", "ready for ai"} and not (
            status == "in progress" and (persisted_quarantine is not None or fixed_quarantine_marker)
        ):
            raise ActionError("run requires Backlog or Ready for AI")
        if fixed_quarantine_marker:
            self._action_step(
                "remove_quarantine_label",
                lambda: self._lifecycle.remove_label(issue_number, _SYSTEM_QUARANTINE_LABEL),
            )
        if persisted_quarantine is not None:
            # An explicit owner Start clears the durable guard once GitHub accepted the marker removal.
            self._action_step(
                "clear_quarantine", lambda: self._state_store.clear_quarantine(issue_number)
            )
        self._action_step(
            "ready_for_ai", lambda: self._lifecycle.set_status(issue_number, "Ready for AI")
        )
        if not self._has_label(issue, "symphony"):
            self._action_step(
                "add_lease", lambda: self._lifecycle.add_label(issue_number, "symphony")
            )
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
        if self._has_label(issue, _OWNER_WAITING_LABEL):
            raise ActionError("lease refused because issue requires owner input")
        if (
            self._state_store.quarantine_for(issue_number) is not None
            or self._has_label(issue, _SYSTEM_QUARANTINE_LABEL)
        ):
            raise ActionError("lease refused because issue is in system quarantine")
        lease_was_new = not self._has_label(issue, "symphony")
        if lease_was_new:
            self._lifecycle.add_label(issue_number, "symphony")
        try:
            self._lifecycle.set_status(issue_number, "In Progress")
        except Exception:
            if lease_was_new:
                self._lifecycle.remove_label(issue_number, "symphony")
            raise
        return {"status": "accepted", "action": "lease", "issue": issue_number}

    def _accept(
        self, issue_number: int, issue: dict[str, Any], snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        status = str(issue.get("status", "")).casefold()
        if status not in {"ready for acceptance", "done"}:
            raise ActionError("accept requires Ready for Acceptance")
        issue_test = issue.get("test")
        if status == "ready for acceptance":
            if not isinstance(issue_test, dict) or not issue_test.get("merge_sha"):
                raise ActionError("accept refused because Issue merge SHA is unavailable")
            if issue_test.get("contains_merge") is not True:
                raise ActionError("accept refused because TEST does not contain the Issue merge")
        if status == "ready for acceptance":
            self._action_step(
                "done", lambda: self._lifecycle.set_status(issue_number, "Done")
            )
        if str(issue.get("state", "OPEN")).upper() != "CLOSED":
            self._action_step("close", lambda: self._lifecycle.close_issue(issue_number))
        return {"status": "accepted", "action": "accept", "issue": issue_number}

    def _rework(self, issue_number: int, issue: dict[str, Any], reason: Any) -> dict[str, Any]:
        if str(issue.get("status", "")).casefold() != "ready for acceptance":
            raise ActionError("rework requires Ready for Acceptance")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ActionError("rework reason is required")
        self._action_step(
            "comment",
            lambda: self._lifecycle.comment(
                issue_number, f"Owner requested rework: {normalized_reason}"
            ),
        )
        self._action_step(
            "ready_for_ai", lambda: self._lifecycle.set_status(issue_number, "Ready for AI")
        )
        if "symphony" not in {str(label).casefold() for label in issue.get("labels", [])}:
            self._action_step(
                "add_lease", lambda: self._lifecycle.add_label(issue_number, "symphony")
            )
        return {"status": "accepted", "action": "rework", "issue": issue_number}

    def _action_step(self, step: str, callback: Callable[[], None]) -> None:
        action_key = self._active_operation_key
        if action_key and self._state_store.action_step_completed(action_key, step):
            return
        callback()
        if action_key:
            self._state_store.record_action_step(action_key, step)

    def _complete_run(
        self, issue_number: int, issue: Any, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(issue, dict):
            raise RetryableActionError(
                f"complete_run requires issue #{issue_number} in fresh control state"
            )
        if type(issue.get("number")) is not int or issue.get("number") != issue_number:
            raise RetryableActionError("complete_run requires a canonical issue number")
        state = str(issue.get("state", "")).upper()
        if state == "CLOSED" or str(issue.get("status", "")).casefold() != "in progress":
            return {"status": "accepted", "action": "complete_run", "issue": issue_number}
        if state != "OPEN":
            raise RetryableActionError("complete_run requires a canonical issue state")
        if self._has_label(issue, "symphony"):
            raise RetryableActionError("complete_run requires the Symphony lease to be absent")
        if self._runtime_contains_issue(snapshot, issue_number):
            raise RetryableActionError("complete_run requires no runtime entry for the issue")
        self._lifecycle.set_status(issue_number, "Ready for Acceptance")
        return {"status": "accepted", "action": "complete_run", "issue": issue_number}

    def _quarantine_before_run(
        self, issue_number: int, issue: dict[str, Any], reason: Any
    ) -> dict[str, Any]:
        persisted = self._state_store.quarantine_for(issue_number)
        if persisted is None and not self._has_label(issue, "symphony"):
            raise ActionError("quarantine_before_run requires the Symphony lease")
        normalized_reason = (
            persisted["reason"] if persisted is not None else self._quarantine_reason(reason)
        )
        if persisted is None:
            self._state_store.set_quarantine(
                issue_number,
                normalized_reason,
                datetime.now(timezone.utc).isoformat(),
            )
        if self._has_label(issue, "symphony"):
            self._lifecycle.remove_label(issue_number, "symphony")
        if not self._has_label(issue, _SYSTEM_QUARANTINE_LABEL):
            self._lifecycle.add_label(issue_number, _SYSTEM_QUARANTINE_LABEL)
        if str(issue.get("status", "")).casefold() != "ready for ai":
            self._lifecycle.set_status(issue_number, "Ready for AI")
        comment = self._quarantine_comment(normalized_reason)
        self._lifecycle.comment(issue_number, comment)
        return {
            "status": "accepted",
            "action": "quarantine_before_run",
            "issue": issue_number,
        }

    @staticmethod
    def _require_canonical_open_quarantine_issue(
        issue_number: int, issue: dict[str, Any]
    ) -> None:
        if type(issue.get("number")) is not int or issue.get("number") != issue_number:
            raise ActionError("quarantine_before_run requires a canonical issue number")
        if str(issue.get("state", "OPEN")).upper() != "OPEN":
            raise ActionError("quarantine_before_run requires an open issue")
        if str(issue.get("status", "")).casefold() not in {"in progress", "ready for ai"}:
            raise ActionError("quarantine_before_run requires In Progress or Ready for AI")

    @staticmethod
    def _has_label(issue: dict[str, Any], label: str) -> bool:
        return label.casefold() in {
            str(value).casefold() for value in issue.get("labels", [])
        }

    @staticmethod
    def _runtime_contains_issue(snapshot: dict[str, Any], issue_number: int) -> bool:
        for lane in ("running", "retrying", "blocked"):
            entries = snapshot.get(lane, [])
            if not isinstance(entries, list):
                return True
            for entry in entries:
                if not isinstance(entry, dict):
                    return True
                if ActionService._runtime_issue_number(entry) in {None, issue_number}:
                    return True
        return False

    @staticmethod
    def _runtime_issue_number(entry: dict[str, Any]) -> int | None:
        raw_issue_id = entry.get("issue_id")
        raw_text = str(raw_issue_id) if raw_issue_id is not None else ""
        if raw_text.isdigit() and int(raw_text) > 0:
            return int(raw_text)
        match = re.search(r"(\d+)$", str(entry.get("issue_identifier") or ""))
        return int(match.group(1)) if match else None

    @staticmethod
    def _quarantine_reason(reason: Any) -> str:
        normalized = _ANSI_ESCAPE_SEQUENCE.sub("", str(reason or ""))
        normalized = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", normalized)
        normalized = " ".join(normalized.split())
        if not normalized:
            raise ActionError("quarantine_before_run requires a failure reason")
        return ActionService._utf8_prefix(normalized, _QUARANTINE_REASON_MAX_BYTES)

    @staticmethod
    def _quarantine_comment(reason: str) -> str:
        return ActionService._utf8_prefix(
            f"Symphony quarantined deterministic before_run failure: {reason}",
            _QUARANTINE_REASON_MAX_BYTES,
        )

    @staticmethod
    def _utf8_prefix(value: str, max_bytes: int) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= max_bytes:
            return value
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

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
