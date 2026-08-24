from __future__ import annotations

import copy
import os
import sys
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
    "workers": {"running": 2, "limit": 5},
    "canonical": {"sha": "be44cf15a1234567", "url": "https://github.test/commit/be44cf15"},
    "test": {"sha": "be44cf15a1234567", "url": "https://test.example", "synced": True, "drift": False},
    "quota": {
        "five_hour": {"used_percent": 18, "window_duration_mins": 300, "resets_at": 1787550000},
        "weekly": {"used_percent": 42, "window_duration_mins": 10080, "resets_at": 1787990400},
    },
    "counts": {
        "ready_for_ai": 2,
        "running": 2,
        "queued": 0,
        "blocked": 1,
        "ready_for_acceptance": 1,
        "backlog": 7,
        "done": 18,
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
                "status": "running",
                "started_at": "2026-08-24T09:02:00Z",
                "turn_count": 7,
                "model": {"selected_tier": "terra", "actual_model": "gpt-5.6-terra"},
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
                "stage": "Reviewing CI",
                "status": "running",
                "started_at": "2026-08-24T09:18:00Z",
                "turn_count": 3,
                "model": {"selected_tier": "luna", "actual_model": "gpt-5.6-luna"},
                "ci": {"status": "success", "url": "https://github.test/actions/runs/2"},
                "usage": {"total_tokens": 4300, "estimated_credits_micros": 110000, "week_impact_percent": None},
            },
        ],
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
        elif action == "resume":
            SNAPSHOT["intake"] = {"active": True, "status": "active"}
        elif action == "stop_service":
            SNAPSHOT["service"] = {"live": False, "status": "exited", "container": "zavod-symphony"}
            SNAPSHOT["workers"]["running"] = 0
        elif action in {"start_service", "restart"}:
            SNAPSHOT["service"] = {"live": True, "status": "running", "container": "zavod-symphony"}
        return {"status": "accepted", "action": action, "params": copy.deepcopy(params)}


if __name__ == "__main__":
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
