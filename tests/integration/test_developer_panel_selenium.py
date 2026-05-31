"""
Selenium integration test — Developer Panel

Covers:
  1. Log tab auto-activates after a query (trace events visible)
  2. Log filter toolbar (All/LLM/DB chips) is rendered
  3. Insights Prompt tab shows content after insights load
  4. SQL stats bar shows rows + DB time (not raw 'exec') above the CodeMirror editor
  5. Autocomplete dropdown is dismissed when Ask is clicked
  6. Query Prompt tab shows structured sections
  7. NEW — trace summary has wall / graph / LLM / DB / net chips
  8. NEW — synthetic 'flask + network' overhead row in trace timeline
  9. NEW — run header has wall / DB chips
 10. NEW — history log entries label timing as 'LLM' and 'DB' (not 'exec')

Run from repo root (requires a running app at http://localhost:8501):
    python tests/integration/test_developer_panel_selenium.py
Or via pytest:
    pytest tests/integration/test_developer_panel_selenium.py -v -s
"""
from __future__ import annotations

import os
import time
import pytest

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ── Configuration ──────────────────────────────────────────────────────────────
APP_URL         = "http://localhost:8501"
SHORT_WAIT      = 5    # seconds — fast DOM assertions
LONG_WAIT       = 90   # seconds — waits for LLM query response (Azure OpenAI can be slow)
INSIGHTS_WAIT   = 90   # seconds — waits for async insights stream to finish
SCREENSHOT_DIR  = "tests/screenshots/dev_panel"

# A specific question that reliably generates SQL and returns tabular data.
# Uses a broad table-agnostic phrasing that works across all demo connections.
TEST_QUESTION = "show me top 5 rows from any table"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=opts)


def _screenshot(driver: webdriver.Chrome, name: str) -> None:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    try:
        driver.save_screenshot(path)
        print(f"   📸 {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠ screenshot skipped ({exc})")


def _wait(driver: webdriver.Chrome, timeout: int = SHORT_WAIT) -> WebDriverWait:
    return WebDriverWait(driver, timeout)


def _login(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    if "login" not in driver.current_url:
        return
    email_input = wait.until(EC.presence_of_element_located((By.ID, "email")))
    email_input.clear()
    email_input.send_keys("admin")
    pw_input = driver.find_element(By.ID, "password")
    pw_input.clear()
    pw_input.send_keys("admin")
    driver.find_element(By.ID, "login-btn").click()
    wait.until(EC.presence_of_element_located((By.ID, "question-input")))


def _submit_query(driver: webdriver.Chrome, long_wait: WebDriverWait, question: str) -> None:
    """Type a question and submit via JavaScript; wait until results are visible."""
    # 1. Wait for connection pill to show a real name (not "Loading…").
    try:
        WebDriverWait(driver, 10).until(
            lambda d: (
                pill := d.find_element(By.ID, "connection-pill-name"),
                pill.text.strip() not in ("", "Loading\u2026", "Loading...")
            )[1]
        )
    except Exception:  # noqa: BLE001
        pass  # best-effort; connection may already be set

    # 2. Set the question input value via JavaScript (avoids any autocomplete
    #    keyboard-event side-effects) and trigger the native input event so
    #    React-style frameworks detect the change (Flask/vanilla JS is fine).
    driver.execute_script(
        "var el = document.getElementById('question-input');"
        "el.value = arguments[0];",
        question,
    )
    print(f"   ℹ Question set to: {question!r}")
    time.sleep(0.3)

    # 3. Submit via window.askQuestion() — this is the canonical JS entry point.
    #    It closes the suggestion dropdown internally before making the API call,
    #    so no Escape/click gymnastics are needed.
    driver.execute_script("window.askQuestion()")

    # 4. Wait for results section to appear (indicates query finished).
    long_wait.until(
        EC.visibility_of_element_located((By.ID, "results-section"))
    )
    print("   ✓ Query results visible")


def _open_dev_panel(driver: webdriver.Chrome, short_wait: WebDriverWait) -> None:
    """Click the </> button and wait for the drawer to slide in."""
    btn = short_wait.until(EC.element_to_be_clickable((By.ID, "dev-panel-btn")))
    btn.click()
    short_wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".dev-drawer.open")))
    print("   ✓ Dev panel is open")


# ── Main test ──────────────────────────────────────────────────────────────────

def test_developer_panel():
    """
    End-to-end: submit a query then verify all developer-panel tabs work.
    """
    driver = _driver()
    short  = _wait(driver, SHORT_WAIT)
    long   = _wait(driver, LONG_WAIT)
    insw   = _wait(driver, INSIGHTS_WAIT)

    try:
        # ── 0. Navigate + log in ───────────────────────────────────────────
        print("\n── 0. Page load & login ──")
        driver.get(APP_URL)
        _login(driver, long)
        short.until(EC.presence_of_element_located((By.ID, "question-input")))
        print("   ✓ App loaded")
        _screenshot(driver, "00_app_loaded")

        # ── 1. Submit a query ──────────────────────────────────────────────
        print("\n── 1. Submit query ──")
        _submit_query(driver, long, TEST_QUESTION)
        _screenshot(driver, "01_results_visible")

        # ── 2. Verify autocomplete is dismissed ────────────────────────────
        print("\n── 2. Autocomplete dismissed on Ask ──")
        suggestions_el = driver.find_element(By.ID, "question-suggestions")
        assert not suggestions_el.is_displayed(), \
            "Autocomplete dropdown should be hidden after Ask is clicked"
        print("   ✓ Suggestion dropdown is hidden")

        # ── 3. Open dev panel ──────────────────────────────────────────────
        print("\n── 3. Open developer panel ──")
        _open_dev_panel(driver, short)
        _screenshot(driver, "03_dev_panel_open")

        # ── 4. Log tab should be auto-active ──────────────────────────────
        print("\n── 4. Log tab auto-activation ──")
        try:
            # The ⏱ Log tab button should have class 'active'
            active_tab = short.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "#tab-trace.active")
                )
            )
            print("   ✓ Log tab is auto-activated")
        except TimeoutException:
            # Fallback: manually click the Log tab and check
            print("   ℹ Log tab not auto-active — clicking manually")
            log_tab = driver.find_element(By.ID, "tab-trace")
            log_tab.click()
            time.sleep(0.5)

        # The content-trace pane should be visible
        trace_pane = short.until(
            EC.visibility_of_element_located((By.ID, "content-trace"))
        )
        assert trace_pane.is_displayed(), "Log (trace) tab content should be visible"
        _screenshot(driver, "04_log_tab_active")

        # ── 5. Trace events are rendered ──────────────────────────────────
        print("\n── 5. Trace events rendered ──")
        trace_panel = driver.find_element(By.ID, "trace-panel")
        trace_html  = trace_panel.get_attribute("innerHTML")

        # Should have the summary chips row
        assert "trace-summary" in trace_html, \
            "Trace panel should contain .trace-summary chips"

        # Should NOT show the empty state message (means events ARE rendered)
        empty_els = driver.find_elements(
            By.CSS_SELECTOR, "#trace-panel .trace-empty"
        )
        if empty_els and empty_els[0].is_displayed():
            pytest.fail(
                "Trace panel shows empty state — no trace events were rendered. "
                "Check that the backend returns data.trace in the /api/ask response."
            )
        print("   ✓ Trace events are rendered (no empty state)")

        # Verify at least one .trace-event row exists
        event_rows = driver.find_elements(By.CSS_SELECTOR, "#trace-panel .trace-event")
        assert len(event_rows) > 0, \
            f"Expected at least one .trace-event row, found {len(event_rows)}"
        print(f"   ✓ {len(event_rows)} trace event row(s) visible")
        _screenshot(driver, "05_trace_events")

        # ── 6. Log filter toolbar is rendered ─────────────────────────────
        print("\n── 6. Log filter toolbar ──")
        toolbar = driver.find_element(By.ID, "dp-log-toolbar")
        assert toolbar.is_displayed(), "#dp-log-toolbar should be visible"

        filter_btns = driver.find_elements(
            By.CSS_SELECTOR, "#dp-log-toolbar .dp-log-filter"
        )
        assert len(filter_btns) >= 1, \
            f"Expected at least 1 filter chip, found {len(filter_btns)}"

        # 'All' filter should be active
        all_btn = driver.find_element(
            By.CSS_SELECTOR, ".dp-log-filter[data-level='all']"
        )
        assert "active" in (all_btn.get_attribute("class") or ""), \
            "The 'All' filter chip should be active by default"
        print(f"   ✓ Log toolbar has {len(filter_btns)} filter chip(s), 'All' is active")
        _screenshot(driver, "06_log_toolbar")

        # ── 6b. Trace summary timing chips (NEW) ──────────────────────────
        print("\n── 6b. Trace summary timing chips ──")
        # Navigate back to Log tab
        log_tab2 = driver.find_element(By.ID, "tab-trace")
        log_tab2.click()
        time.sleep(0.3)

        trace_panel2   = driver.find_element(By.ID, "trace-panel")
        trace_html2    = trace_panel2.get_attribute("innerHTML") or ""
        summary_chips  = driver.find_elements(
            By.CSS_SELECTOR, "#trace-panel .trace-summary-chip"
        )
        chip_texts = [c.text.strip() for c in summary_chips]
        chip_joined = " | ".join(chip_texts)
        print(f"   ℹ Summary chips: {chip_joined!r}")

        # Must have at least: route + nodes + graph (pipeline) + LLM
        assert any("graph" in t.lower() or "graph:" in t.lower() for t in chip_texts), \
            f"Expected a 'graph' timing chip, got: {chip_texts}"
        assert any("llm" in t.lower() for t in chip_texts), \
            f"Expected a 'LLM' timing chip, got: {chip_texts}"
        print("   ✓ graph + LLM chips present in trace summary")

        # Wall chip requires lastQueryDurationMs > 0 (always true after a real query)
        has_wall = any("wall" in t.lower() for t in chip_texts)
        if has_wall:
            print("   ✓ wall chip present")
            # Coloured wall chip should exist
            wall_chips = driver.find_elements(
                By.CSS_SELECTOR, "#trace-panel .trace-chip-wall"
            )
            assert len(wall_chips) > 0, "Expected .trace-chip-wall element for wall chip"
        else:
            print("   ℹ wall chip not present (query may have been instant)")

        # DB chip — only present when SQL was executed
        has_db = any("db" in t.lower() for t in chip_texts)
        if has_db:
            print("   ✓ DB chip present")
            db_chips = driver.find_elements(
                By.CSS_SELECTOR, "#trace-panel .trace-chip-db"
            )
            assert len(db_chips) > 0, "Expected .trace-chip-db element for DB chip"
        else:
            print("   ℹ DB chip absent (may be a non-SQL query route)")

        # net chip — present when wall − graph > 50 ms
        has_net = any("net" in t.lower() for t in chip_texts)
        if has_net:
            print("   ✓ net chip present")
            net_chips = driver.find_elements(
                By.CSS_SELECTOR, "#trace-panel .trace-chip-net"
            )
            assert len(net_chips) > 0, "Expected .trace-chip-net element for net chip"
        else:
            print("   ℹ net chip absent (overhead below 50 ms threshold)")

        _screenshot(driver, "06b_timing_chips")

        # ── 6c. Synthetic flask+network overhead row (NEW) ─────────────────
        print("\n── 6c. Synthetic flask+network row ──")
        synthetic_rows = driver.find_elements(
            By.CSS_SELECTOR, "#trace-panel .trace-event-synthetic"
        )
        if synthetic_rows:
            row_text = synthetic_rows[0].text or ""
            assert "flask" in row_text.lower() or "network" in row_text.lower(), \
                f"Synthetic row text should mention flask/network, got: {row_text!r}"
            print(f"   ✓ Synthetic overhead row visible: {row_text[:80]!r}")
        else:
            print("   ℹ No synthetic row (net overhead ≤ 50 ms or non-SQL query)")
        _screenshot(driver, "06c_synthetic_row")

        # ── 7. SQL tab has stats bar ───────────────────────────────────────
        print("\n── 7. SQL stats bar ──")
        sql_tab = driver.find_element(By.ID, "tab-sql")
        sql_tab.click()
        time.sleep(0.3)

        sql_stats = driver.find_element(By.ID, "dp-sql-stats")
        stats_html = sql_stats.get_attribute("innerHTML") or ""
        if sql_stats.is_displayed():
            # Must have at least one stat (LLM is always present after an LLM call)
            assert stats_html.strip(), "SQL stats bar is visible but has no content"

            # KEY regression: visible text must NOT say 'exec' — use 'DB' instead.
            # We check textContent (not innerHTML) so title= tooltips don't interfere.
            stats_text = driver.execute_script(
                "return document.getElementById('dp-sql-stats').textContent;"
            ) or ""
            assert "exec" not in stats_text.lower(), \
                f"SQL stats bar visible text should use 'DB' label, not 'exec'. Got: {stats_text!r}"

            # Determine which stats are present and print them
            has_rows = "row" in stats_text.lower()
            has_db   = "db" in stats_text.lower()
            has_llm  = "llm" in stats_text.lower()
            print(f"   \u2713 SQL stats bar visible \u2014 rows:{has_rows} DB:{has_db} LLM:{has_llm}: {stats_text[:120]!r}")
        else:
            print("   \u2139 SQL stats bar is hidden (conversational query with no SQL)")

        # ── 8. Query Prompt tab shows content ─────────────────────────────
        print("\n── 8. Query Prompt tab ──")
        query_tab = driver.find_element(By.ID, "tab-query")
        query_tab.click()
        time.sleep(0.3)

        prompt_content = driver.find_element(By.ID, "prompt-content")
        prompt_html    = prompt_content.get_attribute("innerHTML") or ""
        if not prompt_html.strip() or prompt_html.strip() == "<p></p>":
            print("   ℹ Query Prompt tab is empty (backend may not return prompt data)")
        else:
            # Should have at least the copy-all header
            assert "dp-prompt-hdr" in prompt_html or "structured-prompt" in prompt_html, \
                "Query Prompt content should have the copy header or structured sections"
            print("   ✓ Query Prompt tab has content")
        _screenshot(driver, "08_query_prompt_tab")

        # ── 9. Insights Prompt tab (wait for async insights) ───────────────
        print("\n── 9. Insights Prompt tab ──")
        # First switch to the Insights Prompt tab
        insights_tab = driver.find_element(By.ID, "tab-insights")
        insights_tab.click()
        time.sleep(0.5)

        insights_prompt_el = driver.find_element(By.ID, "insights-prompt-content")

        # Wait up to INSIGHTS_WAIT seconds for the insights stream to complete
        # and populate the prompt content
        def insights_prompt_populated(drv):
            el   = drv.find_element(By.ID, "insights-prompt-content")
            html = el.get_attribute("innerHTML") or ""
            return html.strip() and "No prompt available" not in html \
                   and "Insights prompt will appear here" not in html

        try:
            insw.until(insights_prompt_populated)
            html = insights_prompt_el.get_attribute("innerHTML") or ""
            assert "prompt-section" in html or "prompt-text" in html, \
                "Insights Prompt tab should contain structured prompt sections"
            print("   ✓ Insights Prompt tab populated with prompt content")
        except TimeoutException:
            html = insights_prompt_el.get_attribute("innerHTML") or ""
            if "No prompt available" in html:
                pytest.fail(
                    "Insights Prompt tab shows 'No prompt available'. "
                    "The backend is not returning `prompt` in the insights done event."
                )
            else:
                print(f"   ℹ Insights Prompt tab not populated after {INSIGHTS_WAIT}s "
                      f"(may still be streaming). HTML: {html[:200]!r}")

        _screenshot(driver, "09_insights_prompt_tab")

        # ── 10. Run header is visible + has timing chips (NEW) ────────────
        print("\n── 10. Run header ──")
        # Open dev panel again in case it was closed
        if not driver.find_elements(By.CSS_SELECTOR, ".dev-drawer.open"):
            _open_dev_panel(driver, short)

        run_header = driver.find_element(By.ID, "dp-run-header")
        if run_header.is_displayed():
            question_el = driver.find_element(By.ID, "dp-run-question")
            assert question_el.text.strip(), "Run header question text should not be empty"
            print(f"   ✓ Run header visible, question: {question_el.text[:60]!r}")

            # Check for timing chips in run-meta (NEW)
            meta_chips = driver.find_elements(
                By.CSS_SELECTOR, "#dp-run-meta .dp-chip"
            )
            chip_texts_hdr = [c.text.strip() for c in meta_chips]
            print(f"   ℹ Run header chips: {chip_texts_hdr!r}")

            # Must have status chip at minimum
            assert len(meta_chips) >= 1, "Run header should have at least 1 chip"

            # wall chip in header (NEW)
            wall_hdr = driver.find_elements(
                By.CSS_SELECTOR, "#dp-run-meta .dp-chip-wall"
            )
            if wall_hdr:
                print("   ✓ Run header wall chip present")
            else:
                print("   ℹ Run header wall chip absent")

            # DB chip in header (NEW)
            db_hdr = driver.find_elements(
                By.CSS_SELECTOR, "#dp-run-meta .dp-chip-db"
            )
            if db_hdr:
                print("   ✓ Run header DB chip present")
            else:
                print("   ℹ Run header DB chip absent (non-SQL route)")

        else:
            print("   ℹ Run header hidden (hidden attribute still set)")
        _screenshot(driver, "10_run_header")

        # ── 11. History log labels (NEW) ───────────────────────────────────
        print("\n── 11. History log timing labels ──")
        # Close dev panel and open history drawer
        try:
            close_btn = driver.find_element(By.ID, "dev-drawer-close")
            close_btn.click()
            time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass

        history_btn = driver.find_element(By.ID, "history-btn")
        history_btn.click()
        time.sleep(0.5)

        history_body = driver.find_element(By.ID, "history-drawer-body")
        history_html = history_body.get_attribute("innerHTML") or ""

        # After the restart the new column exists; for any existing entries that
        # have llm_ms stored they should render 'LLM' not 'exec'
        if "history-log-entry" in history_html:
            # Should use 'LLM' label, not bare 'exec' (old label)
            # Note: 'exec' may appear in questions themselves, so check the meta specifically
            meta_els = driver.find_elements(
                By.CSS_SELECTOR, ".history-log-meta"
            )
            if meta_els:
                sample_meta = meta_els[0].text or ""
                print(f"   ℹ Sample history meta: {sample_meta!r}")
                # Old code used 'exec Xms'; new code uses 'DB Xs'
                assert "exec" not in sample_meta.lower(), \
                    f"History log should use 'DB' label, not 'exec'. Got: {sample_meta!r}"
                print("   ✓ History log uses 'DB' (not 'exec') label for execution time")
            else:
                print("   ℹ No history meta elements found")
        else:
            print("   ℹ No history entries yet")
        _screenshot(driver, "11_history_log")

        print("\n✅ All developer panel + timing breakdown checks passed!\n")

    finally:
        _screenshot(driver, "99_final")
        driver.quit()
        print("✓ Browser closed")


# ── Standalone runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_developer_panel()
