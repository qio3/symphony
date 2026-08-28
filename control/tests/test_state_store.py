import tempfile
import unittest
from pathlib import Path

from owner_control.state_store import StateStore


class StateStoreHistoryTest(unittest.TestCase):
    def test_phase_observation_closes_previous_phase_without_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner-control.json"
            store = StateStore(path)

            first = store.record_phase_observations(
                "issues",
                [{"key": "524", "phase": "Coding", "entered_at": "2026-08-28T08:05:41Z"}],
                recorded_at="2026-08-28T10:00:00Z",
            )
            duplicate = store.record_phase_observations(
                "issues",
                [{"key": "524", "phase": "Coding"}],
                recorded_at="2026-08-28T10:01:00Z",
            )
            transitioned = store.record_phase_observations(
                "issues",
                [{"key": "524", "phase": "Waiting CI"}],
                recorded_at="2026-08-28T10:02:00Z",
            )

            self.assertEqual(first["524"][0]["entered_at"], "2026-08-28T08:05:41Z")
            self.assertEqual(len(duplicate["524"]), 1)
            self.assertEqual(
                transitioned["524"],
                [
                    {
                        "phase": "Coding",
                        "entered_at": "2026-08-28T08:05:41Z",
                        "exited_at": "2026-08-28T10:02:00Z",
                    },
                    {
                        "phase": "Waiting CI",
                        "entered_at": "2026-08-28T10:02:00Z",
                        "exited_at": None,
                    },
                ],
            )
            self.assertEqual(
                StateStore(path).phase_histories("issues"),
                transitioned,
            )

    def test_wave_phase_history_is_persisted_by_pr_composition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner-control.json"
            store = StateStore(path)

            store.record_phase_observations(
                "waves",
                [{"key": "prs:421,423", "phase": "landing", "entered_at": "2026-08-28T09:30:00Z"}],
                recorded_at="2026-08-28T09:35:00Z",
            )
            histories = StateStore(path).phase_histories("waves")

            self.assertEqual(histories["prs:421,423"][0]["phase"], "landing")
            self.assertEqual(histories["prs:421,423"][0]["entered_at"], "2026-08-28T09:30:00Z")

    def test_persisted_action_steps_and_result_survive_a_new_store_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner-control.json"
            store = StateStore(path)
            store.record_action_step("rework:402", "comment")

            reopened = StateStore(path)
            self.assertTrue(reopened.action_step_completed("rework:402", "comment"))
            self.assertIsNone(reopened.action_result("rework:402"))

            result = {"status": "accepted", "action": "rework", "issue": 402}
            reopened.complete_action("rework:402", result)
            self.assertEqual(StateStore(path).action_result("rework:402"), result)

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
