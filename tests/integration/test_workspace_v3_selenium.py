"""Browser fidelity checks for the Insights Workspace v3 shell.

Run against a local UI:
    APP_URL=http://localhost:8501 pytest tests/integration/test_workspace_v3_selenium.py -q
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


APP_URL = os.getenv("APP_URL", "http://localhost:8501")
# Sign-in used by the fixture; override for stacks whose admin password differs.
E2E_USER = os.getenv("E2E_USER", "admin")
E2E_PASSWORD = os.getenv("E2E_PASSWORD", "ChangeMe123!")
SCREENSHOTS = Path("tests/screenshots/workspace_v3")


@pytest.fixture()
def driver():
    options = Options()
    if os.getenv("HEADED") != "1":
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    browser = webdriver.Chrome(options=options)
    browser.set_window_size(1440, 900)
    try:
        browser.get(APP_URL)
        if "login" in browser.current_url:
            wait = WebDriverWait(browser, 10)
            wait.until(EC.presence_of_element_located((By.ID, "email"))).send_keys(E2E_USER)
            browser.find_element(By.ID, "password").send_keys(E2E_PASSWORD)
            browser.find_element(By.ID, "login-btn").click()
        WebDriverWait(browser, 15).until(
            EC.presence_of_element_located((By.ID, "v3-shell"))
        )
        yield browser
    finally:
        browser.quit()


def _shot(driver, name: str) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    driver.save_screenshot(str(SCREENSHOTS / name))


def _inject_success(driver) -> None:
    driver.execute_script(
        """
        window.JeenLegacyBridge = {
          applyResult() {},
          getChartState() { return null; },
          restoreChartState() {}
        };
        const phases = window.WorkspaceV3Utils.PHASES;
        const trace = [
          ['context_composer', 12, 'logic'],
          ['fused_router', 190, 'llm'],
          ['catalog_lookup', 86, 'db'],
          ['sql_generator', 720, 'llm'],
          ['sqlglot_validate', 8, 'logic'],
          ['execute_query', 268, 'db'],
          ['fused_eval_analytics', 840, 'llm'],
          ['response_formatter', 3, 'logic'],
          ['save_to_memory', 18, 'db']
        ].map(([node, elapsed_ms, type]) => ({
          node, elapsed_ms, type, status: 'node_finished'
        }));
        const turn = {
          id: 'good', question: 'Revenue by month and region',
          status: 'success', durationMs: 2145, trace, traceOpen: false,
          phaseState: Object.fromEntries(phases.map(p => [p.id, 'done'])),
          result: {
            question: 'Revenue by month and region',
            query_id: 'q-1', session_id: 's-1',
            sql: 'SELECT month, region, SUM(revenue) AS revenue FROM sales GROUP BY 1, 2',
            results: {
              columns: ['month', 'region', 'revenue'],
              rows: [
                {month:'2026-01', region:'EU', revenue:128400},
                {month:'2026-01', region:'US', revenue:114200},
                {month:'2026-02', region:'EU', revenue:139800}
              ]
            },
            answer: 'Revenue increased across the selected period.',
            findings: ['EU led revenue in both months.', 'February was the strongest month.'],
            followups: ['Break this down by channel', 'Compare with last year'],
            metrics: {execution_time_ms:268, llm_latency_ms:1750, total_tokens:4210,
                      input_tokens:3400, output_tokens:810, retry_count:0}
          }
        };
        WorkspaceController.turns = [turn];
        WorkspaceController.selectedTurnId = 'good';
        WorkspaceController.selectedResultId = 'good';
        WorkspaceController.lastAppliedResultId = 'good';
        WorkspaceController.render();
        const chart = document.getElementById('chart-display-container');
        if (chart) {
          chart.style.display = 'block';
          chart.style.height = '220px';
          chart.innerHTML = `<svg viewBox="0 0 800 220" width="100%" height="220" aria-label="Test chart">
          <g stroke="var(--border)" fill="none"><path d="M50 20v170h720"/><path d="M50 55h720M50 95h720M50 135h720"/></g>
          <g fill="var(--rose)"><rect x="110" y="92" width="70" height="98" rx="5"/><rect x="270" y="65" width="70" height="125" rx="5"/><rect x="430" y="42" width="70" height="148" rx="5"/></g>
          <g fill="var(--plum)"><rect x="180" y="115" width="70" height="75" rx="5"/><rect x="340" y="88" width="70" height="102" rx="5"/><rect x="500" y="72" width="70" height="118" rx="5"/></g>
        </svg>`;
        }
        """
    )


def test_workspace_structure_empty_and_responsive(driver):
    metrics = driver.execute_script(
        """
        return {
          rail: document.querySelector('.v3-rail').getBoundingClientRect().width,
          topbar: document.querySelector('.v3-topbar').getBoundingClientRect().height,
          panel: document.querySelector('.v3-conversation').getBoundingClientRect().width,
          dock: document.querySelector('.v3-dock-bar').getBoundingClientRect().height
        };
        """
    )
    assert metrics == {"rail": 60, "topbar": 60, "panel": 380, "dock": 42}
    assert driver.find_element(By.ID, "v3-placeholder").is_displayed()
    assert not driver.find_elements(By.ID, "save-analysis-btn")
    _shot(driver, "01_empty_light.png")

    driver.execute_script("document.documentElement.setAttribute('data-theme','dark')")
    assert driver.execute_script(
        "return getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()"
    ) == "#131316"
    _shot(driver, "02_empty_dark.png")

    driver.set_window_size(1099, 900)
    assert driver.execute_script(
        "return getComputedStyle(document.querySelector('.v3-conversation')).display"
    ) == "none"
    _shot(driver, "03_1099_collapsed.png")

    driver.set_window_size(899, 900)
    time.sleep(0.25)
    driver.execute_script("document.getElementById('v3-conversation-toggle').click()")
    WebDriverWait(driver, 3).until(
        lambda d: d.find_element(By.ID, "v3-conversation").value_of_css_property("display")
        == "flex"
    )
    assert driver.find_element(By.ID, "v3-drawer-overlay").is_displayed()
    _shot(driver, "04_899_overlay.png")


def test_result_turn_trace_table_dock_and_error_preservation(driver):
    _inject_success(driver)
    assert driver.find_element(By.ID, "v3-chart-block").is_displayed()
    assert not driver.find_element(By.ID, "v3-chart-frame").is_displayed()
    chart_toggle = driver.find_element(By.ID, "v3-chart-toggle")
    assert chart_toggle.text == "Expand"
    chart_toggle.click()
    assert driver.find_element(By.ID, "v3-chart-frame").is_displayed()
    assert chart_toggle.text == "Collapse"
    assert driver.find_element(By.ID, "v3-table-block").is_displayed()
    assert len(driver.find_elements(By.CSS_SELECTOR, ".v3-grid-row")) == 4

    driver.find_element(By.CSS_SELECTOR, "[data-trace-toggle='good']").click()
    assert len(driver.find_elements(By.CSS_SELECTOR, ".v3-trace-row")) == 9

    driver.find_element(By.CSS_SELECTOR, "[data-dock='sql']").click()
    assert "SELECT month" in driver.find_element(By.CSS_SELECTOR, ".v3-sql-card pre").text
    driver.find_element(By.CSS_SELECTOR, "[data-dock='profiling']").click()
    assert len(driver.find_elements(By.CSS_SELECTOR, ".v3-profile-row")) == 3

    driver.find_element(By.ID, "v3-result-filter").send_keys("EU")
    assert "2 of 3 loaded" in driver.find_element(By.ID, "v3-row-caption").text
    driver.execute_script("document.querySelector('.v3-scroll').scrollTop = 0")
    _shot(driver, "05_result_light.png")
    driver.execute_script("document.documentElement.setAttribute('data-theme','dark')")
    _shot(driver, "06_result_dark.png")
    driver.execute_script("document.documentElement.setAttribute('data-theme','light')")

    driver.execute_script(
        """
        WorkspaceController.turns.push({
          id:'bad', question:'Use a missing support table', status:'error',
          durationMs:320, trace:[{node:'execute_query', status:'node_failed', elapsed_ms:12}],
          phaseState:{execution:'error'}, error:'relation support.tickets does not exist'
        });
        WorkspaceController.selectTurn('bad');
        """
    )
    assert "Revenue by month" in driver.find_element(By.ID, "v3-result-title").text
    assert "newest question failed" in driver.find_element(By.CSS_SELECTOR, ".v3-stale-note").text
    assert driver.find_element(By.CSS_SELECTOR, ".v3-error-block").is_displayed()
    driver.execute_script("document.getElementById('v3-thread').scrollTop = document.getElementById('v3-thread').scrollHeight")
    _shot(driver, "07_result_error_preserved.png")


def test_hebrew_answers_use_rtl_insight_layout(driver):
    _inject_success(driver)
    driver.execute_script(
        """
        const turn = WorkspaceController.turns[0];
        turn.question = 'הצג מכירות שנה מול שנה לפי חודש';
        turn.result.answer = 'המכירות בשנת 2007 היו גבוהות יותר לאורך רוב השנה.';
        turn.result.findings = [
          'דצמבר היה החודש החזק ביותר עם צמיחה משמעותית.',
          'ספטמבר הציג את השינוי החד ביותר לעומת השנה הקודמת.',
          'מרץ היה החריג היחיד עם ירידה מתונה.'
        ];
        turn.result.followups = [
          'איזה ערוץ תרם יותר לצמיחה?',
          'איך נראית המגמה לפי אזור?'
        ];
        WorkspaceController.render();
        """
    )

    summary = driver.find_element(By.CSS_SELECTOR, ".v3-summary")
    insights = driver.find_element(By.CSS_SELECTOR, ".v3-insights")
    finding = driver.find_element(By.CSS_SELECTOR, ".v3-finding")
    badge = finding.find_element(By.CSS_SELECTOR, ".v3-insight-index")
    finding_text = finding.find_elements(By.CSS_SELECTOR, "span")[1]

    assert summary.get_attribute("dir") == "rtl"
    assert insights.get_attribute("dir") == "rtl"
    assert finding_text.get_attribute("dir") == "rtl"
    assert badge.rect["x"] > finding_text.rect["x"]
    assert all(
        chip.get_attribute("dir") == "rtl"
        for chip in driver.find_elements(By.CSS_SELECTOR, ".v3-followups .v3-chip")
    )
    assert "rgba" in insights.value_of_css_property("background-color")
    _shot(driver, "08_hebrew_rtl.png")


def _set_locale(driver, locale: str) -> None:
    """Switch the signed-in account's interface language through the real API.

    csrf.js has wrapped window.fetch on the page, so the PATCH carries the token.
    The server persists the choice, updates the session and sets the `locale`
    cookie; a fresh navigation then renders the whole document in that language.
    """
    status = driver.execute_async_script(
        """
        const [locale, done] = arguments;
        fetch('/api/auth/me/locale', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ locale }),
        }).then((r) => done(r.status)).catch(() => done(0));
        """,
        locale,
    )
    assert status == 200, f"PATCH /api/auth/me/locale -> {status}"
    driver.get(APP_URL)
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "v3-shell")))


def test_interface_language_switch_mirrors_the_shell_and_persists(driver):
    """Hebrew UI: <html lang dir> flips, the chrome is translated, the rail sits
    on the trailing (right) edge, and the choice survives a fresh page load.
    Restores English at the end so the other tests keep asserting English copy."""
    try:
        _set_locale(driver, "he")
        html = driver.find_element(By.TAG_NAME, "html")
        assert html.get_attribute("lang") == "he"
        assert html.get_attribute("dir") == "rtl"

        # Shell copy comes from the Hebrew catalog.
        rail = driver.find_element(By.CSS_SELECTOR, ".v3-rail")
        assert rail.get_attribute("aria-label") == "ניווט ראשי"
        title = driver.find_element(By.ID, "v3-result-title")
        assert title.text == "שאלו שאלה כדי להתחיל"
        assert driver.find_element(By.CSS_SELECTOR, '[data-rail="tables"]').get_attribute("data-tooltip") == "טבלאות"

        # Logical CSS mirrors the layout: the rail hugs the right edge and the
        # conversation panel sits to the right of the workspace.
        width = driver.execute_script("return window.innerWidth")
        assert rail.rect["x"] + rail.rect["width"] >= width - 2
        conversation = driver.find_element(By.ID, "v3-conversation")
        workspace = driver.find_element(By.CSS_SELECTOR, ".v3-workspace")
        assert conversation.rect["x"] > workspace.rect["x"]

        # Persistence: /api/auth/me reports the saved value and a new load stays Hebrew.
        me = driver.execute_async_script(
            "const done = arguments[0]; fetch('/api/auth/me').then(r => r.json()).then(done);"
        )
        assert me["locale"] == "he"
        driver.get(APP_URL)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "v3-shell")))
        assert driver.find_element(By.TAG_NAME, "html").get_attribute("dir") == "rtl"

        # An English answer inside the Hebrew UI keeps its own direction.
        _inject_success(driver)
        assert driver.find_element(By.CSS_SELECTOR, ".v3-summary").get_attribute("dir") == "ltr"

        # Settings > General shows the picker with own-language labels, Hebrew active.
        driver.find_element(By.ID, "v3-settings-button").click()
        picker = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.ID, "sp-language"))
        )
        _shot(driver, "09_hebrew_interface.png")
        options = picker.find_elements(By.CSS_SELECTOR, ".sp-lang-option")
        labels = [o.get_attribute("textContent").strip() for o in options]
        assert labels == ["English", "עברית"]
        assert all(o.is_displayed() for o in options)
        # Each option is tagged with its own language/direction for screen readers.
        assert [o.get_attribute("lang") for o in options] == ["en", "he"]
        active = picker.find_element(By.CSS_SELECTOR, '[aria-checked="true"]')
        assert active.get_attribute("data-locale") == "he"

        # The everyday switch is in the avatar menu: open it and flip back to
        # English from there (saves to the account, then the page reloads LTR).
        driver.get(APP_URL)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "v3-shell")))
        # A first-time account gets the welcome dialog over the shell (mounted
        # asynchronously after onboarding state loads); skip it if it shows up.
        try:
            WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".jo-backdrop [data-skip]"))
            ).click()
            WebDriverWait(driver, 5).until_not(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".jo-backdrop"))
            )
        except TimeoutException:
            pass  # returning user, no dialog
        avatar = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "user-avatar-btn")))
        avatar.click()
        menu_picker = WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located((By.ID, "user-lang-picker"))
        )
        assert menu_picker.find_element(By.CSS_SELECTOR, '[aria-checked="true"]').get_attribute("data-locale") == "he"
        _shot(driver, "10_hebrew_user_menu.png")
        menu_picker.find_element(By.CSS_SELECTOR, '[data-locale="en"]').click()
        WebDriverWait(driver, 15).until(
            lambda d: d.find_element(By.TAG_NAME, "html").get_attribute("lang") == "en"
        )
        assert driver.find_element(By.TAG_NAME, "html").get_attribute("dir") == "ltr"
    finally:
        _set_locale(driver, "en")
        assert driver.find_element(By.TAG_NAME, "html").get_attribute("dir") == "ltr"
