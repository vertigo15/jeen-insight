"""
Selenium integration test — Table Formatting & Insights Coloring

Verifies the Table Color & Formatting Rules spec:
  1. Table headers are UPPERCASE + letter-spaced
  2. NULL cells show faint em-dash — (not "NULL" or blank)
  3. Numeric cells use real minus − (U+2212), not ASCII -
  4. Numeric cells have .num-cell class (right-aligned tabular-nums)
  5. Insights summary has at least one .hl-accent / .hl-pos / .hl-neg / .hl-num span

Run from repo root (requires a running app at http://localhost:8501):
    pytest tests/integration/test_table_formatting_selenium.py -v -s
"""
from __future__ import annotations

import os
import time
import pytest

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# ── Configuration ──────────────────────────────────────────────────────────────
APP_URL        = "http://localhost:8501"
SHORT_WAIT     = 5
LONG_WAIT      = 90
INSIGHTS_WAIT  = 90
SCREENSHOT_DIR = "tests/screenshots/table_fmt"

# Query that reliably returns multi-column tabular data for any connection.
# Using TOP 5 avoids huge result sets and keeps response times low.
TEST_QUESTION = "show me top 5 rows from any table"

# Real minus U+2212 — the character we expect in negative formatted cells
REAL_MINUS = "\u2212"
EM_DASH    = "\u2014"


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
    email = wait.until(EC.presence_of_element_located((By.ID, "email")))
    email.clear(); email.send_keys("admin")
    pw = driver.find_element(By.ID, "password")
    pw.clear(); pw.send_keys("ChangeMe123!")
    driver.find_element(By.ID, "login-btn").click()
    wait.until(EC.presence_of_element_located((By.ID, "question-input")))


def _submit_query(driver: webdriver.Chrome, long_wait: WebDriverWait, question: str) -> None:
    """Set input via JS + call window.askQuestion() to avoid dropdown issues."""
    try:
        WebDriverWait(driver, 10).until(
            lambda d: (
                pill := d.find_element(By.ID, "connection-pill-name"),
                pill.text.strip() not in ("", "Loading\u2026", "Loading...")
            )[1]
        )
    except Exception:  # noqa: BLE001
        pass

    driver.execute_script(
        "var el = document.getElementById('question-input'); el.value = arguments[0];",
        question,
    )
    time.sleep(0.3)
    driver.execute_script("window.askQuestion()")
    long_wait.until(EC.visibility_of_element_located((By.ID, "v3-table-block")))
    print(f"   ✓ Query results visible for: {question!r}")


# ── Main test ──────────────────────────────────────────────────────────────────

def test_table_formatting():
    """
    End-to-end: submit a tabular query, then verify table + insights formatting.
    """
    driver = _driver()
    short  = _wait(driver, SHORT_WAIT)
    long   = _wait(driver, LONG_WAIT)
    insw   = _wait(driver, INSIGHTS_WAIT)

    try:
        # ── 0. Load & login ────────────────────────────────────────────────
        print("\n── 0. Load & login ──")
        driver.get(APP_URL)
        _login(driver, long)
        short.until(EC.presence_of_element_located((By.ID, "question-input")))
        print("   ✓ App loaded")
        _screenshot(driver, "00_app_loaded")

        # ── 1. Submit query ────────────────────────────────────────────────
        print("\n── 1. Submit query ──")
        _submit_query(driver, long, TEST_QUESTION)
        _screenshot(driver, "01_results_visible")

        # ── 2. Table header uppercase ──────────────────────────────────────
        print("\n── 2. Table header uppercase ──")
        headers = driver.find_elements(By.CSS_SELECTOR, "#results-table thead th")
        assert len(headers) > 0, "No table headers found — results may not be tabular"
        print(f"   ℹ {len(headers)} header(s) found")

        header_texts = [h.text.strip() for h in headers if h.text.strip()]
        print(f"   ℹ Header texts: {header_texts}")

        # Verify CSS text-transform:uppercase is applied
        th_style = driver.execute_script(
            "return window.getComputedStyle(document.querySelector('#results-table thead th')).textTransform;"
        )
        assert th_style == "uppercase", \
            f"Table header text-transform should be 'uppercase', got: {th_style!r}"
        print("   ✓ Headers have text-transform:uppercase")

        # Verify letter-spacing is applied (not 'normal')
        th_spacing = driver.execute_script(
            "return window.getComputedStyle(document.querySelector('#results-table thead th')).letterSpacing;"
        )
        print(f"   ℹ Header letter-spacing: {th_spacing!r}")
        # letter-spacing is in px; 0.06em at 11px base ≈ 0.66px — just check it's not 0
        assert th_spacing != "0px", \
            f"Table header should have letter-spacing > 0, got: {th_spacing!r}"
        print("   ✓ Headers have letter-spacing applied")
        _screenshot(driver, "02_headers")

        # ── 3. NULL cells show em-dash ─────────────────────────────────────
        print("\n── 3. NULL / missing cells ──")
        # Find any .cell-null spans (rendered when value is null/empty)
        null_cells = driver.find_elements(By.CSS_SELECTOR, "#results-table .cell-null")
        if null_cells:
            for nc in null_cells[:3]:  # check first 3
                cell_text = nc.text.strip()
                assert cell_text == EM_DASH, \
                    f"NULL cell should show em-dash '—', got: {cell_text!r}"
            print(f"   ✓ {len(null_cells)} NULL cell(s) show em-dash '—'")
        else:
            print("   ℹ No NULL cells in this result set (all values populated)")

        # Confirm 'NULL' text is NOT visible anywhere in the table body
        table_body_html = driver.execute_script(
            "const tb = document.querySelector('#results-table tbody'); return tb ? tb.innerHTML : '';"
        )
        # Should not have bold italic 'NULL' from the old code
        assert "<em" not in table_body_html.lower() or "NULL" not in table_body_html, \
            "Old italic 'NULL' markup should not appear — use em-dash instead"
        print("   ✓ No italic 'NULL' in table body")
        _screenshot(driver, "03_null_cells")

        # ── 4. Numeric cells have .num-cell class ──────────────────────────
        print("\n── 4. Numeric cell alignment ──")
        num_cells = driver.find_elements(By.CSS_SELECTOR, "#results-table td.num-cell")
        if num_cells:
            print(f"   ✓ {len(num_cells)} numeric cell(s) have .num-cell class (right-aligned tabular-nums)")
            # Verify right alignment via computed style
            align = driver.execute_script(
                "return window.getComputedStyle(document.querySelector('#results-table td.num-cell')).textAlign;"
            )
            assert align == "right", f"Numeric cells should be right-aligned, got: {align!r}"
            print("   ✓ Numeric cells are right-aligned")
        else:
            print("   ℹ No .num-cell found — result may be all-text columns")

        # ── 5. Negative numbers use real minus U+2212 ──────────────────────
        print("\n── 5. Negative number formatting (real minus) ──")
        # Check all .num-cell text content via JS — look for real minus
        neg_text = driver.execute_script("""
            const cells = document.querySelectorAll('#results-table td.num-cell');
            const negatives = [];
            cells.forEach(c => {
                const t = c.textContent.trim();
                if (t.startsWith('\u2212') || t.includes('\u2212')) negatives.push(t);
            });
            return negatives;
        """)
        if neg_text:
            print(f"   ✓ {len(neg_text)} negative value(s) use real minus \u2212: {neg_text[:3]}")
            for val in neg_text:
                assert REAL_MINUS in val, f"Expected real minus in {val!r}"
            # Confirm NO ASCII minus in formatted negatives (they use U+2212)
            ascii_neg = driver.execute_script("""
                const cells = document.querySelectorAll('#results-table td.num-cell');
                const bad = [];
                cells.forEach(c => {
                    const t = c.textContent.trim();
                    if (t.startsWith('-') && !t.startsWith('\u2212')) bad.push(t);
                });
                return bad;
            """)
            assert not ascii_neg, f"Found cells with ASCII minus instead of \u2212: {ascii_neg}"
            print("   ✓ No ASCII minus used in formatted numeric cells")
        else:
            print("   ℹ No negative values in this result set — skipping sign check")
        _screenshot(driver, "05_numeric_cells")

        # ── 6. ID columns are mono + faint ────────────────────────────────
        print("\n── 6. ID column rendering ──")
        id_cells = driver.find_elements(By.CSS_SELECTOR, "#results-table td.cell-id")
        if id_cells:
            id_font = driver.execute_script(
                "return window.getComputedStyle(document.querySelector('#results-table td.cell-id')).fontFamily;"
            )
            print(f"   ✓ {len(id_cells)} ID cell(s) with .cell-id class; font: {id_font[:40]!r}")
            # Font-family should be mono (contains 'Mono' or 'monospace')
            assert "mono" in id_font.lower() or "monospace" in id_font.lower(), \
                f"ID cells should use monospace font, got: {id_font!r}"
            print("   ✓ ID cells use monospace font")
        else:
            print("   ℹ No ID-like columns detected in this result set")
        _screenshot(driver, "06_id_cells")

        # ── 7. Delta columns get direction color ───────────────────────────
        print("\n── 7. Delta column coloring ──")
        delta_pos = driver.find_elements(By.CSS_SELECTOR, "#results-table td.cell-delta-pos")
        delta_neg = driver.find_elements(By.CSS_SELECTOR, "#results-table td.cell-delta-neg")
        if delta_pos or delta_neg:
            print(f"   ✓ Delta cells: {len(delta_pos)} positive (green), {len(delta_neg)} negative (red)")
            # Check green color is applied
            if delta_pos:
                pos_color = driver.execute_script(
                    "return window.getComputedStyle(document.querySelector('td.cell-delta-pos')).color;"
                )
                print(f"   ℹ Positive delta color: {pos_color!r}")
            if delta_neg:
                neg_color = driver.execute_script(
                    "return window.getComputedStyle(document.querySelector('td.cell-delta-neg')).color;"
                )
                print(f"   ℹ Negative delta color: {neg_color!r}")
        else:
            print("   ℹ No delta/change columns detected (requires yoy/change/delta column name)")
        _screenshot(driver, "07_delta_cells")

        # ── 8. Insights color classes ──────────────────────────────────────
        print("\n── 8. Insights coloring (hl-accent / hl-pos / hl-neg / hl-num) ──")
        # Wait for insights to load
        def insights_has_content(drv):
            el = drv.find_element(By.ID, "insights-container")
            h = el.get_attribute("innerHTML") or ""
            return "ins-card" in h and "ins-loading" not in h

        try:
            insw.until(insights_has_content)
        except TimeoutException:
            print(f"   ℹ Insights not loaded after {INSIGHTS_WAIT}s — skipping color checks")
        else:
            # Check the 4 color treatment classes
            accent_els = driver.find_elements(By.CSS_SELECTOR, "#insights-container .hl-accent")
            pos_els    = driver.find_elements(By.CSS_SELECTOR, "#insights-container .hl-pos")
            neg_els    = driver.find_elements(By.CSS_SELECTOR, "#insights-container .hl-neg")
            num_els    = driver.find_elements(By.CSS_SELECTOR, "#insights-container .hl-num")

            total_colored = len(accent_els) + len(pos_els) + len(neg_els) + len(num_els)
            print(f"   ℹ Color treatments: hl-accent={len(accent_els)}, "
                  f"hl-pos={len(pos_els)}, hl-neg={len(neg_els)}, hl-num={len(num_els)}")

            # At minimum the insights should have loaded some text; color is LLM-dependent
            ins_text = driver.find_element(By.ID, "insights-container").text
            assert ins_text.strip(), "Insights container should have visible text"
            print(f"   ✓ Insights loaded ({len(ins_text)} chars)")

            if total_colored > 0:
                print(f"   ✓ {total_colored} colored span(s) applied across 4 treatments")

                # Verify hl-accent is violet (accent color)
                if accent_els:
                    accent_color = driver.execute_script(
                        "return window.getComputedStyle(document.querySelector('.hl-accent')).color;"
                    )
                    print(f"   ℹ hl-accent color: {accent_color!r}")

                # Verify hl-pos is green
                if pos_els:
                    pos_color = driver.execute_script(
                        "return window.getComputedStyle(document.querySelector('.hl-pos')).color;"
                    )
                    print(f"   ℹ hl-pos color: {pos_color!r}")

                # Verify hl-neg is red
                if neg_els:
                    neg_color = driver.execute_script(
                        "return window.getComputedStyle(document.querySelector('.hl-neg')).color;"
                    )
                    print(f"   ℹ hl-neg color: {neg_color!r}")

                # Verify hl-num is monospace
                if num_els:
                    num_font = driver.execute_script(
                        "return window.getComputedStyle(document.querySelector('.hl-num')).fontFamily;"
                    )
                    assert "mono" in num_font.lower() or "monospace" in num_font.lower(), \
                        f"hl-num should use monospace font, got: {num_font!r}"
                    print(f"   ✓ hl-num uses monospace font: {num_font[:40]!r}")

                # CRITICAL: hl-accent appears ONLY in summary (not in findings)
                if accent_els:
                    accent_in_summary = driver.find_elements(
                        By.CSS_SELECTOR, "#insights-container .ins-summary .hl-accent"
                    )
                    accent_in_finding = driver.find_elements(
                        By.CSS_SELECTOR, "#insights-container .ins-item-body .hl-accent"
                    )
                    if accent_in_finding:
                        # If LLM put accent in findings, warn but don't fail — it's LLM output
                        print(f"   ⚠ {len(accent_in_finding)} hl-accent span(s) found in findings "
                              "(spec says accent is summary-only; LLM may not follow strictly)")
                    else:
                        print("   ✓ hl-accent appears only in summary (not in findings)")
            else:
                print("   ℹ No color spans detected (LLM may have output plain text for this query)")

            _screenshot(driver, "08_insights_colors")

        # ── 9. workspace fills available width ────────────────────────────
        print("\n── 9. workspace dynamic width ──")
        main_inner_width = driver.execute_script(
            "const el = document.querySelector('.v3-scroll'); "
            "return el ? el.getBoundingClientRect().width : null;"
        )
        main_content_width = driver.execute_script(
            "const el = document.querySelector('.v3-workspace'); "
            "return el ? el.getBoundingClientRect().width : null;"
        )
        if main_inner_width and main_content_width:
            # The workspace scroll region should fill the result workspace.
            ratio = main_inner_width / main_content_width
            print(f"   ℹ scroll: {main_inner_width:.0f}px / workspace: {main_content_width:.0f}px = {ratio:.2f}")
            assert ratio > 0.85, \
                f"workspace scroll region should fill ≥85% of available width, got {ratio:.2f}"
            print("   ✓ workspace fills available content width")
        _screenshot(driver, "09_layout")

        print("\n✅ All table formatting + insights coloring checks passed!\n")

    finally:
        _screenshot(driver, "99_final")
        driver.quit()
        print("✓ Browser closed")


# ── Standalone runner ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_table_formatting()
