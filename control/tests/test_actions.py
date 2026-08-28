import tempfile
import threading
import unittest
from pathlib import Path

from owner_control.actions import ActionError, ActionService, RetryableActionError
from owner_control.state_store import StateStore


class FakeLifecycle:
    def __init__(self):
        self.calls = []
        self.fail_on = None
        self.comment_keys = set()

    def set_status(self, issue, status):
        self.calls.append(("set_status", issue, status))
        if self.fail_on == "set_status":
            raise RuntimeError("status unavailable")

    def add_label(self, issue, label):
        self.calls.append(("add_label", issue, label))
        if self.fail_on == "add_label":
            raise RuntimeError("label unavailable")

    def remove_label(self, issue, label):
        self.calls.append(("remove_label", issue, label))
        if self.fail_on == "remove_label":
            raise RuntimeError("label rollback unavailable")

    def comment(self, issue, body):
        self.calls.append(("comment", issue, body))

    def comment_once(self, issue, body, action_key):
        if action_key in self.comment_keys:
            return
        self.comment_keys.add(action_key)
        self.comment(issue, body)
        if self.fail_on == "comment_response":
            raise RuntimeError("comment response lost")

    def close_issue(self, issue):
        self.calls.append(("close_issue", issue))


class FakeSupervisor:
    def __init__(self):
        self.restart_count = 0
        self.start_count = 0
        self.stop_count = 0
        self.restart_started = threading.Event()
        self.release_restart = threading.Event()
        self.release_restart.set()
        self.stop_started = threading.Event()
        self.release_stop = threading.Event()

    def restart(self):
        self.restart_count += 1
        self.restart_started.set()
        self.release_restart.wait(timeout=2)
        return {"accepted": True}

    def start(self):
        self.start_count += 1
        return {"accepted": True}

    def stop(self):
        self.stop_count += 1
        self.stop_started.set()
        self.release_stop.wait(timeout=2)
        return {"accepted": True}


class ActionServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state_path = Path(self.tempdir.name) / "state.json"
        self.store = StateStore(self.state_path)
        self.lifecycle = FakeLifecycle()
        self.supervisor = FakeSupervisor()
        self.snapshot = {
            "workers": {"running": 0, "limit": 12, "maximum": 12},
            "test": {"synced": True},
            "sources": {
                "supervisor": {"status": "fresh"},
                "runtime": {"status": "fresh"},
                "github": {"status": "fresh"},
                "test": {"status": "fresh"},
            },
            "issues": {
                "401": {"number": 401, "status": "Backlog", "state": "OPEN", "labels": []},
                "402": {
                    "number": 402,
                    "status": "Ready for Acceptance",
                    "state": "OPEN",
                    "labels": [],
                    "test": {"sha": "deployed", "merge_sha": "merged", "contains_merge": True},
                },
                "403": {"number": 403, "status": "Ready for AI", "state": "OPEN", "labels": []},
                "404": {"number": 404, "status": "In Progress", "state": "OPEN", "labels": []},
                "405": {"number": 405, "status": "In Progress", "state": "OPEN", "labels": ["symphony"]},
            },
            "running": [],
            "retrying": [],
            "blocked": [],
        }
        self.actions = ActionService(
            snapshot_provider=lambda: self.snapshot,
            lifecycle=self.lifecycle,
            supervisor=self.supervisor,
            state_store=self.store,
        )

    def test_run_sets_ready_for_ai_before_acquiring_lease(self):
        result = self.actions.execute("run", {"issue": 401})

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(
            self.lifecycle.calls,
            [("set_status", 401, "Ready for AI"), ("add_label", 401, "symphony")],
        )

    def test_lease_only_accepts_an_open_ready_for_ai_issue(self):
        result = self.actions.execute("lease", {"issue": 403})

        self.assertEqual(
            result,
            {"status": "accepted", "action": "lease", "issue": 403},
        )
        self.assertEqual(
            self.lifecycle.calls,
            [
                ("add_label", 403, "symphony"),
                ("set_status", 403, "In Progress"),
            ],
        )

        self.lifecycle.calls.clear()
        self.snapshot["issues"]["403"]["labels"] = ["Symphony"]
        self.actions.execute("lease", {"issue": 403})
        self.assertEqual(self.lifecycle.calls, [("set_status", 403, "In Progress")])
        self.lifecycle.calls.clear()

        for status in ("Backlog", "In Progress"):
            self.snapshot["issues"]["403"].update({"status": status, "state": "OPEN", "labels": []})
            with self.assertRaisesRegex(ActionError, "Ready for AI"):
                self.actions.execute("lease", {"issue": 403})

        self.snapshot["issues"]["403"].update(
            {"status": "Ready for AI", "state": "CLOSED"}
        )
        with self.assertRaisesRegex(ActionError, "open issue"):
            self.actions.execute("lease", {"issue": 403})

        self.snapshot["issues"]["403"].update(
            {"number": 999, "status": "Ready for AI", "state": "OPEN"}
        )
        with self.assertRaisesRegex(ActionError, "canonical issue number"):
            self.actions.execute("lease", {"issue": 403})

        self.snapshot["issues"]["403"]["number"] = 403
        self.store.set_intake_active(False)
        with self.assertRaisesRegex(ActionError, "active intake"):
            self.actions.execute("lease", {"issue": 403})
        self.assertEqual(self.lifecycle.calls, [])

    def test_lease_rolls_back_only_a_new_lease_when_project_status_update_fails(self):
        self.lifecycle.fail_on = "set_status"

        with self.assertRaisesRegex(RuntimeError, "status unavailable"):
            self.actions.execute("lease", {"issue": 403})
        self.assertEqual(
            self.lifecycle.calls,
            [
                ("add_label", 403, "symphony"),
                ("set_status", 403, "In Progress"),
                ("remove_label", 403, "symphony"),
            ],
        )

        self.lifecycle.calls.clear()
        self.snapshot["issues"]["403"]["labels"] = ["symphony"]
        with self.assertRaisesRegex(RuntimeError, "status unavailable"):
            self.actions.execute("lease", {"issue": 403})
        self.assertEqual(self.lifecycle.calls, [("set_status", 403, "In Progress")])

    def test_lease_rejects_an_owner_gated_issue_even_if_project_status_is_ready(self):
        self.snapshot["issues"]["403"]["labels"] = ["ждёт-владельца"]

        with self.assertRaisesRegex(ActionError, "owner input"):
            self.actions.execute("lease", {"issue": 403})

        self.assertEqual(self.lifecycle.calls, [])

    def test_lease_rejects_persisted_or_labeled_system_quarantine(self):
        self.store.set_quarantine(403, "workspace hook failed", "2026-08-25T10:00:00Z")

        with self.assertRaisesRegex(ActionError, "system quarantine"):
            self.actions.execute("lease", {"issue": 403})
        self.assertEqual(self.lifecycle.calls, [])

        self.store.clear_quarantine(403)
        self.snapshot["issues"]["403"]["labels"] = ["symphony:quarantined"]
        with self.assertRaisesRegex(ActionError, "system quarantine"):
            self.actions.execute("lease", {"issue": 403})
        self.assertEqual(self.lifecycle.calls, [])

    def test_run_clears_system_quarantine_before_restoring_ready_for_ai_lease(self):
        self.store.set_quarantine(403, "workspace hook failed", "2026-08-25T10:00:00Z")
        self.snapshot["issues"]["403"]["labels"] = ["symphony:quarantined"]

        result = self.actions.execute("run", {"issue": 403})

        self.assertEqual(result["status"], "accepted")
        self.assertIsNone(self.store.quarantine_for(403))
        self.assertEqual(
            self.lifecycle.calls,
            [
                ("remove_label", 403, "symphony:quarantined"),
                ("set_status", 403, "Ready for AI"),
                ("add_label", 403, "symphony"),
            ],
        )

    def test_run_keeps_persisted_quarantine_if_fixed_label_removal_fails(self):
        self.store.set_quarantine(403, "workspace hook failed", "2026-08-25T10:00:00Z")
        self.snapshot["issues"]["403"]["labels"] = ["symphony:quarantined"]
        self.lifecycle.fail_on = "remove_label"

        with self.assertRaisesRegex(RuntimeError, "label rollback unavailable"):
            self.actions.execute("run", {"issue": 403})

        self.assertEqual(
            self.lifecycle.calls,
            [("remove_label", 403, "symphony:quarantined")],
        )
        self.assertEqual(
            self.store.quarantine_for(403)["reason"],
            "workspace hook failed",
        )

    def test_run_recovers_persisted_quarantine_stuck_in_progress_with_existing_lease(self):
        self.store.set_quarantine(403, "workspace hook failed", "2026-08-25T10:00:00Z")
        self.snapshot["issues"]["403"].update(
            {"status": "In Progress", "labels": ["symphony", "symphony:quarantined"]}
        )

        result = self.actions.execute("run", {"issue": 403})

        self.assertEqual(result["status"], "accepted")
        self.assertIsNone(self.store.quarantine_for(403))
        self.assertEqual(
            self.lifecycle.calls,
            [
                ("remove_label", 403, "symphony:quarantined"),
                ("set_status", 403, "Ready for AI"),
            ],
        )

    def test_run_recovers_label_only_quarantine_stuck_in_progress(self):
        self.snapshot["issues"]["403"].update(
            {"status": "In Progress", "labels": ["symphony:quarantined"]}
        )

        result = self.actions.execute("run", {"issue": 403})

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(
            self.lifecycle.calls,
            [
                ("remove_label", 403, "symphony:quarantined"),
                ("set_status", 403, "Ready for AI"),
                ("add_label", 403, "symphony"),
            ],
        )

    def test_run_rejects_non_quarantined_in_progress_issue(self):
        self.snapshot["issues"]["403"].update({"status": "In Progress", "labels": []})

        with self.assertRaisesRegex(ActionError, "Backlog or Ready for AI"):
            self.actions.execute("run", {"issue": 403})
        self.assertEqual(self.lifecycle.calls, [])

    def test_owner_run_intentionally_clears_quarantine_after_marker_removal_even_if_lease_setup_fails(self):
        self.store.set_quarantine(403, "workspace hook failed", "2026-08-25T10:00:00Z")
        self.snapshot["issues"]["403"]["labels"] = ["symphony:quarantined"]
        self.lifecycle.fail_on = "set_status"

        with self.assertRaisesRegex(RuntimeError, "status unavailable"):
            self.actions.execute("run", {"issue": 403})

        self.assertIsNone(self.store.quarantine_for(403))
        self.assertEqual(
            self.lifecycle.calls,
            [
                ("remove_label", 403, "symphony:quarantined"),
                ("set_status", 403, "Ready for AI"),
            ],
        )

    def test_lease_uses_cached_snapshot_while_owner_actions_require_fresh_state(self):
        snapshot_calls = []

        def fresh_snapshot():
            snapshot_calls.append("fresh")
            return self.snapshot

        actions = ActionService(
            snapshot_provider=lambda: snapshot_calls.append("cached") or self.snapshot,
            fresh_snapshot_provider=fresh_snapshot,
            lifecycle=self.lifecycle,
            supervisor=self.supervisor,
            state_store=self.store,
        )

        actions.execute("lease", {"issue": 403})
        self.assertEqual(snapshot_calls, ["cached"])

        actions.execute("resume", {})
        self.assertEqual(snapshot_calls, ["cached", "fresh"])

    def test_rework_requires_reason_and_comments_before_requeue(self):
        with self.assertRaisesRegex(ActionError, "reason is required"):
            self.actions.execute("rework", {"issue": 402, "reason": "  "})

        self.actions.execute("rework", {"issue": 402, "reason": "Keep legacy IDs"})

        self.assertEqual(
            self.lifecycle.calls,
            [
                ("comment", 402, "Owner requested rework: Keep legacy IDs"),
                ("set_status", 402, "Ready for AI"),
                ("add_label", 402, "symphony"),
            ],
        )

    def test_internal_complete_run_moves_only_an_unleased_in_progress_issue_to_acceptance(self):
        result = self.actions.execute_internal("complete_run", {"issue": 404})

        self.assertEqual(
            result,
            {"status": "accepted", "action": "complete_run", "issue": 404},
        )
        self.assertEqual(
            self.lifecycle.calls,
            [("set_status", 404, "Ready for Acceptance")],
        )

    def test_internal_complete_run_is_idempotent_for_already_non_active_issue_states(self):
        cases = [
            ("ready", {"status": "Ready for AI"}),
            ("closed", {"state": "CLOSED"}),
        ]
        for name, update in cases:
            with self.subTest(name=name):
                self.snapshot["issues"]["404"] = {
                    "number": 404,
                    "status": "In Progress",
                    "state": "OPEN",
                    "labels": [],
                    **update,
                }
                self.lifecycle.calls.clear()
                result = self.actions.execute_internal("complete_run", {"issue": 404})
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(self.lifecycle.calls, [])

        self.snapshot["issues"]["404"] = {
            "number": 404,
            "status": "In Progress",
            "state": "UNKNOWN",
            "labels": [],
        }
        with self.assertRaisesRegex(RetryableActionError, "issue state"):
            self.actions.execute_internal("complete_run", {"issue": 404})

        self.snapshot["issues"]["404"] = {
            "number": 404,
            "status": "In Progress",
            "state": "OPEN",
            "labels": [],
        }
        self.snapshot["issues"]["404"]["labels"] = ["Symphony"]
        with self.assertRaisesRegex(RetryableActionError, "lease"):
            self.actions.execute_internal("complete_run", {"issue": 404})
        self.snapshot["issues"]["404"]["labels"] = []

        for lane in ("running", "retrying", "blocked"):
            with self.subTest(lane=lane):
                self.snapshot[lane] = [{"issue_id": "404"}]
                self.lifecycle.calls.clear()
                with self.assertRaisesRegex(RetryableActionError, "runtime"):
                    self.actions.execute_internal("complete_run", {"issue": 404})
                self.assertEqual(self.lifecycle.calls, [])
                self.snapshot[lane] = []

        self.snapshot["running"] = [{}]
        with self.assertRaisesRegex(RetryableActionError, "runtime"):
            self.actions.execute_internal("complete_run", {"issue": 404})
        self.snapshot["running"] = []

        self.snapshot["running"] = [{"issue_id": "499"}]
        self.actions.execute_internal("complete_run", {"issue": 404})
        self.assertEqual(
            self.lifecycle.calls,
            [("set_status", 404, "Ready for Acceptance")],
        )

    def test_internal_complete_run_marks_unfresh_control_state_retryable(self):
        self.snapshot["sources"]["runtime"] = {"status": "unavailable"}

        with self.assertRaisesRegex(RetryableActionError, "fresh runtime"):
            self.actions.execute_internal("complete_run", {"issue": 404})

    def test_internal_quarantine_persists_across_owner_control_restart_before_releasing_lease(self):
        result = self.actions.execute_internal(
            "quarantine_before_run",
            {"issue": 405, "reason": "\x1b[31mbranch\n mismatch\x00" + "x" * 1_000},
        )

        self.assertEqual(
            result,
            {"status": "accepted", "action": "quarantine_before_run", "issue": 405},
        )
        self.assertEqual(self.lifecycle.calls[0], ("remove_label", 405, "symphony"))
        self.assertEqual(self.lifecycle.calls[1], ("add_label", 405, "symphony:quarantined"))
        self.assertEqual(self.lifecycle.calls[2], ("set_status", 405, "Ready for AI"))
        self.assertEqual(self.lifecycle.calls[3][0:2], ("comment", 405))
        comment = self.lifecycle.calls[3][2]
        self.assertNotRegex(comment, r"[\x00-\x1f\x7f-\x9f]")
        self.assertLessEqual(len(comment), 512)
        persisted = StateStore(self.state_path).quarantine_for(405)
        self.assertEqual(persisted["issue"], 405)
        self.assertTrue(persisted["reason"].startswith("branch mismatch"))
        self.assertLessEqual(len(persisted["reason"].encode("utf-8")), 512)
        self.assertNotRegex(persisted["reason"], r"[\x00-\x1f\x7f-\x9f]")
        self.assertIn("quarantined_at", persisted)

    def test_accept_requires_ready_state_and_synced_test(self):
        self.actions.execute("accept", {"issue": 402})
        self.assertEqual(
            self.lifecycle.calls,
            [("set_status", 402, "Done"), ("close_issue", 402)],
        )

        self.snapshot["test"]["synced"] = False
        self.actions.execute("accept", {"issue": 402})

        with self.assertRaisesRegex(ActionError, "Ready for Acceptance"):
            self.actions.execute("accept", {"issue": 401})

    def test_accept_rejects_issue_specific_test_drift(self):
        self.snapshot["issues"]["402"]["test"] = {
            "sha": "outdated",
            "merge_sha": "merged",
            "contains_merge": False,
        }

        with self.assertRaisesRegex(ActionError, "does not contain the Issue merge"):
            self.actions.execute("accept", {"issue": 402})

        self.assertEqual(self.lifecycle.calls, [])

    def test_accept_can_finish_closing_an_issue_after_a_partial_previous_accept(self):
        self.snapshot["issues"]["402"].update({"status": "Done", "state": "OPEN"})

        self.actions.execute("accept", {"issue": 402})

        self.assertEqual(self.lifecycle.calls, [("close_issue", 402)])

    def test_duplicate_owner_action_returns_the_first_result_without_repeating_mutations(self):
        first = self.actions.execute("accept", {"issue": 402})
        second = self.actions.execute("accept", {"issue": 402})

        self.assertEqual(second, first)
        self.assertEqual(
            self.lifecycle.calls,
            [("set_status", 402, "Done"), ("close_issue", 402)],
        )

    def test_rework_resumes_after_restart_without_duplicate_owner_comment(self):
        params = {"issue": 402, "reason": "Keep legacy IDs"}
        self.lifecycle.fail_on = "comment_response"

        with self.assertRaisesRegex(RuntimeError, "comment response lost"):
            self.actions.execute("rework", params)

        self.lifecycle.fail_on = None
        restarted = ActionService(
            snapshot_provider=lambda: self.snapshot,
            lifecycle=self.lifecycle,
            supervisor=self.supervisor,
            state_store=StateStore(self.state_path),
        )
        result = restarted.execute("rework", params)

        self.assertEqual(result["status"], "accepted")
        comments = [call for call in self.lifecycle.calls if call[0] == "comment"]
        self.assertEqual(
            comments,
            [("comment", 402, "Owner requested rework: Keep legacy IDs")],
        )

        self.lifecycle.calls.clear()
        duplicate = ActionService(
            snapshot_provider=lambda: self.snapshot,
            lifecycle=self.lifecycle,
            supervisor=self.supervisor,
            state_store=StateStore(self.state_path),
        ).execute("rework", params)
        self.assertEqual(duplicate, result)
        self.assertEqual(self.lifecycle.calls, [])

    def test_pause_resume_and_restart_use_control_state_and_fixed_supervisor(self):
        self.assertTrue(self.actions.execute("pause", {})["intake"]["active"] is False)
        self.assertFalse(self.store.intake_active())
        self.assertTrue(self.actions.execute("resume", {})["intake"]["active"])
        self.assertTrue(self.store.intake_active())

        self.actions.execute("restart", {})
        self.assertEqual(self.supervisor.restart_count, 1)
        self.assertGreater(self.store.read()["expected_service_restart_until"], 0)

    def test_owner_can_set_a_bounded_worker_limit(self):
        result = self.actions.execute("set_workers", {"limit": 8})

        self.assertEqual(result["workers"], {"limit": 8, "maximum": 12})
        self.assertEqual(self.store.worker_limit(12), 8)

        with self.assertRaisesRegex(ActionError, "between 1 and 12"):
            self.actions.execute("set_workers", {"limit": 13})

    def test_resume_refuses_red_canonical_ci(self):
        self.store.set_intake_active(False)
        self.snapshot["systemic_gate"] = {
            "blocked": True,
            "reason": "canonical CI is failing",
        }

        with self.assertRaisesRegex(ActionError, "canonical CI is failing"):
            self.actions.execute("resume", {})

        self.assertFalse(self.store.intake_active())

    def test_restart_marks_the_fixed_action_in_progress_until_cache_invalidation(self):
        self.supervisor.release_restart.clear()
        invalidation_states = []
        actions = ActionService(
            snapshot_provider=lambda: self.snapshot,
            lifecycle=self.lifecycle,
            supervisor=self.supervisor,
            state_store=self.store,
            after_action=lambda: invalidation_states.append(
                self.store.read().get("service_action_in_progress")
            ),
        )
        completed = []
        thread = threading.Thread(
            target=lambda: completed.append(actions.execute("restart", {}))
        )
        thread.start()
        self.assertTrue(self.supervisor.restart_started.wait(timeout=1))
        self.assertEqual(self.store.read()["service_action_in_progress"], "restart")
        self.assertGreater(self.store.read()["service_action_in_progress_until"], 0)

        self.supervisor.release_restart.set()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(completed[0]["status"], "accepted")
        self.assertEqual(invalidation_states, ["restart"])
        self.assertIsNone(self.store.read()["service_action_in_progress"])
        self.assertEqual(self.store.read()["service_action_in_progress_until"], 0)

    def test_pause_remains_available_but_resume_requires_fresh_runtime_and_github(self):
        self.snapshot["sources"]["runtime"] = {"status": "stale"}
        self.actions.execute("pause", {})

        with self.assertRaisesRegex(ActionError, "fresh runtime"):
            self.actions.execute("resume", {})

        self.snapshot["sources"]["runtime"] = {"status": "fresh"}
        self.snapshot["sources"]["github"] = {"status": "stale"}
        with self.assertRaisesRegex(ActionError, "fresh github"):
            self.actions.execute("resume", {})

        self.assertFalse(self.store.intake_active())

    def test_start_service_uses_the_fixed_supervisor(self):
        result = self.actions.execute("start_service", {})

        self.assertEqual(result, {"status": "accepted", "service": {"accepted": True}})
        self.assertEqual(self.supervisor.start_count, 1)

    def test_expected_stop_is_persisted_and_cleared_by_start(self):
        self.supervisor.release_stop.set()
        self.actions.execute("stop_service", {})
        self.assertTrue(self.store.read()["expected_service_stop"])

        self.actions.execute("start_service", {})
        self.assertFalse(self.store.read()["expected_service_stop"])

    def test_stop_service_requires_matching_running_worker_confirmation(self):
        self.snapshot["workers"] = {"running": 2}

        with self.assertRaisesRegex(ActionError, "confirm_running_workers"):
            self.actions.execute("stop_service", {})
        with self.assertRaisesRegex(ActionError, "confirm_running_workers"):
            self.actions.execute("stop_service", {"confirm_running_workers": 1})
        self.actions.execute("stop_service", {"confirm_running_workers": 2})

        self.assertEqual(self.supervisor.stop_count, 1)

    def test_stop_service_rejects_boolean_confirmation_for_one_worker(self):
        self.snapshot["workers"] = {"running": 1}

        with self.assertRaisesRegex(ActionError, "confirm_running_workers"):
            self.actions.execute("stop_service", {"confirm_running_workers": True})

    def test_mutating_actions_fail_closed_when_required_sources_are_stale(self):
        self.snapshot["sources"]["github"] = {"status": "stale"}
        with self.assertRaisesRegex(ActionError, "fresh github"):
            self.actions.execute("run", {"issue": 401})
        with self.assertRaisesRegex(ActionError, "fresh github"):
            self.actions.execute("lease", {"issue": 403})
        with self.assertRaisesRegex(ActionError, "fresh github"):
            self.actions.execute("rework", {"issue": 402, "reason": "retry"})

        self.snapshot["sources"]["github"] = {"status": "fresh"}
        self.snapshot["sources"]["test"] = {"status": "stale"}
        with self.assertRaisesRegex(ActionError, "fresh test"):
            self.actions.execute("accept", {"issue": 402})

        self.snapshot["sources"]["runtime"] = {"status": "stale"}
        with self.assertRaisesRegex(ActionError, "fresh runtime"):
            self.actions.execute("stop_service", {})

        self.snapshot["sources"]["supervisor"] = {"status": "unavailable"}
        with self.assertRaisesRegex(ActionError, "fresh supervisor"):
            self.actions.execute("start_service", {})

        self.assertEqual(self.lifecycle.calls, [])
        self.assertEqual(self.supervisor.start_count, 0)
        self.assertEqual(self.supervisor.stop_count, 0)

    def test_action_in_progress_is_rejected_instead_of_queued(self):
        completed = []
        thread = threading.Thread(
            target=lambda: completed.append(
                self.actions.execute("stop_service", {"confirm_running_workers": 0})
            )
        )
        thread.start()
        self.assertTrue(self.supervisor.stop_started.wait(timeout=1))

        with self.assertRaisesRegex(ActionError, "already in progress"):
            self.actions.execute("pause", {})

        self.supervisor.release_stop.set()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(completed[0]["status"], "accepted")

    def test_internal_completion_waits_for_a_short_owner_action(self):
        owner_completed = []
        owner_thread = threading.Thread(
            target=lambda: owner_completed.append(
                self.actions.execute("stop_service", {"confirm_running_workers": 0})
            )
        )
        owner_thread.start()
        self.assertTrue(self.supervisor.stop_started.wait(timeout=1))

        internal_completed = []
        internal_errors = []

        def complete_run():
            try:
                internal_completed.append(
                    self.actions.execute_internal("complete_run", {"issue": 404})
                )
            except Exception as error:
                internal_errors.append(error)

        internal_thread = threading.Thread(target=complete_run)
        internal_thread.start()
        internal_thread.join(timeout=0.05)
        self.assertTrue(internal_thread.is_alive())

        self.supervisor.release_stop.set()
        owner_thread.join(timeout=1)
        internal_thread.join(timeout=1)

        self.assertFalse(owner_thread.is_alive())
        self.assertFalse(internal_thread.is_alive())
        self.assertEqual(internal_errors, [])
        self.assertEqual(internal_completed[0]["status"], "accepted")

    def test_owner_action_waits_for_an_internal_completion_snapshot(self):
        internal_started = threading.Event()
        release_internal = threading.Event()

        def slow_fresh_snapshot():
            internal_started.set()
            self.assertTrue(release_internal.wait(timeout=1))
            return self.snapshot

        actions = ActionService(
            snapshot_provider=lambda: self.snapshot,
            fresh_snapshot_provider=slow_fresh_snapshot,
            lifecycle=self.lifecycle,
            supervisor=self.supervisor,
            state_store=self.store,
        )
        internal_thread = threading.Thread(
            target=lambda: actions.execute_internal("complete_run", {"issue": 404})
        )
        internal_thread.start()
        self.assertTrue(internal_started.wait(timeout=1))

        owner_results = []
        owner_thread = threading.Thread(
            target=lambda: owner_results.append(actions.execute("pause", {}))
        )
        owner_thread.start()
        owner_thread.join(timeout=0.05)
        self.assertTrue(owner_thread.is_alive())

        release_internal.set()
        internal_thread.join(timeout=1)
        owner_thread.join(timeout=1)

        self.assertFalse(internal_thread.is_alive())
        self.assertFalse(owner_thread.is_alive())
        self.assertEqual(owner_results[0]["intake"], {"active": False})

    def test_rejects_unknown_action(self):
        with self.assertRaisesRegex(ActionError, "unsupported action"):
            self.actions.execute("shell", {"command": "whoami"})

    def test_successful_action_invalidates_the_shared_snapshot_cache(self):
        invalidations = []
        actions = ActionService(
            snapshot_provider=lambda: self.snapshot,
            lifecycle=self.lifecycle,
            supervisor=self.supervisor,
            state_store=self.store,
            after_action=lambda: invalidations.append("invalidated"),
        )

        actions.execute("pause", {})

        self.assertEqual(invalidations, ["invalidated"])


if __name__ == "__main__":
    unittest.main()
