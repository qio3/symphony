import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from owner_control.runtime import SnapshotService
from owner_control.state_store import StateStore
from owner_control.supervisor import DockerComposeSupervisor


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


class FailingSymphony:
    def state(self):
        raise OSError("connection refused")


class FakeGitHub:
    def project_snapshot(self):
        return {"items": []}

    def canonical(self, _ref):
        return {"sha": "abc12345", "url": "https://github.test/commit/abc12345"}


class ChangingGitHub(FakeGitHub):
    def __init__(self):
        self.sha = "firstsha"

    def canonical(self, _ref):
        return {"sha": self.sha, "url": None}


class BlockingGitHub(ChangingGitHub):
    def __init__(self):
        super().__init__()
        self.block = False
        self.refresh_started = threading.Event()
        self.release_refresh = threading.Event()

    def project_snapshot(self):
        if self.block:
            self.refresh_started.set()
            self.release_refresh.wait(timeout=2)
        return super().project_snapshot()


class AuthFailingGitHub(FakeGitHub):
    def project_snapshot(self):
        raise RuntimeError("HTTP 401 from api.github.com")


class ToggleGitHub(FakeGitHub):
    def __init__(self):
        self.fail = False

    def project_snapshot(self):
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


class FakeTest:
    def deployment(self):
        return {"sha": "abc12345", "url": "https://test.example"}


class FakeSupervisor:
    def status(self):
        return {"live": True, "container": "zavod-symphony"}


class FailingSupervisor:
    def status(self):
        raise OSError("docker socket unavailable")


class SnapshotServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = StateStore(Path(self.tempdir.name) / "state.json")

    def test_aggregates_all_sources_without_ai(self):
        service = SnapshotService(
            symphony=FakeSymphony(),
            github=FakeGitHub(),
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


class DockerComposeSupervisorTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
