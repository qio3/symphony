from __future__ import annotations

from typing import Any, Callable, Protocol

from .actions import ActionError, ActionService


class ReadOnlyAI(Protocol):
    def answer(self, question: str, snapshot: dict[str, Any]) -> str: ...


def update_is_allowed(update: dict[str, Any], *, owner_chat_id: int, owner_user_id: int) -> bool:
    message = update.get("message") or {}
    return (
        (message.get("chat") or {}).get("id") == owner_chat_id
        and (message.get("from") or {}).get("id") == owner_user_id
    )


class TelegramCommandHandler:
    def __init__(
        self,
        *,
        snapshot_provider: Callable[[], dict[str, Any]],
        action_service: ActionService,
        logs_provider: Callable[[int], list[str]],
        ai: ReadOnlyAI,
    ):
        self._snapshot_provider = snapshot_provider
        self._actions = action_service
        self._logs_provider = logs_provider
        self._ai = ai

    def handle(self, text: str) -> str:
        text = str(text or "").strip()
        if not text:
            return "Send /help for commands."
        if not text.startswith("/"):
            return self._ai.answer(text, self._snapshot_provider())

        command, _, arguments = text.partition(" ")
        command = command.split("@", 1)[0].casefold()
        arguments = arguments.strip()
        try:
            if command == "/status":
                return self._status(self._snapshot_provider())
            if command == "/work":
                return self._items("Work", self._snapshot_provider(), "work_items")
            if command == "/ready":
                return self._items("Ready", self._snapshot_provider(), "ready_for_acceptance")
            if command == "/blocked":
                return self._items("Blocked", self._snapshot_provider(), "blocked", include_question=True)
            if command == "/backlog":
                return self._items("Backlog", self._snapshot_provider(), "backlog")
            if command == "/logs":
                return "Logs\n" + "\n".join(self._logs_provider(30)[-30:])
            if command in {"/pause", "/resume", "/restart"}:
                return self._action(command[1:], {})
            if command in {"/run", "/accept"}:
                return self._action(command[1:], {"issue": self._issue_argument(arguments)})
            if command == "/rework":
                issue, separator, reason = arguments.partition(" ")
                if not separator or not reason.strip():
                    return "Usage: /rework <issue> <reason>"
                return self._action("rework", {"issue": self._issue_argument(issue), "reason": reason.strip()})
            if command == "/help":
                return self._help()
            return "Unknown command. Send /help."
        except ActionError as error:
            return f"Rejected: {error}"

    def _action(self, action: str, params: dict[str, Any]) -> str:
        result = self._actions.execute(action, params)
        issue = f" #{result['issue']}" if result.get("issue") else ""
        return f"{action}{issue}: {result.get('status', 'accepted')}"

    @staticmethod
    def _status(snapshot: dict[str, Any]) -> str:
        counts = snapshot.get("counts", {})
        service = snapshot.get("service", {})
        intake = snapshot.get("intake", {})
        workers = snapshot.get("workers", {})
        models = snapshot.get("models") or {}
        canonical = _short_sha((snapshot.get("canonical") or {}).get("sha"))
        test = snapshot.get("test") or {}
        test_suffix = " ✓" if test.get("synced") else " ⚠ drift"
        return "\n".join(
            [
                f"Symphony {'● Live' if service.get('live') else '○ Down'}",
                f"Intake: {'Active' if intake.get('active') else 'Paused'}",
                f"Workers: {workers.get('running', 0)}/{workers.get('limit', 0)}",
                f"Models: {_model_count(models, 'luna')} · {_model_count(models, 'terra')} · {_model_count(models, 'sol')}",
                "",
                f"Backlog: {counts.get('backlog', 0)}",
                f"Ready for AI: {counts.get('ready_for_ai', 0)}",
                f"Running: {counts.get('running', 0)}",
                f"Queued: {counts.get('queued', 0)}",
                f"Blocked: {counts.get('blocked', 0)}",
                f"Ready: {counts.get('ready_for_acceptance', 0)}",
                "",
                f"Canonical: {canonical}",
                f"TEST: {_short_sha(test.get('sha'))}{test_suffix}",
            ]
        )

    @staticmethod
    def _items(title: str, snapshot: dict[str, Any], lane: str, include_question: bool = False) -> str:
        items = (snapshot.get("owner_view") or {}).get(lane, [])
        if not items:
            return f"{title}: none"
        lines = [f"{title}: {len(items)}"]
        for item in items[:20]:
            number = item.get("number") or item.get("issue_identifier") or "?"
            line = f"#{number} {item.get('title') or ''}" if str(number).isdigit() else f"{number} {item.get('title') or ''}"
            model = item.get("model") or {}
            if model.get("selected_tier"):
                line += f" · {_title(model['selected_tier'])}"
            if include_question and item.get("question"):
                line += f"\nQuestion: {item['question']}"
            lines.append(line.strip())
        return "\n".join(lines)

    @staticmethod
    def _issue_argument(value: str) -> int:
        normalized = value.strip().lstrip("#")
        if not normalized.isdigit() or int(normalized) <= 0:
            raise ActionError("a positive issue number is required")
        return int(normalized)

    @staticmethod
    def _help() -> str:
        return "Commands: /status /work /ready /blocked /backlog /run /accept /rework /pause /resume /restart /logs /help"


class NotificationDetector:
    def detect(self, previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, str]]:
        if previous is None:
            current_owner = current.get("owner_view") or {}
            previous = {
                "service": {"live": True},
                "test": current.get("test") or {},
                "owner_view": {
                    "blocked": current_owner.get("blocked") or [],
                    "ready_for_acceptance": current_owner.get("ready_for_acceptance") or [],
                },
                "failures": [],
            }
        events = []
        previous_owner = previous.get("owner_view") or {}
        current_owner = current.get("owner_view") or {}
        previous_blocked = {
            _number(item): item.get("question") or item.get("reason") or "Owner input required"
            for item in previous_owner.get("blocked", [])
        }
        previous_ready = {
            _number(item): _ready_sha(item, previous)
            for item in previous_owner.get("ready_for_acceptance", [])
        }

        for item in current_owner.get("blocked", []):
            question = item.get("question") or item.get("reason") or "Owner input required"
            if previous_blocked.get(_number(item)) != question:
                events.append(
                    {
                        "kind": "blocked",
                        "fingerprint": f"blocked:{_number(item)}:{question}",
                        "text": f"Blocked #{_number(item)}: {item.get('title') or ''}\nQuestion: {question}\n{item.get('issue_url') or ''}".strip(),
                    }
                )

        for item in current_owner.get("ready_for_acceptance", []):
            if _number(item) not in previous_ready or previous_ready.get(_number(item)) != _ready_sha(item, current):
                test = item.get("test") or current.get("test") or {}
                pr = item.get("pr") or {}
                events.append(
                    {
                        "kind": "ready_for_acceptance",
                        "fingerprint": f"ready:{_number(item)}:{test.get('sha')}",
                        "text": (
                            f"Ready for Acceptance #{_number(item)}: {item.get('title') or ''}\n"
                            f"PR: {pr.get('url') or 'not reported'}\n"
                            f"TEST: {test.get('url') or 'not reported'}\n"
                            f"SHA: {_short_sha(test.get('sha'))}"
                        ),
                    }
                )

        was_live = bool((previous.get("service") or {}).get("live"))
        is_live = bool((current.get("service") or {}).get("live"))
        if was_live and not is_live:
            reason = (current.get("service") or {}).get("reason") or "service unavailable"
            events.append(
                {
                    "kind": "service_stopped",
                    "fingerprint": f"service_stopped:{reason}",
                    "text": f"Symphony unexpectedly stopped.\nReason: {reason}",
                }
            )

        old_failures = {str(item.get("fingerprint")) for item in previous.get("failures", [])}
        for failure in current.get("failures", []):
            if failure.get("unrecoverable") and str(failure.get("fingerprint")) not in old_failures:
                events.append(
                    {
                        "kind": "systemic_failure",
                        "fingerprint": f"failure:{failure.get('fingerprint')}",
                        "text": f"Systemic Symphony failure: {failure.get('message') or 'unknown failure'}",
                    }
                )
        return events


def _number(item: dict[str, Any]) -> Any:
    return item.get("number") or item.get("issue_id") or item.get("issue_identifier")


def _short_sha(value: Any) -> str:
    return str(value)[:8] if value else "unknown"


def _ready_sha(item: dict[str, Any], snapshot: dict[str, Any]) -> Any:
    return ((item.get("test") or snapshot.get("test") or {}).get("sha"))


def _model_count(models: dict[str, Any], tier: str) -> str:
    value = models.get(tier) or {}
    return f"{_title(tier)} {value.get('active', 0)}"


def _title(value: Any) -> str:
    text = str(value or "")
    return text[:1].upper() + text[1:]
