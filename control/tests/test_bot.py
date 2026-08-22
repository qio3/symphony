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


if __name__ == "__main__":
    unittest.main()
