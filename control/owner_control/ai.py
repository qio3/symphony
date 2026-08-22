from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence


class CodexReadOnly:
    """Text-only Codex adapter with an empty read-only workspace and no control tools."""

    def __init__(
        self,
        *,
        executable: Sequence[str] = ("codex",),
        codex_home: Path,
        timeout_seconds: int = 60,
    ):
        self._executable = list(executable)
        self._codex_home = Path(codex_home)
        self._timeout_seconds = timeout_seconds

    def answer(self, question: str, snapshot: dict[str, Any]) -> str:
        prompt = (
            "You are the read-only Symphony owner control assistant. Answer briefly in the user's language. "
            "Use only the deterministic snapshot below. Do not propose or perform actions, use tools, or infer missing facts.\n\n"
            f"SNAPSHOT:\n{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}\n\n"
            f"QUESTION:\n{question.strip()}"
        )
        with tempfile.TemporaryDirectory(prefix="symphony-owner-ai-") as tempdir:
            root = Path(tempdir)
            schema_path = root / "answer.schema.json"
            output_path = root / "answer.json"
            schema_path.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    }
                ),
                encoding="utf-8",
            )
            command = [
                *self._executable,
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                "-C",
                str(root),
                "-",
            ]
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self._timeout_seconds,
                env=self._safe_environment(),
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"Codex read-only query failed with exit code {completed.returncode}")
            raw = output_path.read_text(encoding="utf-8")
            value = json.loads(raw)
            answer = value.get("answer") if isinstance(value, dict) else None
            if not isinstance(answer, str) or not answer.strip():
                raise RuntimeError("Codex read-only query returned no answer")
            return answer.strip()

    def _safe_environment(self) -> dict[str, str]:
        allowed = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE")
        environment = {key: os.environ[key] for key in allowed if os.environ.get(key)}
        environment["CODEX_HOME"] = str(self._codex_home)
        environment.pop("OPENAI_API_KEY", None)
        return environment
