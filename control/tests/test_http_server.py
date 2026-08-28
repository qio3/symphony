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
        self.internal_calls = []
        self.error = None

    def execute(self, action, params):
        if self.error is not None:
            raise self.error
        self.calls.append((action, params))
        return {"status": "accepted", "action": action}

    def execute_internal(self, action, params):
        if self.error is not None:
            raise self.error
        self.internal_calls.append((action, params))
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
        with self.request("/assets/theme-init.js", authorized=False) as response:
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
        with self.request("/assets/owner-control.js", authorized=False) as response:
            self.assertEqual(response.headers.get_content_type(), "text/javascript")

    def test_serves_chartjs_locally_before_the_owner_client(self):
        html, _csrf, headers = self.browser_session()

        theme_src = 'src="/assets/theme-init.js"'
        chart_src = 'src="/assets/chart.umd.min.js"'
        owner_src = 'src="/assets/owner-control.js"'
        self.assertIn(theme_src, html)
        self.assertIn(chart_src, html)
        self.assertIn(owner_src, html)
        self.assertLess(html.index(theme_src), html.index('rel="stylesheet"'))
        self.assertLess(html.index(chart_src), html.index(owner_src))
        self.assertNotRegex(html, r'https?://[^"\']*(?:chart|cdn)')
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

        with self.request("/assets/chart.umd.min.js", authorized=False) as response:
            chart = response.read().decode("utf-8")
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
        self.assertIn("Chart", chart)

    def test_serves_lucide_locally_before_the_owner_client(self):
        html, _csrf, headers = self.browser_session()

        icon_src = 'src="/assets/lucide.min.js"'
        owner_src = 'src="/assets/owner-control.js"'
        self.assertIn(icon_src, html)
        self.assertLess(html.index(icon_src), html.index(owner_src))
        self.assertNotRegex(html, r'https?://[^"\']*(?:lucide|cdn)')
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

        with self.request("/assets/lucide.min.js", authorized=False) as response:
            javascript = response.read().decode("utf-8")
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
        self.assertIn("createIcons", javascript)

    def test_owner_shell_exposes_primary_pages_and_compact_shared_range(self):
        html, _csrf, _headers = self.browser_session()

        for token in (
            'id="primary-nav"',
            'data-page="overview"',
            'data-page="work"',
            'data-page="review"',
            'data-page="waves"',
            'data-page="infrastructure"',
            'data-page="owner"',
            'data-page="quarantine"',
            'data-page="runtime"',
            'id="history-range"',
            'data-history-range="60m"',
            'data-history-range="12h"',
            'data-history-range="24h"',
            'data-history-range="3d"',
            'id="overview-status-chart"',
            'id="overview-history-state"',
            'id="infrastructure-chart"',
        ):
            self.assertIn(token, html)

        with self.request("/assets/owner-control.css", authorized=False) as response:
            css = response.read().decode("utf-8")
        self.assertRegex(css, r"\.history-range\s*\{[^}]*width:\s*fit-content")
        self.assertIn(".history-range[hidden] { display: none; }", css)
        self.assertRegex(css, r"@media \(max-width:\s*760px\)[\s\S]*\.history-range-buttons\s*\{[^}]*display:\s*none")

    def test_owner_shell_uses_the_approved_v5_visual_contract(self):
        html, _csrf, _headers = self.browser_session()

        self.assertRegex(html, r'id="page-overview"[^>]*\bhidden\b')
        self.assertRegex(html, r'id="page-work"[^>]*data-page="work"(?![^>]*\bhidden\b)')
        self.assertIn('id="page-subtitle"', html)
        self.assertIn('id="work-summary"', html)
        self.assertIn('id="work-search"', html)
        self.assertIn('id="work-model-filter"', html)
        self.assertIn('id="work-stage-filter"', html)
        self.assertIn('id="work-sort"', html)
        self.assertIn('id="header-service-status"', html)
        self.assertIn('id="header-service-actions"', html)

        with self.request("/assets/owner-control.css", authorized=False) as response:
            css = response.read().decode("utf-8")
        self.assertIn("--oc-bg: #edf1f4", css)
        self.assertIn("grid-template-columns: 205px minmax(0, 1fr)", css)
        self.assertIn("font-size: 11px", css)
        self.assertRegex(css, r"\.issue-table tbody\s*\{[^}]*display:\s*grid")

        with self.request("/assets/owner-control.js", authorized=False) as response:
            javascript = response.read().decode("utf-8")
        self.assertIn('const THEME_STORAGE_KEY = "owner-control-v5-theme"', javascript)
        self.assertIn('let activePage = "work"', javascript)
        self.assertIn('activatePage("work")', javascript)
        self.assertIn("function taskInlineDetails", javascript)
        self.assertIn("function filteredWorkItems", javascript)
        self.assertIn("function workWeekImpact", javascript)

    def test_owner_shell_has_theme_tabs_table_chart_and_detail_drawer(self):
        html, _csrf, _headers = self.browser_session()

        for token in (
            'id="owner-header"',
            'id="theme-toggle"',
            'id="owner-tabs"',
            'role="tablist"',
            'id="owner-work-chart"',
            'id="issue-table"',
            'id="issue-table-body"',
            'id="issue-drawer"',
            'id="issue-drawer-close"',
            'id="runtime-diagnostics"',
        ):
            self.assertIn(token, html)
        self.assertNotRegex(
            html,
            r'<details[^>]*id="runtime-diagnostics"[^>]*\bopen\b',
        )

        with self.request("/assets/owner-control.css", authorized=False) as response:
            css = response.read().decode("utf-8")
        self.assertRegex(css, r"#owner-header\s*\{[^}]*position:\s*sticky")
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn(".issue-table-wrap", css)
        self.assertRegex(css, r"@media \(max-width:\s*760px\)[\s\S]*#issue-drawer")
        self.assertRegex(
            css,
            r"@media \(max-width:\s*760px\)[\s\S]*\.owner-tabs\s*\{[^}]*grid-template-columns:\s*repeat\(2",
        )
        self.assertRegex(
            css,
            r"@media \(max-width:\s*760px\)[\s\S]*#issue-drawer\s*\{[^}]*top:\s*auto",
        )

    def test_owner_client_persists_theme_and_projects_one_selectable_work_table(self):
        with self.request("/assets/owner-control.js", authorized=False) as response:
            javascript = response.read().decode("utf-8")

        self.assertIn('const THEME_STORAGE_KEY = "owner-control-v5-theme"', javascript)
        self.assertIn("localStorage.getItem(THEME_STORAGE_KEY)", javascript)
        self.assertIn("localStorage.setItem(THEME_STORAGE_KEY", javascript)
        self.assertIn("document.documentElement.dataset.theme", javascript)
        self.assertIn("new Chart(", javascript)
        self.assertIn("snapshot.counts", javascript)
        self.assertIn("data-owner-tab", javascript)
        self.assertIn("data-issue-select", javascript)
        self.assertIn("data-issue-row", javascript)
        self.assertIn("aria-selected", javascript)
        self.assertIn("issue-drawer", javascript)
        self.assertIn("ownerTabs.addEventListener(\"keydown\"", javascript)
        self.assertIn("ArrowRight", javascript)
        self.assertIn("tab.tabIndex", javascript)
        self.assertIn('setAttribute("aria-labelledby"', javascript)
        self.assertIn('fetch(`/ui/actions/${action}`', javascript)
        self.assertNotIn("/ui/actions/shell", javascript)

    def test_owner_client_projects_overview_pages_history_and_escalations_from_snapshot(self):
        with self.request("/assets/owner-control.js", authorized=False) as response:
            javascript = response.read().decode("utf-8")

        for behavior in (
            "function activatePage",
            "function setHistoryRange",
            "function renderOverview",
            "function renderReview",
            "function renderReleaseWaves",
            "function renderInfrastructure",
            "function renderAttentionPages",
            "function escalationChain",
        ):
            self.assertIn(behavior, javascript)
        self.assertIn("snapshot.history", javascript)
        self.assertIn("snapshot.release_waves", javascript)
        self.assertIn("snapshot.infrastructure", javascript)
        self.assertIn('memory_percent', javascript)
        self.assertIn('borderDash', javascript)
        self.assertIn("Release wave data unavailable", javascript)
        self.assertIn("Infrastructure metrics unavailable", javascript)
        self.assertIn('["running", "Active"', javascript)
        self.assertIn('"Waiting delivery"', javascript)
        self.assertIn('event.target.closest("button[data-page]")', javascript)
        self.assertNotIn('event.target.closest("[data-page]")', javascript)
        self.assertIn("function hasVerifiedIssueDelivery", javascript)
        self.assertIn("item.test?.merge_sha === mergeSha", javascript)
        self.assertIn('item.lane === "running"', javascript)
        self.assertIn("function optionalNumber", javascript)
        self.assertIn("overviewHistoryState.textContent", javascript)
        self.assertIn("function workerLimitControl", javascript)
        self.assertIn('fetch("/ui/actions/set_workers"', javascript)
        self.assertIn("function workStageKey", javascript)
        self.assertIn('phase.includes("merge")', javascript)
        self.assertIn('return "landing"', javascript)
        self.assertIn('const coreSourceNames = new Set(["supervisor", "runtime", "github", "test"])', javascript)
        self.assertIn("coreStaleSources", javascript)

    def test_service_action_menu_stacks_above_service_facts(self):
        with self.request("/assets/owner-control.css", authorized=False) as response:
            css = response.read().decode("utf-8")

        self.assertRegex(
            css,
            r"\.service-panel \.section-heading\s*\{[^}]*z-index:\s*2",
        )
        self.assertRegex(
            css,
            r"\.service-panel \.service-facts\s*\{[^}]*z-index:\s*1",
        )

    def test_runtime_source_errors_wrap_on_narrow_viewports(self):
        with self.request("/assets/owner-control.css", authorized=False) as response:
            css = response.read().decode("utf-8")

        self.assertRegex(css, r"\.source-row\s*\{[^}]*flex-wrap:\s*wrap")
        self.assertRegex(css, r"\.source-error\s*\{[^}]*overflow-wrap:\s*anywhere")

    def test_infrastructure_cards_visibly_mark_last_confirmed_metrics(self):
        with self.request("/assets/owner-control.js", authorized=False) as response:
            javascript = response.read().decode("utf-8")

        self.assertIn('host.status || "online"', javascript)
        self.assertIn("Last confirmed metrics", javascript)
        self.assertIn("suggestedMax: 100", javascript)
        self.assertNotIn("beginAtZero: true, max: 100", javascript)

    def test_workbench_has_a_bounded_expandable_table(self):
        with self.request("/assets/owner-control.js", authorized=False) as response:
            javascript = response.read().decode("utf-8")
        with self.request("/assets/owner-control.css", authorized=False) as response:
            css = response.read().decode("utf-8")

        self.assertIn("const TABLE_PAGE_SIZE = 12", javascript)
        self.assertIn("items.slice(0, visibleRows)", javascript)
        self.assertIn("visibleRows += TABLE_PAGE_SIZE", javascript)
        self.assertIn(".issue-table-wrap", css)

    def test_needs_owner_distinguishes_system_quarantine_from_owner_questions(self):
        with self.request("/assets/owner-control.js", authorized=False) as response:
            javascript = response.read().decode("utf-8")
        with self.request("/assets/owner-control.css", authorized=False) as response:
            css = response.read().decode("utf-8")

        work_items = javascript.split("function workItems", 1)[1].split(
            "function findWorkbenchItem", 1
        )[0]
        drawer = javascript.split("function renderDrawer", 1)[1].split(
            "function renderWorkChart", 1
        )[0]
        self.assertIn("system_quarantines", work_items)
        self.assertIn('lane: "quarantine"', work_items)
        self.assertIn('"System quarantine"', drawer)
        self.assertIn('actionButton("Start", "run"', drawer)
        self.assertIn(".state-cell.quarantine", css)

    def test_loaded_summary_and_running_rows_expose_owner_facts_without_skeletons(self):
        with self.request("/assets/owner-control.js", authorized=False) as response:
            javascript = response.read().decode("utf-8")

        render = javascript.split("function render(snapshot", 1)[1].split(
            "function renderFreshness", 1
        )[0]
        row = javascript.split("function issueTableRow", 1)[1].split(
            "function renderDrawer", 1
        )[0]
        self.assertIn('classList.remove("loading-block")', render)
        self.assertIn("modelName(item.model)", row)
        self.assertIn("elapsed(item.started_at)", row)
        self.assertIn("item.turn_count", row)
        self.assertIn("item.usage?.total_tokens", row)

    def test_drawer_can_stay_closed_and_legacy_card_renderers_are_removed(self):
        with self.request("/assets/owner-control.js", authorized=False) as response:
            javascript = response.read().decode("utf-8")

        self.assertIn("let drawerDismissed = false", javascript)
        self.assertIn("drawerDismissed = true", javascript)
        self.assertIn("issueDrawer.inert", javascript)
        self.assertNotIn("function renderCounters", javascript)
        self.assertNotIn("function renderBlocked", javascript)
        self.assertNotIn("function renderRunning", javascript)
        self.assertNotIn("function renderReady", javascript)
        self.assertNotIn("function renderBacklog", javascript)

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
        with self.request(
            "/v1/actions/lease", method="POST", body={"issue": 401}
        ) as response:
            self.assertEqual(json.load(response)["action"], "lease")
        self.assertEqual(self.actions.calls, [("pause", {}), ("lease", {"issue": 401})])

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/v1/actions/shell", method="POST", body={"command": "whoami"})
        self.assertEqual(raised.exception.code, 404)

    def test_internal_runtime_actions_are_bearer_only_and_not_public_or_browser_routes(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/v1/actions/complete_run", method="POST", body={"issue": 401})
        self.assertEqual(raised.exception.code, 404)

        _html, csrf, _headers = self.browser_session()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/ui/actions/complete_run",
                method="POST",
                body={"issue": 401},
                authorized=False,
                headers={"Origin": self.base_url, "X-Owner-Control-CSRF": csrf},
            )
        self.assertEqual(raised.exception.code, 404)

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/v1/internal/actions/complete_run",
                method="POST",
                body={"issue": 401},
                authorized=False,
            )
        self.assertEqual(raised.exception.code, 401)

        with self.request(
            "/v1/internal/actions/complete_run",
            method="POST",
            body={"issue": 401},
        ) as response:
            self.assertEqual(json.load(response)["action"], "complete_run")

        self.assertEqual(self.actions.calls, [])
        self.assertEqual(self.actions.internal_calls, [("complete_run", {"issue": 401})])

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

    def test_internal_retryable_actions_have_a_typed_service_response(self):
        from owner_control.actions import RetryableActionError

        self.actions.error = RetryableActionError("runtime is still active")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/v1/internal/actions/complete_run",
                method="POST",
                body={"issue": 401},
            )

        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(
            json.load(raised.exception)["error"],
            {"code": "retryable", "message": "runtime is still active"},
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
