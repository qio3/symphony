from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


_ISSUE_NUMBER = re.compile(r"(\d+)$")


class SnapshotBuilder:
    """Build the one machine-readable owner projection from deterministic inputs."""

    def build(
        self,
        *,
        service: dict[str, Any],
        intake_active: bool,
        worker_limit: int,
        runtime: dict[str, Any],
        project: dict[str, Any],
        canonical: dict[str, Any],
        test: dict[str, Any],
    ) -> dict[str, Any]:
        items = {
            str(item["number"]): self._project_item(item)
            for item in project.get("items", [])
            if isinstance(item, dict) and item.get("number") is not None
        }
        running = self._runtime_by_issue(runtime.get("running", []))
        retrying = self._runtime_by_issue(runtime.get("retrying", []))
        blocked = self._runtime_by_issue(runtime.get("blocked", []))
        issue_usage = {
            str(issue_key): self._normalize_issue_usage(value)
            for issue_key, value in (runtime.get("issue_usage") or {}).items()
            if isinstance(value, dict)
        }

        for issue_key, entry in {**running, **retrying, **blocked}.items():
            items.setdefault(issue_key, self._runtime_only_item(issue_key, entry))

        lanes: dict[str, list[dict[str, Any]]] = {
            "backlog": [],
            "work_items": [],
            "blocked": [],
            "ready_for_acceptance": [],
        }
        counts = {
            "backlog": 0,
            "ready_for_ai": 0,
            "running": 0,
            "queued": 0,
            "blocked": 0,
            "ready_for_acceptance": 0,
            "done": 0,
        }

        canonical_sha = canonical.get("sha")
        test_sha = test.get("sha")
        synced = bool(canonical_sha and test_sha and canonical_sha == test_sha)
        normalized_test = {
            **test,
            "synced": synced,
            "drift": bool(canonical_sha and test_sha and canonical_sha != test_sha),
        }
        codex_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "seconds_running": 0,
            **(runtime.get("codex_totals") or {}),
        }
        runtime_worker_limit = runtime.get("max_concurrent_agents")
        effective_worker_limit = (
            runtime_worker_limit
            if isinstance(runtime_worker_limit, int) and not isinstance(runtime_worker_limit, bool) and runtime_worker_limit > 0
            else worker_limit
        )

        for issue_key in sorted(items, key=self._issue_sort_key):
            item = items[issue_key]
            usage = issue_usage.get(issue_key)
            if isinstance(usage, dict):
                item["usage"] = deepcopy(usage)
            runtime_blocked = blocked.get(issue_key)
            runtime_running = running.get(issue_key)
            runtime_retry = retrying.get(issue_key)
            status = str(item.get("status") or "Backlog")

            if runtime_blocked is not None or status.casefold() == "blocked":
                item = self._with_runtime(item, runtime_blocked or {})
                item["status"] = "Blocked"
                item["stage"] = "Blocked"
                item["question"] = (
                    item.get("owner_question")
                    or (runtime_blocked or {}).get("last_message")
                    or item.get("question")
                )
                item["reason"] = (runtime_blocked or {}).get("error") or item.get("reason")
                lanes["blocked"].append(item)
                counts["blocked"] += 1
            elif status.casefold() == "ready for acceptance":
                item["test"] = item.get("test") or normalized_test
                lanes["ready_for_acceptance"].append(item)
                counts["ready_for_acceptance"] += 1
            elif runtime_running is not None:
                item = self._with_runtime(item, runtime_running)
                item["status"] = "running"
                item["stage"] = item.get("stage") or "In Progress"
                item["test"] = item.get("test") or normalized_test
                lanes["work_items"].append(item)
                counts["running"] += 1
            elif runtime_retry is not None:
                counts["queued"] += 1
            elif status.casefold() == "in progress":
                pass
            elif status.casefold() == "done" or str(item.get("state", "")).casefold() == "closed":
                counts["done"] += 1
            else:
                lanes["backlog"].append(item)
                if status.casefold() == "ready for ai":
                    counts["ready_for_ai"] += 1
                else:
                    counts["backlog"] += 1

        owner_view = {
            "available": True,
            "updated_at": project.get("updated_at") or runtime.get("generated_at"),
            **lanes,
            "counts": {
                "backlog": counts["backlog"] + counts["ready_for_ai"],
                "blocked": counts["blocked"],
                "ready_for_acceptance": counts["ready_for_acceptance"],
                "done": counts["done"],
            },
        }
        return {
            "version": 1,
            "generated_at": runtime.get("generated_at") or datetime.now(timezone.utc).isoformat(),
            "service": service,
            "intake": {"active": bool(intake_active), "status": "active" if intake_active else "paused"},
            "workers": {"running": counts["running"], "limit": max(int(effective_worker_limit), 0)},
            "models": deepcopy(runtime.get("models") or {}),
            "quota": _quota_windows(runtime.get("rate_limits")),
            "counts": counts,
            "canonical": canonical,
            "test": normalized_test,
            "issues": items,
            "owner_view": owner_view,
            "running": runtime.get("running", []),
            "retrying": runtime.get("retrying", []),
            "blocked": runtime.get("blocked", []),
            "codex_totals": codex_totals,
            "issue_usage": issue_usage,
            "rate_limits": runtime.get("rate_limits"),
        }

    @staticmethod
    def _project_item(item: dict[str, Any]) -> dict[str, Any]:
        copied = deepcopy(item)
        copied.setdefault("identifier", f"#{item['number']}")
        copied.setdefault("issue_identifier", copied["identifier"])
        copied.setdefault("issue_url", copied.get("url"))
        copied.setdefault("stage", copied.get("status"))
        return copied

    @staticmethod
    def _runtime_only_item(issue_key: str, entry: dict[str, Any]) -> dict[str, Any]:
        number = int(issue_key) if issue_key.isdigit() else issue_key
        return {
            "number": number,
            "identifier": entry.get("issue_identifier") or f"#{issue_key}",
            "issue_identifier": entry.get("issue_identifier") or f"#{issue_key}",
            "url": entry.get("issue_url"),
            "issue_url": entry.get("issue_url"),
            "title": None,
            "status": entry.get("state"),
            "stage": entry.get("state"),
            "state": "OPEN",
            "labels": [],
        }

    @staticmethod
    def _with_runtime(item: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        item = deepcopy(item)
        for key in (
            "started_at",
            "last_message",
            "last_event",
            "last_event_at",
            "error",
            "due_at",
            "model",
            "turn_count",
        ):
            if runtime.get(key) is not None:
                item[key] = runtime[key]
        return item

    @staticmethod
    def _runtime_by_issue(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result = {}
        for entry in entries or []:
            issue_key = SnapshotBuilder._runtime_issue_key(entry)
            if issue_key is not None:
                result[issue_key] = entry
        return result

    @staticmethod
    def _runtime_issue_key(entry: dict[str, Any]) -> str | None:
        issue_id = entry.get("issue_id")
        if issue_id is not None and str(issue_id).isdigit():
            return str(issue_id)
        match = _ISSUE_NUMBER.search(str(entry.get("issue_identifier") or ""))
        return match.group(1) if match else None

    @staticmethod
    def _issue_sort_key(issue_key: str) -> tuple[int, str]:
        return (int(issue_key), "") if issue_key.isdigit() else (2**63 - 1, issue_key)

    @staticmethod
    def _normalize_issue_usage(value: dict[str, Any]) -> dict[str, Any]:
        """Flatten the runtime ledger shape for every owner-facing consumer."""
        aggregate = value.get("aggregate")
        current = value.get("current")
        if not isinstance(aggregate, dict):
            return deepcopy(value)

        tokens = aggregate.get("token_usage")
        normalized = deepcopy(tokens) if isinstance(tokens, dict) else {}
        normalized["estimated_credits_micros"] = aggregate.get(
            "estimated_usage_credits_micros"
        )
        normalized["week_impact_percent"] = aggregate.get("week_impact_percent")
        normalized["issue_id"] = value.get("issue_id")
        normalized["issue_identifier"] = value.get("issue_identifier")
        if isinstance(current, dict):
            for key in (
                "thread_id",
                "session_id",
                "model",
                "tier",
                "started_at",
                "completed_at",
            ):
                if current.get(key) is not None:
                    normalized[key] = current[key]
        return normalized


def _quota_windows(rate_limits: Any) -> dict[str, dict[str, Any] | None]:
    by_duration: dict[int, dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            duration = value.get("windowDurationMins", value.get("window_duration_mins"))
            used = value.get("usedPercent", value.get("used_percent"))
            if duration in {300, 10080} and isinstance(used, (int, float)) and not isinstance(used, bool):
                by_duration[int(duration)] = {
                    "used_percent": used,
                    "window_duration_mins": int(duration),
                    "resets_at": value.get("resetsAt", value.get("resets_at")),
                }
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(rate_limits)
    return {"five_hour": by_duration.get(300), "weekly": by_duration.get(10080)}
