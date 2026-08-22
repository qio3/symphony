import tempfile
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

    def restart(self):
        self.restart_count += 1
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
            "issues": {
                "401": {"number": 401, "status": "Backlog", "state": "OPEN", "labels": []},
                "402": {"number": 402, "status": "Ready for Acceptance", "state": "OPEN", "labels": []},
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
