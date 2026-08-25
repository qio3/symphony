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

    def quarantines(self) -> dict[str, dict[str, Any]]:
        value = self.read().get("system_quarantines")
        return value if isinstance(value, dict) else {}

    def quarantine_for(self, issue: int) -> dict[str, Any] | None:
        if type(issue) is not int or issue <= 0:
            return None
        return self.quarantines().get(str(issue))

    def set_quarantine(self, issue: int, reason: str, quarantined_at: str) -> None:
        if type(issue) is not int or issue <= 0 or not isinstance(reason, str) or not reason or not isinstance(quarantined_at, str) or not quarantined_at:
            raise ValueError("invalid system quarantine")

        with self._lock:
            state = self._read_unlocked()
            quarantines = dict(self.quarantines_from(state))
            quarantines[str(issue)] = {
                "issue": issue,
                "reason": reason,
                "quarantined_at": quarantined_at,
            }
            state["system_quarantines"] = quarantines
            self._write_unlocked(state)

    def clear_quarantine(self, issue: int) -> None:
        if type(issue) is not int or issue <= 0:
            raise ValueError("quarantine issue must be a positive integer")

        with self._lock:
            state = self._read_unlocked()
            quarantines = dict(self.quarantines_from(state))
            if str(issue) not in quarantines:
                return
            quarantines.pop(str(issue), None)
            state["system_quarantines"] = quarantines
            self._write_unlocked(state)

    @staticmethod
    def quarantines_from(state: dict[str, Any]) -> dict[str, Any]:
        value = state.get("system_quarantines")
        return value if isinstance(value, dict) else {}

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
