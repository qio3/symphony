import unittest

from owner_control.telegram import NotificationDetector, TelegramCommandHandler, update_is_allowed


class FakeActions:
    def __init__(self):
        self.calls = []

    def execute(self, action, params):
        self.calls.append((action, params))
        return {"status": "accepted", "action": action, "issue": params.get("issue")}


class FakeAI:
    def __init__(self):
        self.questions = []

    def answer(self, question, snapshot):
        self.questions.append((question, snapshot))
        return "Only #401 is running."


def snapshot():
    return {
        "service": {"live": True},
        "intake": {"active": True},
        "workers": {"running": 1, "limit": 2},
        "counts": {
            "backlog": 74,
            "ready_for_ai": 2,
            "running": 1,
            "queued": 0,
            "blocked": 1,
            "ready_for_acceptance": 3,
            "done": 10,
        },
        "canonical": {"sha": "be44cf15aaaaaaaa"},
        "test": {"sha": "be44cf15aaaaaaaa", "synced": True, "url": "https://test.example"},
        "owner_view": {
            "work_items": [{"number": 401, "title": "Work", "stage": "In Progress"}],
            "ready_for_acceptance": [{"number": 402, "title": "Ready"}],
            "blocked": [{"number": 403, "title": "Blocked", "question": "Choose A or B?"}],
            "backlog": [{"number": 404, "title": "Next", "stage": "Backlog"}],
        },
    }


class TelegramCommandHandlerTest(unittest.TestCase):
    def setUp(self):
        self.actions = FakeActions()
        self.ai = FakeAI()
        self.handler = TelegramCommandHandler(
            snapshot_provider=snapshot,
            action_service=self.actions,
            logs_provider=lambda tail: ["one", "two"],
            ai=self.ai,
        )

    def test_allowlist_requires_both_owner_chat_and_owner_user(self):
        allowed = {"message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "/status"}}
        wrong_user = {"message": {"chat": {"id": 10}, "from": {"id": 21}, "text": "/status"}}
        wrong_chat = {"message": {"chat": {"id": 11}, "from": {"id": 20}, "text": "/status"}}

        self.assertTrue(update_is_allowed(allowed, owner_chat_id=10, owner_user_id=20))
        self.assertFalse(update_is_allowed(wrong_user, owner_chat_id=10, owner_user_id=20))
        self.assertFalse(update_is_allowed(wrong_chat, owner_chat_id=10, owner_user_id=20))

    def test_status_is_short_and_owner_oriented(self):
        text = self.handler.handle("/status")

        self.assertIn("Symphony ● Live", text)
        self.assertIn("Workers: 1/2", text)
        self.assertIn("Backlog: 74", text)
        self.assertIn("Ready for AI: 2", text)
        self.assertIn("TEST: be44cf15 ✓", text)
        self.assertNotIn("tokens", text.casefold())

    def test_commands_call_shared_typed_actions(self):
        self.assertIn("accepted", self.handler.handle("/run #401"))
        self.assertIn("accepted", self.handler.handle("/rework 402 Keep legacy IDs"))
        self.handler.handle("/pause")
        self.handler.handle("/restart")

        self.assertEqual(
            self.actions.calls,
            [
                ("run", {"issue": 401}),
                ("rework", {"issue": 402, "reason": "Keep legacy IDs"}),
                ("pause", {}),
                ("restart", {}),
            ],
        )

    def test_plain_text_is_read_only_ai_question(self):
        answer = self.handler.handle("что сейчас происходит?")

        self.assertEqual(answer, "Only #401 is running.")
        self.assertEqual(self.ai.questions, [("что сейчас происходит?", snapshot())])
        self.assertEqual(self.actions.calls, [])


class NotificationDetectorTest(unittest.TestCase):
    def test_first_snapshot_reports_only_service_down_and_systemic_failure(self):
        current = snapshot()
        current["service"] = {"live": False, "reason": "container unavailable"}
        current["failures"] = [
            {
                "fingerprint": "github:RuntimeError:auth",
                "message": "github snapshot unavailable: HTTP 401",
                "unrecoverable": True,
            }
        ]

        events = NotificationDetector().detect(None, current)

        self.assertEqual(
            [event["kind"] for event in events],
            ["service_stopped", "systemic_failure"],
        )

    def test_emits_only_attention_transitions(self):
        previous = snapshot()
        current = snapshot()
        current["owner_view"] = {**current["owner_view"]}
        current["owner_view"]["blocked"] = [
            *current["owner_view"]["blocked"],
            {"number": 405, "title": "New blocker", "question": "Which option?", "issue_url": "https://github.test/issues/405"},
        ]
        current["owner_view"]["ready_for_acceptance"] = [
            *current["owner_view"]["ready_for_acceptance"],
            {
                "number": 406,
                "title": "New ready",
                "issue_url": "https://github.test/issues/406",
                "pr": {"number": 99, "url": "https://github.test/pull/99"},
                "test": {"sha": "be44cf15aaaaaaaa", "url": "https://test.example"},
            },
        ]
        current["running"] = [{"issue_id": "401", "last_message": "ordinary internal turn"}]

        events = NotificationDetector().detect(previous, current)

        self.assertEqual([event["kind"] for event in events], ["blocked", "ready_for_acceptance"])
        self.assertIn("Which option?", events[0]["text"])
        self.assertIn("TEST: https://test.example", events[1]["text"])

    def test_changed_owner_question_or_ready_deploy_is_a_new_attention_event(self):
        previous = snapshot()
        previous["owner_view"]["ready_for_acceptance"][0]["test"] = {
            "sha": "oldsha11",
            "url": "https://test.example",
        }
        current = snapshot()
        current["owner_view"]["blocked"][0]["question"] = "Choose C or D?"
        current["owner_view"]["ready_for_acceptance"][0]["test"] = {
            "sha": "newsha22",
            "url": "https://test.example",
        }

        events = NotificationDetector().detect(previous, current)

        self.assertEqual(
            [event["kind"] for event in events],
            ["blocked", "ready_for_acceptance"],
        )
        self.assertIn("Choose C or D?", events[0]["text"])
        self.assertIn("newsha22", events[1]["text"])

    def test_detects_unexpected_service_stop_once_as_transition(self):
        previous = snapshot()
        current = snapshot()
        current["service"] = {"live": False, "reason": "container exited"}

        events = NotificationDetector().detect(previous, current)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "service_stopped")


if __name__ == "__main__":
    unittest.main()
