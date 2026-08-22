from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any

from .state_store import StateStore
from .telegram import NotificationDetector, TelegramCommandHandler, update_is_allowed


class TelegramApi:
    def __init__(self, *, token: str, owner_chat_id: int):
        self._base_url = f"https://api.telegram.org/bot{token}/"
        self._owner_chat_id = owner_chat_id

    def get_updates(self, *, offset: int, timeout: int) -> list[dict[str, Any]]:
        value = self._call("getUpdates", offset=offset, timeout=timeout, allowed_updates='["message"]')
        result = value.get("result")
        if not value.get("ok") or not isinstance(result, list):
            raise RuntimeError("Telegram getUpdates failed")
        return result

    def send(self, text: str) -> None:
        value = self._call("sendMessage", chat_id=self._owner_chat_id, text=str(text)[:4000])
        if not value.get("ok"):
            raise RuntimeError("Telegram sendMessage failed")

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        encoded = urllib.parse.urlencode(params, encoding="utf-8").encode("utf-8")
        request = urllib.request.Request(self._base_url + method, data=encoded, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                value = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise RuntimeError(f"Telegram {method} request failed") from error
        if not isinstance(value, dict):
            raise RuntimeError(f"Telegram {method} returned invalid JSON")
        return value


class TelegramBot:
    def __init__(
        self,
        *,
        api: TelegramApi,
        handler: TelegramCommandHandler,
        state_store: StateStore,
        owner_chat_id: int,
        owner_user_id: int,
    ):
        self._api = api
        self._handler = handler
        self._state_store = state_store
        self._owner_chat_id = owner_chat_id
        self._owner_user_id = owner_user_id

    def poll_once(self, *, timeout: int = 15) -> None:
        offset = int(self._state_store.read().get("telegram_offset", 0))
        updates = self._api.get_updates(offset=offset, timeout=timeout)
        next_offset = offset
        for update in updates:
            update_id = int(update.get("update_id", -1))
            next_offset = max(next_offset, update_id + 1)
            if not update_is_allowed(
                update,
                owner_chat_id=self._owner_chat_id,
                owner_user_id=self._owner_user_id,
            ):
                continue
            text = str((update.get("message") or {}).get("text") or "")
            try:
                reply = self._handler.handle(text)
            except Exception:
                reply = "Owner Control is temporarily unavailable."
            self._api.send(reply)
        if next_offset != offset:
            self._state_store.update({"telegram_offset": next_offset})


class NotificationPublisher:
    def __init__(
        self,
        *,
        api: TelegramApi,
        state_store: StateStore,
        detector: NotificationDetector,
    ):
        self._api = api
        self._state_store = state_store
        self._detector = detector

    def publish(self, snapshot: dict[str, Any]) -> None:
        state = self._state_store.read()
        previous = state.get("last_notification_snapshot")
        known = set(state.get("notification_fingerprints") or [])
        events = self._detector.detect(previous, snapshot)
        expected_restart = float(state.get("expected_service_restart_until") or 0)
        suppress_service_stop = (
            expected_restart > time.time() and not bool((snapshot.get("service") or {}).get("live"))
        )
        for event in events:
            if event["kind"] == "service_stopped" and suppress_service_stop:
                continue
            fingerprint = event["fingerprint"]
            if fingerprint in known:
                continue
            self._api.send(event["text"])
            known.add(fingerprint)
        projection = self._notification_projection(snapshot)
        if suppress_service_stop and previous:
            projection["service"] = previous.get("service") or {"live": True}
        self._state_store.update(
            {
                "last_notification_snapshot": projection,
                "notification_fingerprints": sorted(known)[-500:],
            }
        )

    @staticmethod
    def _notification_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
        owner = snapshot.get("owner_view") or {}
        return {
            "service": snapshot.get("service") or {},
            "owner_view": {
                "blocked": owner.get("blocked") or [],
                "ready_for_acceptance": owner.get("ready_for_acceptance") or [],
            },
            "failures": snapshot.get("failures") or [],
        }
