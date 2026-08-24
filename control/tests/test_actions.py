import tempfile
import threading
import unittest
from pathlib import Path

from owner_control.actions import ActionError, ActionService
from owner_control.state_store import StateStore


class FakeLifecycle:
    def __init__(self):
        self.calls = []

    def set_status(self, issue, status):
        self.calls.append(("set_status", issue, status))

    def add_label(self, issue, label):
        self.calls.append(("add_label", issue, label))

    def comment(self, issue, body):
        self.calls.append(("comment", issue, body))

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
        self.store = StateStore(Path(self.tempdir.name) / "state.json")
        self.lifecycle = FakeLifecycle()
        self.supervisor = FakeSupervisor()
        self.snapshot = {
            "test": {"synced": True},
            "sources": {
                "supervisor": {"status": "fresh"},
                "runtime": {"status": "fresh"},
                "github": {"status": "fresh"},
                "test": {"status": "fresh"},
            },
            "issues": {
                "401": {"number": 401, "status": "Backlog", "state": "OPEN", "labels": []},
                "402": {"number": 402, "status": "Ready for Acceptance", "state": "OPEN", "labels": []},
                "403": {"number": 403, "status": "Ready for AI", "state": "OPEN", "labels": []},
            },
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
        self.assertEqual(self.lifecycle.calls, [("add_label", 403, "symphony")])

        self.lifecycle.calls.clear()
        self.snapshot["issues"]["403"]["labels"] = ["Symphony"]
        self.actions.execute("lease", {"issue": 403})
        self.assertEqual(self.lifecycle.calls, [])

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

    def test_accept_requires_ready_state_and_synced_test(self):
        self.actions.execute("accept", {"issue": 402})
        self.assertEqual(
            self.lifecycle.calls,
            [("set_status", 402, "Done"), ("close_issue", 402)],
        )

        self.snapshot["test"]["synced"] = False
        with self.assertRaisesRegex(ActionError, "TEST is not synced"):
            self.actions.execute("accept", {"issue": 402})

        with self.assertRaisesRegex(ActionError, "Ready for Acceptance"):
            self.actions.execute("accept", {"issue": 401})

    def test_accept_rejects_issue_specific_test_drift(self):
        self.snapshot["issues"]["402"]["test"] = {
            "sha": "outdated",
            "synced": False,
        }

        with self.assertRaisesRegex(ActionError, "Issue TEST is not synced"):
            self.actions.execute("accept", {"issue": 402})

        self.assertEqual(self.lifecycle.calls, [])

    def test_accept_can_finish_closing_an_issue_after_a_partial_previous_accept(self):
        self.snapshot["issues"]["402"].update({"status": "Done", "state": "OPEN"})

        self.actions.execute("accept", {"issue": 402})

        self.assertEqual(self.lifecycle.calls, [("close_issue", 402)])

    def test_pause_resume_and_restart_use_control_state_and_fixed_supervisor(self):
        self.assertTrue(self.actions.execute("pause", {})["intake"]["active"] is False)
        self.assertFalse(self.store.intake_active())
        self.assertTrue(self.actions.execute("resume", {})["intake"]["active"])
        self.assertTrue(self.store.intake_active())

        self.actions.execute("restart", {})
        self.assertEqual(self.supervisor.restart_count, 1)
        self.assertGreater(self.store.read()["expected_service_restart_until"], 0)

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
