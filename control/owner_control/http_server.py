from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .actions import ActionError, ActionService, RetryableActionError


_ACTIONS = {
    "run",
    "lease",
    "accept",
    "rework",
    "pause",
    "resume",
    "start_service",
    "stop_service",
    "restart",
}
_INTERNAL_ACTIONS = {"complete_run", "quarantine_before_run"}
_MAX_BODY_BYTES = 16_384
_ASSET_ROOT = Path(__file__).with_name("web")
_ASSETS = {
    "/assets/theme-init.js": ("theme-init.js", "text/javascript; charset=utf-8"),
    "/assets/owner-control.css": ("owner-control.css", "text/css; charset=utf-8"),
    "/assets/chart.umd.min.js": ("chart.umd.min.js", "text/javascript; charset=utf-8"),
    "/assets/owner-control.js": ("owner-control.js", "text/javascript; charset=utf-8"),
}
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def create_server(
    address: tuple[str, int],
    *,
    token: str,
    snapshot_provider: Callable[[], dict[str, Any]],
    intake_provider: Callable[[], bool],
    action_service: ActionService,
    logs_provider: Callable[[int], list[str]],
    runtime_diagnostics_url: str = "http://127.0.0.1:4082/",
) -> ThreadingHTTPServer:
    if len(token) < 32:
        raise ValueError("control token must contain at least 32 characters")

    browser_csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SymphonyOwnerControl/1"

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                html = (_ASSET_ROOT / "index.html").read_text(encoding="utf-8")
                html = html.replace("{{OWNER_CONTROL_CSRF}}", browser_csrf).replace(
                    "{{RUNTIME_DIAGNOSTICS_URL}}", runtime_diagnostics_url
                )
                return self._bytes(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")
            if parsed.path in _ASSETS:
                filename, content_type = _ASSETS[parsed.path]
                return self._bytes(
                    HTTPStatus.OK,
                    (_ASSET_ROOT / filename).read_bytes(),
                    content_type,
                )
            if parsed.path == "/ui/snapshot":
                if not self._browser_authorized(browser_csrf):
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "unauthorized"}})
                return self._json(HTTPStatus.OK, snapshot_provider())
            if parsed.path == "/ui/logs":
                if not self._browser_authorized(browser_csrf):
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "unauthorized"}})
                try:
                    tail = self._tail(parse_qs(parsed.query).get("tail", ["100"])[0])
                except ValueError as error:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_tail", "message": str(error)}})
                return self._json(HTTPStatus.OK, {"lines": logs_provider(tail)})
            if not self._authorized():
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "unauthorized"}})
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
            parsed = urlsplit(self.path)

            if parsed.path.startswith("/v1/internal/actions/"):
                if not self._authorized():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "unauthorized"}})
                action = parsed.path[len("/v1/internal/actions/") :]
                if action not in _INTERNAL_ACTIONS or "/" in action:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                try:
                    result = action_service.execute_internal(action, self._request_json())
                except RetryableActionError as error:
                    return self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": {"code": "retryable", "message": str(error)}},
                    )
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

            browser_request = parsed.path.startswith("/ui/actions/")
            if browser_request:
                if not self._browser_authorized(browser_csrf):
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "unauthorized"}})
                prefix = "/ui/actions/"
            else:
                if not self._authorized():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "unauthorized"}})
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

        def _browser_authorized(self, csrf: str) -> bool:
            try:
                loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                loopback = False
            supplied = self.headers.get("X-Owner-Control-CSRF", "")
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin", "")
            referer = self.headers.get("Referer", "")
            fetch_site = self.headers.get("Sec-Fetch-Site", "")
            same_origin = (
                origin == expected_origin
                or referer.startswith(expected_origin + "/")
                or fetch_site == "same-origin"
            )
            return (
                loopback
                and same_origin
                and hmac.compare_digest(supplied.encode("utf-8"), csrf.encode("utf-8"))
            )

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
            self._bytes(status, encoded, "application/json; charset=utf-8")

        def _bytes(self, status: HTTPStatus, encoded: bytes, content_type: str) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _CSP)
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
