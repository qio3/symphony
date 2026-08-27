import tempfile
import unittest
from pathlib import Path

from owner_control.state_store import StateStore


class StateStoreHistoryTest(unittest.TestCase):
    def test_worker_limit_override_is_bounded_by_the_configured_maximum(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "owner-control.json")
            self.assertEqual(store.worker_limit(12), 12)
            store.set_worker_limit(8, maximum=12)
            self.assertEqual(store.worker_limit(12), 8)
            with self.assertRaises(ValueError):
                store.set_worker_limit(13, maximum=12)

    def test_append_only_mutation_journal_survives_state_rewrites(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "owner-control.json")
            store.append_mutation(
                {"actor": "owner-control", "issue": 401, "old": "Ready for AI", "new": "In Progress", "reason": "lease"}
            )
            store.update({"intake_active": False})
            store.append_mutation(
                {"actor": "owner-control", "issue": 401, "old": "In Progress", "new": "Done", "reason": "accept"}
            )

            entries = store.mutation_journal()
            self.assertEqual([entry["new"] for entry in entries], ["In Progress", "Done"])
            self.assertTrue(all(entry.get("timestamp") for entry in entries))

    def test_records_one_status_sample_per_minute_and_prunes_older_than_three_days(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "owner-control.json")
            old = {
                "recorded_at": "2026-08-24T09:59:59+00:00",
                "counts": {"running": 2},
            }
            store.update({"status_history": [old]})

            first = {
                "recorded_at": "2026-08-28T10:00:00+00:00",
                "counts": {
                    "ready_for_ai": 46,
                    "running": 7,
                    "blocked": 4,
                    "ready_for_acceptance": 13,
                    "done": 84,
                },
                "workers": {"running": 7, "limit": 12},
            }
            duplicate_minute = {
                **first,
                "recorded_at": "2026-08-28T10:00:20+00:00",
                "counts": {**first["counts"], "running": 8},
            }
            next_minute = {
                **first,
                "recorded_at": "2026-08-28T10:01:01+00:00",
                "counts": {**first["counts"], "running": 6},
            }

            self.assertEqual(store.record_status_sample(first), [first])
            self.assertEqual(store.record_status_sample(duplicate_minute), [first])
            self.assertEqual(store.record_status_sample(next_minute), [first, next_minute])
            self.assertEqual(store.status_history(), [first, next_minute])


if __name__ == "__main__":
    unittest.main()
