from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateStore:
    """Small atomic local store for owner intent and notification watermarks."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._mutation_path = self._path.with_name(
            self._path.stem + ".mutations.jsonl"
        )
        self._lock = threading.Lock()

    def intake_active(self) -> bool:
        return bool(self.read().get("intake_active", True))

    def set_intake_active(self, active: bool) -> None:
        self.update({"intake_active": bool(active)})

    def worker_limit(self, default: int) -> int:
        value = self.read().get("worker_limit")
        return value if type(value) is int and 0 < value <= default else default

    def set_worker_limit(self, limit: int, *, maximum: int) -> None:
        if type(limit) is not int or type(maximum) is not int or not 1 <= limit <= maximum:
            raise ValueError(f"worker limit must be between 1 and {maximum}")
        self.update({"worker_limit": limit})

    def quarantines(self) -> dict[str, dict[str, Any]]:
        value = self.read().get("system_quarantines")
        return value if isinstance(value, dict) else {}

    def status_history(self) -> list[dict[str, Any]]:
        value = self.read().get("status_history")
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def record_status_sample(
        self,
        sample: dict[str, Any],
        *,
        minimum_interval_seconds: int = 60,
        retention_seconds: int = 3 * 24 * 60 * 60,
    ) -> list[dict[str, Any]]:
        recorded_at = _timestamp(sample.get("recorded_at"))
        if recorded_at is None:
            raise ValueError("status sample requires an ISO recorded_at timestamp")
        normalized = {
            "recorded_at": sample["recorded_at"],
            "counts": dict(sample.get("counts") or {}),
            "workers": dict(sample.get("workers") or {}),
        }
        cutoff = recorded_at - max(int(retention_seconds), 0)

        with self._lock:
            state = self._read_unlocked()
            raw_history = state.get("status_history")
            history = []
            for item in raw_history if isinstance(raw_history, list) else []:
                timestamp = _timestamp(item.get("recorded_at")) if isinstance(item, dict) else None
                if timestamp is not None and cutoff <= timestamp <= recorded_at:
                    history.append(item)
            history.sort(key=lambda item: _timestamp(item.get("recorded_at")) or 0)

            previous_at = _timestamp(history[-1].get("recorded_at")) if history else None
            if previous_at is None or recorded_at - previous_at >= max(int(minimum_interval_seconds), 1):
                history.append(normalized)

            if history != raw_history:
                state["status_history"] = history
                self._write_unlocked(state)
            return history

    def record_infrastructure_sample(
        self,
        sample: dict[str, Any],
        *,
        minimum_interval_seconds: int = 60,
        retention_seconds: int = 3 * 24 * 60 * 60,
    ) -> list[dict[str, Any]]:
        recorded_at = _timestamp(sample.get("recorded_at"))
        if recorded_at is None:
            raise ValueError("infrastructure sample requires an ISO recorded_at timestamp")
        normalized = {
            "recorded_at": sample["recorded_at"],
            "hosts": json.loads(json.dumps(sample.get("hosts") or {})),
        }
        cutoff = recorded_at - max(int(retention_seconds), 0)
        with self._lock:
            state = self._read_unlocked()
            raw_history = state.get("infrastructure_history")
            history = []
            for item in raw_history if isinstance(raw_history, list) else []:
                timestamp = _timestamp(item.get("recorded_at")) if isinstance(item, dict) else None
                if timestamp is not None and cutoff <= timestamp <= recorded_at:
                    history.append(item)
            history.sort(key=lambda item: _timestamp(item.get("recorded_at")) or 0)
            previous_at = _timestamp(history[-1].get("recorded_at")) if history else None
            if previous_at is None or recorded_at - previous_at >= max(int(minimum_interval_seconds), 1):
                history.append(normalized)
            if history != raw_history:
                state["infrastructure_history"] = history
                self._write_unlocked(state)
            return history

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

    def append_mutation(self, entry: dict[str, Any]) -> None:
        normalized = {
            **dict(entry),
            "timestamp": entry.get("timestamp")
            or datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._mutation_path.parent.mkdir(parents=True, exist_ok=True)
            with self._mutation_path.open("a", encoding="utf-8") as journal:
                journal.write(json.dumps(normalized, ensure_ascii=False) + "\n")
                journal.flush()
                os.fsync(journal.fileno())

    def mutation_journal(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                lines = self._mutation_path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
        entries = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                entries.append(value)
        return entries

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


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
