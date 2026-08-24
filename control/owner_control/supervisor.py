from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable


class DockerComposeSupervisor:
    """Fixed-target service operations; no caller-provided command fragments."""

    def __init__(
        self,
        *,
        compose_file: Path,
        container_name: str,
        service_name: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self._compose_file = str(Path(compose_file))
        self._container_name = container_name
        self._service_name = service_name
        self._runner = runner

    def status(self) -> dict[str, Any]:
        completed = self._run(
            ["docker", "inspect", self._container_name, "--format", "{{json .State}}"],
            timeout=10,
            tolerate_failure=True,
        )
        if completed.returncode != 0:
            return {"live": False, "container": self._container_name, "reason": "container unavailable"}
        try:
            state = json.loads(completed.stdout.strip())
        except json.JSONDecodeError:
            return {"live": False, "container": self._container_name, "reason": "invalid container state"}
        return {
            "live": bool(state.get("Running")),
            "container": self._container_name,
            "status": state.get("Status"),
            "started_at": state.get("StartedAt"),
            "restart_count": state.get("RestartCount", 0),
        }

    def restart(self) -> dict[str, Any]:
        self._run(self._compose_command("restart", self._service_name), timeout=60)
        return {"accepted": True, "container": self._container_name}

    def start(self) -> dict[str, Any]:
        self._run(
            self._compose_command("up", "-d", "--no-deps", self._service_name),
            timeout=60,
        )
        return {"accepted": True, "container": self._container_name}

    def stop(self) -> dict[str, Any]:
        self._run(self._compose_command("stop", self._service_name), timeout=60)
        return {"accepted": True, "container": self._container_name}

    def logs(self, tail: int) -> list[str]:
        safe_tail = min(max(int(tail), 1), 500)
        completed = self._run(
            self._compose_command("logs", "--tail", str(safe_tail), self._service_name),
            timeout=15,
        )
        return completed.stdout.splitlines()[-safe_tail:]

    def _compose_command(self, *arguments: str) -> list[str]:
        return ["docker", "compose", "-f", self._compose_file, *arguments]

    def _run(
        self,
        command: list[str],
        *,
        timeout: int,
        tolerate_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        completed = self._runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
        if completed.returncode != 0 and not tolerate_failure:
            raise RuntimeError(f"fixed service command failed with exit code {completed.returncode}")
        return completed
