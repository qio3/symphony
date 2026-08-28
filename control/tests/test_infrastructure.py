import importlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class InfrastructureClientTest(unittest.TestCase):
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

        self.assertEqual(snapshot["queued_jobs"], 1)
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
