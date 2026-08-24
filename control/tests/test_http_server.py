import json
import re
import threading
import unittest
import urllib.error
import urllib.request

from owner_control.http_server import create_server


class FakeActions:
    def __init__(self):
        self.calls = []
        self.error = None

    def execute(self, action, params):
        if self.error is not None:
            raise self.error
        self.calls.append((action, params))
        return {"status": "accepted", "action": action}


class ControlHttpServerTest(unittest.TestCase):
    def setUp(self):
        self.actions = FakeActions()
        self.server = create_server(
            ("127.0.0.1", 0),
            token="a" * 32,
            snapshot_provider=lambda: {"version": 1, "counts": {"running": 1}},
            intake_provider=lambda: True,
            action_service=self.actions,
            logs_provider=lambda tail: [f"line-{tail}"],
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, *, method="GET", body=None, authorized=True, headers=None):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if authorized:
            request_headers["Authorization"] = f"Bearer {'a' * 32}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        return urllib.request.urlopen(
            urllib.request.Request(self.base_url + path, data=data, headers=request_headers, method=method),
            timeout=3,
        )

    def browser_session(self):
        with self.request("/", authorized=False) as response:
            html = response.read().decode("utf-8")
            csrf = re.search(r'<meta name="owner-control-csrf" content="([^"]+)"', html)
            self.assertIsNotNone(csrf)
            return html, csrf.group(1), response.headers

    def test_requires_bearer_auth_even_for_snapshot(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/v1/snapshot", authorized=False)
        self.assertEqual(raised.exception.code, 401)

        with self.request("/v1/snapshot") as response:
            self.assertEqual(json.load(response)["counts"]["running"], 1)

        with self.request("/v1/intake") as response:
            self.assertEqual(json.load(response), {"active": True})

    def test_serves_the_owner_ui_without_exposing_the_control_token(self):
        html, _csrf, headers = self.browser_session()

        self.assertIn("Owner Control", html)
        self.assertIn("Service", html)
        self.assertIn("Codex quota", html)
        self.assertNotIn("a" * 32, html)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        with self.request("/assets/owner-control.css", authorized=False) as response:
            self.assertEqual(response.headers.get_content_type(), "text/css")
        with self.request("/assets/owner-control.js", authorized=False) as response:
            self.assertEqual(response.headers.get_content_type(), "text/javascript")

    def test_service_action_menu_stacks_above_service_facts(self):
        with self.request("/assets/owner-control.css", authorized=False) as response:
            css = response.read().decode("utf-8")

        self.assertRegex(
            css,
            r"\.service-panel \.section-heading\s*\{[^}]*z-index:\s*2",
        )

    def test_browser_snapshot_and_actions_require_same_origin_csrf(self):
        _html, csrf, _headers = self.browser_session()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/ui/snapshot", authorized=False)
        self.assertEqual(raised.exception.code, 401)

        browser_headers = {
            "Origin": self.base_url,
            "X-Owner-Control-CSRF": csrf,
        }
        with self.request(
            "/ui/snapshot", authorized=False, headers=browser_headers
        ) as response:
            self.assertEqual(json.load(response)["counts"]["running"], 1)

        fetch_metadata_headers = {
            "Sec-Fetch-Site": "same-origin",
            "X-Owner-Control-CSRF": csrf,
        }
        with self.request(
            "/ui/snapshot", authorized=False, headers=fetch_metadata_headers
        ) as response:
            self.assertEqual(json.load(response)["counts"]["running"], 1)

        with self.request(
            "/ui/actions/pause",
            method="POST",
            body={},
            authorized=False,
            headers=browser_headers,
        ) as response:
            self.assertEqual(json.load(response)["action"], "pause")
        self.assertEqual(self.actions.calls, [("pause", {})])

    def test_exposes_only_typed_action_routes(self):
        with self.request("/v1/actions/pause", method="POST", body={}) as response:
            self.assertEqual(json.load(response)["action"], "pause")
        self.assertEqual(self.actions.calls, [("pause", {})])

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/v1/actions/shell", method="POST", body={"command": "whoami"})
        self.assertEqual(raised.exception.code, 404)

    def test_exposes_service_actions_and_action_rejections(self):
        with self.request("/v1/actions/start_service", method="POST", body={}) as response:
            self.assertEqual(json.load(response)["action"], "start_service")
        self.assertEqual(self.actions.calls, [("start_service", {})])

        from owner_control.actions import ActionError

        self.actions.error = ActionError("confirm_running_workers must match 2")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/v1/actions/stop_service", method="POST", body={})

        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(
            json.load(raised.exception)["error"]["code"],
            "action_rejected",
        )

    def test_logs_tail_is_numeric_and_bounded(self):
        with self.request("/v1/logs?tail=99999") as response:
            self.assertEqual(json.load(response), {"lines": ["line-500"]})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/v1/logs?tail=not-a-number")
        self.assertEqual(raised.exception.code, 400)

    def test_action_failures_return_a_machine_readable_service_error(self):
        self.actions.error = RuntimeError("GitHub project unavailable")

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/v1/actions/accept", method="POST", body={"issue": 402})

        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(
            json.load(raised.exception)["error"],
            {"code": "action_failed", "message": "GitHub project unavailable"},
        )


if __name__ == "__main__":
    unittest.main()
