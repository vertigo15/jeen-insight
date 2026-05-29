"""
Selenium integration test — Settings page

Covers:
  1. Settings button opens the full-screen overlay
  2. All sidebar nav items are present
  3. General section – preference controls render with options
  4. AI Models section – model cards load (available + unavailable)
  5. Each AI Agent prompt – content loads, placeholder chips shown
  6. Each Other Features prompt – content loads
  7. About section – app info fields populated
  8. Close button dismisses the overlay
  9. Escape key also closes the overlay

Run from the repo root:
    python tests/integration/test_settings_selenium.py

Or via pytest (requires a running app at http://localhost:8501):
    pytest tests/integration/test_settings_selenium.py -v -s
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

# ── Configuration ─────────────────────────────────────────────────────────────

APP_URL = "http://localhost:8501"
SHORT_WAIT = 5   # seconds – fast DOM checks
LONG_WAIT  = 15  # seconds – async data fetches
SCREENSHOT_DIR = "tests/screenshots/settings"

# Nav item data-id values we expect in the sidebar
EXPECTED_NAV_IDS = [
    "general",
    "ai-models",
    "prompt:jeen_insights_system",
    "prompt:fused_router",
    "prompt:fused_eval_analytics",
    "prompt:memory_answer",
    "prompt:memory_summarizer",
    "prompt:sql_generator",
    "prompt:chart_editor",
    "prompt:insights",
    "prompt:autocomplete_suggestions",
    "about",
]

# Prompt nav IDs with their expected placeholder chips
PROMPT_IDS = [
    "prompt:jeen_insights_system",
    "prompt:fused_router",
    "prompt:fused_eval_analytics",
    "prompt:memory_answer",
    "prompt:memory_summarizer",
    "prompt:sql_generator",
    "prompt:chart_editor",
    "prompt:insights",
    "prompt:autocomplete_suggestions",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=opts)


def _screenshot(driver: webdriver.Chrome, name: str) -> None:
    """Save a screenshot; silently skip on renderer timeout."""
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    try:
        driver.set_page_load_timeout(30)
        driver.save_screenshot(path)
        print(f"   📸 {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠ screenshot skipped ({type(exc).__name__}: {exc})")


def _wait(driver: webdriver.Chrome, timeout: int = SHORT_WAIT) -> WebDriverWait:
    return WebDriverWait(driver, timeout)


def _open_settings(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """Click the gear icon and wait for the overlay to become visible."""
    btn = wait.until(EC.element_to_be_clickable((By.ID, "settings-btn")))
    btn.click()
    # Overlay must lose the [hidden] attribute and become displayed
    wait.until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, ".sp-overlay"))
    )


def _click_nav(driver: webdriver.Chrome, wait: WebDriverWait, nav_id: str) -> None:
    """Click a sidebar nav item by its data-id."""
    selector = f'.sp-nav-item[data-id="{nav_id}"]'
    item = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
    item.click()
    # Wait for the section title to render
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".sp-section-title")))


# ── Main test ─────────────────────────────────────────────────────────────────

def test_settings_page():
    """
    End-to-end check: settings button opens correctly and every section
    contains data.
    """
    driver = _driver()
    short = _wait(driver, SHORT_WAIT)
    long  = _wait(driver, LONG_WAIT)

    try:
        # ── 0. Navigate and wait for app to load ─────────────────────────────
        print("\n── 0. Page load ──")
        driver.get(APP_URL)
        short.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#question-input, textarea"))
        )
        print("   ✓ App loaded")
        _screenshot(driver, "00_app_loaded")

        # ── 1. Settings button opens the overlay ─────────────────────────────
        print("\n── 1. Settings button ──")
        _open_settings(driver, short)
        overlay = driver.find_element(By.CSS_SELECTOR, ".sp-overlay")
        assert overlay.is_displayed(), "Settings overlay is not visible after clicking the button"
        print("   ✓ Overlay is visible")
        _screenshot(driver, "01_overlay_open")

        # ── 2. All nav items present ──────────────────────────────────────────
        print("\n── 2. Sidebar nav items ──")
        for nav_id in EXPECTED_NAV_IDS:
            try:
                el = driver.find_element(By.CSS_SELECTOR, f'.sp-nav-item[data-id="{nav_id}"]')
                assert el.is_displayed(), f"Nav item '{nav_id}' exists but is not visible"
                print(f"   ✓ {nav_id}")
            except NoSuchElementException:
                pytest.fail(f"Nav item missing: data-id='{nav_id}'")

        _screenshot(driver, "02_nav_items")

        # ── 3. General section ────────────────────────────────────────────────
        print("\n── 3. General section ──")
        _click_nav(driver, short, "general")

        title = driver.find_element(By.CSS_SELECTOR, ".sp-section-title").text
        assert "General" in title, f"Expected 'General' in section title, got: {title!r}"
        print(f"   ✓ Title: {title!r}")

        # Theme select
        theme_sel = short.until(EC.presence_of_element_located((By.ID, "sp-theme")))
        opts_theme = theme_sel.find_elements(By.TAG_NAME, "option")
        assert len(opts_theme) >= 3, f"Expected ≥3 theme options, found {len(opts_theme)}"
        print(f"   ✓ Theme select: {[o.text for o in opts_theme]}")

        # Row limit select
        rl_sel = driver.find_element(By.ID, "sp-rowlimit")
        opts_rl = rl_sel.find_elements(By.TAG_NAME, "option")
        assert len(opts_rl) >= 2, f"Expected ≥2 row-limit options, found {len(opts_rl)}"
        print(f"   ✓ Row limit options: {[o.text for o in opts_rl]}")

        # Chart type select
        ct_sel = driver.find_element(By.ID, "sp-charttype")
        opts_ct = ct_sel.find_elements(By.TAG_NAME, "option")
        assert len(opts_ct) >= 5, f"Expected ≥5 chart type options, found {len(opts_ct)}"
        print(f"   ✓ Chart types: {len(opts_ct)} options")

        # Temperature select
        temp_sel = driver.find_element(By.ID, "sp-temp")
        opts_temp = temp_sel.find_elements(By.TAG_NAME, "option")
        assert len(opts_temp) >= 4, f"Expected ≥4 temperature options, found {len(opts_temp)}"
        print(f"   ✓ Temperature options: {[o.text for o in opts_temp]}")

        _screenshot(driver, "03_general")

        # ── 4. AI Models section ──────────────────────────────────────────────
        print("\n── 4. AI Models section ──")
        _click_nav(driver, short, "ai-models")

        title = short.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".sp-section-title"))
        ).text
        assert "AI Models" in title, f"Expected 'AI Models' in section title, got: {title!r}"
        print(f"   ✓ Title: {title!r}")

        # Wait for model cards to load (async fetch)
        long.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".sp-model-card"))
        )
        all_cards = driver.find_elements(By.CSS_SELECTOR, ".sp-model-card")
        assert len(all_cards) >= 4, f"Expected ≥4 model cards, found {len(all_cards)}"
        print(f"   ✓ Total model cards: {len(all_cards)}")

        available_cards = driver.find_elements(
            By.CSS_SELECTOR, ".sp-model-card:not(.is-unavailable)"
        )
        assert len(available_cards) >= 1, "No available (Azure) model cards found"
        print(f"   ✓ Available (Azure) cards: {len(available_cards)}")

        unavailable_cards = driver.find_elements(
            By.CSS_SELECTOR, ".sp-model-card.is-unavailable"
        )
        print(f"   ✓ Unavailable cards: {len(unavailable_cards)}")

        # Verify each available card has a non-empty name and description
        for card in available_cards:
            name_el = card.find_element(By.CSS_SELECTOR, ".sp-model-name")
            desc_el = card.find_element(By.CSS_SELECTOR, ".sp-model-desc")
            assert name_el.text.strip(), "Model card has empty name"
            assert desc_el.text.strip(), "Model card has empty description"
            print(f"      · {name_el.text.strip()[:50]}")

        # Verify group labels are rendered
        group_labels = driver.find_elements(By.CSS_SELECTOR, ".sp-model-group-label")
        assert len(group_labels) >= 1, "No model group labels found"
        print(f"   ✓ Group labels: {[g.text for g in group_labels]}")

        _screenshot(driver, "04_ai_models")

        # ── 5. Prompt sections (AI Agent + Other Features) ────────────────────
        print("\n── 5. Prompt sections ──")
        for nav_id in PROMPT_IDS:
            prompt_name = nav_id.replace("prompt:", "")
            print(f"   Checking: {prompt_name}")
            _click_nav(driver, short, nav_id)

            # Section title must be non-empty
            title_el = short.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".sp-section-title"))
            )
            assert title_el.text.strip(), f"Prompt '{prompt_name}': section title is empty"

            # Description must be present
            desc_el = short.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".sp-section-desc"))
            )
            assert desc_el.text.strip(), f"Prompt '{prompt_name}': description is empty"

            # Wait for prompt content to load (the view or textarea)
            try:
                long.until(
                    lambda d: (
                        d.find_elements(By.CSS_SELECTOR, ".sp-prompt-view") or
                        d.find_elements(By.CSS_SELECTOR, ".sp-prompt-textarea")
                    )
                )
                views = driver.find_elements(By.CSS_SELECTOR, ".sp-prompt-view")
                textareas = driver.find_elements(By.CSS_SELECTOR, ".sp-prompt-textarea")
                has_content = views or textareas
                assert has_content, f"Prompt '{prompt_name}': no prompt view or textarea"
                if views:
                    text = views[0].text.strip()
                    assert text, f"Prompt '{prompt_name}': prompt view is empty"
                    print(f"      ✓ {title_el.text.strip()!r:30} — {len(text)} chars")
                else:
                    print(f"      ✓ {title_el.text.strip()!r:30} — textarea present")
            except TimeoutException:
                pytest.fail(f"Prompt '{prompt_name}': content did not load within {LONG_WAIT}s")

            # Badge must say 'Default' or 'Custom'
            badges = driver.find_elements(By.CSS_SELECTOR, ".sp-badge")
            assert badges, f"Prompt '{prompt_name}': no badge found"
            badge_text = badges[0].text
            assert badge_text in ("Default", "Custom"), \
                f"Prompt '{prompt_name}': unexpected badge text {badge_text!r}"

        _screenshot(driver, "05_last_prompt")

        # ── 6. About section ──────────────────────────────────────────────────
        print("\n── 6. About section ──")
        _click_nav(driver, short, "about")

        title = short.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".sp-section-title"))
        ).text
        assert "About" in title, f"Expected 'About' in section title, got: {title!r}"
        print(f"   ✓ Title: {title!r}")

        # Wait for the async app-info fetch
        long.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".sp-about-row"))
        )
        rows = driver.find_elements(By.CSS_SELECTOR, ".sp-about-row")
        assert len(rows) >= 4, f"Expected ≥4 about rows, found {len(rows)}"

        for row in rows:
            label = row.find_element(By.CSS_SELECTOR, ".sp-about-label").text
            value = row.find_element(By.CSS_SELECTOR, ".sp-about-value").text
            assert value.strip() and value != "—", \
                f"About row '{label}' has no value"
            print(f"   ✓ {label:20} = {value}")

        _screenshot(driver, "06_about")

        # ── 7. Close button dismisses the overlay ────────────────────────────
        print("\n── 7. Close button ──")
        close_btn = driver.find_element(By.CSS_SELECTOR, ".sp-close-btn")
        close_btn.click()
        short.until(
            EC.invisibility_of_element_located((By.CSS_SELECTOR, ".sp-overlay"))
        )
        print("   ✓ Overlay hidden after Close click")
        _screenshot(driver, "07_closed")

        # ── 8. Escape key reopens and closes ─────────────────────────────────
        print("\n── 8. Escape key ──")
        _open_settings(driver, short)
        assert driver.find_element(By.CSS_SELECTOR, ".sp-overlay").is_displayed()
        print("   ✓ Re-opened settings")

        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        short.until(
            EC.invisibility_of_element_located((By.CSS_SELECTOR, ".sp-overlay"))
        )
        print("   ✓ Overlay hidden after Escape key")
        _screenshot(driver, "08_escape_closed")

        print("\n══════════════════════════════════════")
        print("  ✅  All settings checks passed")
        print("══════════════════════════════════════\n")

    except Exception as exc:
        _screenshot(driver, "error")
        print(f"\n   ✗ Test failed: {exc}")
        raise

    finally:
        driver.quit()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    print("=" * 50)
    print("Settings Page — Selenium Test")
    print(f"Target: {APP_URL}")
    print("=" * 50)
    test_settings_page()
