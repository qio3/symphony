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

    def test_symphony_failure_reports_down_and_fail_closed_snapshot(self):
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

        self.assertFalse(snapshot["service"]["live"])
        self.assertIn("connection refused", snapshot["service"]["reason"])
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


class DockerComposeSupervisorTest(unittest.TestCase):
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
