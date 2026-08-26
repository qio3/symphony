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
        "models": {
            "luna": {"active": 0, "completed": 4},
            "terra": {"active": 1, "completed": 2},
            "sol": {"active": 0, "completed": 1},
        },
        "counts": {
            "backlog": 74,
            "ready_for_ai": 2,
            "running": 1,
            "queued": 0,
            "blocked": 1,
            "quarantined": 0,
            "ready_for_acceptance": 3,
            "done": 10,
        },
        "canonical": {"sha": "be44cf15aaaaaaaa"},
        "test": {"sha": "be44cf15aaaaaaaa", "synced": True, "url": "https://test.example"},
        "quota": {
            "five_hour": {"used_percent": 18, "window_duration_mins": 300, "resets_at": 1787500000},
            "weekly": {"used_percent": 42, "window_duration_mins": 10080, "resets_at": 1787558400},
        },
        "owner_view": {
            "work_items": [
                {
                    "number": 401,
                    "title": "Work",
                    "stage": "In Progress",
                    "model": {"selected_tier": "terra", "actual_model": "gpt-5.6-terra"},
                }
            ],
            "ready_for_acceptance": [{"number": 402, "title": "Ready"}],
            "blocked": [{"number": 403, "title": "Blocked", "question": "Choose A or B?"}],
            "system_quarantines": [],
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
        self.assertIn("Models: Luna 0 · Terra 1 · Sol 0", text)
        self.assertIn("Weekly: 42% used", text)
        self.assertNotIn("Quarantined:", text)
        self.assertNotIn("tokens", text.casefold())

    def test_status_and_blocked_keep_system_quarantine_separate_from_owner_blockers(self):
        current = snapshot()
        current["counts"]["quarantined"] = 1
        current["owner_view"]["system_quarantines"] = [
            {
                "number": 405,
                "title": "Quarantined work",
                "reason": "workspace before_run hook failed",
                "issue_url": "https://github.test/issues/405",
            }
        ]
        handler = TelegramCommandHandler(
            snapshot_provider=lambda: current,
            action_service=self.actions,
            logs_provider=lambda _tail: [],
            ai=self.ai,
        )

        self.assertIn("Quarantined: 1", handler.handle("/status"))
        blocked = handler.handle("/blocked")
        self.assertIn("Blocked: 1", blocked)
        self.assertIn("System quarantine: 1", blocked)
        self.assertIn("#405 Quarantined work", blocked)
        self.assertIn("Reason: workspace before_run hook failed", blocked)

    def test_status_does_not_report_down_when_supervisor_state_is_unknown(self):
        unknown = snapshot()
        unknown["service"] = {"live": False, "status": "unknown"}
        handler = TelegramCommandHandler(
            snapshot_provider=lambda: unknown,
            action_service=self.actions,
            logs_provider=lambda _tail: [],
            ai=self.ai,
        )

        text = handler.handle("/status")

        self.assertIn("Symphony ◌ Unknown", text)
        self.assertNotIn("Symphony ○ Down", text)

    def test_leading_bot_mention_still_routes_slash_command(self):
        text = self.handler.handle("@Contentzavod_PM_bot /status")

        self.assertIn("Symphony ● Live", text)
        self.assertEqual(self.ai.questions, [])
        self.assertEqual(self.actions.calls, [])

    def test_work_includes_the_actual_routed_model(self):
        text = self.handler.handle("/work")

        self.assertIn("#401 Work · Terra", text)

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

    def test_service_commands_use_the_same_typed_actions(self):
        self.assertIn("accepted", self.handler.handle("/start_service"))
        self.assertIn("accepted", self.handler.handle("/stop_service 1"))

        self.assertEqual(
            self.actions.calls,
            [
                ("start_service", {}),
                ("stop_service", {"confirm_running_workers": 1}),
            ],
        )

    def test_stop_service_rejects_invalid_confirmation_syntax(self):
        text = self.handler.handle("/stop_service yes")

        self.assertEqual(text, "Usage: /stop_service [running workers count]")
        self.assertEqual(self.actions.calls, [])

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
        current["owner_view"]["system_quarantines"] = [
            {
                "number": 407,
                "title": "Existing quarantine",
                "reason": "already known before startup",
                "issue_url": "https://github.test/issues/407",
            }
        ]

        events = NotificationDetector().detect(None, current)

        self.assertEqual(
            [event["kind"] for event in events],
            ["service_stopped", "systemic_failure"],
        )

    def test_system_quarantine_notifies_on_new_reason_but_not_repeated_snapshot(self):
        previous = snapshot()
        current = snapshot()
        current["owner_view"]["system_quarantines"] = [
            {
                "number": 407,
                "title": "Quarantined work",
                "reason": "workspace before_run hook failed",
                "issue_url": "https://github.test/issues/407",
            }
        ]

        events = NotificationDetector().detect(previous, current)

        self.assertEqual([event["kind"] for event in events], ["system_quarantine"])
        self.assertEqual(
            events[0]["fingerprint"],
            "quarantine:407:workspace before_run hook failed",
        )
        self.assertIn("System quarantine #407: Quarantined work", events[0]["text"])
        self.assertIn("Reason: workspace before_run hook failed", events[0]["text"])
        self.assertIn("https://github.test/issues/407", events[0]["text"])
        self.assertEqual(NotificationDetector().detect(current, current), [])

        changed = snapshot()
        changed["owner_view"]["system_quarantines"] = [
            {
                **current["owner_view"]["system_quarantines"][0],
                "reason": "workspace before_run hook failed again",
            }
        ]
        changed_events = NotificationDetector().detect(current, changed)
        self.assertEqual([event["kind"] for event in changed_events], ["system_quarantine"])
        self.assertIn("failed again", changed_events[0]["text"])

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

    def test_changed_owner_question_is_a_new_attention_event(self):
        previous = snapshot()
        current = snapshot()
        current["owner_view"]["blocked"][0]["question"] = "Choose C or D?"

        events = NotificationDetector().detect(previous, current)

        self.assertEqual([event["kind"] for event in events], ["blocked"])
        self.assertIn("Choose C or D?", events[0]["text"])

    def test_same_ready_issue_does_not_notify_again_when_global_test_sha_changes(self):
        previous = snapshot()
        previous["owner_view"]["ready_for_acceptance"][0]["test"] = {
            "sha": "oldsha11",
            "url": "https://test.example",
        }
        current = snapshot()
        current["owner_view"]["ready_for_acceptance"][0]["test"] = {
            "sha": "newsha22",
            "url": "https://test.example",
        }

        events = NotificationDetector().detect(previous, current)

        self.assertEqual(events, [])

    def test_detects_unexpected_service_stop_once_as_transition(self):
        previous = snapshot()
        current = snapshot()
        current["service"] = {"live": False, "reason": "container exited"}

        events = NotificationDetector().detect(previous, current)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "service_stopped")

    def test_supervisor_unknown_does_not_claim_the_service_stopped(self):
        previous = snapshot()
        current = snapshot()
        current["service"] = {
            "live": False,
            "status": "unknown",
            "reason": "docker socket temporarily unavailable",
        }

        events = NotificationDetector().detect(previous, current)

        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
