/**
 * Interface language runtime (classic script).
 *
 * Load order in index.html: the `#i18n-bootstrap` JSON block, the vendored
 * `intl-messageformat` IIFE, then THIS file — before csrf.js and every app
 * script, so `I18n.t()` is available synchronously when the v3 shell is built.
 *
 * The bootstrap is produced by `src.i18n.client_bootstrap()`:
 *   { locale, dir, formatLocale, dateFormat, locales: [{tag, name, dir}], messages }
 * where `messages` is the effective locale's catalog already deep-merged over
 * English, so a missing translation renders English rather than a key path.
 *
 * Contract:
 *   - `t()` returns PLAIN TEXT. Never assign it to innerHTML unescaped when it
 *     carries interpolated user data; use `I18n.h()` (HTML-escaped) in templates.
 *   - Interpolated USER DATA (names, emails, table/column names, raw error
 *     text) should be wrapped with `I18n.isolate(value)` — Unicode bidi
 *     isolates (FSI…PDI) — so a Latin identifier inside a Hebrew sentence (or
 *     vice versa) cannot reorder the words around it. In HTML contexts prefer
 *     `<bdi>`. Catalog-sourced words (units, labels) are interpolated as-is.
 *   - `Intl.*` formatters use `formatLocale` (e.g. "he-IL"); `lang`/catalog use
 *     the UI tag ("he"). UI language never implies currency or time zone.
 */
(function () {
    'use strict';

    const FSI = '\u2068';
    const PDI = '\u2069';

    const FALLBACK = {
        locale: 'en',
        dir: 'ltr',
        formatLocale: 'en-US',
        dateFormat: 'iso',
        locales: [{ tag: 'en', name: 'English', dir: 'ltr' }],
        messages: {},
    };

    function readBootstrap() {
        if (typeof document !== 'undefined') {
            const el = document.getElementById('i18n-bootstrap');
            if (el) {
                try { return JSON.parse(el.textContent || '{}'); } catch (_) { /* fall through */ }
            }
        }
        if (typeof window !== 'undefined' && window.__I18N_BOOTSTRAP__) return window.__I18N_BOOTSTRAP__;
        return null;
    }

    const boot = Object.assign({}, FALLBACK, readBootstrap() || {});
    const messages = boot.messages || {};
    const locale = boot.locale || FALLBACK.locale;
    const dir = boot.dir || FALLBACK.dir;
    const formatLocale = boot.formatLocale || locale;
    let dateFormat = ['auto', 'dmy', 'mdy', 'iso'].includes(boot.dateFormat) ? boot.dateFormat : 'iso';
    const locales = Array.isArray(boot.locales) && boot.locales.length ? boot.locales : FALLBACK.locales;

    const debug = (() => {
        try {
            const host = (typeof location !== 'undefined' && location.hostname) || '';
            return host === 'localhost' || host === '127.0.0.1' || !!(typeof window !== 'undefined' && window.__I18N_DEBUG__);
        } catch (_) { return false; }
    })();

    const imfNamespace = (typeof window !== 'undefined' && window.IntlMessageFormat) || null;
    const MessageFormat = imfNamespace && (imfNamespace.IntlMessageFormat || imfNamespace.default || imfNamespace);

    function lookup(tree, key) {
        let node = tree;
        for (const part of String(key).split('.')) {
            if (!node || typeof node !== 'object' || !(part in node)) return undefined;
            node = node[part];
        }
        return typeof node === 'string' ? node : undefined;
    }

    const compiled = new Map();

    function compile(key, message) {
        const hit = compiled.get(key);
        if (hit && hit.message === message) return hit;
        let entry;
        if (MessageFormat) {
            try {
                entry = { message, fmt: new MessageFormat(message, locale, undefined, { ignoreTag: true }) };
            } catch (err) {
                if (debug) console.warn('[i18n] malformed message for', key, err && err.message);
                entry = { message, fmt: null };
            }
        } else {
            entry = { message, fmt: null };
        }
        compiled.set(key, entry);
        return entry;
    }

    /** Wrap a value in Unicode bidi isolates (FSI…PDI). Empty stays empty. */
    function isolate(value) {
        const text = value == null ? '' : String(value);
        return text ? FSI + text + PDI : text;
    }

    function prepareArgs(args) {
        const out = {};
        for (const [name, value] of Object.entries(args || {})) {
            if (value == null) out[name] = '';
            else if (value instanceof Date || typeof value === 'number' || typeof value === 'boolean' || typeof value === 'string') out[name] = value;
            else out[name] = String(value);
        }
        return out;
    }

    /** Minimal substitution when the ICU formatter is unavailable (tests / partial load). */
    function naiveFormat(message, args) {
        return String(message).replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (m, name) =>
            (name in (args || {})) ? String(args[name]) : m);
    }

    function t(key, args) {
        const message = lookup(messages, key);
        if (message === undefined) {
            if (debug) console.warn('[i18n] missing key', key);
            return String(key);
        }
        if (!args || typeof args !== 'object') return message;
        const entry = compile(key, message);
        const prepared = prepareArgs(args);
        if (!entry.fmt) return naiveFormat(message, prepared);
        try {
            const out = entry.fmt.format(prepared);
            return Array.isArray(out) ? out.join('') : String(out);
        } catch (err) {
            if (debug) console.warn('[i18n] format failed for', key, err && err.message);
            return naiveFormat(message, prepared);
        }
    }

    function escapeHtml(text) {
        return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    /** `t()` escaped for safe use inside innerHTML templates and attributes. */
    function h(key, args) {
        return escapeHtml(t(key, args));
    }

    function has(key) {
        return lookup(messages, key) !== undefined;
    }

    // ── Formatting ───────────────────────────────────────────────────────────

    const numberFormats = new Map();
    function numberFormatter(opts) {
        const sig = JSON.stringify(opts || {});
        let fmt = numberFormats.get(sig);
        if (!fmt) {
            try { fmt = new Intl.NumberFormat(formatLocale, opts || {}); }
            catch (_) { fmt = new Intl.NumberFormat('en-US', opts || {}); }
            numberFormats.set(sig, fmt);
        }
        return fmt;
    }

    function formatNumber(value, opts) {
        const n = Number(value);
        if (!Number.isFinite(n)) return value == null ? '' : String(value);
        return numberFormatter(opts).format(n);
    }

    function formatCompact(value) {
        const n = Number(value);
        if (!Number.isFinite(n)) return value == null ? '\u2014' : String(value);
        return numberFormatter({ notation: n >= 1000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(n);
    }

    const DATE_STYLES = {
        date: { dateStyle: 'medium' },
        dateTime: { dateStyle: 'medium', timeStyle: 'short' },
        time: { hour: '2-digit', minute: '2-digit' },
        timeWithSeconds: { hour: '2-digit', minute: '2-digit', second: '2-digit' },
        weekdayTime: { weekday: 'short', hour: '2-digit', minute: '2-digit' },
        monthYear: { month: 'short', year: 'numeric' },
    };

    function automaticDateFormat() {
        try {
            // Automatic follows the browser/OS region, not the interface
            // language: English UI does not imply United States date order.
            const browserLocale = new Intl.DateTimeFormat().resolvedOptions().locale;
            const parts = new Intl.DateTimeFormat(browserLocale, {
                year: 'numeric', month: '2-digit', day: '2-digit',
            }).formatToParts(new Date(2006, 10, 22, 12));
            const order = parts
                .filter((part) => ['day', 'month', 'year'].includes(part.type))
                .map((part) => part.type)
                .join('-');
            if (order === 'day-month-year') return 'dmy';
            if (order === 'month-day-year') return 'mdy';
        } catch (_) { /* use the stable fallback below */ }
        return 'iso';
    }

    // Resolve only when the account preference changes. Result-table rendering
    // only performs string rearrangement and never creates per-cell formatters.
    let resolvedDateFormat = dateFormat === 'auto' ? automaticDateFormat() : dateFormat;

    function setDateFormat(value, options) {
        if (!['auto', 'dmy', 'mdy', 'iso'].includes(value)) return false;
        const nextResolved = value === 'auto' ? automaticDateFormat() : value;
        const changed = value !== dateFormat || nextResolved !== resolvedDateFormat;
        dateFormat = value;
        resolvedDateFormat = nextResolved;
        if (changed && (!options || options.emit !== false)
            && typeof document !== 'undefined' && typeof CustomEvent !== 'undefined') {
            document.dispatchEvent(new CustomEvent('jeen:preferences-changed', {
                detail: { key: 'dateFormat', value, resolvedValue: nextResolved },
            }));
        }
        return true;
    }

    function formatCalendarDate(value) {
        const text = value == null ? '' : String(value);
        const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
        if (!match) return text;
        const [, year, month, day] = match;
        if (resolvedDateFormat === 'dmy') return `${day}/${month}/${year}`;
        if (resolvedDateFormat === 'mdy') return `${month}/${day}/${year}`;
        return `${year}-${month}-${day}`;
    }

    const dateFormats = new Map();
    function formatDate(value, style) {
        const date = value instanceof Date ? value : new Date(value);
        if (Number.isNaN(date.getTime())) return value == null ? '' : String(value);
        const opts = typeof style === 'object' && style ? style : (DATE_STYLES[style] || DATE_STYLES.dateTime);
        const sig = JSON.stringify(opts);
        let fmt = dateFormats.get(sig);
        if (!fmt) {
            try { fmt = new Intl.DateTimeFormat(formatLocale, opts); }
            catch (_) { fmt = new Intl.DateTimeFormat('en-US', opts); }
            dateFormats.set(sig, fmt);
        }
        return fmt.format(date);
    }

    let relativeFormat = null;
    function relativeFormatter() {
        if (relativeFormat) return relativeFormat;
        try { relativeFormat = new Intl.RelativeTimeFormat(formatLocale, { numeric: 'auto' }); }
        catch (_) { relativeFormat = new Intl.RelativeTimeFormat('en-US', { numeric: 'auto' }); }
        return relativeFormat;
    }

    /**
     * "3 minutes ago" / "לפני 3 דקות". `now` is injectable for tests.
     * Returns '' for unparsable input and the `common.never` message for null.
     */
    function formatRelative(value, now) {
        if (value == null || value === '') return t('common.never');
        const date = value instanceof Date ? value : new Date(value);
        if (Number.isNaN(date.getTime())) return '';
        const base = now instanceof Date ? now.getTime() : (typeof now === 'number' ? now : Date.now());
        const diffSec = Math.round((date.getTime() - base) / 1000);
        const abs = Math.abs(diffSec);
        const rtf = relativeFormatter();
        if (abs < 45) return t('common.justNow');
        if (abs < 3600) return rtf.format(Math.round(diffSec / 60), 'minute');
        if (abs < 86400) return rtf.format(Math.round(diffSec / 3600), 'hour');
        if (abs < 86400 * 30) return rtf.format(Math.round(diffSec / 86400), 'day');
        if (abs < 86400 * 365) return rtf.format(Math.round(diffSec / (86400 * 30)), 'month');
        return rtf.format(Math.round(diffSec / (86400 * 365)), 'year');
    }

    function nativeName(tag) {
        const hit = locales.find((l) => l.tag === tag);
        if (hit && hit.name) return hit.name;
        try {
            const name = new Intl.DisplayNames([tag], { type: 'language' }).of(tag);
            if (name && name !== tag) return name.charAt(0).toLocaleUpperCase(tag) + name.slice(1);
        } catch (_) { /* fall through */ }
        return tag;
    }

    // ── Interface-error boundary ─────────────────────────────────────────────
    // Backend/proxy responses carry English `error`/`detail` text (and sometimes
    // a stable `code`). The UI shows a localized headline from the code or HTTP
    // status and keeps the raw text only as secondary technical detail.

    const CODE_KEYS = {
        UNAUTHENTICATED: 'errors.unauthenticated',
        FORBIDDEN: 'errors.forbidden',
        CSRF: 'errors.csrf',
        RATE_LIMITED: 'errors.rateLimited',
        SETUP_REQUIRED: 'errors.setupRequired',
        BACKEND_UNAVAILABLE: 'errors.backendUnavailable',
        INVALID_LOCALE: 'errors.badRequest',
        DB_ERROR: 'errors.serverError',
        QUERY_FAILED: 'errors.queryFailed',
    };

    function statusKey(status) {
        const s = Number(status);
        if (s === 400 || s === 422) return 'errors.badRequest';
        if (s === 401) return 'errors.unauthenticated';
        if (s === 403) return 'errors.forbidden';
        if (s === 404) return 'errors.notFound';
        if (s === 409) return 'errors.conflict';
        if (s === 429) return 'errors.rateLimited';
        if (s === 502 || s === 503 || s === 504) return 'errors.backendUnavailable';
        if (s >= 500) return 'errors.serverError';
        return 'errors.requestFailed';
    }

    function rawDetail(payload) {
        if (!payload) return '';
        if (typeof payload === 'string') return payload;
        const d = payload.detail != null ? payload.detail : payload.error;
        if (d == null) return '';
        if (typeof d === 'string') {
            // Flask wraps upstream bodies as {"error": "<json text>"}; unwrap once.
            const trimmed = d.trim();
            if (trimmed.startsWith('{')) {
                try { return rawDetail(JSON.parse(trimmed)) || trimmed; } catch (_) { return trimmed; }
            }
            return trimmed;
        }
        try { return JSON.stringify(d); } catch (_) { return String(d); }
    }

    /**
     * Localized description of a failed request.
     * @param {{status?: number, code?: string, payload?: any, error?: Error, network?: boolean}} info
     * @returns {{ message: string, detail: string, code: string }}
     */
    function describeError(info) {
        info = info || {};
        const payload = info.payload;
        const code = info.code || (payload && typeof payload === 'object' && payload.code) || '';
        let key;
        if (info.network || (info.error && info.error.name === 'TypeError' && !info.status)) key = 'errors.network';
        else if (code && CODE_KEYS[code]) key = CODE_KEYS[code];
        else if (info.status) key = statusKey(info.status);
        else key = 'errors.requestFailed';
        const detail = rawDetail(payload) || (info.error && info.error.message) || '';
        return { message: t(key), detail, code: String(code || '') };
    }

    /**
     * Headline + optional technical detail for a toast. Detail is kept LTR and
     * bidi-isolated because it is machine text (SQL, stack, English prose).
     */
    function errorText(info) {
        const d = describeError(info);
        return d.detail && d.detail !== d.message ? `${d.message} \u2014 ${FSI}${d.detail}${PDI}` : d.message;
    }

    // ── Document ─────────────────────────────────────────────────────────────

    function apply() {
        if (typeof document === 'undefined') return;
        const html = document.documentElement;
        if (html.getAttribute('lang') !== locale) html.setAttribute('lang', locale);
        if (html.getAttribute('dir') !== dir) html.setAttribute('dir', dir);
    }

    const api = {
        locale,
        dir,
        formatLocale,
        get dateFormat() { return dateFormat; },
        get resolvedDateFormat() { return resolvedDateFormat; },
        locales,
        isRtl: dir === 'rtl',
        t,
        h,
        has,
        escapeHtml,
        isolate,
        formatNumber,
        formatCompact,
        formatCalendarDate,
        setDateFormat,
        formatDate,
        formatRelative,
        nativeName,
        describeError,
        errorText,
        apply,
        DATE_STYLES,
    };

    if (typeof window !== 'undefined') {
        window.I18n = api;
        apply();
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
