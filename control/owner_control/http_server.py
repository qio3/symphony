from __future__ import annotations

import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .actions import ActionError, ActionService


_ACTIONS = {"run", "accept", "rework", "pause", "resume", "restart"}
_MAX_BODY_BYTES = 16_384


def create_server(
    address: tuple[str, int],
    *,
    token: str,
    snapshot_provider: Callable[[], dict[str, Any]],
    intake_provider: Callable[[], bool],
    action_service: ActionService,
    logs_provider: Callable[[int], list[str]],
) -> ThreadingHTTPServer:
    if len(token) < 32:
        raise ValueError("control token must contain at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        server_version = "SymphonyOwnerControl/1"

        def do_GET(self) -> None:
            if not self._authorized():
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "unauthorized"}})
            parsed = urlsplit(self.path)
            if parsed.path == "/v1/snapshot":
                return self._json(HTTPStatus.OK, snapshot_provider())
            if parsed.path == "/v1/intake":
                return self._json(HTTPStatus.OK, {"active": bool(intake_provider())})
            if parsed.path == "/v1/logs":
                try:
                    tail = self._tail(parse_qs(parsed.query).get("tail", ["100"])[0])
                except ValueError as error:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_tail", "message": str(error)}})
                return self._json(HTTPStatus.OK, {"lines": logs_provider(tail)})
            return self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def do_POST(self) -> None:
            if not self._authorized():
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "unauthorized"}})
            parsed = urlsplit(self.path)
            prefix = "/v1/actions/"
            action = parsed.path[len(prefix) :] if parsed.path.startswith(prefix) else ""
            if action not in _ACTIONS or "/" in action:
                return self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
            try:
                params = self._request_json()
                result = action_service.execute(action, params)
            except ActionError as error:
                return self._json(HTTPStatus.CONFLICT, {"error": {"code": "action_rejected", "message": str(error)}})
            except (ValueError, json.JSONDecodeError) as error:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_request", "message": str(error)}})
            except Exception as error:
                return self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": {"code": "action_failed", "message": str(error)}},
                )
            return self._json(HTTPStatus.ACCEPTED, result)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

        def _request_json(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0")
            length = int(raw_length)
            if length < 0 or length > _MAX_BODY_BYTES:
                raise ValueError("request body is too large")
            if length == 0:
                return {}
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def _json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        @staticmethod
        def _tail(value: str) -> int:
            if not value.isdigit():
                raise ValueError("tail must be numeric")
            return min(max(int(value), 1), 500)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return ThreadingHTTPServer(address, Handler)
