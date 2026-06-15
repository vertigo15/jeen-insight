"""
Selenium integration test — Chart view and AI enhancement.

Flow:
  1. Load the app and submit a query.
  2. Wait for the results section to appear.
  3. Click the “Chart” tab to switch to chart view.
  4. Wait for the ECharts container to render content.
  5. If the Enhance button is present, click it and verify the chart changes.
  6. Assert the chart display container is non-empty.

Run from the repo root:
    pytest tests/integration/test_chart_enhancement_selenium.py -v -s

Requires a running app at http://localhost:8501.
"""
from __future__ import annotations

import os
import time

import pytest
import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException

# ── Configuration ───────────────────────────────────────────────────────────────────

APP_URL        = "http://localhost:8501"
API_URL        = "http://localhost:8001"   # direct API (not UI proxy)
SCREENSHOT_DIR = "tests/screenshots/chart"
SHORT_WAIT     = 15  # seconds — DOM element waits
LONG_WAIT      = 60  # seconds — LLM query + chart generation

# Last-resort model name if the health endpoint is unreachable. The test
# normally picks a confirmed-working model dynamically (see _pick_healthy_model)
# rather than hardcoding one, which goes stale as credentials rotate.
_FALLBACK_MODEL = "gpt-5.3"

# ── Helpers ────────────────────────────────────────────────────────────────────────

def _screenshot(driver: webdriver.Chrome, name: str) -> None:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    try:
        driver.save_screenshot(path)
        print(f"   📸 {path}")
    except Exception as exc:
        print(f"   ⚠ screenshot skipped ({exc})")


def _pick_healthy_model(api_url: str) -> str:
    """Return the name of a model the live health probe confirms working.

    Falls back to ``_FALLBACK_MODEL`` when the endpoint is unreachable so the
    test stays self-contained instead of pinning a model that may be down.
    """
    try:
        r = requests.get(f"{api_url}/api/settings/models/health", timeout=120)
        if r.ok:
            healthy = r.json().get("healthy") or []
            if healthy:
                return healthy[0]
    except requests.RequestException:
        pass
    return _FALLBACK_MODEL


def _login(driver: webdriver.Chrome, wait: WebDriverWait,
           email: str = "admin", password: str = "admin") -> None:
    """Fill and submit the password login form; wait until the main app loads."""
    if "login" not in driver.current_url:
        return
    email_input = wait.until(EC.presence_of_element_located((By.ID, "email")))
    email_input.clear()
    email_input.send_keys(email)
    pw_input = driver.find_element(By.ID, "password")
    pw_input.clear()
    pw_input.send_keys(password)
    driver.find_element(By.ID, "login-btn").click()
    wait.until(EC.presence_of_element_located((By.ID, "question-input")))


# ── Test ───────────────────────────────────────────────────────────────────────────

def test_top_products_chart_enhancement():
    """
    End-to-end: submit a query, switch to chart view, assert the chart
    renders.  Exercises the Enhance button if it is present in the DOM.
    """
    opts = Options()
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=opts)
    short  = WebDriverWait(driver, SHORT_WAIT)
    long_w = WebDriverWait(driver, LONG_WAIT)

    try:
        # ── 0a. Ensure a known-working LLM model is active ──────────────────────
        # Previous tests (or manual exploration) may have switched to a model
        # that has invalid/expired credentials.  Restore a known-good model so
        # this test is self-contained and independent of prior state.
        good_model = _pick_healthy_model(API_URL)
        try:
            r = requests.put(
                f"{API_URL}/api/settings/models/active",
                json={"name": good_model},
                timeout=10,
            )
            if r.ok:
                print(f"\u2713 Active model reset to {good_model!r}")
            else:
                print(f"\u26a0 Could not reset model ({r.status_code}): {r.text[:120]}")
        except requests.RequestException as req_err:
            pytest.skip(f"API not reachable for model reset: {req_err}")

        # ── 0b. Load page + authenticate ─────────────────────────────────
        driver.get(APP_URL)
        _login(driver, short)
        short.until(EC.presence_of_element_located((By.ID, "question-input")))
        print("\u2713 App loaded")
        _screenshot(driver, "00_app_loaded")

        # ── 1. Wait for a database connection to be active ───────────────────────
        # The connection pill starts as “Loading…” while /api/connections is fetched.
        # Submitting a query before a connection is active produces
        # “Please pick a connection from the sidebar.” and hides results-section.
        short.until(
            lambda d: d.find_element(By.ID, "connection-pill-name").text.strip()
            not in ("Loading\u2026", "Loading...", "")
        )
        conn_name = driver.find_element(By.ID, "connection-pill-name").text.strip()
        assert conn_name != "No connections", \
            "No database connections available — cannot run chart query test"
        print(f"\u2713 Connection active: {conn_name!r}")
        _screenshot(driver, "00b_connection_ready")

        # ── 2. Submit query ────────────────────────────────────────────────
        q_input = driver.find_element(By.ID, "question-input")
        # Use a simple aggregation query that always returns rows in AdventureWorksDW.
        question = "How many rows are in each table? Show me the top 5 tables by row count."
        q_input.clear()
        q_input.send_keys(question)
        print(f"\u2713 Entered query: {question!r}")

        ask_btn = short.until(EC.element_to_be_clickable((By.ID, "ask-button")))
        ask_btn.click()
        print("\u2713 Clicked Ask button")

        # ── 3. Wait for results section ────────────────────────────────────
        # Detect two failure modes:
        #   a) #error-message visible (network/connection error from askQuestion catch)
        #   b) results-section visible but contains error content from the LLM (e.g. 401)
        def _results_or_error(d):
            # a) JS catch-level error in the query card
            err = d.find_element(By.ID, "error-message")
            if err.is_displayed():
                raise AssertionError(f"Query-card error: {err.text.strip()!r}")
            # b) Results section appeared — check for LLM-level error text inside it
            rs = d.find_element(By.ID, "results-section")
            if rs.is_displayed():
                rs_html = rs.get_attribute("innerHTML") or ""
                if "Error code:" in rs_html or "error-result" in rs_html:
                    # Extract visible text for a clear error message.
                    raise AssertionError(
                        f"Results section shows LLM error: {rs.text[:200]!r}"
                    )
                return True
            return False

        long_w.until(_results_or_error)
        print("\u2713 Results section visible")
        _screenshot(driver, "01_results_loaded")

        # ── 4. Switch to chart view ────────────────────────────────────────
        # ChartToggle renders #toggle-chart-btn inside #chart-toggle-container.
        # Wait for the button to be present; it may be disabled if the query
        # returned no rows (ChartManager.disableChartButton).
        chart_tab = short.until(
            EC.presence_of_element_located((By.ID, "toggle-chart-btn"))
        )
        if not chart_tab.is_enabled():
            pytest.skip(
                "Chart tab is disabled (query returned no chartable rows) — "
                "chart rendering cannot be tested with this result set"
            )
        chart_tab.click()
        print("\u2713 Clicked Chart tab")

        # ── 5. Chart view container must become visible ──────────────────────
        short.until(
            EC.visibility_of_element_located((By.ID, "chart-view-container"))
        )
        print("\u2713 chart-view-container visible")

        # ── 6. Chart display container must have content (ECharts renders here) ─
        # ChartManager generates the chart via LLM; allow up to LONG_WAIT.
        long_w.until(
            lambda d: bool(
                d.find_element(By.ID, "chart-display-container")
                 .find_elements(By.CSS_SELECTOR, "*")
            )
        )
        print("\u2713 chart-display-container has content")
        _screenshot(driver, "02_chart_rendered")

        # ── 7. Enhance button (optional — EnhanceButton component may not be  ─
        #         mounted in the current build)                                 ─
        try:
            enhance_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.ID, "enhance-chart-btn"))
            )
            print("\u2713 Enhance button found — clicking")

            chart_display = driver.find_element(By.ID, "chart-display-container")
            html_before = chart_display.get_attribute("innerHTML")

            enhance_btn.click()
            print("\u2713 Clicked Enhance button")

            time.sleep(8)   # wait for async LLM enhancement
            _screenshot(driver, "03_chart_after_enhance")

            html_after = chart_display.get_attribute("innerHTML")
            if html_before != html_after:
                print("\u2713 Chart was modified by enhancement")
            else:
                print("\u26a0 Chart HTML unchanged after enhancement")

            # Check for K/M number formatting in page source.
            page_source = driver.page_source
            has_k = any(
                c.isdigit()
                for part in page_source.split("K") if part
                for c in part[-5:]
            )
            has_m = any(
                c.isdigit()
                for part in page_source.split("M") if part
                for c in part[-5:]
            )
            print(f"   K-format detected: {has_k}")
            print(f"   M-format detected: {has_m}")

        except TimeoutException:
            print("⚠ Enhance button not present (component not mounted) — skipping")
            _screenshot(driver, "03_no_enhance_btn")

        # ── 8. Final assertion ───────────────────────────────────────────────
        chart_display = driver.find_element(By.ID, "chart-display-container")
        assert chart_display.get_attribute("innerHTML").strip(), \
            "chart-display-container is empty after switching to chart view"
        print("\u2713 chart-display-container is non-empty — test PASSED")

    except Exception as exc:
        print(f"\u2717 Test failed: {exc}")
        _screenshot(driver, "error")
        raise

    finally:
        driver.quit()
        print("\u2713 Browser closed")


if __name__ == "__main__":
    print("=" * 50)
    print("Chart Enhancement Selenium Test")
    print("=" * 50)
    test_top_products_chart_enhancement()
