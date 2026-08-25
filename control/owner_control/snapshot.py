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
        quarantines: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        items = {
            str(item["number"]): self._project_item(item)
            for item in project.get("items", [])
            if isinstance(item, dict) and item.get("number") is not None
        }
        running = self._runtime_by_issue(runtime.get("running", []))
        retrying = self._runtime_by_issue(runtime.get("retrying", []))
        blocked = self._runtime_by_issue(runtime.get("blocked", []))
        system_quarantines = self._quarantines_by_issue(quarantines)
        issue_usage = {
            str(issue_key): self._normalize_issue_usage(value)
            for issue_key, value in (runtime.get("issue_usage") or {}).items()
            if isinstance(value, dict)
        }

        for issue_key, entry in {**running, **retrying, **blocked}.items():
            items.setdefault(issue_key, self._runtime_only_item(issue_key, entry))
        for issue_key in system_quarantines:
            items.setdefault(issue_key, self._runtime_only_item(issue_key, {}))

        lanes: dict[str, list[dict[str, Any]]] = {
            "backlog": [],
            "work_items": [],
            "blocked": [],
            "system_quarantines": [],
            "ready_for_acceptance": [],
            "follow_ups": [],
        }
        project_only_in_progress: list[dict[str, Any]] = []
        counts = {
            "backlog": 0,
            "ready_for_ai": 0,
            "running": 0,
            "queued": 0,
            "blocked": 0,
            "quarantined": 0,
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
            system_quarantine = system_quarantines.get(issue_key)
            status = str(item.get("status") or "Backlog")

            if system_quarantine is not None:
                item = self._with_system_quarantine(item, system_quarantine)
                lanes["system_quarantines"].append(item)
                counts["quarantined"] += 1
            elif runtime_blocked is not None or status.casefold() == "blocked":
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
                item["stage"] = "In Progress"
                item["test"] = item.get("test") or normalized_test
                item["display_phase"] = self._display_phase(item)
                lanes["work_items"].append(item)
                counts["running"] += 1
            elif runtime_retry is not None:
                item = self._with_runtime(item, runtime_retry)
                item["stage"] = item.get("stage") or "Delivery follow-up"
                item["display_phase"] = "Retrying"
                lanes["follow_ups"].append(item)
                counts["queued"] += 1
            elif status.casefold() == "in progress":
                project_only_in_progress.append(deepcopy(item))
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
            "diagnostics": {
                "project_only_in_progress": project_only_in_progress,
            },
            "counts": {
                "backlog": counts["backlog"] + counts["ready_for_ai"],
                "blocked": counts["blocked"],
                "quarantined": counts["quarantined"],
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
            "quarantined": [
                system_quarantines[issue_key]
                for issue_key in sorted(system_quarantines, key=self._issue_sort_key)
            ],
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
            "last_progress_at",
            "error",
            "deferred_reason",
            "delay_type",
            "due_at",
            "model",
            "turn_count",
            "attempt",
        ):
            if runtime.get(key) is not None:
                item[key] = runtime[key]
        return item

    @staticmethod
    def _display_phase(item: dict[str, Any]) -> str:
        pr = item.get("pr")
        if not isinstance(pr, dict):
            return "Agent active"

        if pr.get("merged") is True:
            test = item.get("test")
            if not isinstance(test, dict) or test.get("synced") is not True:
                return "Waiting TEST"
            return "Agent active"

        state = str(pr.get("state") or "").casefold()
        if state not in {"", "open"}:
            return "Agent active"

        ci = item.get("ci")
        ci_status = str(ci.get("status") or "").casefold() if isinstance(ci, dict) else ""
        if ci_status in {"pending", "queued", "in_progress", "waiting", "expected"}:
            return "Waiting CI"
        if ci_status == "success":
            return "Waiting merge"
        return "Agent active"

    @staticmethod
    def _with_system_quarantine(item: dict[str, Any], quarantine: dict[str, Any]) -> dict[str, Any]:
        item = deepcopy(item)
        item["status"] = "System quarantine"
        item["stage"] = "System quarantine"
        item["reason"] = quarantine["reason"]
        item["quarantined_at"] = quarantine["quarantined_at"]
        item["system_quarantine"] = True
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
    def _quarantines_by_issue(quarantines: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not isinstance(quarantines, dict):
            return {}
        return {
            str(value["issue"]): value
            for key, value in quarantines.items()
            if isinstance(value, dict)
            and isinstance(value.get("issue"), int)
            and value["issue"] > 0
            and str(key) == str(value["issue"])
            and isinstance(value.get("reason"), str)
            and isinstance(value.get("quarantined_at"), str)
        }

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
