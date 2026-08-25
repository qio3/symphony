import unittest

from owner_control.snapshot import SnapshotBuilder


class SnapshotBuilderTest(unittest.TestCase):
    def test_owner_work_items_contains_only_runtime_running_entries(self):
        snapshot = SnapshotBuilder().build(
            service={"live": True},
            intake_active=True,
            worker_limit=2,
            runtime={
                "generated_at": "2026-08-24T10:00:00Z",
                "running": [{"issue_id": "501", "issue_identifier": "GH-501"}],
                "retrying": [{"issue_id": "503", "issue_identifier": "GH-503"}],
                "blocked": [],
                "codex_totals": {},
                "rate_limits": None,
            },
            project={
                "items": [
                    {"number": 501, "title": "Has a worker", "status": "In Progress", "state": "OPEN"},
                    {"number": 502, "title": "No worker", "status": "In Progress", "state": "OPEN"},
                ]
            },
            canonical={"sha": "aaaaaaaa11111111"},
            test={"sha": "aaaaaaaa11111111", "url": "https://test.example"},
        )

        self.assertEqual([item["number"] for item in snapshot["owner_view"]["work_items"]], [501])
        self.assertEqual(snapshot["workers"]["running"], 1)
        self.assertEqual(snapshot["counts"]["running"], 1)
        self.assertEqual(snapshot["counts"]["queued"], 1)
        self.assertIn("502", snapshot["issues"])
        self.assertIn("503", snapshot["issues"])
        self.assertEqual([item["issue_id"] for item in snapshot["retrying"]], ["503"])

    def test_separates_live_workers_from_retry_followups_and_project_only_progress(self):
        """The main work lane must never turn a Project column into worker count."""
        snapshot = SnapshotBuilder().build(
            service={"live": True},
            intake_active=True,
            worker_limit=5,
            runtime={
                "generated_at": "2026-08-25T10:00:00Z",
                "running": [{"issue_id": "501", "issue_identifier": "GH-501"}],
                "retrying": [
                    {
                        "issue_id": "503",
                        "issue_identifier": "GH-503",
                        "due_at": "2026-08-25T10:01:00Z",
                        "error": "agent exited: :boom",
                        "delay_type": "capacity",
                        "deferred_reason": "no available orchestrator slots",
                    }
                ],
                "blocked": [],
                "codex_totals": {},
                "rate_limits": None,
            },
            project={
                "items": [
                    {"number": 501, "title": "Live worker", "status": "In Progress", "state": "OPEN"},
                    {"number": 502, "title": "Project-only delivery", "status": "In Progress", "state": "OPEN"},
                    {"number": 503, "title": "Retrying delivery", "status": "In Progress", "state": "OPEN"},
                ]
            },
            canonical={"sha": "aaaaaaaa11111111"},
            test={"sha": "aaaaaaaa11111111", "url": "https://test.example"},
        )

        self.assertEqual(snapshot["workers"], {"running": 1, "limit": 5})
        self.assertEqual([item["number"] for item in snapshot["owner_view"]["work_items"]], [501])
        self.assertEqual([item["number"] for item in snapshot["owner_view"]["follow_ups"]], [503])
        self.assertEqual(
            snapshot["owner_view"]["follow_ups"][0]["deferred_reason"],
            "no available orchestrator slots",
        )
        self.assertEqual(
            [item["number"] for item in snapshot["owner_view"]["diagnostics"]["project_only_in_progress"]],
            [502],
        )

        main_owner_numbers = [
            item["number"]
            for lane in ("work_items", "blocked", "ready_for_acceptance", "backlog")
            for item in snapshot["owner_view"][lane]
        ]
        self.assertNotIn(502, main_owner_numbers)
        self.assertNotIn(503, main_owner_numbers)

    def test_places_each_issue_in_one_owner_lane_and_reports_sha_sync(self):
        project = {
            "items": [
                {
                    "number": 401,
                    "identifier": "#401",
                    "title": "Queued issue",
                    "url": "https://github.test/issues/401",
                    "status": "Ready for AI",
                    "state": "OPEN",
                    "labels": ["symphony"],
                },
                {
                    "number": 402,
                    "identifier": "#402",
                    "title": "Needs a decision",
                    "url": "https://github.test/issues/402",
                    "status": "Blocked",
                    "state": "OPEN",
                    "owner_question": "Which source is canonical?",
                },
                {
                    "number": 403,
                    "identifier": "#403",
                    "title": "Review on TEST",
                    "url": "https://github.test/issues/403",
                    "status": "Ready for Acceptance",
                    "state": "OPEN",
                    "pr": {"number": 88, "url": "https://github.test/pull/88", "merged": True},
                    "ci": {"status": "success", "url": "https://github.test/pull/88/checks"},
                },
                {
                    "number": 404,
                    "identifier": "#404",
                    "title": "Completed",
                    "url": "https://github.test/issues/404",
                    "status": "Done",
                    "state": "CLOSED",
                },
            ]
        }
        runtime = {
            "generated_at": "2026-08-23T10:00:00Z",
            "running": [
                {
                    "issue_id": "401",
                    "issue_identifier": "GH-401",
                    "started_at": "2026-08-23T09:45:00Z",
                    "last_message": "working",
                }
            ],
            "retrying": [],
            "blocked": [],
            "codex_totals": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3, "seconds_running": 4},
            "rate_limits": None,
        }

        snapshot = SnapshotBuilder().build(
            service={"live": True},
            intake_active=True,
            worker_limit=2,
            runtime=runtime,
            project=project,
            canonical={"sha": "be44cf15aaaaaaaa", "url": "https://github.test/commit/be44cf15"},
            test={"sha": "be44cf15aaaaaaaa", "url": "https://test.example"},
        )

        self.assertEqual(
            snapshot["counts"],
            {
                "backlog": 0,
                "ready_for_ai": 0,
                "running": 1,
                "queued": 0,
                "blocked": 1,
                "quarantined": 0,
                "ready_for_acceptance": 1,
                "done": 1,
            },
        )
        self.assertEqual(snapshot["workers"], {"running": 1, "limit": 2})
        self.assertTrue(snapshot["test"]["synced"])
        self.assertFalse(snapshot["test"]["drift"])
        self.assertEqual([item["number"] for item in snapshot["owner_view"]["work_items"]], [401])
        self.assertEqual(snapshot["owner_view"]["work_items"][0]["stage"], "In Progress")
        self.assertEqual([item["number"] for item in snapshot["owner_view"]["blocked"]], [402])
        self.assertEqual([item["number"] for item in snapshot["owner_view"]["ready_for_acceptance"]], [403])
        owner_numbers = [
            item["number"]
            for lane in ("work_items", "blocked", "ready_for_acceptance", "backlog")
            for item in snapshot["owner_view"][lane]
        ]
        self.assertEqual(len(owner_numbers), len(set(owner_numbers)))

    def test_marks_test_drift_and_promotes_runtime_blocker_question(self):
        snapshot = SnapshotBuilder().build(
            service={"live": True},
            intake_active=False,
            worker_limit=2,
            runtime={
                "generated_at": "2026-08-23T10:00:00Z",
                "running": [],
                "retrying": [],
                "blocked": [
                    {
                        "issue_id": "405",
                        "issue_identifier": "GH-405",
                        "issue_url": "https://github.test/issues/405",
                        "error": "owner input required",
                        "last_message": "Should the migration preserve legacy IDs?",
                    }
                ],
                "codex_totals": {},
                "rate_limits": None,
            },
            project={
                "items": [
                    {
                        "number": 405,
                        "identifier": "#405",
                        "title": "Migration",
                        "url": "https://github.test/issues/405",
                        "status": "In Progress",
                        "state": "OPEN",
                    }
                ]
            },
            canonical={"sha": "aaaaaaaa11111111", "url": None},
            test={"sha": "bbbbbbbb22222222", "url": "https://test.example"},
        )

        self.assertEqual(snapshot["intake"], {"active": False, "status": "paused"})
        self.assertEqual(
            snapshot["codex_totals"],
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "seconds_running": 0},
        )
        self.assertTrue(snapshot["test"]["drift"])
        self.assertFalse(snapshot["test"]["synced"])
        self.assertEqual(
            snapshot["owner_view"]["blocked"][0]["question"],
            "Should the migration preserve legacy IDs?",
        )

    def test_projects_persisted_system_quarantine_without_inflating_owner_blocked(self):
        snapshot = SnapshotBuilder().build(
            service={"live": True},
            intake_active=True,
            worker_limit=2,
            runtime={
                "generated_at": "2026-08-25T10:00:00Z",
                "running": [],
                "retrying": [],
                "blocked": [],
                "codex_totals": {},
                "rate_limits": None,
            },
            project={
                "items": [
                    {
                        "number": 405,
                        "title": "Hook failure",
                        "url": "https://github.test/issues/405",
                        "status": "Ready for AI",
                        "state": "OPEN",
                        "labels": ["symphony:quarantined"],
                    }
                ]
            },
            canonical={"sha": "aaaaaaaa11111111"},
            test={"sha": "aaaaaaaa11111111", "url": "https://test.example"},
            quarantines={
                "405": {
                    "issue": 405,
                    "reason": "workspace before_run hook failed",
                    "quarantined_at": "2026-08-25T10:00:00Z",
                }
            },
        )

        self.assertEqual(snapshot["counts"]["blocked"], 0)
        self.assertEqual(snapshot["counts"]["quarantined"], 1)
        self.assertEqual(snapshot["owner_view"]["blocked"], [])
        self.assertEqual(snapshot["owner_view"]["backlog"], [])
        quarantine = snapshot["owner_view"]["system_quarantines"][0]
        self.assertEqual(quarantine["number"], 405)
        self.assertEqual(quarantine["stage"], "System quarantine")
        self.assertEqual(quarantine["reason"], "workspace before_run hook failed")
        self.assertEqual(snapshot["quarantined"][0]["issue"], 405)

    def test_projects_runtime_model_routing_without_reclassifying_issues(self):
        model = {
            "selected_tier": "terra",
            "actual_model": "gpt-5.6-terra",
            "routing_reason": "escalation:max_turns_exhausted",
            "escalated_from": "luna",
            "escalation_history": [
                {"from": "luna", "to": "terra", "reason": "max_turns_exhausted"}
            ],
        }
        runtime = {
            "generated_at": "2026-08-23T10:00:00Z",
            "running": [
                {
                    "issue_id": "406",
                    "issue_identifier": "GH-406",
                    "started_at": "2026-08-23T09:50:00Z",
                    "model": model,
                }
            ],
            "retrying": [],
            "blocked": [],
            "models": {
                "luna": {"active": 0, "completed": 4},
                "terra": {"active": 1, "completed": 2},
                "sol": {"active": 0, "completed": 1},
            },
            "codex_totals": {},
            "rate_limits": None,
        }

        snapshot = SnapshotBuilder().build(
            service={"live": True},
            intake_active=True,
            worker_limit=2,
            runtime=runtime,
            project={
                "items": [
                    {
                        "number": 406,
                        "identifier": "#406",
                        "title": "Escalated task",
                        "url": "https://github.test/issues/406",
                        "status": "In Progress",
                        "state": "OPEN",
                    }
                ]
            },
            canonical={"sha": "aaaaaaaa11111111"},
            test={"sha": "aaaaaaaa11111111", "url": "https://test.example"},
        )

        self.assertEqual(snapshot["models"], runtime["models"])
        self.assertEqual(snapshot["owner_view"]["work_items"][0]["model"], model)

    def test_selects_quota_windows_by_duration_not_primary_secondary_position(self):
        runtime = {
            "generated_at": "2026-08-23T10:00:00Z",
            "running": [],
            "retrying": [],
            "blocked": [],
            "codex_totals": {},
            "rate_limits": {
                "primary": {
                    "usedPercent": 42,
                    "windowDurationMins": 10080,
                    "resetsAt": 1787558400,
                },
                "secondary": {
                    "usedPercent": 17,
                    "windowDurationMins": 300,
                    "resetsAt": 1787500000,
                },
            },
        }

        snapshot = SnapshotBuilder().build(
            service={"live": True},
            intake_active=True,
            worker_limit=5,
            runtime=runtime,
            project={"items": []},
            canonical={"sha": "aaaaaaaa11111111"},
            test={"sha": "aaaaaaaa11111111", "url": "https://test.example"},
        )

        self.assertEqual(snapshot["quota"]["five_hour"]["used_percent"], 17)
        self.assertEqual(snapshot["quota"]["five_hour"]["window_duration_mins"], 300)
        self.assertEqual(snapshot["quota"]["weekly"]["used_percent"], 42)
        self.assertEqual(snapshot["quota"]["weekly"]["window_duration_mins"], 10080)

    def test_joins_persisted_issue_usage_into_owner_lanes(self):
        runtime = {
            "generated_at": "2026-08-23T10:00:00Z",
            "running": [
                {
                    "issue_id": "407",
                    "issue_identifier": "GH-407",
                    "started_at": "2026-08-23T09:50:00Z",
                    "turn_count": 6,
                }
            ],
            "retrying": [],
            "blocked": [],
            "codex_totals": {},
            "rate_limits": None,
            "max_concurrent_agents": 5,
            "issue_usage": {
                "407": {
                    "issue_id": "internal-407",
                    "issue_identifier": "GH-407",
                    "current": {
                        "thread_id": "thread-407",
                        "model": "gpt-5.6-terra",
                        "tier": "terra",
                        "started_at": "2026-08-23T09:50:00Z",
                        "completed_at": None,
                    },
                    "aggregate": {
                        "token_usage": {
                            "input_tokens": 1200,
                            "cached_input_tokens": 800,
                            "cache_write_input_tokens": 100,
                            "output_tokens": 300,
                            "reasoning_output_tokens": 120,
                            "total_tokens": 1500,
                        },
                        "estimated_usage_credits_micros": 44000,
                    },
                }
            },
        }

        snapshot = SnapshotBuilder().build(
            service={"live": True},
            intake_active=True,
            worker_limit=2,
            runtime=runtime,
            project={
                "items": [
                    {
                        "number": 407,
                        "title": "Usage survives completion",
                        "url": "https://github.test/issues/407",
                        "status": "In Progress",
                        "state": "OPEN",
                    }
                ]
            },
            canonical={"sha": "aaaaaaaa11111111"},
            test={"sha": "aaaaaaaa11111111", "url": "https://test.example"},
        )

        item = snapshot["owner_view"]["work_items"][0]
        self.assertEqual(item["turn_count"], 6)
        self.assertEqual(item["usage"]["total_tokens"], 1500)
        self.assertEqual(item["usage"]["estimated_credits_micros"], 44000)
        self.assertEqual(item["usage"]["tier"], "terra")
        self.assertEqual(snapshot["issue_usage"]["407"]["cached_input_tokens"], 800)
        self.assertEqual(snapshot["workers"], {"running": 1, "limit": 5})


if __name__ == "__main__":
    unittest.main()
