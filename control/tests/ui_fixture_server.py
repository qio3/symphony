from __future__ import annotations

import copy
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from owner_control.http_server import create_server
from owner_control.actions import ActionError


SNAPSHOT = {
    "version": 1,
    "generated_at": "2026-08-24T09:30:00Z",
    "refreshed_at": "2026-08-24T09:30:00Z",
    "stale": False,
    "service": {"live": True, "status": "running", "container": "zavod-symphony"},
    "intake": {"active": True, "status": "active"},
    "workers": {"running": 2, "limit": 5, "maximum": 12},
    "canonical": {"sha": "be44cf15a1234567", "url": "https://github.test/commit/be44cf15"},
    "test": {"sha": "be44cf15a1234567", "url": "https://test.example", "synced": True, "drift": False},
    "quota": {
        "five_hour": {"used_percent": 18, "window_duration_mins": 300, "resets_at": 1787550000},
        "weekly": {"used_percent": 42, "window_duration_mins": 10080, "resets_at": 1787990400},
    },
    "counts": {
        "ready_for_ai": 2,
        "running": 2,
        "queued": 2,
        "blocked": 1,
        "ready_for_acceptance": 1,
        "backlog": 7,
        "done": 18,
    },
    "history": [
        {"recorded_at": "2026-08-24T08:30:00Z", "counts": {"ready_for_ai": 6, "running": 1, "blocked": 2, "ready_for_acceptance": 0, "done": 15}, "workers": {"running": 1, "limit": 5}},
        {"recorded_at": "2026-08-24T08:45:00Z", "counts": {"ready_for_ai": 5, "running": 2, "blocked": 2, "ready_for_acceptance": 0, "done": 15}, "workers": {"running": 2, "limit": 5}},
        {"recorded_at": "2026-08-24T09:00:00Z", "counts": {"ready_for_ai": 4, "running": 2, "blocked": 1, "ready_for_acceptance": 1, "done": 16}, "workers": {"running": 2, "limit": 5}},
        {"recorded_at": "2026-08-24T09:15:00Z", "counts": {"ready_for_ai": 3, "running": 2, "blocked": 1, "ready_for_acceptance": 1, "done": 17}, "workers": {"running": 2, "limit": 5}},
        {"recorded_at": "2026-08-24T09:30:00Z", "counts": {"ready_for_ai": 2, "running": 2, "blocked": 1, "ready_for_acceptance": 1, "done": 18}, "workers": {"running": 2, "limit": 5}},
    ],
    "release_waves": {
        "mode": "landing-valve",
        "available": True,
        "limit": 2,
        "waves": [
            {
                "number": 28,
                "position": 1,
                "status": "collecting",
                "ready_prs": 1,
                "target_prs": 2,
                "progress_percent": 25,
                "summary": "1 of 2 pull requests is queued in the deterministic landing valve.",
                "status_entered_at": "2026-08-24T09:20:00Z",
                "status_history": [
                    {"phase": "queued", "entered_at": "2026-08-24T09:00:00Z", "exited_at": "2026-08-24T09:20:00Z"},
                    {"phase": "collecting", "entered_at": "2026-08-24T09:20:00Z", "exited_at": None},
                ],
                "issues": [
                    {"number": 401, "title": "Stabilize article publishing", "url": "https://github.test/issues/401", "pr": {"number": 421}, "ci": {"status": "success"}, "usage": {"week_impact_percent": 0.7}},
                ],
            },
            {
                "number": None,
                "position": 2,
                "status": "queued",
                "ready_prs": 2,
                "target_prs": 2,
                "progress_percent": 0,
                "summary": "Queued behind wave 1.",
                "issues": [
                    {"number": 403, "title": "Queued delivery A", "url": "https://github.test/issues/403", "pr": {"number": 423}},
                    {"number": 404, "title": "Queued delivery B", "url": "https://github.test/issues/404", "pr": {"number": 424}},
                ],
            },
            {
                "number": None,
                "position": 3,
                "status": "queued",
                "ready_prs": 1,
                "target_prs": 2,
                "progress_percent": 0,
                "summary": "Queued behind wave 2.",
                "issues": [
                    {"number": 405, "title": "Queued delivery C", "url": "https://github.test/issues/405", "pr": {"number": 425}},
                ],
            },
        ],
        "candidates": [
            {"number": 402, "title": "Add owner-safe delivery evidence", "url": "https://github.test/issues/402", "phase": "Coding", "usage": {"week_impact_percent": 0.2}},
        ],
        "recent": [
            {"id": 2700, "run_number": 27, "status": "completed", "conclusion": "success", "url": "https://github.test/actions/runs/2700", "head_sha": "be44cf15a1234567", "created_at": "2026-08-24T08:45:00Z"},
        ],
    },
    "infrastructure": {
        "queued_jobs": 3,
        "alerts": 0,
        "hosts": [
            {"name": "Local Symphony", "kind": "local", "role": "runtime", "status": "online", "cpu_percent": 31, "memory_percent": 48, "runners_busy": 0, "runners_online": 0, "runners_total": 0, "jobs": [{"issue": 401, "name": "Terra session"}]},
            {"name": "CI_1", "kind": "ci", "role": "primary-ci", "status": "online", "cpu_percent": 57, "memory_percent": 62, "runners_busy": 2, "runners_online": 3, "runners_total": 3, "jobs": [{"issue": 401, "name": "Backend"}, {"issue": 402, "name": "Frontend"}]},
            {"name": "CI_2", "kind": "ci", "role": "primary-ci", "status": "online", "cpu_percent": 84, "memory_percent": 71, "runners_busy": 2, "runners_online": 3, "runners_total": 3, "jobs": [{"issue": 399, "name": "E2E"}]},
            {"name": "Backup", "kind": "ci", "role": "control-only", "status": "online", "cpu_percent": 14, "memory_percent": 22, "runners_busy": 1, "runners_online": 1, "runners_total": 1, "jobs": [{"issue": None, "name": "Landing valve reconcile"}]},
        ],
        "capacity": {
            "primary_ci": {"busy": 4, "online": 6, "total": 6},
            "control": {"busy": 1, "online": 1, "total": 1},
        },
        "history": [
            {"recorded_at": "2026-08-24T08:30:00Z", "hosts": {"Local Symphony": {"cpu_percent": 12, "memory_percent": 41}, "CI_1": {"cpu_percent": 24, "memory_percent": 46}, "CI_2": {"cpu_percent": 38, "memory_percent": 58}}},
            {"recorded_at": "2026-08-24T08:45:00Z", "hosts": {"Local Symphony": {"cpu_percent": 38, "memory_percent": 44}, "CI_1": {"cpu_percent": 48, "memory_percent": 52}, "CI_2": {"cpu_percent": 74, "memory_percent": 64}}},
            {"recorded_at": "2026-08-24T09:00:00Z", "hosts": {"Local Symphony": {"cpu_percent": 29, "memory_percent": 47}, "CI_1": {"cpu_percent": 63, "memory_percent": 59}, "CI_2": {"cpu_percent": 88, "memory_percent": 69}}},
            {"recorded_at": "2026-08-24T09:15:00Z", "hosts": {"Local Symphony": {"cpu_percent": 42, "memory_percent": 49}, "CI_1": {"cpu_percent": 51, "memory_percent": 61}, "CI_2": {"cpu_percent": 79, "memory_percent": 72}}},
            {"recorded_at": "2026-08-24T09:30:00Z", "hosts": {"Local Symphony": {"cpu_percent": 31, "memory_percent": 48}, "CI_1": {"cpu_percent": 57, "memory_percent": 62}, "CI_2": {"cpu_percent": 84, "memory_percent": 71}}},
        ],
    },
    "sources": {
        "supervisor": {"status": "fresh", "confirmed_at": "2026-08-24T09:30:00Z", "error": None},
        "runtime": {"status": "fresh", "confirmed_at": "2026-08-24T09:30:00Z", "error": None},
        "github": {"status": "fresh", "confirmed_at": "2026-08-24T09:30:00Z", "error": None},
        "test": {"status": "fresh", "confirmed_at": "2026-08-24T09:30:00Z", "error": None},
    },
    "failures": [],
    "owner_view": {
        "blocked": [
            {
                "number": 388,
                "title": "Choose the canonical migration source without breaking legacy URLs",
                "issue_url": "https://github.test/issues/388",
                "question": "Should existing public IDs remain canonical after the migration?",
                "reason": "Owner decision is required before implementation can continue.",
            }
        ],
        "work_items": [
            {
                "number": 401,
                "title": "Stabilize article publishing and preserve exact TEST delivery",
                "issue_url": "https://github.test/issues/401",
                "stage": "Implementing",
                "display_phase": "Waiting CI",
                "status": "running",
                "started_at": "2026-08-24T09:02:00Z",
                "turn_count": 7,
                "model": {
                    "selected_tier": "terra",
                    "actual_model": "gpt-5.6-terra",
                    "escalation_history": [{"from": "luna", "reason": "max turns"}],
                },
                "pr": {"number": 421, "url": "https://github.test/pull/421"},
                "ci": {"status": "pending", "url": "https://github.test/pull/421/checks"},
                "test": {"status": "waiting", "url": "https://test.example"},
                "usage": {
                    "input_tokens": 12400,
                    "cached_input_tokens": 8900,
                    "cache_write_input_tokens": 1200,
                    "output_tokens": 3100,
                    "reasoning_output_tokens": 1400,
                    "total_tokens": 15500,
                    "estimated_credits_micros": 820000,
                    "week_impact_percent": 0.7,
                },
            },
            {
                "number": 402,
                "title": "Add owner-safe delivery evidence",
                "issue_url": "https://github.test/issues/402",
                "stage": "Implementing",
                "display_phase": "Coding",
                "status": "running",
                "started_at": "2026-08-24T09:18:00Z",
                "turn_count": 3,
                "status_entered_at": "2026-08-24T09:18:00Z",
                "status_history": [
                    {"phase": "Ready for AI", "entered_at": "2026-08-24T08:45:00Z", "exited_at": "2026-08-24T09:18:00Z"},
                    {"phase": "Coding", "entered_at": "2026-08-24T09:18:00Z", "exited_at": None},
                ],
                "model": {"selected_tier": "luna", "actual_model": "gpt-5.6-luna"},
                "ci": {"status": "success", "url": "https://github.test/actions/runs/2"},
                "usage": {"total_tokens": 4300, "estimated_credits_micros": 110000, "week_impact_percent": None},
            },
        ],
        "follow_ups": [
            {
                "number": 407,
                "title": "Resume delivery after the current CI gate",
                "issue_url": "https://github.test/issues/407",
                "stage": "In Progress",
                "attempt": 2,
                "error": "agent exited: :boom",
                "deferred_reason": "no available orchestrator slots",
            },
            {
                "number": 408,
                "title": "Continue after canonical merge",
                "issue_url": "https://github.test/issues/408",
                "stage": "In Progress",
                "attempt": 1,
                "error": "continuation scheduled",
            },
        ],
        "diagnostics": {
            "project_only_in_progress": [
                {
                    "number": 409,
                    "title": "Delivery lifecycle waiting outside Symphony",
                    "issue_url": "https://github.test/issues/409",
                    "stage": "In Progress",
                }
            ]
        },
        "ready_for_acceptance": [
            {
                "number": 399,
                "title": "Keep the owner dashboard stable during runtime restart",
                "issue_url": "https://github.test/issues/399",
                "stage": "Ready for Acceptance",
                "pr": {"number": 419, "url": "https://github.test/pull/419"},
                "ci": {"status": "success", "url": "https://github.test/pull/419/checks"},
                "test": {"sha": "be44cf15a1234567", "url": "https://test.example", "synced": True},
                "usage": {"total_tokens": 22100, "estimated_credits_micros": 1250000, "week_impact_percent": 1.1},
            }
        ],
        "backlog": [
            {"number": 405, "title": "Clarify deploy evidence", "issue_url": "https://github.test/issues/405", "status": "Ready for AI", "stage": "Ready for AI"},
            {"number": 406, "title": "Improve empty states", "issue_url": "https://github.test/issues/406", "status": "Backlog", "stage": "Backlog"},
        ],
        "done": [
            {
                "number": 397,
                "title": "Historical task without Symphony accounting",
                "issue_url": "https://github.test/issues/397",
                "status": "Done",
                "stage": "Done",
                "completed_at": "2026-08-18T08:48:00Z",
                "usage": {"week_impact_percent": None, "week_impact_availability": "not-recorded"},
            },
            {
                "number": 396,
                "title": "Recorded task below display precision",
                "issue_url": "https://github.test/issues/396",
                "status": "Done",
                "stage": "Done",
                "completed_at": "2026-08-24T08:47:00Z",
                "usage": {"total_tokens": 18, "estimated_credits_micros": 1, "week_impact_percent": 0.0},
            },
            {
                "number": 398,
                "title": "Persist task usage across service restarts",
                "issue_url": "https://github.test/issues/398",
                "status": "Done",
                "stage": "Done",
                "completed_at": "2026-08-24T08:48:00Z",
                "model": {"selected_tier": "terra", "actual_model": "gpt-5.6-terra"},
                "usage": {"total_tokens": 18400, "estimated_credits_micros": 940000, "week_impact_percent": 0.8},
            }
        ],
    },
}


class FixtureActions:
    def __init__(self, mode: str):
        self._mode = mode

    def execute(self, action, params):
        if self._mode == "reject":
            raise ActionError("State changed while reviewing. Refresh and verify the latest evidence.")
        if action == "pause":
            SNAPSHOT["intake"] = {"active": False, "status": "paused"}
        elif action == "set_workers":
            SNAPSHOT["workers"]["limit"] = int(params["limit"])
        elif action == "resume":
            SNAPSHOT["intake"] = {"active": True, "status": "active"}
        elif action == "stop_service":
            SNAPSHOT["service"] = {"live": False, "status": "exited", "container": "zavod-symphony"}
            SNAPSHOT["workers"]["running"] = 0
        elif action in {"start_service", "restart"}:
            SNAPSHOT["service"] = {"live": True, "status": "running", "container": "zavod-symphony"}
        return {"status": "accepted", "action": action, "params": copy.deepcopy(params)}


def refresh_fixture_clock() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def iso(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    SNAPSHOT["generated_at"] = iso(now)
    SNAPSHOT["refreshed_at"] = iso(now)
    for source in SNAPSHOT["sources"].values():
        source["confirmed_at"] = iso(now)
    SNAPSHOT["owner_view"]["work_items"][0]["started_at"] = iso(now - timedelta(minutes=28))
    SNAPSHOT["owner_view"]["work_items"][1]["started_at"] = iso(now - timedelta(minutes=12))
    task_history = SNAPSHOT["owner_view"]["work_items"][1]["status_history"]
    task_history[0]["entered_at"] = iso(now - timedelta(minutes=45))
    task_history[0]["exited_at"] = iso(now - timedelta(minutes=12))
    task_history[1]["entered_at"] = iso(now - timedelta(minutes=12))
    SNAPSHOT["owner_view"]["work_items"][1]["status_entered_at"] = iso(now - timedelta(minutes=12))
    wave = SNAPSHOT["release_waves"]["waves"][0]
    wave["status_history"][0]["entered_at"] = iso(now - timedelta(minutes=35))
    wave["status_history"][0]["exited_at"] = iso(now - timedelta(minutes=10))
    wave["status_history"][1]["entered_at"] = iso(now - timedelta(minutes=10))
    wave["status_entered_at"] = iso(now - timedelta(minutes=10))
    for index, sample in enumerate(SNAPSHOT["history"]):
        sample["recorded_at"] = iso(now - timedelta(minutes=15 * (len(SNAPSHOT["history"]) - index - 1)))
    for index, sample in enumerate(SNAPSHOT["infrastructure"]["history"]):
        sample["recorded_at"] = iso(now - timedelta(minutes=15 * (len(SNAPSHOT["infrastructure"]["history"]) - index - 1)))


if __name__ == "__main__":
    refresh_fixture_clock()
    mode = os.environ.get("OWNER_UI_FIXTURE_MODE", "healthy")
    if mode == "stale":
        SNAPSHOT["stale"] = True
        SNAPSHOT["sources"]["github"] = {
            "status": "stale",
            "confirmed_at": "2026-08-24T09:25:00Z",
            "error": "temporary GitHub outage",
        }
        SNAPSHOT["failures"] = [
            {
                "fingerprint": "github:OSError:transient",
                "message": "github snapshot unavailable: temporary GitHub outage",
                "unrecoverable": False,
            }
        ]
    elif mode == "rate_limit":
        SNAPSHOT["stale"] = True
        SNAPSHOT["sources"]["github"] = {
            "status": "stale",
            "confirmed_at": "2026-08-24T09:25:00Z",
            "error": "GitHub GraphQL rate limit exhausted",
        }
        SNAPSHOT["failures"] = [
            {
                "fingerprint": "github:RuntimeError:transient",
                "message": "github snapshot unavailable: GitHub GraphQL rate limit exhausted",
                "unrecoverable": False,
            }
        ]
    elif mode == "drift":
        SNAPSHOT["test"].update({"sha": "deadbeef00000000", "synced": False, "drift": True})
    elif mode == "item_drift":
        SNAPSHOT["owner_view"]["ready_for_acceptance"][0]["test"].update(
            {"sha": "deadbeef00000000", "synced": False}
        )
    elif mode == "stopped":
        SNAPSHOT["service"] = {
            "live": False,
            "status": "exited",
            "container": "zavod-symphony",
        }
        SNAPSHOT["workers"]["running"] = 0
        SNAPSHOT["counts"]["running"] = 0
        SNAPSHOT["running"] = []
        SNAPSHOT["owner_view"]["work_items"] = []
        SNAPSHOT["stale"] = True
        SNAPSHOT["sources"]["runtime"] = {
            "status": "stale",
            "confirmed_at": "2026-08-24T09:25:00Z",
            "error": "runtime stopped",
        }
    elif mode == "starting":
        SNAPSHOT["service"] = {
            "live": False,
            "status": "starting",
            "container": "zavod-symphony",
        }
        SNAPSHOT["workers"]["running"] = 0
        SNAPSHOT["counts"]["running"] = 0
        SNAPSHOT["running"] = []
        SNAPSHOT["owner_view"]["work_items"] = []
        SNAPSHOT["stale"] = True
        SNAPSHOT["sources"]["runtime"] = {
            "status": "stale",
            "confirmed_at": "2026-08-24T09:25:00Z",
            "error": "Symphony service is starting",
        }
    elif mode == "source_failure":
        SNAPSHOT["stale"] = True
        SNAPSHOT["service"] = {"live": False, "status": "unknown"}
        SNAPSHOT["sources"]["supervisor"] = {
            "status": "unavailable",
            "error": "Docker temporarily unavailable",
        }
        SNAPSHOT["sources"]["runtime"] = {
            "status": "stale",
            "confirmed_at": "2026-08-24T09:25:00Z",
            "error": "runtime temporarily unavailable",
        }
    elif mode == "long":
        SNAPSHOT["owner_view"]["blocked"][0]["title"] = "A very long owner-facing Issue title that must wrap cleanly without pushing controls beyond a narrow viewport"
        SNAPSHOT["owner_view"]["blocked"][0]["question"] = "Choose whether the existing canonical identifiers must remain stable across every public URL, historical import, redirect, and external integration before this work can safely continue."
        SNAPSHOT["owner_view"]["backlog"] = [
            {
                "number": 500 + index,
                "title": f"Backlog item {index} with a deliberately descriptive title for narrow viewport stress",
                "issue_url": f"https://github.test/issues/{500 + index}",
                "status": "Ready for AI" if index < 3 else "Backlog",
                "stage": "Ready for AI" if index < 3 else "Backlog",
            }
            for index in range(18)
        ]
    port = int(os.environ.get("OWNER_UI_FIXTURE_PORT", "4090"))
    server = create_server(
        ("127.0.0.1", port),
        token="fixture-control-token-0000000000000000",
        snapshot_provider=lambda: copy.deepcopy(SNAPSHOT),
        intake_provider=lambda: bool(SNAPSHOT["intake"]["active"]),
        action_service=FixtureActions(mode),
        logs_provider=lambda tail: ["fixture runtime log", f"tail={tail}"],
        runtime_diagnostics_url="http://127.0.0.1:4082/",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
