"""UI smoke test against a running console and API.

Skipped unless both servers answer, so `make test` stays runnable without the full stack
while CI (`make check` / the ui job) exercises it for real.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import Page, sync_playwright

CONSOLE_URL = os.environ.get("DRONEOPS_CONSOLE_URL", "http://127.0.0.1:4173")
API_HEALTH_URL = os.environ.get("DRONEOPS_API_HEALTH_URL", "http://127.0.0.1:8000/health")
BANNER = "Simulation only. Not authorised for flight."

pytestmark = pytest.mark.e2e


def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return bool(200 <= response.status < 400)
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture
def page() -> Page:
    if not (_reachable(CONSOLE_URL + "/") and _reachable(API_HEALTH_URL)):
        pytest.skip("console preview or API is not running; start them with `make console` and `make api`")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        session = browser.new_page()
        try:
            yield session
        finally:
            browser.close()


def test_console_shows_the_mandatory_safety_banner(page: Page) -> None:
    page.goto(CONSOLE_URL, wait_until="load")
    banners = page.locator(".safety-banner")
    assert banners.count() >= 1
    assert BANNER in (banners.first.inner_text() or "")
    assert page.locator("h1").inner_text().strip() != ""


def test_console_reports_no_flight_authority(page: Page) -> None:
    page.goto(CONSOLE_URL, wait_until="load")
    page.wait_for_selector("#status-list dd")
    body = page.inner_text("body")
    assert "authorised for flight" in body.lower()
    assert "no" in page.locator("#status-list").inner_text().lower()


def test_console_offers_no_command_affordance(page: Page) -> None:
    page.goto(CONSOLE_URL, wait_until="load")
    forbidden = ("launch", "takeoff", "land", "return to launch", "rtl", "arm", "send command", "upload mission")
    labels = [
        (element.text_content() or "").strip().lower()
        for element in page.locator("button, a, input[type=submit], input[type=button]").element_handles()
    ]
    assert labels is not None
    for label in labels:
        for token in forbidden:
            assert token not in label, f"console exposes a command-like control: {label}"
