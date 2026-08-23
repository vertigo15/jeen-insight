/**
 * Preferences store
 *
 * Single source of truth for user-tunable runtime settings. Backed by
 * localStorage. Writes are typed and clamped on write so a malformed
 * value can't slip into the rest of the app.
 *
 * Keys (kept stable; renaming would silently reset users' choices):
 *   - 'theme'                  string  'light' | 'dark' | 'system'
 *   - 'rowLimit'               number  in {25, 100, 500, 1000}
   *   - 'chartTypePreference'    string  'auto' | 'bar' | … | 'map'
 *   - 'autoInsights'           string  'on' | 'off'
 *   - 'temperature'            number  in [0, 1] OR null/undefined for "auto"
 *
 * Note: 'theme' was already used by index.html's anti-FOUC bootstrap, so
 * we don't change its key. The bootstrap reads the raw value; this module
 * is the authoritative writer.
 *
 * @module preferences
 */

const KEYS = {
    theme: 'theme',
    rowLimit: 'rowLimit',
    chartType: 'chartTypePreference',
    autoInsights: 'autoInsights',
    temperature: 'temperature',
    aiAnalytics: 'aiAnalytics',   // 'on' | 'off'  — run insights in background
    llmTimeout: 'llmTimeout',     // seconds as string, or 'server' for server default
};

const DEFAULTS = Object.freeze({
    theme: 'light',
    rowLimit: 100,
    chartType: 'auto',
    autoInsights: 'on',
    temperature: null,   // null means "use server default"
    aiAnalytics: 'on',   // show insights in background by default
    llmTimeout: 'server', // 'server' = use server default; else seconds as string
});

const ALLOWED_THEMES = new Set(['light', 'dark', 'system']);
const ALLOWED_ROW_LIMITS = new Set([25, 100, 500, 1000]);
const ALLOWED_CHART_TYPES = new Set([
    'auto', 'bar', 'line', 'pie', 'area', 'scatter', 'horizontal_bar',
    'stacked_bar', 'stacked_area', 'donut', 'combo', 'heatmap', 'gauge', 'map', 'osm_map',
]);
const ALLOWED_AUTO_INSIGHTS = new Set(['on', 'off']);
const ALLOWED_AI_ANALYTICS  = new Set(['on', 'off']);
const ALLOWED_LLM_TIMEOUTS  = new Set(['server', '10', '15', '20', '30', '60', '120']);

function _readString(key, allowed, fallback) {
    try {
        const v = localStorage.getItem(key);
        return v && allowed.has(v) ? v : fallback;
    } catch (_) {
        return fallback;
    }
}

function _readInt(key, allowed, fallback) {
    try {
        const v = parseInt(localStorage.getItem(key) || '', 10);
        return Number.isFinite(v) && allowed.has(v) ? v : fallback;
    } catch (_) {
        return fallback;
    }
}

function _readTemperature() {
    try {
        const raw = localStorage.getItem(KEYS.temperature);
        if (raw === null || raw === '' || raw === 'auto') return null;
        const n = Number(raw);
        if (!Number.isFinite(n)) return null;
        // Clamp to valid bounds; matches the server-side schema.
        return Math.min(1, Math.max(0, n));
    } catch (_) {
        return null;
    }
}

export const Preferences = {
    /** @returns {{ theme: string, rowLimit: number, chartType: string, autoInsights: string, temperature: (number|null) }} */
    getAll() {
        return {
            theme: _readString(KEYS.theme, ALLOWED_THEMES, DEFAULTS.theme),
            rowLimit: _readInt(KEYS.rowLimit, ALLOWED_ROW_LIMITS, DEFAULTS.rowLimit),
            chartType: _readString(KEYS.chartType, ALLOWED_CHART_TYPES, DEFAULTS.chartType),
            autoInsights: _readString(KEYS.autoInsights, ALLOWED_AUTO_INSIGHTS, DEFAULTS.autoInsights),
            temperature: _readTemperature(),
            aiAnalytics: _readString(KEYS.aiAnalytics, ALLOWED_AI_ANALYTICS, DEFAULTS.aiAnalytics),
            llmTimeout: _readString(KEYS.llmTimeout, ALLOWED_LLM_TIMEOUTS, DEFAULTS.llmTimeout),
        };
    },

    setTheme(value) {
        if (!ALLOWED_THEMES.has(value)) return false;
        localStorage.setItem(KEYS.theme, value);
        return true;
    },

    setRowLimit(value) {
        const n = parseInt(value, 10);
        if (!ALLOWED_ROW_LIMITS.has(n)) return false;
        localStorage.setItem(KEYS.rowLimit, String(n));
        return true;
    },

    setChartType(value) {
        if (!ALLOWED_CHART_TYPES.has(value)) return false;
        localStorage.setItem(KEYS.chartType, value);
        return true;
    },

    setAutoInsights(value) {
        if (!ALLOWED_AUTO_INSIGHTS.has(value)) return false;
        localStorage.setItem(KEYS.autoInsights, value);
        return true;
    },

    setAiAnalytics(value) {
        if (!ALLOWED_AI_ANALYTICS.has(value)) return false;
        localStorage.setItem(KEYS.aiAnalytics, value);
        return true;
    },

    /**
     * Set the per-request LLM timeout override.
     * 'server' removes the override so the server default is used.
     */
    setLlmTimeout(value) {
        if (!ALLOWED_LLM_TIMEOUTS.has(value)) return false;
        if (value === 'server') {
            localStorage.removeItem(KEYS.llmTimeout);
        } else {
            localStorage.setItem(KEYS.llmTimeout, value);
        }
        return true;
    },

    /** Returns the timeout in seconds (number) or null if "use server default". */
    getLlmTimeoutSeconds() {
        const v = _readString(KEYS.llmTimeout, ALLOWED_LLM_TIMEOUTS, DEFAULTS.llmTimeout);
        if (v === 'server') return null;
        const n = parseInt(v, 10);
        return Number.isFinite(n) ? n : null;
    },

    /** Pass null/'auto'/empty to fall back to the server default. */
    setTemperature(value) {
        if (value === null || value === undefined || value === '' || value === 'auto') {
            localStorage.removeItem(KEYS.temperature);
            return true;
        }
        const n = Number(value);
        if (!Number.isFinite(n)) return false;
        const clamped = Math.min(1, Math.max(0, n));
        localStorage.setItem(KEYS.temperature, String(clamped));
        return true;
    },

    /**
     * Reset every preference to its default and clear chart-LLM caches that
     * could otherwise mask the change.
     */
    resetAll() {
        Object.values(KEYS).forEach(k => localStorage.removeItem(k));
        // Clear cached LLM chart configs so the next render reflects the new
        // defaults instead of replaying a stale auto/bar config.
        try {
            for (const k of Object.keys(localStorage)) {
                if (k.startsWith('chart_llm_') || k.startsWith('chart_')) {
                    localStorage.removeItem(k);
                }
            }
        } catch (_) { /* ignore */ }
    },

    DEFAULTS,
};
