import tempfile
import unittest
from pathlib import Path

from owner_control.state_store import StateStore


class StateStoreHistoryTest(unittest.TestCase):
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
