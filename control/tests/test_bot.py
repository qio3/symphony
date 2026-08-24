import tempfile
import time
import unittest
from pathlib import Path

from owner_control.bot import NotificationPublisher, TelegramBot
from owner_control.state_store import StateStore
from owner_control.telegram import NotificationDetector


class FakeTelegramApi:
    def __init__(self, updates=None):
        self.updates = updates or []
        self.offsets = []
        self.sent = []

    def get_updates(self, *, offset, timeout):
        self.offsets.append((offset, timeout))
        return self.updates

    def send(self, text):
        self.sent.append(text)


class FailingTelegramApi(FakeTelegramApi):
    def __init__(self, *, updates=None, fail_send_calls=None):
        super().__init__(updates)
        self.send_calls = 0
        self.fail_send_calls = set(fail_send_calls or [])

    def get_updates(self, *, offset, timeout):
        self.offsets.append((offset, timeout))
        return [update for update in self.updates if update["update_id"] >= offset]

    def send(self, text):
        self.send_calls += 1
        if self.send_calls in self.fail_send_calls:
            raise RuntimeError("simulated Telegram failure")
        self.sent.append(text)


class FakeHandler:
    def __init__(self):
        self.texts = []

    def handle(self, text):
        self.texts.append(text)
        return f"reply:{text}"


def base_snapshot():
    return {
        "service": {"live": True},
        "owner_view": {"blocked": [], "ready_for_acceptance": []},
        "failures": [],
    }


class TelegramBotTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = StateStore(Path(self.tempdir.name) / "state.json")

    def test_processes_only_allowlisted_owner_and_advances_offset_for_all_updates(self):
        api = FakeTelegramApi(
            [
                {"update_id": 11, "message": {"chat": {"id": 10}, "from": {"id": 999}, "text": "/status"}},
                {"update_id": 12, "message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "/status"}},
            ]
        )
        handler = FakeHandler()
        bot = TelegramBot(
            api=api,
            handler=handler,
            state_store=self.store,
            owner_chat_id=10,
            owner_user_id=20,
        )

        bot.poll_once(timeout=0)

        self.assertEqual(handler.texts, ["/status"])
        self.assertEqual(api.sent, ["reply:/status"])
        self.assertEqual(self.store.read()["telegram_offset"], 13)

    def test_checkpoints_each_update_before_a_later_reply_failure(self):
        api = FailingTelegramApi(
            updates=[
                {"update_id": 11, "message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "/status"}},
                {"update_id": 12, "message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "/work"}},
            ],
            fail_send_calls={2},
        )
        handler = FakeHandler()
        bot = TelegramBot(
            api=api,
            handler=handler,
            state_store=self.store,
            owner_chat_id=10,
            owner_user_id=20,
        )

        with self.assertRaisesRegex(RuntimeError, "simulated Telegram failure"):
            bot.poll_once(timeout=0)
        bot.poll_once(timeout=0)

        self.assertEqual(handler.texts, ["/status", "/work"])
        self.assertEqual(api.sent, ["reply:/status"])
        self.assertEqual(self.store.read()["telegram_offset"], 13)


class NotificationPublisherTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = StateStore(Path(self.tempdir.name) / "state.json")
        self.api = FakeTelegramApi()
        self.publisher = NotificationPublisher(
            api=self.api,
            state_store=self.store,
            detector=NotificationDetector(),
        )

    def test_first_snapshot_seeds_state_and_new_event_is_sent_once(self):
        self.publisher.publish(base_snapshot())
        self.assertEqual(self.api.sent, [])

        changed = base_snapshot()
        changed["owner_view"] = {
            "blocked": [{"number": 401, "title": "Blocked", "question": "Choose A?"}],
            "ready_for_acceptance": [],
        }
        self.publisher.publish(changed)
        self.publisher.publish(changed)

        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("Choose A?", self.api.sent[0])

    def test_expected_restart_suppresses_transient_stop_but_not_a_lingering_outage(self):
        self.publisher.publish(base_snapshot())
        self.store.update({"expected_service_restart_until": time.time() + 60})
        down = base_snapshot()
        down["service"] = {"live": False, "reason": "restarting"}

        self.publisher.publish(down)
        self.assertEqual(self.api.sent, [])

        self.store.update({"expected_service_restart_until": time.time() - 1})
        self.publisher.publish(down)

        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("unexpectedly stopped", self.api.sent[0])

    def test_owner_requested_stop_never_emits_unexpected_stop_notification(self):
        self.publisher.publish(base_snapshot())
        self.store.update({"expected_service_stop": True})
        down = base_snapshot()
        down["service"] = {"live": False, "status": "exited", "reason": "owner stopped"}

        self.publisher.publish(down)
        self.publisher.publish(down)

        self.assertEqual(self.api.sent, [])

    def test_expected_stop_clears_after_service_is_observed_live_again(self):
        self.publisher.publish(base_snapshot())
        self.store.update({"expected_service_stop": True})

        self.publisher.publish(base_snapshot())
        self.assertFalse(self.store.read()["expected_service_stop"])

        down = base_snapshot()
        down["service"] = {"live": False, "status": "exited", "reason": "crash"}
        self.publisher.publish(down)

        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("unexpectedly stopped", self.api.sent[0])

    def test_transient_source_failure_does_not_turn_recovery_into_new_attention_events(self):
        healthy = base_snapshot()
        healthy["owner_view"]["ready_for_acceptance"] = [
            {"number": 401, "title": "Already ready", "test": {"sha": "abc12345"}}
        ]
        self.publisher.publish(healthy)

        transient = base_snapshot()
        transient["failures"] = [
            {
                "fingerprint": "github:RuntimeError:transient",
                "message": "github snapshot unavailable",
                "unrecoverable": False,
            }
        ]
        self.publisher.publish(transient)
        self.publisher.publish(healthy)

        self.assertEqual(self.api.sent, [])

    def test_initial_transient_source_failure_waits_for_a_healthy_attention_baseline(self):
        transient = base_snapshot()
        transient["failures"] = [
            {
                "fingerprint": "github:RuntimeError:transient",
                "message": "github snapshot unavailable",
                "unrecoverable": False,
            }
        ]
        healthy = base_snapshot()
        healthy["owner_view"]["ready_for_acceptance"] = [
            {"number": 401, "title": "Already ready", "test": {"sha": "abc12345"}}
        ]

        self.publisher.publish(transient)
        self.publisher.publish(healthy)

        self.assertEqual(self.api.sent, [])

    def test_checkpoints_successful_notification_before_a_later_send_failure(self):
        api = FailingTelegramApi(fail_send_calls={2})
        publisher = NotificationPublisher(
            api=api,
            state_store=self.store,
            detector=NotificationDetector(),
        )
        publisher.publish(base_snapshot())
        changed = base_snapshot()
        changed["owner_view"]["ready_for_acceptance"] = [
            {"number": 401, "title": "First", "test": {"sha": "abc12345"}},
            {"number": 402, "title": "Second", "test": {"sha": "abc12345"}},
        ]

        with self.assertRaisesRegex(RuntimeError, "simulated Telegram failure"):
            publisher.publish(changed)
        publisher.publish(changed)

        self.assertEqual(sum("#401" in message for message in api.sent), 1)
        self.assertEqual(sum("#402" in message for message in api.sent), 1)


if __name__ == "__main__":
    unittest.main()
