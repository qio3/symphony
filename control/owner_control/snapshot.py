from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
        landing: dict[str, Any] | None = None,
        quarantines: dict[str, Any] | None = None,
        worker_max: int | None = None,
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
        quota = _quota_windows(runtime.get("rate_limits"))
        issue_usage = {
            str(issue_key): self._normalize_issue_usage(value)
            for issue_key, value in (runtime.get("issue_usage") or {}).items()
            if isinstance(value, dict)
        }
        self._classify_done_usage(issue_usage, items, quota.get("weekly"))
        self._allocate_week_impacts(issue_usage, quota.get("weekly"))

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
            "done": [],
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
        canonical_ci = canonical.get("ci") if isinstance(canonical, dict) else None
        canonical_ci_status = (
            str(canonical_ci.get("status") or "unknown").casefold()
            if isinstance(canonical_ci, dict)
            else "unknown"
        )
        systemic_gate = {
            "blocked": canonical_ci_status == "failure",
            "reason": (
                "canonical CI is failing"
                if canonical_ci_status == "failure"
                else None
            ),
        }
        effective_intake_active = bool(intake_active) and not systemic_gate["blocked"]
        codex_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "seconds_running": 0,
            **(runtime.get("codex_totals") or {}),
        }
        effective_worker_limit = worker_limit

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
                item.setdefault("completed_at", item.get("closed_at"))
                if not isinstance(item.get("usage"), dict):
                    item["usage"] = {
                        "week_impact_percent": None,
                        "week_impact_availability": "not-recorded",
                    }
                lanes["done"].append(item)
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
        release_waves = self._release_waves(
            landing,
            items,
            lanes,
            canonical,
            normalized_test,
        )
        return {
            "version": 1,
            "generated_at": runtime.get("generated_at") or datetime.now(timezone.utc).isoformat(),
            "service": service,
            "intake": {
                "active": effective_intake_active,
                "requested_active": bool(intake_active),
                "status": (
                    "blocked-systemic"
                    if systemic_gate["blocked"]
                    else "active"
                    if intake_active
                    else "paused"
                ),
            },
            "systemic_gate": systemic_gate,
            "workers": {
                "running": counts["running"],
                "limit": max(int(effective_worker_limit), 0),
                **(
                    {"maximum": max(int(worker_max), 0)}
                    if worker_max is not None
                    else {}
                ),
            },
            "models": deepcopy(runtime.get("models") or {}),
            "quota": quota,
            "counts": counts,
            "canonical": canonical,
            "test": normalized_test,
            "issues": items,
            "owner_view": owner_view,
            "release_waves": release_waves,
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
    def _release_waves(
        landing: dict[str, Any] | None,
        project_items: dict[str, dict[str, Any]],
        lanes: dict[str, list[dict[str, Any]]],
        canonical: dict[str, Any],
        test: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(landing, dict) or landing.get("available") is False:
            return {"mode": "landing-valve", "available": False, "waves": [], "recent": []}

        limit = landing.get("limit") if landing.get("limit") in {1, 2} else 1
        queued = sorted(
            [item for item in landing.get("queued") or [] if isinstance(item, dict)],
            key=lambda item: item.get("number") if isinstance(item.get("number"), int) else 2**63 - 1,
        )
        runs = [item for item in landing.get("runs") or [] if isinstance(item, dict)]
        active_run = next(
            (
                run
                for run in runs
                if str(run.get("status") or "").casefold() in {"queued", "in_progress", "waiting"}
            ),
            None,
        )
        issues_by_pr = {
            (item.get("pr") or {}).get("number"): item
            for item in project_items.values()
            if isinstance(item.get("pr"), dict) and (item.get("pr") or {}).get("number")
        }
        wave_item_groups = []
        for pull in queued:
            pull_items = []
            issue_numbers = [
                number for number in pull.get("issue_numbers") or [] if str(number) in project_items
            ]
            if not issue_numbers and pull.get("number") in issues_by_pr:
                issue_numbers = [issues_by_pr[pull["number"]]["number"]]
            if not issue_numbers:
                pull_items.append(
                    {
                        "number": None,
                        "title": pull.get("title"),
                        "url": None,
                        "pr": deepcopy(pull),
                        "ci": {"status": "success"},
                    }
                )
            else:
                for number in issue_numbers:
                    item = project_items[str(number)]
                    pull_items.append(
                        {
                            "number": item.get("number"),
                            "title": item.get("title"),
                            "url": item.get("issue_url") or item.get("url"),
                            "pr": {**deepcopy(pull), "url": pull.get("url")},
                            "ci": {"status": "success"},
                            "usage": deepcopy(item.get("usage")),
                        }
                    )
            wave_item_groups.append(pull_items)

        canonical_ci = str(((canonical.get("ci") or {}).get("status") or "unknown")).casefold()
        if active_run is not None:
            status, progress, summary = (
                "landing",
                58,
                "The fixed GitHub landing valve is processing the queued pull requests.",
            )
        elif canonical_ci in {"pending", "queued", "in_progress", "waiting"}:
            status, progress, summary = (
                "wave CI",
                72,
                "Queued pull requests are waiting for the current canonical CI gate.",
            )
        elif canonical_ci == "failure":
            status, progress, summary = (
                "blocked",
                72,
                "Canonical CI is failing; the landing valve remains fail closed.",
            )
        elif canonical.get("sha") and test.get("sha") and canonical.get("sha") != test.get("sha"):
            status, progress, summary = (
                "waiting TEST",
                88,
                "The landed canonical SHA is waiting for exact-SHA TEST delivery.",
            )
        else:
            status, progress, summary = (
                "collecting",
                min(50, round(50 * len(queued) / limit)),
                f"{len(queued)} of {limit} pull requests are queued in the deterministic landing valve.",
            )

        waves = []
        if queued or active_run is not None:
            run_number = (active_run or (runs[0] if runs else {})).get("run_number")
            chunks = [
                wave_item_groups[index : index + limit]
                for index in range(0, len(wave_item_groups), limit)
            ] or [[]]
            for position, chunk in enumerate(chunks, start=1):
                first = position == 1
                waves.append(
                    {
                        "number": run_number if first else None,
                        "position": position,
                        "status": status if first else "queued",
                        "ready_prs": len(chunk),
                        "target_prs": limit,
                        "progress_percent": progress if first else 0,
                        "summary": summary if first else f"Queued behind wave {position - 1}.",
                        "issues": [item for group in chunk for item in group],
                        "run": deepcopy(active_run) if first else None,
                    }
                )

        candidates = []
        queued_prs = {pull.get("number") for pull in queued}
        for item in [*(lanes.get("work_items") or []), *(lanes.get("follow_ups") or [])]:
            pr = item.get("pr") or {}
            if pr.get("number") in queued_prs:
                continue
            if pr.get("number"):
                candidates.append(
                    {
                        "number": item.get("number"),
                        "title": item.get("title"),
                        "url": item.get("issue_url") or item.get("url"),
                        "pr": deepcopy(pr),
                        "ci": deepcopy(item.get("ci")),
                        "phase": item.get("display_phase") or item.get("stage"),
                        "usage": deepcopy(item.get("usage")),
                    }
                )

        recent = [
            run
            for run in runs
            if str(run.get("status") or "").casefold() == "completed"
        ][:5]
        return {
            "mode": "landing-valve",
            "available": True,
            "limit": limit,
            "waves": waves,
            "candidates": candidates,
            "recent": recent,
        }

    @staticmethod
    def _classify_done_usage(
        issue_usage: dict[str, dict[str, Any]],
        project_items: dict[str, dict[str, Any]],
        weekly: dict[str, Any] | None,
    ) -> None:
        window_start = SnapshotBuilder._weekly_window_start(weekly)
        for issue_key, item in project_items.items():
            status = str(item.get("status") or "").casefold()
            state = str(item.get("state") or "").casefold()
            if status != "done" and state != "closed":
                continue
            usage = issue_usage.get(issue_key)
            if not isinstance(usage, dict):
                continue

            completed_at = SnapshotBuilder._parse_datetime(usage.get("completed_at"))
            closed_at = SnapshotBuilder._parse_datetime(item.get("closed_at"))
            if completed_at is None and closed_at is not None:
                usage["completed_at"] = item.get("closed_at")
                usage["completion_basis"] = "github-closed-at"
                completed_at = closed_at

            if completed_at is None:
                usage["week_impact_percent"] = None
                usage.pop("week_impact_basis", None)
                usage["week_impact_availability"] = "completion-time-unavailable"
            elif window_start is not None and completed_at < window_start:
                usage["week_impact_percent"] = None
                usage.pop("week_impact_basis", None)
                usage["week_impact_availability"] = "outside-current-week"

    @staticmethod
    def _weekly_window_start(weekly: dict[str, Any] | None) -> datetime | None:
        if not isinstance(weekly, dict):
            return None
        duration = weekly.get("window_duration_mins")
        reset = SnapshotBuilder._parse_datetime(weekly.get("resets_at"))
        if not isinstance(duration, int) or duration <= 0 or reset is None:
            return None
        return reset - timedelta(minutes=duration)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.fromtimestamp(float(value), timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    @staticmethod
    def _allocate_week_impacts(
        issue_usage: dict[str, dict[str, Any]], weekly: dict[str, Any] | None
    ) -> None:
        used = weekly.get("used_percent") if isinstance(weekly, dict) else None
        if not isinstance(used, (int, float)) or used < 0:
            return
        credits = {
            key: value.get("estimated_credits_micros")
            for key, value in issue_usage.items()
            if isinstance(value.get("estimated_credits_micros"), int)
            and value.get("estimated_credits_micros") > 0
            and value.get("week_impact_availability")
            not in {"completion-time-unavailable", "outside-current-week"}
        }
        total = sum(credits.values())
        if total <= 0:
            return
        for key, value in credits.items():
            if issue_usage[key].get("week_impact_percent") is not None:
                continue
            issue_usage[key]["week_impact_percent"] = round(used * value / total, 2)
            issue_usage[key]["week_impact_basis"] = "recorded-credit-share"

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
            return "Coding"

        if pr.get("merged") is True:
            test = item.get("test")
            if not isinstance(test, dict) or test.get("contains_merge") is not True:
                return "Waiting TEST"
            return "Coding"

        state = str(pr.get("state") or "").casefold()
        if state not in {"", "open"}:
            return "Coding"

        ci = item.get("ci")
        ci_status = str(ci.get("status") or "").casefold() if isinstance(ci, dict) else ""
        if ci_status in {"pending", "queued", "in_progress", "waiting", "expected"}:
            return "Waiting CI"
        if ci_status == "success":
            return "Waiting merge"
        return "Coding"

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
