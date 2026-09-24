"""The client i18n runtime, run through Node with the REAL vendored formatter.

Loads ``intl-messageformat.iife.js`` + ``static/i18n/i18n.js`` with a fixture
bootstrap (the way index.html does), then checks interpolation, Hebrew
plurals, English fallback, bidi isolation, XSS-inert output, formatting and
the interface-error boundary. Also checks that ``dir_for`` / locale
negotiation agree between the Python registry and the client runtime.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from src import i18n

ROOT = Path(__file__).resolve().parents[2]
IIFE = ROOT / "src/static/vendor/intl-messageformat/intl-messageformat.iife.js"
RUNTIME = ROOT / "src/static/i18n/i18n.js"


def _run(
    locale: str,
    script: str,
    *,
    date_format: str = "iso",
    browser_locale: str = "en-US",
    timezone: str = "UTC",
) -> dict:
    bootstrap = i18n.client_bootstrap(locale)
    bootstrap["dateFormat"] = date_format
    prelude = f"""
      const fs = require('fs');
      const vm = require('vm');
      const ctx = {{ window: {{}} , console }};
      ctx.window.__I18N_BOOTSTRAP__ = {json.dumps(bootstrap, ensure_ascii=False)};
      vm.createContext(ctx);
      vm.runInContext(fs.readFileSync({str(IIFE)!r}, 'utf8'), ctx);
      ctx.window.IntlMessageFormat = ctx.IntlMessageFormat;
      vm.runInContext(`
        const NativeDateTimeFormat = Intl.DateTimeFormat;
        Intl.DateTimeFormat = function(locales, options) {{
          return new NativeDateTimeFormat(
            locales === undefined ? {json.dumps(browser_locale)} : locales,
            options
          );
        }};
        Intl.DateTimeFormat.prototype = NativeDateTimeFormat.prototype;
      `, ctx);
      vm.runInContext(fs.readFileSync({str(RUNTIME)!r}, 'utf8'), ctx);
      const I18n = ctx.window.I18n;
      const out = {{}};
    """
    code = prelude + script + "\nprocess.stdout.write(JSON.stringify(out));"
    env = {**os.environ, "TZ": timezone}
    result = subprocess.run(
        ["node", "-e", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout)


def test_runtime_reads_bootstrap_and_formats_icu():
    out = _run("en", """
      out.locale = I18n.locale; out.dir = I18n.dir; out.formatLocale = I18n.formatLocale;
      out.plain = I18n.t('common.save');
      out.one = I18n.t('conversation.list.questionCount', { count: 1 });
      out.many = I18n.t('conversation.list.questionCount', { count: 5 });
      out.args = I18n.t('conversation.empty.suggestedFor', { connection: 'Sales DW' });
      out.missing = I18n.t('nope.missing.key');
      out.has = [I18n.has('common.save'), I18n.has('nope.missing.key')];
      out.number = I18n.formatNumber(1234567.891, { maximumFractionDigits: 1 });
      out.compact = I18n.formatCompact(1234567);
      out.rel = I18n.formatRelative(new Date(Date.now() - 3 * 60000).toISOString());
      out.never = I18n.formatRelative(null);
    """)
    assert (out["locale"], out["dir"], out["formatLocale"]) == ("en", "ltr", "en-US")
    assert out["plain"] == "Save"
    assert out["one"] == "1 question" and out["many"] == "5 questions"
    assert out["args"] == "Suggested for Sales DW"
    assert out["missing"] == "nope.missing.key"
    assert out["has"] == [True, False]
    assert out["number"] == "1,234,567.9"
    assert out["compact"] == "1.2M"
    assert out["rel"] == "3 minutes ago"
    assert out["never"] == "never"


def test_hebrew_runtime_plurals_direction_and_formatting():
    out = _run("he", """
      out.dir = I18n.dir; out.isRtl = I18n.isRtl; out.formatLocale = I18n.formatLocale;
      out.one = I18n.t('conversation.list.questionCount', { count: 1 });
      out.two = I18n.t('conversation.list.questionCount', { count: 2 });
      out.many = I18n.t('conversation.list.questionCount', { count: 11 });
      out.save = I18n.t('common.save');
      out.rel = I18n.formatRelative(new Date(Date.now() - 3 * 60000).toISOString());
      out.names = I18n.locales.map((l) => l.name);
      out.nativeHe = I18n.nativeName('he');
    """)
    assert out["dir"] == "rtl" and out["isRtl"] is True and out["formatLocale"] == "he-IL"
    assert out["one"] == "שאלה אחת"
    assert out["two"] == "2 שאלות"
    assert out["many"] == "11 שאלות"
    assert out["save"] == "שמירה"
    assert "3" in out["rel"] and "דקות" in out["rel"]
    assert out["names"] == ["English", "עברית"]
    assert out["nativeHe"] == "עברית"


def test_calendar_date_format_defaults_to_iso_and_auto_uses_browser_region():
    default = _run("en", """
      out.preference = I18n.dateFormat;
      out.resolved = I18n.resolvedDateFormat;
      out.date = I18n.formatCalendarDate('2026-09-24');
    """)
    auto_us = _run(
        "he",
        "out.resolved = I18n.resolvedDateFormat; out.date = I18n.formatCalendarDate('2026-09-24');",
        date_format="auto",
        browser_locale="en-US",
    )
    auto_gb = _run(
        "en",
        "out.resolved = I18n.resolvedDateFormat; out.date = I18n.formatCalendarDate('2026-09-24');",
        date_format="auto",
        browser_locale="en-GB",
    )
    iso = _run("en", "out.date = I18n.formatCalendarDate('2026-09-24');", date_format="iso")
    dmy = _run("en", "out.date = I18n.formatCalendarDate('2026-09-24');", date_format="dmy")

    assert default == {"preference": "iso", "resolved": "iso", "date": "2026-09-24"}
    assert auto_us == {"resolved": "mdy", "date": "09/24/2026"}
    assert auto_gb == {"resolved": "dmy", "date": "24/09/2026"}
    assert iso["date"] == "2026-09-24"
    assert dmy["date"] == "24/09/2026"


def test_calendar_date_format_changes_live_rejects_invalid_and_is_timezone_safe():
    script = """
      out.before = [I18n.dateFormat, I18n.formatCalendarDate('2026-01-02')];
      out.changed = I18n.setDateFormat('dmy');
      out.after = [I18n.dateFormat, I18n.resolvedDateFormat, I18n.formatCalendarDate('2026-01-02')];
      out.rejected = I18n.setDateFormat('browser');
      out.final = [I18n.dateFormat, I18n.formatCalendarDate('2026-01-02')];
    """
    west = _run("en", script, timezone="Pacific/Honolulu")
    east = _run("en", script, timezone="Pacific/Kiritimati")
    invalid_boot = _run(
        "en",
        "out.preference = I18n.dateFormat; out.date = I18n.formatCalendarDate('2026-01-02');",
        date_format="invalid",
    )

    expected = {
        "before": ["iso", "2026-01-02"],
        "changed": True,
        "after": ["dmy", "dmy", "02/01/2026"],
        "rejected": False,
        "final": ["dmy", "02/01/2026"],
    }
    assert west == expected
    assert east == expected
    assert invalid_boot == {"preference": "iso", "date": "2026-01-02"}


def test_translations_are_inert_text_and_isolation_is_explicit():
    payload = '<img src=x onerror=alert(1)>'
    out = _run("en", f"""
      const evil = {json.dumps(payload)};
      out.text = I18n.t('conversation.empty.suggestedFor', {{ connection: evil }});
      out.html = I18n.h('conversation.empty.suggestedFor', {{ connection: evil }});
      out.iso = I18n.isolate('Sales');
      out.isoEmpty = I18n.isolate('');
      out.plainArgKeepsUnit = I18n.t('analysis.validation.atLeast', {{ value: '12 months' }});
    """)
    # t() is plain text: the payload passes through unchanged (callers decide the sink)…
    assert out["text"] == f"Suggested for {payload}"
    # …while h() is safe to drop into innerHTML.
    assert "<img" not in out["html"] and "&lt;img" in out["html"]
    assert out["iso"] == "\u2068Sales\u2069" and out["isoEmpty"] == ""
    # Catalog-sourced words are not wrapped in bidi marks unless asked.
    assert out["plainArgKeepsUnit"] == "At least 12 months."


def test_error_boundary_maps_codes_and_statuses():
    out = _run("he", """
      out.code = I18n.describeError({ status: 503, payload: { error: 'Backend unavailable: boom', code: 'BACKEND_UNAVAILABLE' } });
      out.status = I18n.describeError({ status: 404, payload: { detail: 'Session not found' } });
      out.wrapped = I18n.describeError({ status: 400, payload: { error: '{"detail": "Question cannot be empty"}' } });
      out.network = I18n.describeError({ error: Object.assign(new TypeError('Failed to fetch'), {}), network: true });
      out.text = I18n.errorText({ status: 404, payload: { detail: 'Session not found' } });
    """)
    assert out["code"]["message"] == "שירות הניתוח אינו זמין כרגע"
    assert out["code"]["detail"] == "Backend unavailable: boom"
    assert out["status"]["message"] == "לא נמצא" and out["status"]["detail"] == "Session not found"
    assert out["wrapped"]["detail"] == "Question cannot be empty"
    assert out["network"]["message"].startswith("לא ניתן להתחבר לשרת")
    assert out["text"].startswith("לא נמצא") and "\u2068Session not found\u2069" in out["text"]


def test_python_and_client_agree_on_direction_and_negotiation():
    """The client derives `dir` from the bootstrap the server computed."""
    for locale in i18n.LOCALES:
        out = _run(locale, "out.dir = I18n.dir;")
        assert out["dir"] == i18n.dir_for(locale)
