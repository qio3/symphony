import unittest

from owner_control.snapshot import SnapshotBuilder


class SnapshotBuilderTest(unittest.TestCase):
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
                "ready_for_acceptance": 1,
                "done": 1,
            },
        )
        self.assertEqual(snapshot["workers"], {"running": 1, "limit": 2})
        self.assertTrue(snapshot["test"]["synced"])
        self.assertFalse(snapshot["test"]["drift"])
        self.assertEqual([item["number"] for item in snapshot["owner_view"]["work_items"]], [401])
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


if __name__ == "__main__":
    unittest.main()
