import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from owner_control.clients import SymphonyClient
from owner_control.runtime import SnapshotService
from owner_control.state_store import StateStore
from owner_control.supervisor import DockerComposeSupervisor
from owner_control.telegram import NotificationDetector


class FakeSymphony:
    def state(self):
        return {
            "generated_at": "2026-08-23T10:00:00Z",
            "running": [],
            "retrying": [],
            "blocked": [],
            "codex_totals": {},
            "rate_limits": None,
        }


class CountingSymphony(FakeSymphony):
    def __init__(self):
        self.calls = 0

    def state(self):
        self.calls += 1
        return super().state()


class FailingSymphony:
    def state(self):
        raise OSError("connection refused")


class FakeGitHub:
    def __init__(self):
        self.reconcile_requests = []

    def project_snapshot(self, *, reconcile_intake=False):
        self.reconcile_requests.append(reconcile_intake)
        return {"items": []}

    def canonical(self, _ref):
        return {"sha": "abc12345", "url": "https://github.test/commit/abc12345"}


class ChangingGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.sha = "firstsha"

    def canonical(self, _ref):
        return {"sha": self.sha, "url": None}


class CountingGitHub(ChangingGitHub):
    def __init__(self):
        super().__init__()
        self.project_calls = 0
        self.canonical_calls = 0
        self.fail = False

    def project_snapshot(self, *, reconcile_intake=False):
        self.project_calls += 1
        if self.fail:
            raise RuntimeError("GitHub GraphQL rate limit exhausted")
        return super().project_snapshot(reconcile_intake=reconcile_intake)

    def canonical(self, ref):
        self.canonical_calls += 1
        return super().canonical(ref)


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class BlockingGitHub(ChangingGitHub):
    def __init__(self):
        super().__init__()
        self.block = False
        self.refresh_started = threading.Event()
        self.release_refresh = threading.Event()

    def project_snapshot(self, *, reconcile_intake=False):
        if self.block:
            self.refresh_started.set()
            self.release_refresh.wait(timeout=2)
        return super().project_snapshot(reconcile_intake=reconcile_intake)


class AuthFailingGitHub(FakeGitHub):
    def project_snapshot(self, *, reconcile_intake=False):
        raise RuntimeError("HTTP 401 from api.github.com")


class ToggleGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.fail = False

    def project_snapshot(self, *, reconcile_intake=False):
        self.reconcile_requests.append(reconcile_intake)
        if self.fail:
            raise OSError("temporary GitHub outage")
        return {
            "items": [
                {
                    "number": 401,
                    "title": "Keep the owner view stable",
                    "url": "https://github.test/issues/401",
                    "status": "Ready for Acceptance",
                    "state": "OPEN",
                }
            ]
        }


class ToggleSymphony(FakeSymphony):
    def __init__(self):
        self.fail = False

    def state(self):
        if self.fail:
            raise OSError("runtime API is restarting")
        return {
            **super().state(),
            "running": [
                {
                    "issue_id": "402",
                    "issue_identifier": "GH-402",
                    "started_at": "2026-08-23T09:45:00Z",
                }
            ],
        }


class RichSymphony(ToggleSymphony):
    def state(self):
        value = super().state()
        value.update(
            {
                "retrying": [
                    {
                        "issue_id": "403",
                        "issue_identifier": "GH-403",
                        "due_at": "2026-08-23T10:05:00Z",
                    }
                ],
                "codex_totals": {"total_tokens": 4321},
                "rate_limits": {
                    "weekly": {
                        "windowDurationMins": 10080,
                        "usedPercent": 21,
                    }
                },
                "issue_usage": {
                    "402": {
                        "aggregate": {
                            "token_usage": {"total_tokens": 1234},
                            "estimated_usage_credits_micros": 99,
                            "week_impact_percent": None,
                        }
                    }
                },
            }
        )
        return value


class FakeTest:
    def deployment(self):
        return {"sha": "abc12345", "url": "https://test.example"}


class FakeSupervisor:
    def status(self):
        return {"live": True, "container": "zavod-symphony"}


class FailingSupervisor:
    def status(self):
        raise OSError("docker socket unavailable")


class ToggleSupervisor(FakeSupervisor):
    def __init__(self):
        self.live = True

    def status(self):
        return {
            "live": self.live,
            "container": "zavod-symphony",
            "status": "running" if self.live else "exited",
        }


class FlakySupervisor(ToggleSupervisor):
    def __init__(self):
        super().__init__()
        self.fail = False

    def status(self):
        if self.fail:
            raise OSError("docker socket unavailable")
        return super().status()


class SnapshotServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = StateStore(Path(self.tempdir.name) / "state.json")

    def test_aggregates_all_sources_without_ai(self):
        github = FakeGitHub()
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=github,
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
        )

        snapshot = service.snapshot()

        self.assertEqual(snapshot["service"], {"live": True, "container": "zavod-symphony"})
        self.assertEqual(snapshot["canonical"]["sha"], "abc12345")
        self.assertEqual(snapshot["test"]["sha"], "abc12345")
        self.assertEqual(snapshot["workers"], {"running": 0, "limit": 2})
        self.assertEqual(github.reconcile_requests, [True])

    def test_snapshot_exposes_persisted_three_day_status_history(self):
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=12,
            canonical_ref="rebrand/stanina",
        )

        snapshot = service.snapshot()

        self.assertEqual(len(snapshot["history"]), 1)
        self.assertEqual(
            snapshot["history"][0]["counts"],
            {
                "ready_for_ai": 0,
                "running": 0,
                "blocked": 0,
                "ready_for_acceptance": 0,
                "done": 0,
            },
        )
        self.assertEqual(snapshot["history"][0]["workers"], {"running": 0, "limit": 12})
        self.assertEqual(self.store.status_history(), snapshot["history"])

    def test_cached_snapshot_fails_closed_until_initial_collection_completes(self):
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
        )

        with self.assertRaisesRegex(RuntimeError, "snapshot is not ready"):
            service.cached_snapshot()

        collected = service.snapshot()

        self.assertIs(service.cached_snapshot(), collected)

    def test_symphony_failure_reports_runtime_unavailable_without_overriding_container_state(self):
        service = SnapshotService(
            symphony=FailingSymphony(),
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
        )

        snapshot = service.snapshot()

        self.assertTrue(snapshot["service"]["live"])
        self.assertEqual(snapshot["sources"]["runtime"]["status"], "unavailable")
        self.assertIn("connection refused", snapshot["sources"]["runtime"]["error"])
        self.assertEqual(snapshot["running"], [])

    def test_supervisor_failure_still_returns_a_down_snapshot(self):
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=FailingSupervisor(),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
        )

        snapshot = service.snapshot()

        self.assertFalse(snapshot["service"]["live"])
        self.assertIn("docker socket unavailable", snapshot["service"]["reason"])
        self.assertEqual(snapshot["failures"][0]["fingerprint"], "supervisor:OSError:transient")
        self.assertEqual(snapshot["sources"]["runtime"]["status"], "fresh")

    def test_unconfirmed_docker_state_cannot_emit_service_stopped_notification(self):
        inspect_calls = 0

        def runner(command, **kwargs):
            nonlocal inspect_calls
            inspect_calls += 1
            if inspect_calls == 1:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"Running": true, "Status": "running"}',
                    stderr="",
                )
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="Docker daemon temporarily unavailable",
            )

        service = SnapshotService(
            symphony=FakeSymphony(),
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=DockerComposeSupervisor(
                compose_file=Path("C:/control/docker-compose.yml"),
                container_name="zavod-symphony",
                service_name="symphony",
                runner=runner,
            ),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
        )

        previous = service.snapshot(fresh=True)
        current = service.snapshot(fresh=True)

        self.assertTrue(previous["service"]["live"])
        self.assertEqual(current["service"]["status"], "unknown")
        self.assertEqual(current["sources"]["supervisor"]["status"], "unavailable")
        self.assertEqual(NotificationDetector().detect(previous, current), [])

    def test_invalidate_forces_the_next_reader_to_rebuild_snapshot(self):
        github = ChangingGitHub()
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=github,
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
            cache_seconds=60,
        )
        self.assertEqual(service.snapshot()["canonical"]["sha"], "firstsha")
        github.sha = "secondsha"
        self.assertEqual(service.snapshot()["canonical"]["sha"], "firstsha")

        service.invalidate()

        deadline = time.monotonic() + 1
        while service.snapshot()["canonical"]["sha"] != "secondsha" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(service.snapshot()["canonical"]["sha"], "secondsha")

    def test_regular_refresh_keeps_runtime_fresh_without_requerying_github_inside_ttl(self):
        symphony = CountingSymphony()
        github = CountingGitHub()
        service = SnapshotService(
            symphony=symphony,
            github=github,
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
            cache_seconds=0,
        )

        service.snapshot()
        service.snapshot()

        deadline = time.monotonic() + 1
        while symphony.calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(symphony.calls, 2)
        self.assertEqual(github.project_calls, 1)
        self.assertEqual(github.canonical_calls, 1)

    def test_forced_snapshot_bypasses_github_ttl_for_action_preflight(self):
        github = CountingGitHub()
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=github,
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
        )
        self.assertEqual(service.snapshot()["canonical"]["sha"], "firstsha")
        github.sha = "secondsha"

        forced = service.snapshot(fresh=True)

        self.assertEqual(forced["canonical"]["sha"], "secondsha")
        self.assertEqual(github.project_calls, 2)
        self.assertEqual(github.canonical_calls, 2)

    def test_github_failure_cooldown_preserves_runtime_refresh_and_then_recovers(self):
        clock = FakeClock()
        symphony = CountingSymphony()
        github = CountingGitHub()
        service = SnapshotService(
            symphony=symphony,
            github=github,
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
            cache_seconds=0,
        )

        with patch("owner_control.runtime.time.monotonic", clock):
            service.snapshot(fresh=True)
            github.fail = True
            stale = service.snapshot(fresh=True)
            calls_after_failure = github.project_calls
            runtime_calls_after_failure = symphony.calls

            service.snapshot()
            deadline = time.time() + 1
            while symphony.calls == runtime_calls_after_failure and time.time() < deadline:
                time.sleep(0.01)

            self.assertGreater(symphony.calls, runtime_calls_after_failure)
            self.assertEqual(github.project_calls, calls_after_failure)
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["sources"]["github"]["status"], "stale")

            github.fail = False
            github.sha = "recoveredsha"
            clock.advance(61)
            service.snapshot()
            deadline = time.time() + 1
            while github.project_calls == calls_after_failure and time.time() < deadline:
                time.sleep(0.01)

            self.assertGreater(github.project_calls, calls_after_failure)
            deadline = time.time() + 1
            recovered = service.snapshot()
            while recovered["sources"]["github"]["status"] != "fresh" and time.time() < deadline:
                time.sleep(0.01)
                recovered = service.snapshot()
            self.assertEqual(recovered["canonical"]["sha"], "recoveredsha")
            self.assertEqual(recovered["sources"]["github"]["status"], "fresh")

    def test_invalidate_immediately_patches_owner_truth_after_expected_stop(self):
        github = BlockingGitHub()
        supervisor = ToggleSupervisor()
        service = SnapshotService(
            symphony=RichSymphony(),
            github=github,
            test_environment=FakeTest(),
            supervisor=supervisor,
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
            cache_seconds=60,
        )
        healthy = service.snapshot(fresh=True)
        self.assertEqual(healthy["workers"], {"running": 1, "limit": 5})
        self.assertEqual(healthy["counts"]["queued"], 1)
        self.assertEqual(healthy["quota"]["weekly"]["used_percent"], 21)

        github.block = True
        supervisor.live = False
        self.store.set_intake_active(False)
        self.store.update({"expected_service_stop": True})

        started_at = time.monotonic()
        service.invalidate()
        patched = service.snapshot()
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.2)
        self.assertFalse(patched["service"]["live"])
        self.assertEqual(patched["service"]["status"], "exited")
        self.assertEqual(patched["intake"], {"active": False, "status": "paused"})
        self.assertEqual(patched["sources"]["runtime"]["status"], "stale")
        self.assertEqual(patched["workers"]["running"], 0)
        self.assertEqual(patched["counts"]["running"], 0)
        self.assertEqual(patched["running"], [])
        self.assertEqual(patched["owner_view"]["work_items"], [])
        self.assertEqual(len(patched["retrying"]), 1)
        self.assertEqual(patched["counts"]["queued"], 1)
        self.assertEqual(patched["quota"]["weekly"]["used_percent"], 21)
        self.assertEqual(patched["issue_usage"]["402"]["total_tokens"], 1234)
        self.assertEqual(patched["canonical"]["sha"], "firstsha")
        self.assertTrue(github.refresh_started.wait(timeout=1))

        github.release_refresh.set()
        refreshed = service.snapshot(fresh=True)
        self.assertFalse(refreshed["service"]["live"])
        self.assertEqual(refreshed["running"], [])
        self.assertEqual(len(refreshed["retrying"]), 1)
        self.assertEqual(refreshed["counts"]["queued"], 1)
        self.assertEqual(refreshed["quota"]["weekly"]["used_percent"], 21)
        self.assertEqual(refreshed["issue_usage"]["402"]["total_tokens"], 1234)

    def test_inflight_running_refresh_cannot_overwrite_a_new_stop(self):
        github = BlockingGitHub()
        supervisor = ToggleSupervisor()
        service = SnapshotService(
            symphony=RichSymphony(),
            github=github,
            test_environment=FakeTest(),
            supervisor=supervisor,
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
            cache_seconds=60,
        )
        service.snapshot(fresh=True)
        github.block = True
        refreshed = []
        refresh_thread = threading.Thread(
            target=lambda: refreshed.append(service.snapshot(fresh=True))
        )
        refresh_thread.start()
        self.assertTrue(github.refresh_started.wait(timeout=1))

        supervisor.live = False
        self.store.update({"expected_service_stop": True})
        service.invalidate()
        self.assertFalse(service.snapshot()["service"]["live"])

        github.release_refresh.set()
        refresh_thread.join(timeout=2)
        self.assertFalse(refresh_thread.is_alive())
        self.assertFalse(refreshed[0]["service"]["live"])
        self.assertEqual(refreshed[0]["running"], [])
        self.assertFalse(service.snapshot()["service"]["live"])

    def test_inflight_stopped_refresh_cannot_overwrite_a_new_start(self):
        github = BlockingGitHub()
        supervisor = ToggleSupervisor()
        service = SnapshotService(
            symphony=RichSymphony(),
            github=github,
            test_environment=FakeTest(),
            supervisor=supervisor,
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
            cache_seconds=60,
        )
        service.snapshot(fresh=True)
        supervisor.live = False
        self.store.update({"expected_service_stop": True})
        service.snapshot(fresh=True)

        github.block = True
        refreshed = []
        refresh_thread = threading.Thread(
            target=lambda: refreshed.append(service.snapshot(fresh=True))
        )
        refresh_thread.start()
        self.assertTrue(github.refresh_started.wait(timeout=1))

        self.store.update(
            {
                "expected_service_stop": False,
                "expected_service_restart_until": time.time() + 120,
            }
        )
        service.invalidate()
        starting = service.snapshot()
        self.assertFalse(starting["service"]["live"])
        self.assertEqual(starting["service"]["status"], "starting")
        self.assertEqual(starting["running"], [])
        self.assertEqual(starting["quota"]["weekly"]["used_percent"], 21)

        supervisor.live = True
        github.release_refresh.set()
        refresh_thread.join(timeout=2)
        self.assertFalse(refresh_thread.is_alive())
        self.assertTrue(refreshed[0]["service"]["live"])
        self.assertEqual(refreshed[0]["sources"]["runtime"]["status"], "fresh")
        self.assertTrue(service.snapshot()["service"]["live"])

    def test_invalidate_with_unknown_supervisor_preserves_active_runtime(self):
        supervisor = FlakySupervisor()
        service = SnapshotService(
            symphony=RichSymphony(),
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=supervisor,
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
            cache_seconds=60,
        )
        service.snapshot(fresh=True)
        supervisor.fail = True

        service.invalidate()
        patched = service.snapshot()

        self.assertEqual(patched["service"]["status"], "unknown")
        self.assertEqual(patched["sources"]["supervisor"]["status"], "unavailable")
        self.assertEqual(patched["workers"]["running"], 1)
        self.assertEqual(len(patched["running"]), 1)
        service.snapshot(fresh=True)

    def test_live_container_stays_starting_until_runtime_is_fresh(self):
        symphony = RichSymphony()
        service = SnapshotService(
            symphony=symphony,
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=ToggleSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
            cache_seconds=60,
        )
        service.snapshot(fresh=True)
        self.store.update(
            {
                "expected_service_stop": False,
                "expected_service_restart_until": time.time() + 120,
            }
        )
        symphony.fail = True

        service.invalidate()
        starting = service.snapshot(fresh=True)

        self.assertTrue(starting["service"]["live"])
        self.assertEqual(starting["service"]["status"], "starting")
        self.assertEqual(starting["sources"]["runtime"]["status"], "stale")
        self.assertEqual(starting["workers"]["running"], 0)
        self.assertEqual(starting["running"], [])
        self.assertEqual(len(starting["retrying"]), 1)
        self.assertEqual(starting["quota"]["weekly"]["used_percent"], 21)
        self.assertGreater(self.store.read()["expected_service_restart_until"], 0)

        symphony.fail = False
        running = service.snapshot(fresh=True)

        self.assertTrue(running["service"]["live"])
        self.assertEqual(running["service"]["status"], "running")
        self.assertEqual(running["sources"]["runtime"]["status"], "fresh")
        self.assertEqual(running["workers"]["running"], 1)
        self.assertEqual(self.store.read()["expected_service_restart_until"], 0)

    def test_restart_in_progress_cannot_confirm_the_previous_live_runtime(self):
        service = SnapshotService(
            symphony=RichSymphony(),
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=ToggleSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
        )
        service.snapshot(fresh=True)
        self.store.update(
            {
                "expected_service_restart_until": time.time() + 120,
                "service_action_in_progress": "restart",
                "service_action_in_progress_until": time.time() + 120,
            }
        )

        during_restart = service.snapshot(fresh=True)

        self.assertEqual(during_restart["service"]["status"], "starting")
        self.assertEqual(during_restart["workers"]["running"], 0)
        self.assertEqual(during_restart["running"], [])
        self.assertGreater(self.store.read()["expected_service_restart_until"], 0)

    def test_expired_action_marker_cannot_block_fresh_external_recovery(self):
        service = SnapshotService(
            symphony=RichSymphony(),
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=ToggleSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
        )
        self.store.update(
            {
                "expected_service_stop": True,
                "service_action_in_progress": "stop_service",
                "service_action_in_progress_until": time.time() - 1,
            }
        )

        recovered = service.snapshot(fresh=True)

        self.assertTrue(recovered["service"]["live"])
        self.assertEqual(recovered["service"]["status"], "running")
        self.assertEqual(recovered["sources"]["runtime"]["status"], "fresh")
        self.assertFalse(self.store.read()["expected_service_stop"])
        self.assertIsNone(self.store.read()["service_action_in_progress"])
        self.assertEqual(self.store.read()["service_action_in_progress_until"], 0)

    def test_expired_snapshot_returns_stale_cache_while_refresh_runs(self):
        github = BlockingGitHub()
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=github,
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
            cache_seconds=0,
            github_cache_seconds=0,
        )
        self.assertEqual(service.snapshot()["canonical"]["sha"], "firstsha")
        github.sha = "secondsha"
        github.block = True

        started_at = time.monotonic()
        stale = service.snapshot()
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.2)
        self.assertEqual(stale["canonical"]["sha"], "firstsha")
        self.assertTrue(github.refresh_started.wait(timeout=1))
        github.release_refresh.set()
        deadline = time.monotonic() + 1
        while service.snapshot()["canonical"]["sha"] != "secondsha" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(service.snapshot()["canonical"]["sha"], "secondsha")

    def test_cold_start_marks_persisted_github_data_stale_until_refresh_finishes(self):
        warm = SnapshotService(
            symphony=FakeSymphony(),
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
        )
        warm.snapshot(fresh=True)
        github = BlockingGitHub()
        github.block = True
        cold = SnapshotService(
            symphony=FakeSymphony(),
            github=github,
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
        )

        try:
            snapshot = cold.snapshot()

            self.assertTrue(github.refresh_started.wait(timeout=1))
            self.assertEqual(snapshot["sources"]["github"]["status"], "stale")
            self.assertTrue(snapshot["stale"])
        finally:
            github.release_refresh.set()
            cold.snapshot(fresh=True)

    def test_slow_github_refresh_does_not_block_runtime_refresh(self):
        symphony = CountingSymphony()
        github = BlockingGitHub()
        service = SnapshotService(
            symphony=symphony,
            github=github,
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
            cache_seconds=0,
            github_cache_seconds=0,
        )
        service.snapshot(fresh=True)
        github.block = True

        try:
            service.snapshot()
            self.assertTrue(github.refresh_started.wait(timeout=1))
            runtime_calls_before = symphony.calls

            deadline = time.monotonic() + 1
            while symphony.calls == runtime_calls_before and time.monotonic() < deadline:
                service.snapshot()
                time.sleep(0.01)

            self.assertGreater(symphony.calls, runtime_calls_before)
        finally:
            github.release_refresh.set()

    def test_authentication_failure_is_marked_systemic_for_attention_notification(self):
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=AuthFailingGitHub(),
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=2,
            canonical_ref="rebrand/stanina",
        )

        failure = service.snapshot()["failures"][0]

        self.assertTrue(failure["unrecoverable"])
        self.assertIn("github snapshot unavailable", failure["message"])

    def test_temporary_github_failure_keeps_last_confirmed_owner_data_as_stale(self):
        github = ToggleGitHub()
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=github,
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
        )
        healthy = service.snapshot(fresh=True)
        self.assertEqual(healthy["counts"]["ready_for_acceptance"], 1)

        github.fail = True
        stale = service.snapshot(fresh=True)

        self.assertEqual(stale["counts"]["ready_for_acceptance"], 1)
        self.assertEqual(stale["canonical"]["sha"], "abc12345")
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["sources"]["github"]["status"], "stale")
        self.assertIn("temporary GitHub outage", stale["sources"]["github"]["error"])

    def test_runtime_restart_does_not_turn_a_running_container_or_counters_into_down_zeroes(self):
        symphony = ToggleSymphony()
        service = SnapshotService(
            symphony=symphony,
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
        )
        healthy = service.snapshot(fresh=True)
        self.assertEqual(healthy["workers"], {"running": 1, "limit": 5})

        symphony.fail = True
        stale = service.snapshot(fresh=True)

        self.assertTrue(stale["service"]["live"])
        self.assertEqual(stale["workers"], {"running": 1, "limit": 5})
        self.assertEqual(stale["sources"]["runtime"]["status"], "stale")
        self.assertTrue(stale["stale"])

    def test_non_object_symphony_response_keeps_last_good_runtime_as_stale(self):
        responses = [
            {
                "generated_at": "2026-08-25T10:00:00Z",
                "running": [{"issue_id": "402", "issue_identifier": "GH-402"}],
                "retrying": [],
                "blocked": [],
                "codex_totals": {},
                "rate_limits": None,
            },
            [],
        ]
        symphony = SymphonyClient(
            "http://127.0.0.1:4082",
            transport=lambda *_args, **_kwargs: responses.pop(0),
        )
        service = SnapshotService(
            symphony=symphony,
            github=FakeGitHub(),
            test_environment=FakeTest(),
            supervisor=FakeSupervisor(),
            state_store=self.store,
            worker_limit=5,
            canonical_ref="rebrand/stanina",
        )
        healthy = service.snapshot(fresh=True)
        stale = service.snapshot(fresh=True)

        self.assertEqual(healthy["workers"], {"running": 1, "limit": 5})
        self.assertEqual(stale["workers"], {"running": 1, "limit": 5})
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["sources"]["runtime"]["status"], "stale")
        self.assertIn("Symphony state response must be a JSON object", stale["sources"]["runtime"]["error"])


class DockerComposeSupervisorTest(unittest.TestCase):
    def test_status_confirms_a_stopped_container_from_valid_docker_state(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"Running": false, "Status": "exited"}',
                stderr="",
            )

        supervisor = DockerComposeSupervisor(
            compose_file=Path("C:/control/docker-compose.yml"),
            container_name="zavod-symphony",
            service_name="symphony",
            runner=runner,
        )

        status = supervisor.status()

        self.assertFalse(status["live"])
        self.assertEqual(status["status"], "exited")

    def test_status_is_unknown_when_docker_cannot_confirm_container_state(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="Docker daemon temporarily unavailable",
            )

        supervisor = DockerComposeSupervisor(
            compose_file=Path("C:/control/docker-compose.yml"),
            container_name="zavod-symphony",
            service_name="symphony",
            runner=runner,
        )

        with self.assertRaisesRegex(RuntimeError, "container state unavailable"):
            supervisor.status()

    def test_status_is_unknown_when_docker_returns_unconfirmed_state(self):
        for stdout in (
            "not-json",
            "{}",
            "[]",
            '{"Running": false}',
            '{"Running": "false", "Status": "exited"}',
            '{"Running": false, "Status": ""}',
            '{"Running": false, "Status": "unavailable"}',
        ):
            with self.subTest(stdout=stdout):
                def runner(command, **kwargs):
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=stdout,
                        stderr="",
                    )

                supervisor = DockerComposeSupervisor(
                    compose_file=Path("C:/control/docker-compose.yml"),
                    container_name="zavod-symphony",
                    service_name="symphony",
                    runner=runner,
                )

                with self.assertRaisesRegex(RuntimeError, "invalid container state"):
                    supervisor.status()

    def test_start_stop_and_status_use_fixed_targets_without_shell(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[1] == "inspect":
                return subprocess.CompletedProcess(
                    command, 0, stdout='{"Running": true, "Status": "running"}', stderr=""
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        supervisor = DockerComposeSupervisor(
            compose_file=Path("C:/control/docker-compose.yml"),
            container_name="zavod-symphony",
            service_name="symphony",
            runner=runner,
        )

        self.assertEqual(supervisor.start(), {"accepted": True, "container": "zavod-symphony"})
        self.assertEqual(supervisor.stop(), {"accepted": True, "container": "zavod-symphony"})
        self.assertEqual(supervisor.status()["status"], "running")

        self.assertEqual(
            [call[0] for call in calls],
            [
                [
                    "docker",
                    "compose",
                    "-f",
                    "C:\\control\\docker-compose.yml",
                    "up",
                    "-d",
                    "--no-deps",
                    "symphony",
                ],
                ["docker", "compose", "-f", "C:\\control\\docker-compose.yml", "stop", "symphony"],
                ["docker", "inspect", "zavod-symphony", "--format", "{{json .State}}"],
            ],
        )
        self.assertTrue(all(call[1].get("shell") is False for call in calls))

    def test_restart_and_logs_use_fixed_compose_target_without_shell(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="line one\nline two\n", stderr="")

        supervisor = DockerComposeSupervisor(
            compose_file=Path("C:/control/docker-compose.yml"),
            container_name="zavod-symphony",
            service_name="symphony",
            runner=runner,
        )

        supervisor.restart()
        lines = supervisor.logs(20)

        self.assertEqual(
            calls[0][0],
            ["docker", "compose", "-f", "C:\\control\\docker-compose.yml", "restart", "symphony"],
        )
        self.assertEqual(
            calls[1][0],
            ["docker", "compose", "-f", "C:\\control\\docker-compose.yml", "logs", "--tail", "20", "symphony"],
        )
        self.assertTrue(all(call[1].get("shell") is False for call in calls))
        self.assertEqual(lines, ["line one", "line two"])
        self.assertTrue(all(call[1].get("encoding") == "utf-8" for call in calls))
        self.assertTrue(all(call[1].get("errors") == "replace" for call in calls))


if __name__ == "__main__":
    unittest.main()
