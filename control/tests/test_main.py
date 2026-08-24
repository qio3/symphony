import unittest

from owner_control import __main__ as owner_main


class FakeSnapshots:
    def __init__(self):
        self.fresh_values = []

    def snapshot(self, *, fresh=False):
        self.fresh_values.append(fresh)
        return {"marker": "snapshot"}


class FakeNotifications:
    def __init__(self):
        self.published = []

    def publish(self, snapshot):
        self.published.append(snapshot)


class MainLoopTest(unittest.TestCase):
    def test_notification_poll_reuses_the_regular_snapshot_cache(self):
        snapshots = FakeSnapshots()
        notifications = FakeNotifications()
        publish_once = getattr(owner_main, "_publish_notifications_once", None)
        self.assertIsNotNone(publish_once)

        publish_once(notifications, snapshots)

        self.assertEqual(snapshots.fresh_values, [False])
        self.assertEqual(notifications.published, [{"marker": "snapshot"}])


if __name__ == "__main__":
    unittest.main()
