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
        "sources": {
            "runtime": {"status": "fresh"},
            "github": {"status": "fresh"},
            "test": {"status": "fresh"},
        },
        "owner_view": {
            "blocked": [],
            "ready_for_acceptance": [],
            "system_quarantines": [],
        },
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
        self.state_path = Path(self.tempdir.name) / "state.json"
        self.store = StateStore(self.state_path)
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

    def test_expected_restart_clears_only_after_runtime_is_fresh(self):
        self.publisher.publish(base_snapshot())
        self.store.update({"expected_service_restart_until": time.time() + 60})
        starting = base_snapshot()
        starting["service"] = {"live": True, "status": "starting"}
        starting["sources"]["runtime"] = {"status": "stale"}

        self.publisher.publish(starting)
        self.assertGreater(self.store.read()["expected_service_restart_until"], 0)

        self.publisher.publish(base_snapshot())
        self.assertEqual(self.store.read()["expected_service_restart_until"], 0)

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

    def test_ready_alert_does_not_repeat_across_test_sha_changes_or_restart(self):
        self.publisher.publish(base_snapshot())
        ready = base_snapshot()
        ready["owner_view"]["ready_for_acceptance"] = [
            {"number": 401, "title": "Ready", "test": {"sha": "firstsha"}}
        ]
        self.publisher.publish(ready)

        for sha in ("secondsha", "thirdsha", "fourthsha"):
            changed_deploy = base_snapshot()
            changed_deploy["owner_view"]["ready_for_acceptance"] = [
                {"number": 401, "title": "Ready", "test": {"sha": sha}}
            ]
            self.publisher.publish(changed_deploy)

        restarted = NotificationPublisher(
            api=self.api,
            state_store=StateStore(self.state_path),
            detector=NotificationDetector(),
        )
        restarted.publish(changed_deploy)

        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("#401", self.api.sent[0])

    def test_restart_seeds_current_ready_backlog_without_catch_up_alerts(self):
        previous = base_snapshot()
        previous["owner_view"]["ready_for_acceptance"] = [
            {"number": 401, "title": "Previously ready", "test": {"sha": "abc12345"}}
        ]
        self.publisher.publish(previous)

        restarted = NotificationPublisher(
            api=self.api,
            state_store=StateStore(self.state_path),
            detector=NotificationDetector(),
        )
        current = base_snapshot()
        current["owner_view"]["ready_for_acceptance"] = [
            *previous["owner_view"]["ready_for_acceptance"],
            {"number": 402, "title": "Ready while offline", "test": {"sha": "abc12345"}},
            {"number": 403, "title": "Also ready while offline", "test": {"sha": "abc12345"}},
        ]

        restarted.publish(current)
        restarted.publish(current)

        self.assertEqual(self.api.sent, [])
        self.assertEqual(
            set(self.store.read()["notification_fingerprints"]),
            {"ready:401", "ready:402", "ready:403"},
        )

        newly_ready = base_snapshot()
        newly_ready["owner_view"]["ready_for_acceptance"] = [
            *current["owner_view"]["ready_for_acceptance"],
            {"number": 404, "title": "Ready after startup", "test": {"sha": "abc12345"}},
        ]
        restarted.publish(newly_ready)

        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("#404", self.api.sent[0])

    def test_restart_seeds_bulk_blocked_import_without_catch_up_alerts(self):
        self.publisher.publish(base_snapshot())
        restarted = NotificationPublisher(
            api=self.api,
            state_store=StateStore(self.state_path),
            detector=NotificationDetector(),
        )
        imported = base_snapshot()
        imported["owner_view"]["blocked"] = [
            {
                "number": number,
                "title": f"Imported blocker {number}",
                "question": "Owner input required",
            }
            for number in range(500, 524)
        ]

        restarted.publish(imported)

        self.assertEqual(self.api.sent, [])
        after_startup = base_snapshot()
        after_startup["owner_view"]["blocked"] = [
            *imported["owner_view"]["blocked"],
            {"number": 524, "title": "New blocker", "question": "Choose A or B?"},
        ]
        restarted.publish(after_startup)
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("#524", self.api.sent[0])

    def test_restart_waits_for_fresh_sources_before_seeding_ready_backlog(self):
        previous = base_snapshot()
        previous["owner_view"]["ready_for_acceptance"] = [
            {"number": 401, "title": "Previously ready", "test": {"sha": "abc12345"}}
        ]
        self.publisher.publish(previous)

        restarted = NotificationPublisher(
            api=self.api,
            state_store=StateStore(self.state_path),
            detector=NotificationDetector(),
        )
        stale = base_snapshot()
        stale["sources"]["github"] = {"status": "stale"}
        stale["owner_view"]["ready_for_acceptance"] = []
        restarted.publish(stale)

        recovered = base_snapshot()
        recovered["owner_view"]["ready_for_acceptance"] = [
            *previous["owner_view"]["ready_for_acceptance"],
            {"number": 402, "title": "Ready while offline", "test": {"sha": "abc12345"}},
        ]
        restarted.publish(recovered)

        self.assertEqual(self.api.sent, [])
        self.assertEqual(
            set(self.store.read()["notification_fingerprints"]),
            {"ready:401", "ready:402"},
        )

    def test_restart_baseline_still_sends_system_events_once(self):
        self.publisher.publish(base_snapshot())
        restarted = NotificationPublisher(
            api=self.api,
            state_store=StateStore(self.state_path),
            detector=NotificationDetector(),
        )
        failed = base_snapshot()
        failed["service"] = {"live": False, "status": "exited", "reason": "crash"}
        failed["failures"] = [
            {
                "fingerprint": "runtime:RuntimeError:worker pool crashed",
                "message": "worker pool crashed",
                "unrecoverable": True,
            }
        ]

        restarted.publish(failed)
        restarted.publish(failed)

        self.assertEqual(len(self.api.sent), 2)
        self.assertTrue(any("unexpectedly stopped" in text for text in self.api.sent))
        self.assertTrue(any("Systemic Symphony failure" in text for text in self.api.sent))

    def test_ready_reentry_with_the_same_test_sha_notifies_again(self):
        self.publisher.publish(base_snapshot())
        ready = base_snapshot()
        ready["owner_view"]["ready_for_acceptance"] = [
            {"number": 401, "title": "Ready", "test": {"sha": "abc12345"}}
        ]

        self.publisher.publish(ready)
        self.publisher.publish(base_snapshot())
        self.publisher.publish(ready)

        self.assertEqual(len(self.api.sent), 2)
        self.assertTrue(all("#401" in message for message in self.api.sent))

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

    def test_system_quarantine_alert_is_once_per_reason_across_owner_control_restart(self):
        self.publisher.publish(base_snapshot())
        quarantined = base_snapshot()
        quarantined["owner_view"]["system_quarantines"] = [
            {
                "number": 407,
                "title": "Quarantined work",
                "reason": "workspace before_run hook failed",
                "issue_url": "https://github.test/issues/407",
            }
        ]

        self.publisher.publish(quarantined)
        self.publisher.publish(quarantined)
        restarted = NotificationPublisher(
            api=self.api,
            state_store=StateStore(self.state_path),
            detector=NotificationDetector(),
        )
        restarted.publish(quarantined)

        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("System quarantine #407: Quarantined work", self.api.sent[0])
        self.assertIn("Reason: workspace before_run hook failed", self.api.sent[0])
        self.assertIn("https://github.test/issues/407", self.api.sent[0])

        changed_reason = base_snapshot()
        changed_reason["owner_view"]["system_quarantines"] = [
            {
                **quarantined["owner_view"]["system_quarantines"][0],
                "reason": "workspace before_run hook failed again",
            }
        ]
        restarted.publish(changed_reason)
        self.assertEqual(len(self.api.sent), 2)
        self.assertIn("failed again", self.api.sent[1])

    def test_github_outage_preserves_system_quarantine_notification_baseline(self):
        healthy = base_snapshot()
        healthy["owner_view"]["system_quarantines"] = [
            {
                "number": 408,
                "title": "Existing quarantine",
                "reason": "workspace before_run hook failed",
                "issue_url": "https://github.test/issues/408",
            }
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
