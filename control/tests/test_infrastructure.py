import importlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class InfrastructureClientTest(unittest.TestCase):
    def test_host_failure_keeps_last_good_cpu_and_memory_as_stale(self):
        module = importlib.import_module("owner_control.infrastructure")
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "infrastructure.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hosts": [
                            {
                                "name": "CI_1",
                                "host": "192.0.2.10",
                                "user": "root",
                                "identity_file": str(Path(directory) / "ci-key"),
                                "runner_names": ["zavod-r1"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            fail_ssh = False

            def runner(command, **kwargs):
                if command[:2] == ["gh", "api"]:
                    endpoint = command[2]
                    if endpoint.endswith("actions/runners?per_page=100"):
                        value = {"runners": [{"name": "zavod-r1", "status": "online", "busy": False}]}
                    else:
                        value = {"workflow_runs": []}
                    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(value), stderr="")
                if fail_ssh:
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="offline")
                value = {"cpu_percent": 31.5, "memory_percent": 44.0, "memory_usage": "7.0 / 16.0 GiB"}
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(value), stderr="")

            client = module.InfrastructureClient(
                config_path=config_path,
                repository="qio3/zavod",
                runner=runner,
                cache_seconds=0,
            )
            client.snapshot()
            fail_ssh = True

            stale = client.snapshot()

        self.assertTrue(stale["stale"])
        self.assertEqual(stale["hosts"][0]["status"], "stale")
        self.assertEqual(stale["hosts"][0]["cpu_percent"], 31.5)
        self.assertEqual(stale["hosts"][0]["memory_percent"], 44.0)

    def test_github_failure_propagates_so_snapshot_layer_can_keep_last_good_state(self):
        module = importlib.import_module("owner_control.infrastructure")
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "infrastructure.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hosts": [
                            {
                                "name": "CI_1",
                                "host": "192.0.2.10",
                                "user": "root",
                                "identity_file": str(Path(directory) / "ci-key"),
                                "runner_names": ["zavod-r1"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            def runner(command, **kwargs):
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="offline")

            client = module.InfrastructureClient(
                config_path=config_path,
                repository="qio3/zavod",
                runner=runner,
                cache_seconds=0,
            )

            with self.assertRaisesRegex(RuntimeError, "infrastructure command failed"):
                client.snapshot()

    def test_groups_fixed_hosts_with_runner_jobs_and_live_cpu_memory(self):
        try:
            module = importlib.import_module("owner_control.infrastructure")
        except ModuleNotFoundError:
            self.fail("owner_control.infrastructure is required")
        self.assertTrue(hasattr(module, "InfrastructureClient"))

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "infrastructure.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hosts": [
                            {
                                "name": "CI_1",
                                "host": "192.0.2.10",
                                "user": "root",
                                "port": 22,
                                "identity_file": str(Path(directory) / "ci-key"),
                                "runner_names": ["zavod-r1", "zavod-r2"],
                            },
                            {
                                "name": "Backup",
                                "host": "192.0.2.20",
                                "user": "dev",
                                "port": 2223,
                                "identity_file": str(Path(directory) / "backup-key"),
                                "runner_names": ["zavod-light-backup"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            calls = []

            def runner(command, **kwargs):
                calls.append(command)
                endpoint = command[2] if command[:2] == ["gh", "api"] else ""
                if endpoint.endswith("actions/runners?per_page=100"):
                    output = {
                        "runners": [
                            {"name": "zavod-r1", "status": "online", "busy": True},
                            {"name": "zavod-r2", "status": "online", "busy": False},
                            {"name": "zavod-light-backup", "status": "online", "busy": False},
                        ]
                    }
                elif endpoint.endswith("actions/runs?status=in_progress&per_page=30"):
                    output = {
                        "workflow_runs": [
                            {
                                "id": 100,
                                "head_branch": "symphony/GH-401",
                                "display_title": "Issue delivery",
                            }
                        ]
                    }
                elif endpoint.endswith("actions/runs?status=queued&per_page=30"):
                    output = {
                        "workflow_runs": [
                            {
                                "id": 101,
                                "head_branch": "symphony/GH-402",
                                "display_title": "Queued issue",
                                "status": "queued",
                            }
                        ]
                    }
                elif endpoint.endswith("actions/runs/100/jobs?per_page=100"):
                    output = {
                        "jobs": [
                            {
                                "name": "Backend",
                                "status": "in_progress",
                                "runner_name": "zavod-r1",
                                "html_url": "https://github.test/jobs/1",
                            },
                            {
                                "name": "Waiting shard",
                                "status": "queued",
                                "runner_name": None,
                                "html_url": "https://github.test/jobs/2",
                            },
                        ]
                    }
                elif endpoint.endswith("actions/runs/101/jobs?per_page=100"):
                    output = {
                        "jobs": [
                            {
                                "name": "Queued backend",
                                "status": "queued",
                                "runner_name": None,
                                "html_url": "https://github.test/jobs/3",
                            }
                        ]
                    }
                else:
                    output = {
                        "cpu_percent": 57.5,
                        "memory_percent": 62.25,
                        "memory_usage": "9.9 / 16.0 GiB",
                    }
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(output),
                    stderr="",
                )

            client = module.InfrastructureClient(
                config_path=config_path,
                repository="qio3/zavod",
                runner=runner,
                cache_seconds=0,
            )

            snapshot = client.snapshot()

        self.assertEqual(snapshot["queued_jobs"], 2)
        self.assertEqual(snapshot["alerts"], 0)
        self.assertEqual(
            snapshot["hosts"][0],
            {
                "name": "CI_1",
                "kind": "ci",
                "status": "online",
                "cpu_percent": 57.5,
                "memory_percent": 62.25,
                "memory_usage": "9.9 / 16.0 GiB",
                "runners_busy": 1,
                "runners_total": 2,
                "jobs": [
                    {
                        "issue": 401,
                        "name": "Backend",
                        "url": "https://github.test/jobs/1",
                    }
                ],
            },
        )
        self.assertEqual(snapshot["hosts"][1]["runners_total"], 1)
        self.assertTrue(any(command[:2] == ["gh", "api"] for command in calls))
        self.assertTrue(any(command[0] == "ssh" for command in calls))


if __name__ == "__main__":
    unittest.main()
