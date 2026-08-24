from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class StateStore:
    """Small atomic local store for owner intent and notification watermarks."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def intake_active(self) -> bool:
        return bool(self.read().get("intake_active", True))

    def set_intake_active(self, active: bool) -> None:
        self.update({"intake_active": bool(active)})

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._read_unlocked()
            state.update(values)
            self._write_unlocked(state)
            return state

    def _read_unlocked(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"intake_active": True, "notification_fingerprints": []}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"intake_active": False, "notification_fingerprints": []}
        return value if isinstance(value, dict) else {"intake_active": False, "notification_fingerprints": []}

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self._path)
