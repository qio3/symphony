from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


_REMOTE_METRICS_SCRIPT = """
import json, time
def cpu_sample():
    values = [int(value) for value in open('/proc/stat', encoding='utf-8').readline().split()[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle
before = cpu_sample()
memory = {}
for line in open('/proc/meminfo', encoding='utf-8'):
    key, value = line.split(':', 1)
    memory[key] = int(value.strip().split()[0])
time.sleep(0.2)
after = cpu_sample()
delta_total = max(after[0] - before[0], 1)
delta_idle = max(after[1] - before[1], 0)
total_kib = memory.get('MemTotal', 0)
available_kib = memory.get('MemAvailable', memory.get('MemFree', 0))
used_kib = max(total_kib - available_kib, 0)
print(json.dumps({
    'cpu_percent': round(100.0 * (delta_total - delta_idle) / delta_total, 2),
    'memory_percent': round(100.0 * used_kib / total_kib, 2) if total_kib else None,
    'memory_usage': f'{used_kib / 1048576:.1f} / {total_kib / 1048576:.1f} GiB' if total_kib else None,
}))
""".strip()
_REMOTE_METRICS_COMMAND = (
    "python3 -c \"import base64;exec(base64.b64decode('"
    + base64.b64encode(_REMOTE_METRICS_SCRIPT.encode("utf-8")).decode("ascii")
    + "'))\""
)
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.-]+$")
_SAFE_USER = re.compile(r"^[A-Za-z0-9._-]+$")
_ISSUE = re.compile(r"(?:GH[-_/ ]?|#)(\d+)", re.IGNORECASE)


class InfrastructureClient:
    def __init__(
        self,
        *,
        config_path: Path,
        repository: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        cache_seconds: float = 30.0,
        gh_executable: str = "gh",
        ssh_executable: str = "ssh",
    ):
        self._hosts = _load_hosts(config_path)
        self._repository = repository
        self._runner = runner
        self._cache_seconds = max(float(cache_seconds), 0.0)
        self._gh_executable = gh_executable
        self._ssh_executable = ssh_executable
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._refreshing = False
        self._last_error: Exception | None = None

    def host_roles(self) -> dict[str, str]:
        return {host["name"]: host["role"] for host in self._hosts}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            cached = self._cached
            expired = cached is None or now - self._cached_at >= self._cache_seconds
            if expired and not self._refreshing:
                self._refreshing = True
                threading.Thread(
                    target=self._refresh,
                    name="owner-control-infrastructure-refresh",
                    daemon=True,
                ).start()
            if cached is None:
                reason = str(self._last_error) if self._last_error else "loading"
                raise RuntimeError(f"infrastructure metrics are {reason}")
            result = deepcopy(cached)
            if expired or self._refreshing or self._last_error is not None:
                result["stale"] = True
                result["alerts"] = int(result.get("alerts") or 0) + 1
                for host in result.get("hosts") or []:
                    if isinstance(host, dict) and str(
                        host.get("status") or "online"
                    ).casefold() == "online":
                        host["status"] = "stale"
            return result

    def _refresh(self) -> None:
        with self._lock:
            previous = deepcopy(self._cached)
        try:
            value = self._collect(previous)
        except Exception as error:
            with self._lock:
                self._last_error = error
                self._refreshing = False
            return
        with self._lock:
            self._cached = value
            self._cached_at = time.monotonic()
            self._last_error = None
            self._refreshing = False

    def _collect(self, previous: dict[str, Any] | None) -> dict[str, Any]:
        alerts = 0
        stale = False
        runners, jobs, queued_jobs = self._github_state()
        cached_hosts = {
            str(host.get("name")): host
            for host in ((previous or {}).get("hosts") or [])
            if isinstance(host, dict) and host.get("name")
        }

        hosts: list[dict[str, Any]] = []
        for configured in self._hosts:
            host_runners = [
                runners[name]
                for name in configured["runner_names"]
                if name in runners
            ]
            host_jobs = [
                job
                for name in configured["runner_names"]
                for job in jobs.get(name, [])
            ]
            try:
                metrics = self._host_metrics(configured)
                status = "online"
            except Exception:
                previous = cached_hosts.get(configured["name"])
                if isinstance(previous, dict) and previous.get("cpu_percent") is not None:
                    metrics = {
                        "cpu_percent": previous.get("cpu_percent"),
                        "memory_percent": previous.get("memory_percent"),
                        "memory_usage": previous.get("memory_usage"),
                    }
                    status = "stale"
                else:
                    metrics = {
                        "cpu_percent": None,
                        "memory_percent": None,
                        "memory_usage": None,
                    }
                    status = "degraded" if host_runners else "unavailable"
                alerts += 1
                stale = True
            hosts.append(
                {
                    "name": configured["name"],
                    "kind": "ci",
                    "role": configured["role"],
                    "status": status,
                    **metrics,
                    "runners_busy": sum(bool(runner.get("busy")) for runner in host_runners),
                    "runners_online": sum(
                        str(runner.get("status") or "").casefold() == "online"
                        for runner in host_runners
                    ),
                    "runners_total": len(host_runners),
                    "jobs": host_jobs,
                }
            )
        return {
            "hosts": hosts,
            "capacity": _capacity_by_role(hosts),
            "queued_jobs": queued_jobs,
            "alerts": alerts,
            "stale": stale,
        }

    def _github_state(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], int]:
        runner_pages = self._gh_pages(
            f"repos/{self._repository}/actions/runners?per_page=100"
        )
        runners = {
            str(runner.get("name")): runner
            for page in runner_pages
            for runner in page.get("runners") or []
            if isinstance(runner, dict) and runner.get("name")
        }
        jobs: dict[str, list[dict[str, Any]]] = {}
        queued_jobs = 0
        workflow_runs: list[dict[str, Any]] = []
        for status in ("in_progress", "queued"):
            run_pages = self._gh_pages(
                f"repos/{self._repository}/actions/runs?status={status}&per_page=30"
            )
            workflow_runs.extend(
                run
                for page in run_pages
                for run in (page.get("workflow_runs") or [])
                if isinstance(run, dict)
            )
        for run in workflow_runs:
            if not isinstance(run, dict) or run.get("id") is None:
                continue
            issue = _issue_number(run)
            job_pages = self._gh_pages(
                f"repos/{self._repository}/actions/runs/{run['id']}/jobs?per_page=100"
            )
            for job in (
                job
                for page in job_pages
                for job in (page.get("jobs") or [])
            ):
                if not isinstance(job, dict):
                    continue
                status = str(job.get("status") or "").casefold()
                if status == "queued":
                    queued_jobs += 1
                runner_name = str(job.get("runner_name") or "")
                if status != "in_progress" or not runner_name:
                    continue
                jobs.setdefault(runner_name, []).append(
                    {
                        "issue": issue,
                        "name": job.get("name") or run.get("display_title") or "CI job",
                        "url": job.get("html_url"),
                    }
                )
        return runners, jobs, queued_jobs

    def _gh_pages(self, endpoint: str) -> list[dict[str, Any]]:
        completed = self._run(
            [self._gh_executable, "api", endpoint, "--paginate", "--slurp"],
            timeout=20,
            environment=_gh_cli_environment(),
        )
        value = json.loads(completed.stdout)
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list) and all(isinstance(page, dict) for page in value):
            return value
        raise RuntimeError("GitHub infrastructure response must contain object pages")

    def _host_metrics(self, host: dict[str, Any]) -> dict[str, Any]:
        completed = self._run(
            [
                self._ssh_executable,
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "StrictHostKeyChecking=yes",
                "-i",
                host["identity_file"],
                "-p",
                str(host["port"]),
                f"{host['user']}@{host['host']}",
                _REMOTE_METRICS_COMMAND,
            ],
            timeout=10,
        )
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise RuntimeError("host metrics response must be an object")
        return {
            "cpu_percent": _optional_number(value.get("cpu_percent")),
            "memory_percent": _optional_number(value.get("memory_percent")),
            "memory_usage": value.get("memory_usage"),
        }

    def _run(
        self,
        command: list[str],
        *,
        timeout: int,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        arguments: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "shell": False,
            "check": False,
        }
        if environment is not None:
            arguments["env"] = environment
        completed = self._runner(command, **arguments)
        if completed.returncode != 0:
            raise RuntimeError(f"infrastructure command failed with {completed.returncode}")
        return completed


def _load_hosts(config_path: Path) -> list[dict[str, Any]]:
    value = json.loads(config_path.read_text(encoding="utf-8"))
    raw_hosts = value.get("hosts") if isinstance(value, dict) else None
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise ValueError("infrastructure config requires hosts")
    hosts: list[dict[str, Any]] = []
    for raw in raw_hosts:
        if not isinstance(raw, dict):
            raise ValueError("infrastructure host must be an object")
        name = str(raw.get("name") or "").strip()
        host = str(raw.get("host") or "").strip()
        user = str(raw.get("user") or "").strip()
        identity = Path(str(raw.get("identity_file") or ""))
        runners = raw.get("runner_names")
        role = str(raw.get("role") or "primary-ci").strip().casefold()
        port = raw.get("port", 22)
        if not name or not _SAFE_HOST.fullmatch(host) or not _SAFE_USER.fullmatch(user):
            raise ValueError("infrastructure host identity is invalid")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("infrastructure host port is invalid")
        if not identity.is_absolute():
            raise ValueError("infrastructure identity file must be absolute")
        if not isinstance(runners, list) or not all(
            isinstance(runner, str) and runner for runner in runners
        ):
            raise ValueError("infrastructure runner_names must be strings")
        if role not in {"primary-ci", "control-only"}:
            raise ValueError("infrastructure role must be primary-ci or control-only")
        hosts.append(
            {
                "name": name,
                "host": host,
                "user": user,
                "port": port,
                "identity_file": str(identity),
                "runner_names": list(runners),
                "role": role,
            }
        )
    return hosts


def _gh_cli_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("GITHUB_TOKEN", None)
    environment.pop("GH_TOKEN", None)
    return environment


def _issue_number(run: dict[str, Any]) -> int | None:
    for value in (run.get("head_branch"), run.get("display_title"), run.get("name")):
        match = _ISSUE.search(str(value or ""))
        if match:
            return int(match.group(1))
    return None


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _capacity_by_role(hosts: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    result = {
        "primary_ci": {"busy": 0, "online": 0, "total": 0},
        "control": {"busy": 0, "online": 0, "total": 0},
    }
    for host in hosts:
        key = "control" if host.get("role") == "control-only" else "primary_ci"
        for field in ("busy", "online", "total"):
            result[key][field] += int(host.get(f"runners_{field}") or 0)
    return result
