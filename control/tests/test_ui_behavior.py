from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - browser tests are opt-in
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


@unittest.skipUnless(
    os.environ.get("OWNER_CONTROL_BROWSER_TESTS") == "1" and sync_playwright,
    "set OWNER_CONTROL_BROWSER_TESTS=1 on a host with Playwright for native UI checks",
)
class OwnerControlUiBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "control")
        environment["OWNER_UI_FIXTURE_PORT"] = str(cls.port)
        cls.server = subprocess.Popen(
            [sys.executable, str(ROOT / "control" / "tests" / "ui_fixture_server.py")],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("Owner Control fixture did not start")
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.terminate()
        cls.server.wait(timeout=5)

    def open_page(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"http://127.0.0.1:{self.port}/")
        page.wait_for_selector('[data-issue-row="402"]')
        return page

    def test_coding_task_highlights_code_and_hides_unassociated_delivery_evidence(self):
        page = self.open_page()
        try:
            page.locator('[data-issue-select="402"]').click()
            row = page.locator('[data-issue-row="402"]')
            self.assertEqual(row.locator(".task-stage.is-current").inner_text(), "Code")
            self.assertEqual(row.locator(".evidence-row").inner_text().splitlines(), ["PR —", "CI —", "TEST —"])
        finally:
            page.close()

    def test_expanded_task_shows_phase_timeline_and_time_in_current_stage(self):
        page = self.open_page()
        try:
            page.locator('[data-issue-select="402"]').click()
            row = page.locator('[data-issue-row="402"]')
            self.assertIn("in stage", row.locator(".progress-label").inner_text())
            self.assertEqual(row.locator(".status-timeline-item").count(), 2)
            self.assertIn("Ready for AI", row.locator(".status-timeline-item").nth(0).inner_text())
            self.assertIn("Coding", row.locator(".status-timeline-item").nth(1).inner_text())
        finally:
            page.close()

    def test_wave_and_infrastructure_explain_elapsed_state_and_runner_roles(self):
        page = self.open_page()
        try:
            page.locator('.primary-nav-item[data-page="waves"]').click()
            self.assertIn("in stage", page.locator(".wave-hero h2").inner_text())
            page.locator('.primary-nav-item[data-page="infrastructure"]').click()
            self.assertIn("primary ci", page.locator(".machine-option", has_text="CI_1").inner_text().lower())
            self.assertIn("control only", page.locator(".machine-option", has_text="Backup").inner_text().lower())
            counters = page.locator("#infrastructure-counters").inner_text().lower()
            self.assertIn("ci busy", counters)
            self.assertIn("control busy", counters)
        finally:
            page.close()

    def test_done_usage_distinguishes_unrecorded_history_and_always_shows_complete_progress(self):
        page = self.open_page()
        try:
            page.locator('[data-owner-tab="done"]').click()
            page.locator('#work-sort').select_option('impact')
            rows = page.locator('[data-issue-row]')
            self.assertEqual(rows.nth(0).get_attribute('data-issue-row'), '398')
            self.assertEqual(rows.nth(1).get_attribute('data-issue-row'), '396')
            self.assertEqual(rows.nth(2).get_attribute('data-issue-row'), '397')
            self.assertEqual(
                page.locator('[data-issue-row="397"] .week-impact').inner_text(),
                '—\nNo recorded Symphony usage',
            )
            for issue in ('396', '397', '398'):
                self.assertIn(
                    '100%',
                    page.locator(f'[data-issue-row="{issue}"] .progress-label').inner_text(),
                )
            page.locator('[data-issue-select="397"]').click()
            usage = page.locator('[data-issue-row="397"] .usage-row').inner_text()
            self.assertEqual(usage, 'No recorded Symphony usage')
            self.assertNotIn('tokens', usage)
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
