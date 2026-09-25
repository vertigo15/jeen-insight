/**
 * Insights Workspace v3
 * One conversation store drives the thread, selected result, chart, table and dock.
 */
(function () {
    'use strict';

    // Interface strings come from the locale catalog (static/i18n/i18n.js).
    // `t` is plain text; `h` is HTML-escaped for template literals/attributes.
    const t = (key, args) => (window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
    const escFull = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
    const h = (key, args) => escFull(t(key, args));
    // Bidi-isolate user data (names, questions, identifiers) interpolated into a sentence.
    const iso = (value) => (window.I18n && typeof window.I18n.isolate === 'function' ? window.I18n.isolate(value) : String(value == null ? '' : value));
    const isRtl = () => Boolean(window.I18n && window.I18n.isRtl);
    // Interface-error boundary: a localized headline (from a stable code or the
    // HTTP status) with the raw backend text kept as secondary, bidi-isolated detail.
    const errorText = (info, fallback) => (window.I18n && typeof window.I18n.errorText === 'function'
        ? window.I18n.errorText(info)
        : fallback);

    const PHASES = [
        { id: 'memory', label: t('phases.memory') },
        { id: 'router', label: t('phases.router') },
        { id: 'catalog', label: t('phases.catalog') },
        { id: 'generation', label: t('phases.generation') },
        { id: 'validation', label: t('phases.validation') },
        { id: 'execution', label: t('phases.execution') },
        { id: 'analytics', label: t('phases.analytics') },
        { id: 'format', label: t('phases.format') },
        { id: 'save', label: t('phases.save') },
    ];

    const NODE_PHASE = {
        context_composer: 'memory',
        memory_answer_generator: 'memory',
        history_search: 'memory',
        fused_router: 'router',
        capability_answer: 'router',
        pre_graph_setup: 'catalog',
        catalog_help_answer: 'catalog',
        catalog_lookup: 'catalog',
        prior_data_binder: 'catalog',
        prompt_builder: 'catalog',
        dax_catalog_lookup: 'catalog',
        dax_entity_resolver: 'catalog',
        dax_prompt_builder: 'catalog',
        dax_query_planner: 'generation',
        sql_generator: 'generation',
        dax_generator: 'generation',
        dax_repair: 'generation',
        sqlglot_validate: 'validation',
        dlp_check: 'validation',
        dax_static_validate: 'validation',
        result_integrity_check: 'validation',
        execute_query: 'execution',
        pbi_execute_query: 'execution',
        trivial_result_check: 'execution',
        feedback_classifier: 'execution',
        dax_feedback_router: 'execution',
        fused_eval_analytics: 'analytics',
        response_formatter: 'format',
        save_to_memory: 'save',
        observability_log: 'save',
    };

    // Mirrors the 'conversationTabs' preference owned by settings/preferences.js.
    // Read raw here because that module is imported asynchronously after this
    // script has already mounted the shell; hidden is the default.
    function conversationTabsPreferred() {
        try { return localStorage.getItem('conversationTabs') === 'show'; } catch (_) { return false; }
    }

    const esc = (value) => {
        if (typeof window.escapeHtml === 'function') return window.escapeHtml(String(value == null ? '' : value));
        return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    };

    function textOf(value) {
        if (value == null) return '';
        if (typeof value === 'string' || typeof value === 'number') return String(value);
        if (Array.isArray(value)) return value.map(textOf).join('');
        if (typeof value === 'object') return textOf(value.t || value.text || value.content || '');
        return '';
    }

    function directionOf(value) {
        const text = textOf(value);
        const rtlCount = (text.match(/[\u0590-\u05ff\u0600-\u08ff]/g) || []).length;
        const ltrCount = (text.match(/[A-Za-z\u00c0-\u02af]/g) || []).length;
        return rtlCount > ltrCount ? 'rtl' : 'ltr';
    }

    // Producers whose text answer is trusted to be Markdown (rendered as safe,
    // formatted HTML). Everything else stays escaped plain text so a stray * or #
    // from user/DB data is never reformatted. Keyed by the turn's route, which is
    // present live (routing.route) and after reload (metrics.route).
    const MARKDOWN_ROUTES = new Set(['capability', 'catalog_help']);
    function routeOf(result) {
        const r = result || {};
        return (r.routing && r.routing.route) || (r.metrics && r.metrics.route) || '';
    }
    // A string answer from an allowlisted route is rendered as Markdown; fragment
    // arrays (eval summaries) and every other producer keep the escaped path.
    function wantsMarkdown(result) {
        return !!(result && typeof result.answer === 'string'
            && MARKDOWN_ROUTES.has(routeOf(result)) && window.MarkdownLite);
    }
    function markdownDiv(src) {
        return `<div class="v3-markdown" dir="${directionOf(src)}">${window.MarkdownLite.render(src)}</div>`;
    }

    function formatMs(ms) {
        if (ms === null || ms === undefined || ms === '') return '—';
        const n = Number(ms);
        if (!Number.isFinite(n)) return '—';
        return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 1 : 2)}s` : `${Math.round(n)}ms`;
    }

    function formatCompact(value) {
        if (window.I18n && typeof window.I18n.formatCompact === 'function') return window.I18n.formatCompact(value);
        const n = Number(value);
        if (!Number.isFinite(n)) return value == null ? '—' : String(value);
        return new Intl.NumberFormat(undefined, { notation: n >= 1000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(n);
    }

    function rowValue(row, column, index) {
        return Array.isArray(row) ? row[index] : row && row[column];
    }

    function normalizeRows(results) {
        return (results && (results.rows || results.data)) || [];
    }

    function filterResultRows(results, query) {
        const columns = (results && results.columns) || [];
        const rows = normalizeRows(results);
        const needle = String(query || '').trim().toLowerCase();
        if (!needle) return rows.slice();
        const metadata = resultColumnMetadata(results);
        return rows.filter((row) => columns.some((column, index) => {
            const raw = rowValue(row, column, index);
            const rawText = String(raw ?? '').toLowerCase();
            if (rawText.includes(needle)) return true;
            const meta = metadata[index];
            return meta?.type === 'datetime'
                && formatResultDateTime(raw, meta.omitMidnightTime).toLowerCase().includes(needle);
        }));
    }

    /**
     * Settings > General "Auto-insights" (`autoInsights`) is the user-facing
     * switch; the legacy `aiAnalytics` key from the old panel still counts as
     * an off switch so an existing preference is not silently ignored.
     */
    function insightsEnabled(prefs) {
        return (prefs.autoInsights || 'on') === 'on' && (prefs.aiAnalytics || 'on') === 'on';
    }

    /**
     * A turn whose answer pane can be shown: finished, or still streaming with
     * its provisional rows already here, or failed after those rows arrived
     * (the table stays; the thread shows the error).
     */
    function turnShowsResult(turn) {
        if (!turn) return false;
        if (turn.status === 'success' || turn.status === 'streaming') return true;
        return turn.status === 'error'
            && turn.provisionalRevision != null
            && normalizeRows(turn.result && turn.result.results).length > 0;
    }

    function selectionForTurn(selectedResultId, turn) {
        return {
            selectedTurnId: turn?.id || null,
            selectedResultId: turnShowsResult(turn) ? turn.id : selectedResultId,
        };
    }

    /**
     * The number in a cell, or NaN. Postgres `money` (and formatted numbers)
     * reach the browser as strings like "$9,389,789.94" — they are still
     * numbers to the grid: sortable, formattable, profilable.
     */
    function numericValue(value) {
        if (typeof value === 'number') return value;
        if (typeof value !== 'string') return NaN;
        const text = value.trim();
        if (!/^[-+]?[$€£¥₪]?\s*[-+]?\d[\d,]*(\.\d+)?%?$/.test(text)) return NaN;
        return Number(text.replace(/[^0-9.\-]/g, ''));
    }

    function inferColumnType(values) {
        const present = values.filter((v) => v !== null && v !== undefined && v !== '');
        if (!present.length) return 'empty';
        if (present.every((v) => typeof v === 'number' || (typeof v === 'string' && Number.isFinite(numericValue(v))))) return 'number';
        if (present.every((v) => typeof v === 'boolean')) return 'boolean';
        if (present.every((v) => resultDateTimeParts(v)
            || (!Number.isNaN(Date.parse(v)) && /[-/:T]/.test(String(v))))) return 'datetime';
        return 'text';
    }

    /**
     * Parse a canonical SQL/ISO calendar timestamp without constructing a Date.
     * Keeping the calendar components as strings prevents timezone conversion
     * from shifting a database date to the previous or next day.
     */
    function resultDateTimeParts(value) {
        const text = value == null ? '' : String(value).trim();
        const match = /^(\d{4}-\d{2}-\d{2})(?:([ T])(\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?(Z|[+-]\d{2}(?::?\d{2})?)?)?$/.exec(text);
        if (!match) return null;
        const hasTime = Boolean(match[2]);
        const fraction = match[6] || '';
        return {
            date: match[1],
            hasTime,
            time: hasTime ? text.slice(11) : '',
            midnight: !hasTime || (
                match[3] === '00'
                && match[4] === '00'
                && (!match[5] || match[5] === '00')
                && (!fraction || /^\.[0]+$/.test(fraction))
            ),
        };
    }

    function formatResultDateTime(value, omitMidnightTime) {
        const parts = resultDateTimeParts(value);
        if (!parts) return String(value);
        const date = window.I18n?.formatCalendarDate?.(parts.date) || parts.date;
        if (!parts.hasTime || (omitMidnightTime && parts.midnight)) return date;
        return `${date} ${parts.time}`;
    }

    const resultColumnMetaCache = new WeakMap();

    /**
     * Stable, preference-independent metadata for one result object. Filtering,
     * sorting and map interactions re-render the same result, so scan its rows
     * once rather than rebuilding date-column metadata on every interaction.
     */
    function resultColumnMetadata(results) {
        if (!results || typeof results !== 'object') return [];
        const columns = results.columns || [];
        const rows = normalizeRows(results);
        const cached = resultColumnMetaCache.get(results);
        if (cached && cached.columns === columns && cached.rows === rows
            && cached.columnCount === columns.length && cached.rowCount === rows.length) {
            return cached.metadata;
        }

        const metadata = columns.map((name, index) => {
            const sampled = rows.slice(0, 50).map((row) => rowValue(row, name, index));
            const type = inferColumnType(sampled);
            const numeric = type === 'number';
            const plain = numeric && isPlainNumberColumn(
                name,
                rows.map((row) => rowValue(row, name, index)),
            );
            let sawDate = false;
            let omitMidnightTime = type === 'datetime';
            if (type === 'datetime') {
                for (const row of rows) {
                    const value = rowValue(row, name, index);
                    if (value === null || value === undefined || value === '') continue;
                    sawDate = true;
                    const parts = resultDateTimeParts(value);
                    if (!parts || !parts.midnight) {
                        omitMidnightTime = false;
                        break;
                    }
                }
            }
            return {
                name,
                sourceIndex: index,
                type,
                numeric,
                plain,
                omitMidnightTime: sawDate && omitMidnightTime,
            };
        });
        resultColumnMetaCache.set(results, {
            columns,
            rows,
            columnCount: columns.length,
            rowCount: rows.length,
            metadata,
        });
        return metadata;
    }

    const ID_WORDS = new Set(['id', 'key', 'pk', 'uuid', 'code']);
    // Hebrew puts the identifier word first: "מזהה_לקוח", "קוד_מוצר".
    const HE_ID_WORDS = new Set(['מזהה', 'קוד', 'מפתח']);
    const YEAR_WORDS = new Set(['year', 'yr', 'fy', 'שנה', 'שנת']);

    /** "CustomerKey" / "order_year" -> ["customer", "key"] / ["order", "year"]. */
    function nameWords(name) {
        return String(name || '')
            .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
            .toLowerCase()
            .split(/[^a-z0-9\u0590-\u05ff]+/)
            .filter(Boolean);
    }

    /**
     * Numbers that name something rather than measure it: ids and keys (by the
     * column name alone) and years (a year word plus 4-digit integers in every
     * row, so "sales_per_year" totals still get separators).
     */
    function isPlainNumberColumn(name, values) {
        const words = nameWords(name);
        if (!words.length) return false;
        if (ID_WORDS.has(words[words.length - 1]) || HE_ID_WORDS.has(words[0])) return true;
        if (!words.some((word) => YEAR_WORDS.has(word))) return false;
        const present = values.filter((v) => v !== null && v !== undefined && v !== '');
        return present.length > 0 && present.every((v) => {
            const n = numericValue(v);
            return Number.isInteger(n) && n >= 1000 && n <= 9999;
        });
    }

    function compactProfile(results) {
        const columns = (results && results.columns) || [];
        const rows = normalizeRows(results);
        const metadata = resultColumnMetadata(results);
        return columns.map((name, index) => {
            const values = rows.map((row) => rowValue(row, name, index));
            const present = values.filter((v) => v !== null && v !== undefined && v !== '');
            const meta = metadata[index] || {};
            const type = meta.type || inferColumnType(values);
            let range = t('results.profile.noValues');
            if (present.length) {
                if (type === 'number') {
                    const nums = present.map(numericValue);
                    const show = isPlainNumberColumn(name, present) ? String : formatCompact;
                    range = `${show(Math.min(...nums))} – ${show(Math.max(...nums))}`;
                } else if (type === 'datetime') {
                    const strings = present.map(String).sort((a, b) => a.localeCompare(b));
                    range = `${formatResultDateTime(strings[0], meta.omitMidnightTime)} – ${formatResultDateTime(strings[strings.length - 1], meta.omitMidnightTime)}`;
                } else {
                    const strings = present.map(String).sort((a, b) => a.localeCompare(b));
                    range = `${strings[0]} – ${strings[strings.length - 1]}`;
                }
            }
            return {
                name,
                type,
                range,
                distinct: new Set(present.map((v) => String(v))).size,
                nullPct: rows.length ? Math.round(((rows.length - present.length) / rows.length) * 1000) / 10 : 0,
                fillPct: rows.length ? Math.max(4, (present.length / rows.length) * 100) : 0,
            };
        });
    }

    function cappedMeta(results) {
        if (!results) return { capped: false };
        const cap = results.cap || results.row_cap || results.max_rows || null;
        const total = results.total_matched || results.total_rows || null;
        const loaded = normalizeRows(results).length;
        const capped = Boolean(results.truncated || results.is_partial || (cap && loaded >= cap));
        return { capped, cap: cap || loaded, total, loaded };
    }

    function safeTraceNote(event) {
        const node = event?.node || '';
        if (/generator|repair|prompt_builder|summarizer|memory_answer/.test(node)) {
            return event.type === 'llm' ? t('conversation.trace.modelStep') : t('conversation.trace.contextPrepared');
        }
        if (node === 'fused_eval_analytics') return t('conversation.trace.analysisCompleted');
        if (node === 'execute_query' || node === 'pbi_execute_query') {
            return /^\d+ rows/.test(event.detail || '') ? event.detail : t('conversation.trace.readOnlyQuery');
        }
        const safeNodes = new Set([
            'context_composer', 'fused_router', 'pre_graph_setup', 'catalog_lookup',
            'dax_catalog_lookup', 'sqlglot_validate', 'dlp_check',
            'dax_static_validate', 'trivial_result_check', 'feedback_classifier',
            'dax_feedback_router', 'result_integrity_check', 'response_formatter',
            'save_to_memory', 'observability_log',
        ]);
        return safeNodes.has(node) ? String(event.detail || event.type || '') : String(event.type || '');
    }

    const ICON = {
        table: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11"/></svg>',
        history: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></svg>',
        settings: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21h-4v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1-2.8-2.8.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3v-4h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1 2.8-2.8.1.1a1.7 1.7 0 0 0 1.8.3 1.7 1.7 0 0 0 1-1.5V3h4v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1 2.8 2.8-.1.1a1.7 1.7 0 0 0-.3 1.8 1.7 1.7 0 0 0 1.5 1h.2v4h-.2a1.7 1.7 0 0 0-1.4 1Z"/></svg>',
        conversation: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/></svg>',
        railConversation: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/><path d="M8 10h8M8 14h5"/></svg>',
        pin: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 3h6l-1 6 3 3H7l3-3-1-6Z"/></svg>',
        export: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 3v12M7 8l5-5 5 5M5 14v6h14v-6"/></svg>',
        copy: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>',
        star: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-2.9-5.6 2.9 1.1-6.2L3 9.6l6.2-.9L12 3Z"/></svg>',
        code: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m8 9-3 3 3 3M16 9l3 3-3 3M14 5l-4 14"/></svg>',
        pencil: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
        thumbUp: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3Z"/><path d="M7 11l4-7a2.2 2.2 0 0 1 2.2 2.2V9h5.3a2 2 0 0 1 2 2.3l-1.1 6.9a2 2 0 0 1-2 1.8H7"/></svg>',
        thumbDown: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M17 13V4h3a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-3Z"/><path d="M17 13l-4 7a2.2 2.2 0 0 1-2.2-2.2V15H5.5a2 2 0 0 1-2-2.3l1.1-6.9A2 2 0 0 1 6.6 4H17"/></svg>',
        // Jeen UI "feedback" glyph (speech bubble with two lines) and the
        // "chat + heart" mark used in the Give feedback dialog header.
        feedback: '<svg width="16" height="16" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M6.07 7.29h5.85M6.07 9.89h3.58"/><path d="M2.5 8.59c0-.5.01-.99.03-1.46.05-1.55.08-2.32.7-2.95.63-.63 1.42-.67 3.01-.74A76 76 0 0 1 9 3.39c.96 0 1.89.02 2.76.06 1.59.07 2.38.1 3 .73.63.64.66 1.41.71 2.95.02.47.03.96.03 1.46s-.01.99-.03 1.46c-.05 1.55-.08 2.32-.71 2.95-.62.63-1.41.66-3 .73-.48.02-.97.04-1.48.05-.48.01-.72.01-.93.09-.21.08-.39.24-.75.54l-1.42 1.22a.47.47 0 0 1-.78-.36V13.74l-.16-.01c-1.59-.07-2.38-.1-3.01-.73-.62-.63-.65-1.41-.7-2.95A38 38 0 0 1 2.5 8.59Z"/></svg>',
        chatHeart: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"><path d="M11.34 10.47h.01M7.88 10.47h.01"/><path d="M20 9.6c0 .29.001.58.001.87 0 .67-.012 1.32-.034 1.95-.073 2.06-.109 3.09-.946 3.93-.836.84-1.894.89-4.01.98-.636.03-1.294.05-1.971.06-.642.01-.963.02-1.246.13-.282.11-.52.31-.994.72l-1.889 1.62a.63.63 0 0 1-1.045-.48v-2.04l-.211-.008c-2.115-.09-3.173-.14-4.01-.98-.836-.84-.873-1.87-.945-3.93A52 52 0 0 1 2.667 10.47c0-.67.012-1.32.034-1.95.072-2.06.109-3.09.945-3.93.837-.84 1.895-.89 4.01-.98.897-.04 1.839-.06 2.812-.07"/><path class="v3-fb-heart" d="M16.53 8.73s-3.47-2.14-3.47-4.21c0-1.02.73-1.85 1.74-1.85.52 0 1.04.18 1.73.89.7-.71 1.22-.89 1.74-.89 1 0 1.73.83 1.73 1.85 0 2.07-3.47 4.21-3.47 4.21Z" fill="currentColor" stroke="none"/></svg>',
        starFill: '<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M12.006 17.865l4.737 3.029a.76.76 0 0 0 1.084-.836l-1.288-5.652 4.216-3.781a.75.75 0 0 0-.415-1.33l-5.532-.468-2.132-5.365a.75.75 0 0 0-1.34 0L9.205 8.837l-5.533.468a.75.75 0 0 0-.415 1.335l4.216 3.781-1.288 5.648a.76.76 0 0 0 1.084.836l4.737-3.03Z"/></svg>',
    };

    const WorkspaceController = {
        turns: [],
        selectedTurnId: null,
        selectedResultId: null,
        seq: 0,
        sending: false,
        activeTab: 'conversation',
        dockTab: 'sql',
        dockOpen: false,
        _sqlDockWasOpen: false,
        chartCollapsed: true,
        chartOptionsOpen: false,
        filter: '',
        mapSelectedRows: new Set(),
        desktopPreference: true,
        autoCollapsed: false,
        // Inline "edit a sent message": the turn currently in edit mode and the
        // working draft of its question. Only one turn edits at a time.
        editingTurnId: null,
        editDraft: '',
        // The open "Give feedback" dialog (turnId, rating, type, message…) or null.
        feedbackDialog: null,
        lastAppliedResultId: null,
        // Conversation persistence: the restored conversation header, the
        // hydration state and the generation guard that lets a connection
        // switch / reset / new hydration invalidate anything still in flight
        // (a live query stream, a /last fetch, an artifact fetch).
        conversation: null,
        hydrating: false,
        readOnly: false,
        _pendingOpen: null,
        _generation: 0,
        _selectionVersion: 0,
        _streamAbort: null,
        _hydrateAbort: null,
        _hydration: null,
        _analysisRerunInFlight: null,
        _analysisRerunAbort: null,
        _turnRerunAborts: new Set(),
        _proposalExpiryTimer: null,
        savedView: 'answers',
        _favoriteList: null,
        _unavailableSavedAnswer: null,

        init() {
            if (document.getElementById('v3-shell')) return;
            this._buildShell();
            this._moveProductionNodes();
            this._bind();
            document.addEventListener('jeen:connection-resolved', (event) => {
                const sourceKey = event.detail?.source_key;
                if (sourceKey) this.hydrate(sourceKey);
            });
            document.addEventListener('jeen:osm-map-select', (event) => {
                const indexes = Array.isArray(event.detail?.rowIndexes) ? event.detail.rowIndexes : [];
                this.mapSelectedRows = new Set(indexes.map(Number));
                this.renderTable();
                const first = indexes[0];
                const target = document.querySelector(`#v3-grid [data-source-row="${first}"]`);
                target?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            });
            document.addEventListener('jeen:osm-map-ready', () => this.renderTable());
            document.addEventListener('jeen:chart-rendered', (event) => this._onChartRendered(event.detail || {}));
            document.addEventListener('jeen:conversation-tabs', (event) => this.setTabsVisible(!!event.detail?.visible));
            // Send visibility depends on the signed-in user, which auth.js loads after the shell.
            document.addEventListener('jeen:current-user', () => this._setActionsEnabled(Boolean(this._actionsEnabled)));
            document.addEventListener('jeen:preferences-changed', (event) => {
                if (event.detail?.key !== 'dateFormat') return;
                this.renderTable();
                this.renderDock();
            });
            this.setTabsVisible(conversationTabsPreferred());
            this.setTab('conversation');
            this._renderEmptySuggestions();
            // Restore the persisted desktop open/closed preference before the
            // responsive pass so a wide reload honours the last explicit toggle.
            const storedOpen = this._readConversationOpen();
            if (storedOpen !== null) this.desktopPreference = storedOpen;
            this._applyResponsive();
            if (window.innerWidth > 1100) this.setConversation(this.desktopPreference);
            this.render();
            this.syncRail();
            document.body.classList.add('v3-ready');
            document.body.classList.remove('v3-booting');
            window.askQuestion = () => this.submitComposer();
            window._jeenQuestionClick = (q) => this.send(q);
            window._fillFollowUp = (q) => this.send(q);
            window.ChatController = this;
        },

        _buildShell() {
            const shell = document.createElement('div');
            shell.id = 'v3-shell';
            shell.className = 'v3-shell';
            shell.innerHTML = `
              <header class="v3-topbar">
                <img class="v3-logo" src="/static/images/jeen-mark.png" alt="Jeen">
                <span class="v3-topbar-divider" aria-hidden="true"></span>
                <div id="v3-connection-slot" class="v3-connection-slot"></div>
                <div class="v3-topbar-spacer"></div>
                <div id="v3-theme-slot"></div>
                <div id="v3-user-slot"></div>
              </header>
              <div class="v3-main-row">
                <nav class="v3-rail" aria-label="${h('shell.rail.navigation')}">
                  <button class="v3-rail-btn is-active" data-rail="conversation" aria-label="${h('shell.rail.conversation')}" data-tooltip="${h('shell.rail.hideConversation')}" aria-controls="v3-conversation" aria-expanded="true">${ICON.railConversation}<span class="v3-rail-dot" hidden></span></button>
                  <button class="v3-rail-btn" data-rail="tables" aria-label="${h('shell.rail.tables')}" data-tooltip="${h('shell.rail.tables')}">${ICON.table}</button>
                  <button class="v3-rail-btn" data-rail="saved" aria-label="${h('shell.rail.savedLabel')}" data-tooltip="${h('shell.rail.saved')}">${ICON.pin}</button>
                  <button class="v3-rail-btn" data-rail="history" aria-label="${h('shell.rail.historyLabel')}" data-tooltip="${h('shell.rail.history')}">${ICON.history}</button>
                  <div class="v3-rail-spacer"></div>
                  <button id="v3-settings-button" class="v3-rail-btn v3-rail-btn--settings" data-rail="settings" aria-label="${h('shell.rail.settings')}" data-tooltip="${h('shell.rail.settings')}">${ICON.settings}</button>
                </nav>
                <div class="v3-app">
                <div class="v3-body">
                  <div id="v3-drawer-overlay" class="v3-drawer-overlay"></div>
                  <aside id="v3-conversation" class="v3-conversation" aria-label="${h('shell.panels.workspaceLabel')}">
                    <div class="v3-panel-head">
                      <strong id="v3-panel-title">${h('shell.tabs.conversation')}</strong>
                      <button id="v3-new-conversation" type="button" class="v3-new-conversation">${h('conversation.newConversation')}</button>
                    </div>
                    <div class="v3-tabs-wrap">
                      <div class="v3-tabs" role="tablist" aria-label="${h('shell.tabs.sections')}">
                        <button id="v3-tab-conversation" class="v3-tab" data-tab="conversation" role="tab" aria-selected="true" aria-controls="v3-panel-conversation">${h('shell.tabs.conversation')}</button>
                        <button id="v3-tab-tables" class="v3-tab" data-tab="tables" role="tab" aria-selected="false" aria-controls="v3-panel-tables">${h('shell.tabs.tables')}</button>
                        <button id="v3-tab-saved" class="v3-tab" data-tab="saved" role="tab" aria-selected="false" aria-controls="v3-panel-saved">${h('shell.tabs.saved')}</button>
                        <button id="v3-tab-conversations" class="v3-tab" data-tab="conversations" role="tab" aria-selected="false" aria-controls="v3-panel-conversations">${h('shell.tabs.history')}</button>
                      </div>
                    </div>
                    <section id="v3-panel-conversation" class="v3-panel" data-panel="conversation" role="tabpanel" aria-labelledby="v3-tab-conversation">
                      <div id="v3-thread" class="v3-thread" role="log" aria-live="polite"></div>
                    </section>
                    <section id="v3-panel-conversations" class="v3-panel v3-panel-list" data-panel="conversations" role="tabpanel" aria-labelledby="v3-tab-conversations" hidden>
                      <div class="v3-conversations-head">
                        <span class="v3-thread-empty-label">${h('shell.panels.previousConversations')}</span>
                        <button class="v3-text-btn" data-question-log title="${h('shell.panels.questionLogTitle')}">${h('shell.panels.questionLog')}</button>
                      </div>
                      <div id="v3-conversations-list" class="v3-conversations-list"></div>
                    </section>
                    <section id="v3-panel-tables" class="v3-panel v3-panel-list" data-panel="tables" role="tabpanel" aria-labelledby="v3-tab-tables" hidden>
                      <div id="v3-table-search-slot" class="v3-panel-search"></div>
                      <div id="v3-tables-slot"></div>
                    </section>
                    <section id="v3-panel-saved" class="v3-panel v3-panel-list" data-panel="saved" role="tabpanel" aria-labelledby="v3-tab-saved" hidden>
                      <div class="v3-saved-switch" role="tablist" aria-label="${h('favorite.sections')}">
                        <button id="v3-saved-tab-answers" type="button" data-saved-view="answers" role="tab" aria-selected="true" aria-controls="v3-saved-answers">${h('favorite.answers')}</button>
                        <button id="v3-saved-tab-questions" type="button" data-saved-view="questions" role="tab" aria-selected="false" aria-controls="v3-saved-questions" tabindex="-1">${h('favorite.questions')}</button>
                      </div>
                      <div id="v3-saved-answers" data-saved-pane="answers" role="tabpanel" aria-labelledby="v3-saved-tab-answers">
                        <div id="v3-favorites-list" class="v3-favorites-list"></div>
                      </div>
                      <div id="v3-saved-questions" data-saved-pane="questions" role="tabpanel" aria-labelledby="v3-saved-tab-questions" hidden>
                        <div id="v3-question-search-slot" class="v3-panel-search"></div>
                        <div class="v3-thread-empty-label">${h('shell.panels.pinnedRecent')}</div>
                        <div id="v3-pinned-slot"></div>
                      </div>
                    </section>
                    <div class="v3-composer-wrap">
                      <div class="v3-composer">
                        <div id="v3-input-slot"></div>
                        <div id="v3-suggestions-slot"></div>
                        <div class="v3-composer-bottom">
                          <span class="v3-composer-hint"><kbd>@</kbd> ${h('shell.composer.hintTables')} · <kbd>#</kbd> ${h('shell.composer.hintColumns')} · <kbd>/</kbd> ${h('shell.composer.hintTemplates')}</span>
                          <span class="v3-composer-spacer"></span>
                          <div id="v3-ask-slot"></div>
                        </div>
                      </div>
                    </div>
                  </aside>
                  <main class="v3-workspace" aria-live="polite">
                    <header class="v3-result-head">
                      <div class="v3-result-copy">
                        <h1 id="v3-result-title" class="v3-result-title">${h('shell.result.getStarted')}</h1>
                        <div id="v3-meta-row" class="v3-meta-row"></div>
                      </div>
                      <div id="v3-actions" class="v3-actions">
                        <button id="v3-favorite-action" type="button" class="v3-favorite-action" hidden>${ICON.star}<span>${h('favorite.add')}</span></button>
                      </div>
                    </header>
                    <div class="v3-scroll">
                      <div id="v3-placeholder" class="v3-placeholder">
                        <strong>${h('shell.result.noResultYet')}</strong>
                        <span>${h('shell.result.placeholderCopy')}</span>
                      </div>
                      <div id="v3-ml-definition" class="v3-ml-definition" hidden></div>
                      <section id="v3-analysis-adjust" class="v3-analysis-adjust" aria-labelledby="v3-analysis-adjust-title" hidden>
                        <div class="v3-analysis-adjust-copy">
                          <strong id="v3-analysis-adjust-title">${h('charts.chat.adjustAnalysis')}</strong>
                          <span>${h('charts.chat.adjustHelp')}</span>
                        </div>
                        <div id="v3-analysis-adjust-content"></div>
                      </section>
                      <div id="v3-chart-status" class="v3-chart-status" role="status" hidden>
                        <span>${h('results.chart.unavailable')}</span>
                        <button type="button" class="v3-text-btn" data-chart-retry>${h('common.retry')}</button>
                      </div>
                      <section id="v3-chart-block" class="v3-data-block" hidden>
                        <div class="v3-toolbar">
                          <span id="v3-chart-caption" class="v3-caption">${h('shell.result.chart')}</span>
                          <span class="v3-toolbar-spacer"></span>
                          <div id="v3-chart-types" class="v3-chart-types">
                            <div id="v3-chart-primary" class="v3-chart-primary"></div>
                            <button id="v3-chart-more" type="button" class="v3-chart-more"
                                    aria-expanded="false" aria-controls="v3-chart-secondary">
                              ${h('charts.options.more')}
                            </button>
                            <div id="v3-chart-secondary" class="v3-chart-secondary"></div>
                          </div>
                          <button id="v3-chart-toggle" type="button" class="v3-text-btn"
                                  aria-expanded="false" aria-controls="v3-chart-types v3-chart-frame v3-chart-edit"
                                  aria-label="${h('results.chart.expandLabel')}">${h('common.expand')}</button>
                        </div>
                        <div id="v3-chart-frame" class="v3-chart-frame"></div>
                        <div id="v3-chart-edit" class="v3-chart-edit"></div>
                      </section>
                      <section id="v3-table-block" class="v3-data-block" hidden>
                        <div class="v3-toolbar">
                          <span id="v3-row-caption" class="v3-caption"></span>
                          <span class="v3-toolbar-spacer"></span>
                          <input id="v3-result-filter" type="search" dir="auto" placeholder="${h('shell.result.filterRows')}" aria-label="${h('shell.result.filterRowsLabel')}">
                        </div>
                        <div id="v3-cap-banner" class="v3-cap-banner" hidden></div>
                        <div id="v3-grid-wrap" class="v3-grid-wrap"><div id="v3-grid" class="v3-grid"></div></div>
                        <div class="v3-table-actions"><div id="v3-describe-slot"></div></div>
                        <div id="v3-describe-content"></div>
                      </section>
                    </div>
                    <section class="v3-dock">
                      <div class="v3-dock-bar">
                        ${ICON.code}
                        <div class="v3-dock-tabs" role="tablist" aria-label="${h('shell.dock.label')}">
                          <button class="v3-dock-tab" data-dock="sql" role="tab">${h('shell.dock.sqlTab')}</button>
                          <button class="v3-dock-tab" data-dock="profiling" role="tab">${h('shell.dock.profilingTab')}</button>
                          <button class="v3-dock-tab v3-dock-tab--model" data-dock="model" role="tab" hidden>${h('shell.dock.modelTab')}</button>
                        </div>
                        <span id="v3-dock-meta" class="v3-dock-meta">${h('shell.dock.noRunYet')}</span>
                        <button id="v3-dock-toggle" class="v3-text-btn" aria-expanded="false">${h('common.show')}</button>
                      </div>
                      <div id="v3-dock-body" class="v3-dock-body" hidden></div>
                    </section>
                  </main>
                </div>
                </div>
              </div>`;
            document.body.insertBefore(shell, document.body.firstChild);
        },

        _move(selector, targetSelector, setup) {
            const node = document.querySelector(selector);
            const target = document.querySelector(targetSelector);
            if (!node || !target) return null;
            if (setup) setup(node);
            target.appendChild(node);
            return node;
        },

        _moveProductionNodes() {
            this._move('.connection-switcher', '#v3-connection-slot');
            this._move('#theme-toggle', '#v3-theme-slot');
            this._move('#user-menu-wrap', '#v3-user-slot');
            this.input = this._move('#question-input', '#v3-input-slot', (node) => {
                node.rows = 2;
                node.placeholder = t('shell.composer.followupPlaceholder');
                node.setAttribute('dir', 'auto');
                node.removeAttribute('style');
            });
            this.suggestions = this._move('#question-suggestions', '#v3-suggestions-slot');
            this.askButton = this._move('#ask-button', '#v3-ask-slot', (node) => {
                node.className = 'v3-ask';
                node.style.display = '';
                node.innerHTML = `${h('shell.composer.ask')} <span style="opacity:.6">↵</span>`;
            });
            this._move('#table-search', '#v3-table-search-slot', (node) => { node.style.display = ''; });
            this._move('#tables-list', '#v3-tables-slot');
            this._move('#question-search', '#v3-question-search-slot', (node) => {
                node.style.display = '';
                node.placeholder = t('shell.panels.searchQuestions');
                node.setAttribute('dir', 'auto');
            });
            this._move('#question-history', '#v3-pinned-slot');

            const actions = [
                ['#export-btn', t('common.export'), 'v3-icon-action', ICON.export],
                ['#copy-results-btn', t('common.copy'), 'v3-icon-action', ICON.copy],
                ['#send-result-btn', t('common.send'), '', null],
            ];
            actions.forEach(([selector, label, className, icon]) => {
                this._move(selector, '#v3-actions', (node) => {
                    node.style.display = '';
                    node.className = className;
                    node.title = label;
                    node.setAttribute('aria-label', label);
                    if (icon) node.innerHTML = icon;
                });
            });
            this._move('#describe-btn', '#v3-describe-slot', (node) => {
                node.style.display = '';
                node.className = 'v3-text-btn';
                node.textContent = t('results.actions.describe');
            });
            this._move('#describe-section', '#v3-describe-content');
            this._move('#chart-type-selector-container', '#v3-chart-primary', (node) => { node.style.display = ''; });
            this._move('#chart-options-panel-container', '#v3-chart-secondary', (node) => { node.style.display = ''; });
            const chart = this._move('#chart-view-container', '#v3-chart-frame', (node) => {
                node.style.display = 'block';
            });
            if (chart) {
                const chat = chart.querySelector('#chart-chat-container');
                if (chat) document.getElementById('v3-chart-edit').appendChild(chat);
            }
            this._setActionsEnabled(false);
        },

        _placeChartInteraction(_isAnalysis) {
            const chartEdit = document.getElementById('v3-chart-edit');
            const analysisAdjust = document.getElementById('v3-analysis-adjust');
            const chat = document.getElementById('chart-chat-container');
            // The compact chat always edits the currently rendered ECharts
            // option. ML parameter changes belong to Edit setup; this surface
            // must never invoke /api/analysis/rerun or execute a new query.
            if (chat && chartEdit && chat.parentNode !== chartEdit) chartEdit.appendChild(chat);
            if (analysisAdjust) analysisAdjust.hidden = true;
        },

        _bind() {
            document.querySelectorAll('[data-tab]').forEach((button) => button.addEventListener('click', () => this.setTab(button.dataset.tab)));
            document.querySelectorAll('[data-dock]').forEach((button) => button.addEventListener('click', () => this.toggleDock(button.dataset.dock)));
            document.querySelector('.v3-tabs').addEventListener('keydown', (event) => this._tabKeydown(event, '[data-tab]', (button) => this.setTab(button.dataset.tab)));
            document.querySelector('.v3-dock-tabs').addEventListener('keydown', (event) => this._tabKeydown(event, '[data-dock]', (button) => {
                this.dockTab = button.dataset.dock;
                this.dockOpen = true;
                this.renderDock();
            }));
            document.querySelectorAll('[data-rail]').forEach((button) => button.addEventListener('click', () => this._rail(button.dataset.rail)));
            document.querySelectorAll('[data-saved-view]').forEach((button) => button.addEventListener('click', () => this.setSavedView(button.dataset.savedView)));
            document.querySelector('.v3-saved-switch').addEventListener('keydown', (event) => {
                this._tabKeydown(event, '[data-saved-view]', (button) => this.setSavedView(button.dataset.savedView));
            });
            document.getElementById('v3-new-conversation').addEventListener('click', () => this.newConversation());
            document.getElementById('v3-favorite-action').addEventListener('click', () => this.toggleFavorite());
            document.getElementById('v3-drawer-overlay').addEventListener('click', () => this.setConversation(false, true));
            document.getElementById('v3-dock-toggle').addEventListener('click', () => this.toggleDock(this.dockTab));
            document.querySelector('[data-question-log]')?.addEventListener('click', () => document.getElementById('history-btn')?.click());
            document.getElementById('v3-chart-toggle').addEventListener('click', () => {
                this.chartCollapsed = !this.chartCollapsed;
                // Remember the choice for this result so it survives re-renders and
                // overrides the per-type default (ML expanded, SQL collapsed).
                const current = this.turns.find((item) => item.id === this.selectedResultId);
                if (current) current.chartCollapsed = this.chartCollapsed;
                this._renderChartCollapse();
            });
            document.getElementById('v3-chart-more').addEventListener('click', () => {
                this.chartOptionsOpen = !this.chartOptionsOpen;
                this._renderChartOptionsOverflow();
            });
            document.getElementById('v3-result-filter').addEventListener('input', (event) => {
                this.filter = event.target.value.toLowerCase();
                this.renderTable();
            });
            if (this.askButton) this.askButton.addEventListener('click', (event) => {
                event.preventDefault();
                this.submitComposer();
            });
            if (this.input) {
                this.input.addEventListener('keydown', (event) => {
                    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
                        event.preventDefault();
                        this.submitComposer();
                    }
                }, true);
                this.input.addEventListener('input', () => {
                    this.input.style.height = 'auto';
                    this.input.style.height = `${Math.min(this.input.scrollHeight, 140)}px`;
                });
            }
            window.addEventListener('resize', () => this._applyResponsive());
            document.addEventListener('keydown', (event) => {
                if (event.key === 'Escape' && window.innerWidth < 900) this.setConversation(false, true);
            });
        },

        _tabKeydown(event, selector, activate) {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            const buttons = [...event.currentTarget.querySelectorAll(selector)];
            const current = Math.max(0, buttons.indexOf(document.activeElement));
            let next = current;
            // In RTL the tab strip is mirrored, so ArrowRight moves to the previous tab.
            const forwardKey = isRtl() ? 'ArrowLeft' : 'ArrowRight';
            const backwardKey = isRtl() ? 'ArrowRight' : 'ArrowLeft';
            if (event.key === forwardKey) next = (current + 1) % buttons.length;
            if (event.key === backwardKey) next = (current - 1 + buttons.length) % buttons.length;
            if (event.key === 'Home') next = 0;
            if (event.key === 'End') next = buttons.length - 1;
            event.preventDefault();
            buttons[next].focus();
            activate(buttons[next]);
        },

        _rail(action) {
            // 'new' is kept as an alias so older callers (onboarding) still work.
            if (action === 'conversation' || action === 'new') {
                // The Conversation icon is the panel toggle: when its own tab is
                // already showing, a click collapses the panel; otherwise it
                // switches to Conversation and opens the panel.
                if (this.activeTab === 'conversation' && this.isConversationOpen()) {
                    this.setConversation(false, true);
                    this._persistConversationOpen(false);
                    return;
                }
                this.setTab('conversation');
                this.setConversation(true);
                this._persistConversationOpen(true);
                if (this.input) this.input.focus();
            } else if (action === 'tables') {
                // setTab('tables') already refreshes the table list once.
                this.setTab('tables');
                this.setConversation(true);
            } else if (action === 'saved' || action === 'pinned') {
                this.setTab('saved');
                this.setConversation(true);
            } else if (action === 'history') {
                this.setTab('conversations');
                this.setConversation(true);
            } else if (action === 'settings') {
                if (window._settingsPage?.toggle) window._settingsPage.toggle();
                else document.getElementById('settings-btn')?.click();
            }
        },

        setTab(tab) {
            this.activeTab = tab;
            const panelTitles = {
                conversation: t('shell.tabs.conversation'),
                tables: t('shell.tabs.tables'),
                saved: t('shell.tabs.saved'),
                conversations: t('shell.tabs.history'),
            };
            const panelTitle = document.getElementById('v3-panel-title');
            if (panelTitle) panelTitle.textContent = panelTitles[tab] || panelTitles.conversation;
            document.querySelectorAll('[data-tab]').forEach((button) => {
                const active = button.dataset.tab === tab;
                button.setAttribute('aria-selected', String(active));
                button.tabIndex = active ? 0 : -1;
            });
            document.querySelectorAll('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== tab; });
            // syncRail() owns the rail's active/collapsed visuals: it lights the
            // matching icon only when the panel is actually open.
            this.syncRail();
            if (tab === 'tables' && typeof window.loadTables === 'function') window.loadTables();
            if (tab === 'saved') {
                if (this.savedView === 'answers') this.loadFavoriteAnswers();
                else if (typeof window.displayHistory === 'function') window.displayHistory();
            }
            if (tab === 'conversations') this.loadConversationList();
        },

        /**
         * Show or hide the Conversation / Tables / Pinned / History tab bar at
         * the top of the chat panel. The side rail exposes the same sections,
         * so the bar is hidden unless the user opts in from Settings > General.
         */
        setTabsVisible(visible) {
            const wrap = document.querySelector('.v3-tabs-wrap');
            if (!wrap) return;
            wrap.hidden = !visible;
            document.getElementById('v3-conversation')?.classList.toggle('v3-tabs-hidden', !visible);
        },

        /** True when the conversation panel is visible (desktop) or forced open (drawer). */
        isConversationOpen() {
            const panel = document.getElementById('v3-conversation');
            if (!panel || panel.hidden) return false;
            if (window.innerWidth <= 1100) return panel.classList.contains('v3-force-open');
            return true;
        },

        toggleConversation() {
            const open = !this.isConversationOpen();
            this.setConversation(open, !open);
            this._persistConversationOpen(open);
        },

        setConversation(open, restoreFocus = false) {
            const panel = document.getElementById('v3-conversation');
            const overlay = document.getElementById('v3-drawer-overlay');
            if (window.innerWidth <= 1100) {
                panel.hidden = false;
                panel.classList.toggle('v3-force-open', open);
            } else {
                panel.hidden = !open;
                panel.classList.remove('v3-force-open');
                this.desktopPreference = open;
            }
            overlay.classList.toggle('is-open', open && window.innerWidth < 900);
            overlay.setAttribute('aria-hidden', String(!(open && window.innerWidth < 900)));
            panel.setAttribute('aria-hidden', String(!open));
            this.syncRail();
            if (!open && restoreFocus) document.querySelector('[data-rail="conversation"]')?.focus();
            setTimeout(() => window.dispatchEvent(new Event('resize')), 0);
        },

        /**
         * Single owner of the rail's active/collapsed visuals. A section icon is
         * "active" (rose) only when its panel is actually open; the Conversation
         * icon additionally shows a collapsed state + an unread-style dot (when
         * the panel is closed and the thread has turns) and carries the toggle's
         * aria/tooltip. Called from setTab, setConversation and after render.
         */
        syncRail() {
            const railForTab = { conversation: ['conversation', 'new'], tables: ['tables'], saved: ['saved', 'pinned'], conversations: ['history'] };
            const activeRails = railForTab[this.activeTab] || [];
            const open = this.isConversationOpen();
            document.querySelectorAll('[data-rail]').forEach((button) => {
                button.classList.toggle('is-active', open && activeRails.includes(button.dataset.rail));
            });
            const convo = document.querySelector('[data-rail="conversation"]');
            if (!convo) return;
            const conversationActive = activeRails.includes('conversation');
            const collapsed = !open;
            convo.classList.toggle('is-collapsed', collapsed);
            // The button controls the whole panel (aria-controls="v3-conversation"),
            // so expanded reflects panel visibility regardless of which tab shows.
            convo.setAttribute('aria-expanded', String(open));
            const label = (open && conversationActive) ? t('shell.rail.hideConversation') : t('shell.rail.showConversation');
            convo.setAttribute('data-tooltip', label);
            convo.setAttribute('aria-label', label);
            const dot = convo.querySelector('.v3-rail-dot');
            if (dot) dot.hidden = !(collapsed && this.turns.length >= 1);
        },

        /** Persist the explicit desktop open/closed choice (never the responsive auto-collapse). */
        _persistConversationOpen(open) {
            if (window.innerWidth <= 1100) return;
            try { window.localStorage.setItem('v3.conversationOpen', open ? 'true' : 'false'); } catch (_) { /* storage may be unavailable */ }
        },

        _readConversationOpen() {
            try {
                const raw = window.localStorage.getItem('v3.conversationOpen');
                return raw === null ? null : raw === 'true';
            } catch (_) { return null; }
        },

        _applyResponsive() {
            if (window.innerWidth <= 1100) {
                if (!this.autoCollapsed) {
                    this.autoCollapsed = true;
                    this.setConversation(false);
                }
            } else if (this.autoCollapsed) {
                this.autoCollapsed = false;
                this.setConversation(this.desktopPreference);
            }
        },

        submitComposer() {
            const question = (this.input && this.input.value || '').trim();
            if (!question) return;
            this.input.value = '';
            this.input.style.height = '';
            this.send(question);
        },

        async send(question, options = {}) {
            const q = String(question || '').trim();
            if (!q || this.sending) return;
            if (this.readOnly) {
                if (typeof window.showToast === 'function') {
                    window.showToast(t('conversation.readOnlySend'), 'error');
                }
                return;
            }
            const connection = typeof window.getActiveConnection === 'function' ? window.getActiveConnection() : '';
            if (!connection) {
                if (typeof window.showToast === 'function') window.showToast(t('errors.selectConnectionFirst'), 'error');
                return;
            }
            // A brand-new question (from the composer, a follow-up chip or a
            // programmatic send) leaves any open inline edit; re-running an
            // edited turn (replaceTurnId) is the edit committing itself.
            const replaceTurn = options.replaceTurnId ? this.turns.find((item) => item.id === options.replaceTurnId) : null;
            // A stale rerun target (turn gone) must never fall through to
            // appending a brand-new turn.
            if (options.replaceTurnId && !replaceTurn) return;
            if (!replaceTurn) this._cancelEdit();

            this.sending = true;
            this.setTab('conversation');
            this.setConversation(true);
            // A live send does not bump the generation (it belongs to the current
            // conversation) but records it, so a switch/reset that happens while
            // the stream is open can abort the fetch and drop late events.
            const generation = this._generation;
            const abort = new AbortController();
            this._streamAbort = abort;
            if (replaceTurn) {
                // Re-run an existing turn in place (edit / edited-retry): wipe every
                // result-derived field and bump its revision so late async
                // writebacks from the previous run are dropped.
                this._resetTurnForRerun(replaceTurn);
                replaceTurn.question = q;
                this.lastAppliedResultId = null;
                this.selectedResultId = replaceTurn.id;
            }
            const turn = replaceTurn || {
                id: `turn-${Date.now()}-${++this.seq}`,
                question: q,
                status: 'running',
                startedAt: performance.now(),
                askedAt: Date.now(),
                rev: 0,
                phaseState: Object.fromEntries(PHASES.map((phase) => [phase.id, 'pending'])),
                trace: [],
                traceOpen: false,
                result: null,
                error: null,
                // Wall-clock milestones since the question was sent: when the
                // table, the summary/insights and the chart became visible.
                timeline: {},
            };
            if (!replaceTurn) this.turns.push(turn);
            this.selectedTurnId = turn.id;
            this._setComposerBusy(true);
            this.render();
            this._scrollThread();

            const prefs = window.JeenPreferences ? window.JeenPreferences.getAll() : {};
            const payload = {
                question: q,
                connection,
                session_id: typeof window._jeenGetSessionId === 'function' ? window._jeenGetSessionId() : null,
                eval_analytics: insightsEnabled(prefs),
            };
            // Settings > General "Row limit" (preferences.js key `rowLimit`).
            if (prefs.rowLimit) payload.limit = Number(prefs.rowLimit);
            if (prefs.temperature !== undefined && prefs.temperature !== null) payload.temperature = Number(prefs.temperature);
            const llmTimeout = window.JeenPreferences && window.JeenPreferences.getLlmTimeoutSeconds();
            if (llmTimeout !== null && llmTimeout !== undefined) payload.llm_timeout = llmTimeout;
            // Force the branch for this one question: "Answer with SQL instead" /
            // "Answer directly" (false), or "Run the analysis" (true) from a route
            // clarification. Omitted → the router decides.
            if (options.analysis === true || options.analysis === false) payload.analysis = options.analysis;
            // Filter clarifications: the answer to "which field / which value did
            // you mean" is remembered for the conversation and sent with every
            // later question so the grounder never asks the same thing twice.
            if (options.filterChoice) this._rememberFilterChoice(payload.session_id, options.filterChoice);
            const choices = this._filterChoicesFor(payload.session_id);
            if (choices.length) payload.filter_choices = choices;

            const stale = () => generation !== this._generation;
            try {
                await this._stream(payload, (event, data) => {
                    if (stale()) return;
                    if (event === 'node') this._onNode(turn, data);
                    if (event === 'partial') this._onPartial(turn, data);
                    if (event === 'result') this._onResult(turn, data);
                    if (event === 'enrichment') this._onEnrichment(turn, data);
                    if (event === 'error') throw new Error(errorText({ payload: data, code: data.code || 'QUERY_FAILED' }, data.detail || data.error || t('errors.queryFailed')));
                }, abort.signal);
                if (stale()) return;
                if (turn.status === 'running' || turn.status === 'streaming') throw new Error(t('errors.streamEnded'));
            } catch (error) {
                if (stale() || abort.signal.aborted) return;
                // A failed fetch (offline, proxy down) surfaces as a TypeError with
                // browser-English text; give it the localized network headline.
                if (error && error.name === 'TypeError') {
                    this._onError(turn, new Error(errorText({ error, network: true }, error.message)));
                } else {
                    this._onError(turn, error);
                }
            } finally {
                if (this._streamAbort === abort) this._streamAbort = null;
                if (!stale()) {
                    this.sending = false;
                    this._setComposerBusy(false);
                    this.render();
                }
            }
        },

        _filterChoicesFor(sessionId) {
            const key = sessionId || '_pending';
            return (this._filterChoices && this._filterChoices[key]) || [];
        },

        _rememberFilterChoice(sessionId, choice) {
            if (!choice || !choice.literal) return;
            const key = sessionId || '_pending';
            this._filterChoices = this._filterChoices || {};
            const list = (this._filterChoices[key] || []).filter((c) => String(c.literal).toLowerCase() !== String(choice.literal).toLowerCase());
            list.push(choice);
            this._filterChoices[key] = list.slice(-20);
        },

        /** Structured "which field / which value did you mean" card from the grounder. */
        _filterClarifyHtml(clarify, answer) {
            const options = Array.isArray(clarify.options) ? clarify.options : [];
            const buttons = options.map((option, index) => {
                const value = option.value ? ` <small>= ${esc(option.value)}</small>` : '';
                const title = option.description ? ` title="${esc(option.description)}"` : '';
                return `<button type="button" class="v3-ml-alt v3-filter-option" data-filter-option="${index}"${title}>${esc(option.label)}${value}</button>`;
            }).join('');
            const any = clarify.allow_any && options.length > 1
                ? `<button type="button" class="v3-ml-alt v3-filter-option" data-filter-any>${h('conversation.text.anyOfFields')}</button>` : '';
            const other = clarify.allow_other
                ? `<button type="button" class="v3-text-btn" data-filter-other>${h('conversation.text.somethingElse')}</button>` : '';
            return `<div class="v3-route-clarify v3-filter-clarify">
              <p class="v3-ml-message" dir="${directionOf(answer)}">${esc(answer)}</p>
              <div class="v3-ml-actions">${buttons}${any}</div>
              ${other ? `<div class="v3-filter-other">${other}</div>` : ''}
            </div>`;
        },

        _bindFilterClarify(root, turn, clarify) {
            const options = Array.isArray(clarify.options) ? clarify.options : [];
            root.querySelectorAll('[data-filter-option]').forEach((button) => button.addEventListener('click', () => {
                const option = options[Number(button.dataset.filterOption)];
                if (!option) return;
                const choice = { literal: clarify.literal, table: option.table, column: option.column };
                // A value option ("Moscow") tells the grounder the exact spelling;
                // the question itself is re-sent unchanged so the same literal is
                // planned again and matched to this remembered answer.
                if (clarify.kind === 'value' && option.value) choice.value = option.value;
                this.send(turn.question, { filterChoice: choice });
            }));
            root.querySelector('[data-filter-any]')?.addEventListener('click', () => {
                this.send(turn.question, { filterChoice: { literal: clarify.literal, any: true } });
            });
            root.querySelector('[data-filter-other]')?.addEventListener('click', () => {
                const input = document.getElementById('v3-composer-input') || document.querySelector('textarea, input[type="text"]');
                if (input) { input.focus(); }
            });
        },

        /** "Filtered by …" provenance block for the SQL dock. */
        _filtersHtml(data) {
            const filters = data && data.filters;
            if (!filters) return '';
            const chips = [];
            (filters.resolved || []).forEach((f, index) => {
                const value = Array.isArray(f.value) ? f.value.join(', ') : String(f.value ?? '');
                const raw = f.raw_value != null && String(f.raw_value).toLowerCase() !== value.toLowerCase()
                    ? ` <small>(${h('conversation.filters.from', { raw: iso(String(f.raw_value)) })})</small>` : '';
                const where = f.any_of_columns && f.any_of_columns.length > 1
                    ? f.any_of_columns.join(` ${t('conversation.filters.or')} `) : `${f.table}.${f.column}`;
                const evidence = f.evidence ? ` <span class="v3-filter-evidence" title="${h('conversation.filters.verifiedVia', { evidence: iso(`${f.evidence}${f.tier ? ' ' + f.tier : ''}`) })}">${esc(f.evidence)}</span>` : '';
                // Other fields that hold this value: one click re-asks with that field.
                const switches = (f.alternatives || []).map((alt, altIndex) =>
                    `<button type="button" class="v3-filter-switch" data-filter-switch="${index}:${altIndex}" title="${h('conversation.filters.useInstead', { field: iso(`${alt.table}.${alt.column}`) })}">${h('conversation.filters.use', { field: iso(`${alt.table}.${alt.column}`) })}</button>`).join('');
                const op = f.op === 'in' ? t('conversation.filters.opIn') : f.op === 'contains' ? t('conversation.filters.opContains') : '=';
                chips.push(`<span class="v3-filter-chip is-verified"><bdi>${esc(where)}</bdi> ${esc(op)} <strong><bdi>${esc(value)}</bdi></strong>${raw}${evidence}${switches}</span>`);
            });
            (filters.unverified || []).forEach((f) => {
                const value = Array.isArray(f.value) ? f.value.join(', ') : String(f.value ?? '');
                chips.push(`<span class="v3-filter-chip is-unverified" title="${escFull(f.reason || t('conversation.filters.notVerified'))}"><bdi>${esc(`${f.table || ''}.${f.column || ''}`)}</bdi> ≈ <strong><bdi>${esc(value)}</bdi></strong> <small>${h('conversation.filters.unverified')}</small></span>`);
            });
            if (!chips.length && !(filters.assumptions || []).length) return '';
            const notes = (filters.assumptions || []).map((a) => `<div class="v3-filter-note">${esc(a)}</div>`).join('');
            return `<div class="v3-filtered-by"><div class="v3-filtered-by-title">${h('conversation.filters.title')}</div><div class="v3-filter-chips">${chips.join('')}</div>${notes}</div>`;
        },

        async _stream(payload, onEvent, signal) {
            const response = await fetch('/api/ask/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
                body: JSON.stringify(payload),
                signal,
            });
            if (!response.ok) {
                const body = await response.text();
                let payload = body;
                try { payload = JSON.parse(body); } catch (_) { /* plain text */ }
                throw new Error(errorText({ status: response.status, payload }, body || t('errors.queryFailedStatus', { status: response.status })));
            }
            if (!response.body) throw new Error(t('errors.streamingUnavailable'));
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (true) {
                const { done, value } = await reader.read();
                buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, '\n');
                let boundary;
                while ((boundary = buffer.indexOf('\n\n')) >= 0) {
                    const block = buffer.slice(0, boundary);
                    buffer = buffer.slice(boundary + 2);
                    if (!block || block.startsWith(':')) continue;
                    let event = 'message';
                    const dataLines = [];
                    block.split('\n').forEach((line) => {
                        if (line.startsWith('event:')) event = line.slice(6).trim();
                        if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
                    });
                    let data = {};
                    if (dataLines.length) {
                        try { data = JSON.parse(dataLines.join('\n')); } catch (_) { data = { detail: dataLines.join('\n') }; }
                    }
                    onEvent(event, data);
                }
                if (done) break;
            }
        },

        _onNode(turn, event) {
            const phase = NODE_PHASE[event.node] || 'execution';
            if (event.status === 'node_started') {
                turn.phaseState[phase] = 'running';
                turn.trace.push({ ...event, startedAt: performance.now() });
            } else {
                const open = [...turn.trace].reverse().find((item) => item.node === event.node && item.status === 'node_started' && !item.closed);
                if (open) open.closed = true;
                turn.trace.push({ ...event });
                turn.phaseState[phase] = event.status === 'node_failed' ? 'error' : 'done';
            }
            this.renderConversation();
            this._scrollThread();
        },

        /**
         * Provisional rows from the graph (`partial` SSE event): the SQL ran and
         * its result was accepted, but the summary / insights / follow-ups are
         * still being written. Paint the table (and start the chart) now; the
         * `result` event later merges the narrative into the same turn.
         *
         * Every partial is a new dataset: a semantic retry re-executes SQL
         * whose text may be identical, so the revision is bumped each time and
         * any chart in flight for the previous rows is dropped.
         */
        _onPartial(turn, data) {
            if (!data || !data.results || turn.status === 'success' || turn.status === 'error') return;
            if (turn.provisionalRevision != null) {
                this._captureSelectedChart();
                turn.rev = (turn.rev || 0) + 1;
                turn.chartState = null;
                turn.chartCollapsed = undefined;
                this.lastAppliedResultId = null;
            }
            this._tableRepaintHold = null;
            turn.result = {
                question: turn.question,
                query_id: data.query_id || null,
                session_id: data.session_id || null,
                sql: data.sql || null,
                results: data.results,
                answer: null,
                error: null,
                metrics: {},
                findings: [],
                suggestions: [],
                followups: [],
                trace: [],
            };
            turn.provisionalRevision = Number.isFinite(Number(data.revision)) ? Number(data.revision) : 0;
            // New rows: the table milestone is now; a chart for the previous
            // revision (if any) no longer counts.
            turn.timeline = { ...(turn.timeline || {}), tableMs: this._sinceStart(turn) };
            delete turn.timeline.chartMs;
            delete turn.timeline.chartSettled;
            turn.status = 'streaming';
            turn.turnId = data.query_id || turn.turnId || null;
            turn.conversationId = data.session_id || turn.conversationId || null;
            turn.resultKind = 'table';
            turn.phaseState.execution = 'done';
            this.selectedTurnId = turn.id;
            this.selectedResultId = turn.id;
            this.filter = '';
            if (data.session_id && typeof window._jeenSetSessionId === 'function') window._jeenSetSessionId(data.session_id);
            this.render();
            this._scrollThread();
        },

        _onResult(turn, data) {
            if (data.error && !data.results) {
                this._onError(turn, new Error(data.error), data);
                return;
            }
            // The rows already arrived as a partial for this exact dataset: keep
            // that object (the table and chart were built from it) and only
            // merge the narrative on top, so the chart is not re-requested and
            // the grid is not repainted.
            const provisional = turn.provisionalRevision != null && turn.result && turn.result.results;
            const sameDataset = Boolean(provisional)
                && (data.sql || null) === (turn.result.sql || null)
                && normalizeRows(data.results).length === normalizeRows(turn.result.results).length;
            if (provisional && !sameDataset) {
                this._captureSelectedChart();
                turn.rev = (turn.rev || 0) + 1;
                turn.chartState = null;
                turn.chartCollapsed = undefined;
                this.lastAppliedResultId = null;
            }
            const used = new Set();
            (data.trace || []).forEach((raw) => {
                const index = turn.trace.findIndex((event, eventIndex) =>
                    !used.has(eventIndex)
                    && event.status === 'node_finished'
                    && event.node === raw.node
                );
                if (index >= 0) {
                    turn.trace[index] = { ...turn.trace[index], ...raw, status: 'node_finished' };
                    used.add(index);
                } else {
                    turn.trace.push({ ...raw, status: 'node_finished' });
                }
                turn.phaseState[NODE_PHASE[raw.node] || 'execution'] = 'done';
            });
            turn.status = 'success';
            // Onboarding signal: a question was answered successfully.
            document.dispatchEvent(new CustomEvent('jeen:onboarding:ask_first_question'));
            if (sameDataset) {
                const results = turn.result.results;
                Object.assign(turn.result, data, { results });
                this._tableRepaintHold = this._tableKey(turn);
                // The legacy panels (prompt, trace, dev header) were fed the
                // provisional payload; give them the narrative without a full
                // applyResult, which would rebuild the chart.
                if (this.lastAppliedResultId === turn.id) window.JeenLegacyBridge?.applyResultNarrative?.(turn.result);
            } else {
                turn.result = data;
            }
            turn.provisionalRevision = null;
            turn.turnId = data.query_id || turn.turnId || null;
            turn.conversationId = data.session_id || turn.conversationId || null;
            turn.isFavorite = false;
            turn.feedback = null;
            turn.feedbackSent = false;
            // ML skills: a confirm / clarify / guard stop is a result (a card),
            // not a table and not an error.
            turn.resultKind = data.proposal ? 'proposal' : turn.resultKind;
            turn.durationMs = Math.round(performance.now() - turn.startedAt);
            turn.timeline = turn.timeline || {};
            if (!sameDataset) {
                // No partial preceded this result (or it was superseded): the
                // table and the narrative land together.
                delete turn.timeline.chartMs;
                delete turn.timeline.chartSettled;
                if (normalizeRows(data.results).length) turn.timeline.tableMs = turn.durationMs;
            }
            if (data.answer || (data.findings || []).length) turn.timeline.insightsMs = turn.durationMs;
            turn.phaseState.format = 'done';
            turn.phaseState.save = 'done';
            if (!sameDataset) this._captureSelectedChart();
            this.selectedTurnId = turn.id;
            this.selectedResultId = turn.id;
            this.filter = '';
            if (data.session_id && typeof window._jeenSetSessionId === 'function') window._jeenSetSessionId(data.session_id);
            if (!this.conversation && data.session_id) {
                // First answer of a fresh conversation: mirror the header the
                // server derives (title = first question) until the next hydration.
                this.conversation = { id: String(data.session_id), title: turn.question, source_key: null };
            }
            this.render();
            this._scrollThread();
            // A 0-row result shows its "no records" answer immediately; the
            // likely-cause hint (when the server did not already include one)
            // arrives shortly after via a background call, so the user is not
            // kept waiting for it.
            if (data.empty_result && !data.empty_hint) this._fetchEmptyHint(turn);
        },

        /** Background likely-cause hint for an empty (0-row) result. Best-effort. */
        async _fetchEmptyHint(turn) {
            const data = turn.result || {};
            if (!data || !data.sql || data.empty_hint) return;
            const connection = typeof getActiveConnection === 'function' ? getActiveConnection() : '';
            if (!connection) return;
            // Drop a late hint if the turn was re-run (edited) meanwhile.
            const rev = turn.rev || 0;
            try {
                const res = await this._postJson('/api/empty-result-hint', {
                    connection,
                    query_id: data.query_id ? String(data.query_id) : null,
                    question: turn.question,
                    sql: data.sql,
                });
                if (res && res.hint && turn.result === data && (turn.rev || 0) === rev) {
                    turn.result.empty_hint = res.hint;
                    this.render();
                }
            } catch (_) { /* the hint is optional — drop it silently */ }
        },

        _onError(turn, error, data) {
            turn.status = 'error';
            turn.error = error && error.message ? error.message : String(error);
            // Rows that already streamed in stay in the answer pane
            // (turnShowsResult); a failure payload without rows must not
            // replace them.
            const keepProvisional = turn.provisionalRevision != null && !(data && data.results);
            if (keepProvisional) {
                if (data && data.error && turn.result) turn.result.error = data.error;
            } else {
                turn.result = data || turn.result;
            }
            turn.durationMs = Math.round(performance.now() - turn.startedAt);
            this.selectedTurnId = turn.id;
            Object.keys(turn.phaseState).forEach((key) => {
                if (turn.phaseState[key] === 'running') turn.phaseState[key] = 'error';
            });
            this.render();
            this._scrollThread();
        },

        _sinceStart(turn) {
            return Math.max(0, Math.round(performance.now() - (turn.startedAt || performance.now())));
        },

        /**
         * The chart for a turn is on screen (ChartManager `jeen:chart-rendered`).
         * Stamp the first render of the current rows; later re-renders (type
         * switches, edits) are not "time to chart". Restored turns are skipped:
         * their clock started at hydration, not at the question.
         */
        _onChartRendered(detail) {
            const queryId = detail && detail.queryId != null ? String(detail.queryId) : null;
            const turn = (queryId && this.turns.find((item) => item.result && String(item.result.query_id) === queryId))
                || this.turns.find((item) => item.id === this.selectedResultId);
            if (!turn || turn.restored || !turn.timeline || turn.timeline.chartSettled) return;
            if (turn.status !== 'streaming' && turn.status !== 'success') return;
            turn.timeline.chartMs = this._sinceStart(turn);
            turn.timeline.chartSettled = true;
            console.info('[Workspace] timeline', turn.question, turn.timeline);
            this.renderConversation();
        },

        _onEnrichment(turn, data) {
            if (!turn.result || !data) return;
            Object.assign(turn.result, data);
            if (data.result_handle) window._resultHandle = data.result_handle;
            if (turn.id === this.selectedResultId) this._setActionsEnabled(true);
        },

        selectTurn(id) {
            const turn = this.turns.find((item) => item.id === id);
            if (!turn) return;
            if (id !== this.selectedTurnId) this._selectionVersion += 1;
            this._unavailableSavedAnswer = null;
            const switchingResult = this.selectedResultId !== turn.id;
            this._captureSelectedChart();
            if (switchingResult) {
                // Leaving a turn closes its "time to chart" window: a chart
                // re-applied when the user comes back is not the first render.
                const leaving = this.turns.find((item) => item.id === this.selectedResultId);
                if (leaving && leaving.timeline) leaving.timeline.chartSettled = true;
                // The previous manager may remain mounted while the destination
                // turn hydrates (or may be a text-only turn). Invalidate its
                // chat/export/save surface immediately after capturing it.
                window.JeenLegacyBridge?.setChartInteractionEnabled?.(false);
            }
            const selection = selectionForTurn(this.selectedResultId, turn);
            this.selectedTurnId = selection.selectedTurnId;
            this.selectedResultId = selection.selectedResultId;
            if (turnShowsResult(turn)) {
                this.filter = '';
                document.getElementById('v3-result-filter').value = '';
            }
            this.render();
            // Restored turns fetch their rows lazily on selection. The
            // generation + selection guard inside _loadArtifact drops a slow
            // response for a turn the user has already left.
            if (turn.restored && turn.artifactState === 'missing' && turn.snapshotStatus === 'stored') {
                this._loadArtifact(turn);
            }
        },

        _captureSelectedChart() {
            const current = this.turns.find((item) => item.id === this.selectedResultId);
            if (!current || this.lastAppliedResultId !== current.id
                || current.chartLoading || current.chartUnavailable) return;
            if (current && current.result?.results && window.JeenLegacyBridge?.getChartState) {
                const state = window.JeenLegacyBridge.getChartState();
                if (state && state.chart_config) {
                    // Keep the versioned page snapshot (baseline + working +
                    // view), not a reference to manager-owned state. The legacy
                    // top-level config/spec remain for server artifacts.
                    try {
                        current.chartState = JSON.parse(JSON.stringify(state));
                    } catch (_) {
                        current.chartState = state;
                    }
                }
            }
        },

        _abortInFlight() {
            if (this._streamAbort) {
                try { this._streamAbort.abort(); } catch (_) { /* already settled */ }
                this._streamAbort = null;
            }
            if (this._hydrateAbort) {
                try { this._hydrateAbort.abort(); } catch (_) { /* already settled */ }
                this._hydrateAbort = null;
            }
            if (this._analysisRerunAbort) {
                try { this._analysisRerunAbort.abort(); } catch (_) { /* already settled */ }
                this._analysisRerunAbort = null;
            }
            this._turnRerunAborts.forEach((abort) => {
                try { abort.abort(); } catch (_) { /* already settled */ }
            });
            this._turnRerunAborts.clear();
            this._analysisRerunInFlight = null;
            window.JeenLegacyBridge?.setAnalysisRerunBusy?.(false);
        },

        _hasActiveWork() {
            return Boolean(
                this.sending
                || this.hydrating
                || this._analysisRerunInFlight
                || this.turns.some((turn) => turn.rerunning)
            );
        },

        _syncNewConversationAction() {
            const button = document.getElementById('v3-new-conversation');
            if (!button) return;
            const busy = this._hasActiveWork();
            const label = t(busy ? 'conversation.stopAndStartNew' : 'conversation.newConversation');
            button.disabled = false;
            button.title = label;
            button.setAttribute('aria-label', label);
        },

        reset() {
            this._generation += 1;
            this._abortInFlight();
            this.closeFeedbackDialog();
            this.turns = [];
            this.editingTurnId = null;
            this.editDraft = '';
            this._editSel = null;
            this.conversation = null;
            this._unavailableSavedAnswer = null;
            this.hydrating = false;
            this.readOnly = false;
            this.selectedTurnId = null;
            this.selectedResultId = null;
            this.lastAppliedResultId = null;
            this.filter = '';
            this.sending = false;
            this._setComposerBusy(false);
            if (typeof window._jeenSetSessionId === 'function') window._jeenSetSessionId(null);
            this.render();
        },

        /** Explicit "New conversation": clear the thread; the server creates the
         *  conversation row lazily on the first question. */
        newConversation() {
            const cancelledWork = this._hasActiveWork();
            this.reset();
            this.setTab('conversation');
            this.setConversation(true);
            if (this.input) this.input.focus();
            if (cancelledWork && typeof window.showToast === 'function') {
                window.showToast(t('conversation.cancelledForNew'), 'info');
            }
        },

        _favoriteCoordinates(turn) {
            if (!turn || turn.status !== 'success' || turn.result?.proposal) return null;
            const conversationId = turn.conversationId || turn.result?.session_id || this.conversation?.id;
            const turnId = turn.turnId || turn.result?.query_id;
            return conversationId && turnId
                ? { conversationId: String(conversationId), turnId: String(turnId) }
                : null;
        },

        _renderFavoriteAction(turn) {
            const button = document.getElementById('v3-favorite-action');
            if (!button) return;
            // A streaming turn has no persisted success row yet (save_to_memory
            // runs after the narrative), so it cannot be favorited until then.
            const coordinates = turn && turn.status === 'streaming' ? null : this._favoriteCoordinates(turn);
            button.hidden = !coordinates;
            if (!coordinates) return;
            const active = Boolean(turn.isFavorite);
            button.disabled = Boolean(turn.favoriteSaving);
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', String(active));
            button.setAttribute('aria-label', active ? t('favorite.remove') : t('favorite.add'));
            button.title = active ? t('favorite.remove') : t('favorite.add');
            const label = button.querySelector('span');
            if (label) label.textContent = active ? t('favorite.saved') : t('favorite.add');
        },

        async toggleFavorite() {
            const turn = this.turns.find((item) => item.id === this.selectedResultId);
            const coordinates = this._favoriteCoordinates(turn);
            if (!turn || !coordinates || turn.favoriteSaving) return;
            await this.setFavoriteState(
                coordinates.conversationId,
                coordinates.turnId,
                !turn.isFavorite,
                turn,
            );
        },

        async setFavoriteState(conversationId, turnId, favorite, knownTurn = null) {
            this._favoriteSavingKeys = this._favoriteSavingKeys || new Set();
            const mutationKey = `${conversationId}:${turnId}`;
            if (this._favoriteSavingKeys.has(mutationKey)) return false;
            this._favoriteSavingKeys.add(mutationKey);
            const turn = knownTurn || this.turns.find((item) =>
                String(item.turnId || item.result?.query_id || '') === String(turnId)
            );
            const currentSelection = () => this.turns.find((item) => item.id === this.selectedResultId);
            const prior = Boolean(turn?.isFavorite);
            if (turn) {
                turn.isFavorite = favorite;
                turn.favoriteSaving = true;
            }
            this._renderFavoriteAction(currentSelection());
            this.renderConversation();
            if (this.activeTab === 'saved' && this.savedView === 'answers') this.renderFavoriteAnswers();
            try {
                const response = await fetch(
                    `/api/conversations/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}/favorite`,
                    { method: favorite ? 'PUT' : 'DELETE', headers: { 'Content-Type': 'application/json' }, body: favorite ? '{}' : undefined }
                );
                if (!response.ok && !(favorite === false && response.status === 404)) {
                    throw new Error(`favorite ${response.status}`);
                }
                if (turn) turn.isFavorite = favorite;
                this._favoriteList = null;
                if (this.activeTab === 'saved' && this.savedView === 'answers') await this.loadFavoriteAnswers();
                if (typeof window.showToast === 'function') {
                    window.showToast(favorite ? t('favorite.added') : t('favorite.removed'), 'success');
                }
                return true;
            } catch (error) {
                console.warn('[Workspace] favorite update failed', error);
                if (turn) turn.isFavorite = prior;
                if (typeof window.showToast === 'function') window.showToast(t('favorite.updateFailed'), 'error');
                return false;
            } finally {
                this._favoriteSavingKeys.delete(mutationKey);
                if (turn) turn.favoriteSaving = false;
                this._renderFavoriteAction(currentSelection());
                this.renderConversation();
                if (this.activeTab === 'saved' && this.savedView === 'answers') this.renderFavoriteAnswers();
            }
        },

        async _hydrationModule() {
            if (this._hydration) return this._hydration;
            if (window.ConversationHydration) {
                this._hydration = window.ConversationHydration;
                return this._hydration;
            }
            const module = await import('./conversationHydration.js?v=3');
            this._hydration = module;
            return module;
        },

        /**
         * Restore the user's last conversation on `sourceKey`. Called once the
         * active connection is resolved on load and again on every switch.
         * Any earlier hydration or live stream is invalidated first.
         */
        async hydrate(sourceKey, options = {}) {
            // A conversation opened from the History tab on another connection
            // is remembered across the connection switch that precedes it.
            const pendingOpen = this._pendingOpen;
            this._pendingOpen = null;
            const conversationId = options.conversationId
                || (pendingOpen && pendingOpen.sourceKey === sourceKey ? pendingOpen.conversationId : null);
            const targetTurnId = options.turnId
                || (pendingOpen && pendingOpen.sourceKey === sourceKey ? pendingOpen.turnId : null);
            const favoriteItem = options.favoriteItem
                || (pendingOpen && pendingOpen.sourceKey === sourceKey ? pendingOpen.favoriteItem : null);
            const readOnly = Boolean(options.readOnly);
            if (!sourceKey && !conversationId) return;
            this._generation += 1;
            const generation = this._generation;
            this._abortInFlight();
            const abort = new AbortController();
            this._hydrateAbort = abort;
            this.closeFeedbackDialog();
            this.turns = [];
            this.editingTurnId = null;
            this.editDraft = '';
            this.conversation = null;
            this._unavailableSavedAnswer = null;
            this.readOnly = readOnly;
            this.selectedTurnId = null;
            this.selectedResultId = null;
            this.lastAppliedResultId = null;
            this.hydrating = true;
            this.render();

            const url = conversationId
                ? `/api/conversations/${encodeURIComponent(conversationId)}`
                : `/api/conversations/last?connection=${encodeURIComponent(sourceKey)}`;
            const stale = () => generation !== this._generation;
            try {
                const [mod, response] = await Promise.all([
                    this._hydrationModule(),
                    fetch(url, { signal: abort.signal }),
                ]);
                if (stale()) return;
                if (response.status === 401) return;
                if (response.status === 404 && conversationId) {
                    this.hydrating = false;
                    if (favoriteItem) {
                        this._unavailableSavedAnswer = {
                            item: favoriteItem,
                            retryable: false,
                        };
                    } else if (typeof window.showToast === 'function') {
                        window.showToast(t('conversation.list.gone'), 'error');
                    }
                    this.render();
                    return;
                }
                if (!response.ok) throw new Error(`restore failed (${response.status})`);
                const detail = await response.json();
                if (stale()) return;
                this.hydrating = false;
                if (!detail || !detail.conversation) {
                    if (favoriteItem) {
                        this._unavailableSavedAnswer = { item: favoriteItem, retryable: true };
                    }
                    this.render();
                    return;
                }
                this.conversation = detail.conversation;
                if (detail.conversation.connection_available === false) this.readOnly = true;
                this.turns = mod.turnsFromDetail(detail);
                // Read-only conversations (connection gone) must never receive new
                // turns, so the session id stays cleared.
                if (typeof window._jeenSetSessionId === 'function') {
                    window._jeenSetSessionId(this.readOnly ? null : detail.conversation.id);
                }
                const newest = this.turns[this.turns.length - 1];
                let requested = targetTurnId
                    ? this.turns.find((item) => String(item.turnId) === String(targetTurnId))
                    : null;
                // A favorite can outlive the normal hydration page. Fetch its
                // metadata directly so Saved always opens the exact answer.
                if (targetTurnId && !requested) {
                    const turnResponse = await fetch(
                        `/api/conversations/${encodeURIComponent(detail.conversation.id)}/turns/${encodeURIComponent(targetTurnId)}`,
                        { signal: abort.signal }
                    );
                    if (stale()) return;
                    if (turnResponse.ok) {
                        requested = mod.turnFromServer(await turnResponse.json(), detail.conversation.id);
                        this.turns.push(requested);
                        this.turns.sort((a, b) => a.sequence - b.sequence);
                    } else {
                        this._unavailableSavedAnswer = {
                            item: favoriteItem || {
                                conversation_id: detail.conversation.id,
                                turn_id: targetTurnId,
                                question: detail.conversation.title,
                                source_key: detail.conversation.source_key,
                                source_label: detail.conversation.source_label,
                            },
                            retryable: turnResponse.status !== 404,
                        };
                    }
                }
                if (targetTurnId && !requested) {
                    this.selectedTurnId = null;
                    this.selectedResultId = null;
                    this.render();
                    return;
                }
                const initial = requested || newest;
                if (initial) {
                    this.selectedTurnId = initial.id;
                    this.selectedResultId = initial.status === 'success' ? initial.id : null;
                    if (!this.selectedResultId) {
                        const lastOk = [...this.turns].reverse().find((item) => item.status === 'success');
                        this.selectedResultId = lastOk ? lastOk.id : null;
                    }
                }
                this.render();
                if (requested) this._scrollTurnIntoView(requested.id);
                else this._scrollThread();
                const target = this.turns.find((item) => item.id === this.selectedResultId);
                if (target && target.artifactState === 'missing' && target.snapshotStatus === 'stored') {
                    await this._loadArtifact(target);
                }
            } catch (error) {
                if (stale() || abort.signal.aborted) return;
                console.warn('[Workspace] conversation restore failed', error);
                this.hydrating = false;
                if (favoriteItem) {
                    this._unavailableSavedAnswer = { item: favoriteItem, retryable: true };
                }
                this.render();
            } finally {
                if (this._hydrateAbort === abort) this._hydrateAbort = null;
            }
        },

        /** Fetch the rows (+ chart baseline) for one restored turn. */
        async _loadArtifact(turn) {
            if (!turn || !turn.restored || !turn.conversationId || !turn.turnId) return;
            if (turn.artifactState === 'loading' || turn.artifactState === 'loaded') return;
            const generation = this._generation;
            const rev = turn.rev || 0;
            turn.artifactState = 'loading';
            this.renderWorkspace();
            try {
                const mod = await this._hydrationModule();
                const response = await fetch(
                    `/api/conversations/${encodeURIComponent(turn.conversationId)}/turns/${encodeURIComponent(turn.turnId)}/artifact`
                );
                // Drop stale rows if the connection reset (generation) or the turn
                // was re-run in place (rev) while the fetch was open.
                if (generation !== this._generation || (turn.rev || 0) !== rev) return;
                if (!response.ok) throw new Error(`artifact ${response.status}`);
                const artifact = await response.json();
                if (generation !== this._generation || (turn.rev || 0) !== rev) return;
                mod.applyArtifact(turn, artifact);
                if (!turn.result.results) {
                    // Snapshot vanished between listing and fetch (pruned); offer Load data.
                    turn.artifactState = 'missing';
                }
            } catch (error) {
                if (generation !== this._generation) return;
                console.warn('[Workspace] artifact load failed', error);
                turn.artifactState = 'failed';
            }
            // Per-selection guard: the rows are kept on the turn either way, but
            // the result pane is only re-rendered if this turn is still selected.
            if (this.selectedResultId === turn.id) {
                this.lastAppliedResultId = null;
                this.render();
            } else {
                this.renderConversation();
            }
        },

        /** "Load data" / "Refresh": re-execute the stored query for a restored turn. */
        async rerunTurn(turnId) {
            const turn = this.turns.find((item) => item.id === turnId);
            if (!turn || !turn.restored || turn.rerunning) return;
            if (this.readOnly) {
                if (typeof window.showToast === 'function') window.showToast(t('conversation.rerun.connectionGone'), 'error');
                return;
            }
            const generation = this._generation;
            const abort = new AbortController();
            this._turnRerunAborts.add(abort);
            turn.rerunning = true;
            turn.rerunError = null;
            this.render();
            try {
                const mod = await this._hydrationModule();
                const response = await fetch(
                    `/api/conversations/${encodeURIComponent(turn.conversationId)}/turns/${encodeURIComponent(turn.turnId)}/rerun`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}', signal: abort.signal }
                );
                if (generation !== this._generation) return;
                if (!response.ok) {
                    let detail = '';
                    try { detail = (await response.json()).error || ''; } catch (_) { /* plain text */ }
                    throw new Error(this._rerunMessage(response.status, detail));
                }
                const artifact = await response.json();
                if (generation !== this._generation) return;
                mod.applyArtifact(turn, artifact);
                turn.chartState = null;
                turn.hasChart = false;
                // snapshot_at is only set when the fresh rows were persisted; a
                // too_large result is shown but has no durable snapshot time.
                turn.snapshotAt = artifact.snapshot_at || null;
                if (turn.result?.results) turn.artifactState = 'loaded';
                this.selectedTurnId = turn.id;
                this.selectedResultId = turn.id;
                this.lastAppliedResultId = null;
                if (typeof window.showToast === 'function') window.showToast(t('conversation.rerun.refreshed'), 'success');
            } catch (error) {
                if (generation !== this._generation || abort.signal.aborted) return;
                turn.rerunError = error && error.message ? error.message : String(error);
                if (typeof window.showToast === 'function') window.showToast(turn.rerunError, 'error');
            } finally {
                this._turnRerunAborts.delete(abort);
                turn.rerunning = false;
                if (generation === this._generation) this.render();
            }
        },

        _rerunMessage(status, detail) {
            const text = String(detail || '');
            if (status === 403 || /grant_required/.test(text)) return t('conversation.rerun.reconnectPowerBi');
            if (status === 409 && /connection_unavailable/.test(text)) return t('conversation.rerun.connectionGone');
            if (status === 409 && /turn_has_no_query/.test(text)) return t('conversation.rerun.noQuery');
            if (status === 504) return t('conversation.rerun.timedOut');
            if (status === 502) return text.replace(/^\{.*?"detail":\s*"?/, '').slice(0, 200) || t('conversation.rerun.sourceError');
            return t('conversation.rerun.failedStatus', { status });
        },

        setSavedView(view) {
            this.savedView = view === 'questions' ? 'questions' : 'answers';
            document.querySelectorAll('[data-saved-view]').forEach((button) => {
                const active = button.dataset.savedView === this.savedView;
                button.setAttribute('aria-selected', String(active));
                button.tabIndex = active ? 0 : -1;
            });
            document.querySelectorAll('[data-saved-pane]').forEach((pane) => {
                pane.hidden = pane.dataset.savedPane !== this.savedView;
            });
            if (this.savedView === 'answers') this.loadFavoriteAnswers();
            else if (typeof window.displayHistory === 'function') window.displayHistory();
        },

        async loadFavoriteAnswers(options = {}) {
            const host = document.getElementById('v3-favorites-list');
            if (!host) return;
            const append = Boolean(options.append);
            const before = append ? this._favoriteNextCursor : null;
            if (append && !before) return;
            const requestId = (this._favoriteLoadSeq || 0) + 1;
            this._favoriteLoadSeq = requestId;
            this._favoritesLoading = true;
            if (!append) host.innerHTML = `<div class="v3-thread-empty-label">${h('favorite.loading')}</div>`;
            try {
                const query = new URLSearchParams({ limit: '50' });
                if (before) query.set('before', before);
                const response = await fetch(`/api/conversations/favorites?${query.toString()}`);
                if (!response.ok) throw new Error(`favorites ${response.status}`);
                const data = await response.json();
                if (requestId !== this._favoriteLoadSeq) return;
                const incoming = Array.isArray(data.items) ? data.items : [];
                if (append) {
                    const seen = new Set((this._favoriteList || []).map((item) => String(item.turn_id)));
                    this._favoriteList = [...(this._favoriteList || []), ...incoming.filter((item) => !seen.has(String(item.turn_id)))];
                } else {
                    this._favoriteList = incoming;
                }
                this._favoriteNextCursor = data.next_cursor || null;
                this.renderFavoriteAnswers();
            } catch (error) {
                if (requestId !== this._favoriteLoadSeq) return;
                console.warn('[Workspace] favorite answers failed', error);
                if (!append) host.innerHTML = `<div class="v3-thread-empty-label">${h('favorite.loadFailed')}</div>`;
                else if (typeof window.showToast === 'function') window.showToast(t('favorite.loadFailed'), 'error');
            } finally {
                if (requestId === this._favoriteLoadSeq) {
                    this._favoritesLoading = false;
                    const more = host.querySelector('[data-favorite-more]');
                    if (more) more.disabled = false;
                }
            }
        },

        renderFavoriteAnswers() {
            const host = document.getElementById('v3-favorites-list');
            if (!host) return;
            const items = this._favoriteList || [];
            if (!items.length) {
                host.innerHTML = `<div class="v3-saved-empty"><strong>${h('favorite.emptyTitle')}</strong><span>${h('favorite.emptyCopy')}</span></div>`;
                return;
            }
            host.innerHTML = items.map((item) => {
                const answer = textOf(item.answer);
                const when = item.favorited_at ? this._formatWhen(item.favorited_at) : '';
                const mutationKey = `${item.conversation_id}:${item.turn_id}`;
                const saving = Boolean(this._favoriteSavingKeys?.has(mutationKey));
                const badges = [];
                if (item.connection_available === false) {
                    badges.push(`<span class="v3-favorite-badge is-unavailable">${h('favorite.connectionUnavailable')}</span>`);
                }
                if (item.result_kind === 'table' && item.snapshot_status !== 'stored') {
                    badges.push(`<span class="v3-favorite-badge">${h('favorite.refreshRequired')}</span>`);
                }
                return `<article class="v3-favorite-item" data-favorite-open="${esc(item.turn_id)}" tabindex="0" role="button">
                  <div class="v3-favorite-item-head">
                    <strong dir="${directionOf(item.question)}">${esc(item.question)}</strong>
                    <button type="button" data-favorite-remove="${esc(item.turn_id)}" aria-label="${h('favorite.remove')}" title="${h('favorite.remove')}"${saving ? ' disabled' : ''}>${ICON.star}</button>
                  </div>
                  ${answer ? `<p dir="${directionOf(answer)}">${esc(answer)}</p>` : ''}
                  ${badges.length ? `<div class="v3-favorite-badges">${badges.join('')}</div>` : ''}
                  <div class="v3-favorite-meta"><bdi>${esc(item.source_label || item.source_key)}</bdi>${when ? ` · ${esc(when)}` : ''}</div>
                </article>`;
            }).join('') + (this._favoriteNextCursor
                ? `<button type="button" class="v3-favorites-more" data-favorite-more${this._favoritesLoading ? ' disabled' : ''}>${h('favorite.loadMore')}</button>`
                : '');
            host.querySelectorAll('[data-favorite-open]').forEach((card) => {
                const open = () => {
                    const item = items.find((candidate) => String(candidate.turn_id) === card.dataset.favoriteOpen);
                    if (item) this.openFavorite(item);
                };
                card.addEventListener('click', (event) => {
                    if (!event.target.closest('[data-favorite-remove]')) open();
                });
                card.addEventListener('keydown', (event) => {
                    if (event.target === card && (event.key === 'Enter' || event.key === ' ')) {
                        event.preventDefault();
                        open();
                    }
                });
            });
            host.querySelectorAll('[data-favorite-remove]').forEach((button) => button.addEventListener('click', async (event) => {
                event.stopPropagation();
                const item = items.find((candidate) => String(candidate.turn_id) === button.dataset.favoriteRemove);
                if (item) await this.setFavoriteState(item.conversation_id, item.turn_id, false);
            }));
            host.querySelector('[data-favorite-more]')?.addEventListener('click', () => this.loadFavoriteAnswers({ append: true }));
        },

        openFavorite(item) {
            if (!item) return;
            this.openConversation({
                id: item.conversation_id,
                title: item.conversation_title,
                source_key: item.source_key,
                source_label: item.source_label,
                connection_available: item.connection_available,
            }, { turnId: item.turn_id, favoriteItem: item });
        },

        // ── History tab: browse / open / rename / delete conversations ──────
        _conversationList: null,

        async loadConversationList() {
            const host = document.getElementById('v3-conversations-list');
            if (!host) return;
            host.innerHTML = `<div class="v3-thread-empty-label">${h('conversation.list.loading')}</div>`;
            try {
                const response = await fetch('/api/conversations?all=true&limit=100');
                if (!response.ok) throw new Error(`list ${response.status}`);
                const data = await response.json();
                this._conversationList = Array.isArray(data.items) ? data.items : [];
            } catch (error) {
                console.warn('[Workspace] conversation list failed', error);
                host.innerHTML = `<div class="v3-thread-empty-label">${h('conversation.list.loadFailed')}</div>`;
                return;
            }
            this.renderConversationList();
        },

        renderConversationList() {
            const host = document.getElementById('v3-conversations-list');
            if (!host) return;
            const items = this._conversationList || [];
            if (!items.length) {
                host.innerHTML = `<div class="v3-thread-empty-label">${h('conversation.list.empty')}</div>`;
                return;
            }
            const active = typeof window.getActiveConnection === 'function' ? window.getActiveConnection() : '';
            const groups = [
                { label: t('conversation.list.thisConnection'), items: items.filter((c) => c.connection_available !== false && c.source_key === active) },
                { label: t('conversation.list.otherConnections'), items: items.filter((c) => c.connection_available !== false && c.source_key !== active) },
                { label: t('conversation.list.unavailableConnections'), items: items.filter((c) => c.connection_available === false), readOnly: true },
            ].filter((group) => group.items.length);
            host.innerHTML = groups.map((group) => `
              <div class="v3-conv-group">
                <div class="v3-thread-empty-label">${esc(group.label)}</div>
                ${group.items.map((c) => this._conversationItemHtml(c, group.readOnly)).join('')}
              </div>`).join('');
            host.querySelectorAll('[data-open-conversation]').forEach((node) => node.addEventListener('click', (event) => {
                if (event.target.closest('[data-conv-action]')) return;
                const item = items.find((c) => c.id === node.dataset.openConversation);
                if (item) this.openConversation(item);
            }));
            host.querySelectorAll('[data-conv-action]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                const item = items.find((c) => c.id === button.dataset.convId);
                if (!item) return;
                if (button.dataset.convAction === 'rename') this.renameConversation(item);
                if (button.dataset.convAction === 'delete') this.deleteConversation(item);
            }));
        },

        _conversationItemHtml(c, readOnly) {
            const current = this.conversation && this.conversation.id === c.id;
            const when = c.last_activity_at ? this._formatWhen(c.last_activity_at) : '';
            const savedCount = Number(c.saved_answer_count) || 0;
            return `<div class="v3-conv-item${current ? ' is-current' : ''}" data-open-conversation="${esc(c.id)}" role="button" tabindex="0">
              <div class="v3-conv-title" dir="${directionOf(c.title)}" title="${esc(c.title)}">${esc(c.title)}</div>
              <div class="v3-conv-meta">
                <span><bdi>${esc(c.source_label || c.source_key)}</bdi>${readOnly ? ` · ${h('conversation.list.readOnly')}` : ''}${savedCount ? ` · ${h('conversation.list.savedCount', { count: savedCount })}` : ''}</span>
                <span>${h('conversation.list.questionCount', { count: Number(c.turn_count) || 0 })}${when ? ` · ${esc(when)}` : ''}</span>
              </div>
              ${c.last_question && c.last_question !== c.title ? `<div class="v3-conv-last" dir="${directionOf(c.last_question)}">${esc(c.last_question)}</div>` : ''}
              <div class="v3-conv-actions">
                <button class="v3-text-btn" data-conv-action="rename" data-conv-id="${esc(c.id)}">${h('common.rename')}</button>
                <button class="v3-text-btn" data-conv-action="delete" data-conv-id="${esc(c.id)}">${h('common.delete')}</button>
              </div>
            </div>`;
        },

        openConversation(item, options = {}) {
            if (!item) return;
            const active = typeof window.getActiveConnection === 'function' ? window.getActiveConnection() : '';
            this.setTab('conversation');
            if (item.connection_available === false) {
                // Connection is gone: open read-only without switching connections.
                this.hydrate(item.source_key, {
                    conversationId: item.id,
                    turnId: options.turnId,
                    favoriteItem: options.favoriteItem,
                    readOnly: true,
                });
                return;
            }
            if (item.source_key && item.source_key !== active && typeof window.onConnectionChange === 'function') {
                // Switching connections triggers hydrate() via 'jeen:connection-resolved';
                // remember which conversation to open instead of the newest one.
                this._pendingOpen = {
                    sourceKey: item.source_key,
                    conversationId: item.id,
                    turnId: options.turnId || null,
                    favoriteItem: options.favoriteItem || null,
                };
                window.onConnectionChange(item.source_key);
                return;
            }
            this.hydrate(item.source_key || active, {
                conversationId: item.id,
                turnId: options.turnId,
                favoriteItem: options.favoriteItem,
            });
        },

        async renameConversation(item) {
            const title = window.prompt(t('conversation.rename.prompt'), item.title || '');
            if (title === null) return;
            const clean = title.trim();
            if (!clean) return;
            try {
                const response = await fetch(`/api/conversations/${encodeURIComponent(item.id)}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title: clean.slice(0, 200) }),
                });
                if (!response.ok) throw new Error(`rename ${response.status}`);
                item.title = clean.slice(0, 200);
                if (this.conversation && this.conversation.id === item.id) {
                    this.conversation.title = item.title;
                    this.renderConversation();
                }
                this.renderConversationList();
            } catch (error) {
                console.warn('[Workspace] rename failed', error);
                if (typeof window.showToast === 'function') window.showToast(t('conversation.rename.failed'), 'error');
            }
        },

        async deleteConversation(item) {
            let savedCount = Number(item.saved_answer_count) || 0;
            let deleteSaved = savedCount > 0;
            const confirmed = window.confirm(deleteSaved
                ? t('conversation.delete.confirmSaved', { title: iso(item.title), count: savedCount })
                : t('conversation.delete.confirm', { title: iso(item.title) }));
            if (!confirmed) return;
            try {
                let url = `/api/conversations/${encodeURIComponent(item.id)}`;
                if (deleteSaved) url += '?delete_saved=true';
                let response = await fetch(url, { method: 'DELETE' });
                if (response.status === 409 && !deleteSaved) {
                    let payload = {};
                    try { payload = await response.json(); } catch (_) { /* plain text */ }
                    const detail = payload.detail || payload;
                    if (detail.code !== 'conversation_has_saved_answers') {
                        throw new Error('unexpected delete conflict');
                    }
                    savedCount = Number(detail.saved_answer_count) || 0;
                    if (!window.confirm(t('conversation.delete.confirmSaved', {
                        title: iso(item.title),
                        count: savedCount,
                    }))) return;
                    deleteSaved = true;
                    response = await fetch(
                        `/api/conversations/${encodeURIComponent(item.id)}?delete_saved=true`,
                        { method: 'DELETE' }
                    );
                }
                if (!response.ok && response.status !== 404) throw new Error(`delete ${response.status}`);
                this._conversationList = (this._conversationList || []).filter((c) => c.id !== item.id);
                this._favoriteList = (this._favoriteList || []).filter((favorite) => favorite.conversation_id !== item.id);
                if (this.conversation && this.conversation.id === item.id) this.reset();
                this.renderConversationList();
                if (this.activeTab === 'saved' && this.savedView === 'answers') this.renderFavoriteAnswers();
                if (typeof window.showToast === 'function') window.showToast(t('conversation.delete.done'), 'success');
            } catch (error) {
                console.warn('[Workspace] delete failed', error);
                if (typeof window.showToast === 'function') window.showToast(t('conversation.delete.failed'), 'error');
            }
        },
        activate() { this.setConversation(true); },
        deactivate() {},
        refreshStarters() { this._renderEmptySuggestions(); },

        _setComposerBusy(busy) {
            if (this.askButton) {
                this.askButton.disabled = busy;
                this.askButton.setAttribute('aria-busy', String(busy));
            }
            this._syncNewConversationAction();
        },

        _renderEmptySuggestions() {
            if (this.turns.length) return;
            this.renderConversation();
        },

        render() {
            this._clearProposalExpiryTimer();
            this._syncNewConversationAction();
            this.renderConversation();
            this.renderWorkspace();
            this.syncRail();
            this._scheduleNextProposalExpiry();
        },

        renderConversation() {
            const thread = document.getElementById('v3-thread');
            if (!thread) return;
            if (!this.turns.length && this.hydrating) {
                thread.innerHTML = `<div class="v3-thread-empty v3-thread-restoring" aria-busy="true">
                  <div class="v3-thread-empty-label">${h('shell.result.restoring')}</div>
                </div>`;
                return;
            }
            if (!this.turns.length) {
                const connectionName = document.getElementById('connection-pill-name')?.textContent?.trim() || t('connection.thisDataset');
                const suggestions = typeof window.getStarterSuggestions === 'function' ? window.getStarterSuggestions(4) : [];
                // ML quick-start chips appear once the catalog has a date column
                // and a measure on one table — this is how the feature is found.
                const ml = this._ensureMlSuggestions(this._analysisConnection());
                const mlChips = ml.map((item) => `<button class="v3-chip v3-chip--ml" dir="${directionOf(item.text)}" data-suggestion="${esc(item.text)}" title="${escFull(item.skill || t('conversation.empty.analysis'))}">${esc(item.text)}</button>`).join('');
                thread.innerHTML = `<div class="v3-thread-empty">
                  <h2>${h('conversation.empty.title')}</h2>
                  <p>${h('conversation.empty.copy')}</p>
                  <div class="v3-thread-empty-label">${h('conversation.empty.suggestedFor', { connection: iso(connectionName) })}</div>
                  <div class="v3-suggestions">${suggestions.map((item) => `<button class="v3-chip" dir="${directionOf(item.text)}" data-suggestion="${esc(item.text)}">${esc(item.text)}</button>`).join('')}${mlChips}</div>
                </div>`;
                thread.querySelectorAll('[data-suggestion]').forEach((button) => button.addEventListener('click', () => this.send(button.dataset.suggestion)));
                return;
            }

            const title = this.conversation ? this.conversation.title : '';
            const readOnlyNote = this.readOnly
                ? `<div class="v3-readonly-note">${h('conversation.readOnlyNote', { connection: iso(this.conversation?.source_label || this.conversation?.source_key || '') })}</div>`
                : '';
            const head = `<div class="v3-thread-head">
                <span class="v3-thread-title" dir="${directionOf(title)}" title="${esc(title)}">${esc(title)}</span>
              </div>${readOnlyNote}`;
            // Capture edit focus BEFORE the rebuild so a re-render (enrichment,
            // empty-hint, etc.) restores focus only when the field actually had
            // it — never yanking it back from the composer (README §5).
            const editHadFocus = Boolean(this.editingTurnId)
                && document.activeElement instanceof HTMLElement
                && document.activeElement.matches('[data-edit-input]');
            thread.innerHTML = head + this.turns.map((turn) => this._turnHtml(turn)).join('');
            thread.querySelectorAll('[data-load-data]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                this.rerunTurn(button.dataset.loadData);
            }));
            thread.querySelectorAll('[data-turn]').forEach((card) => {
                card.addEventListener('click', () => this.selectTurn(card.dataset.turn));
                // Cards are focusable so keyboard users can bring an answer back too.
                card.addEventListener('keydown', (event) => {
                    if (event.target !== card) return;
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        this.selectTurn(card.dataset.turn);
                    }
                });
            });
            thread.querySelectorAll('[data-trace-toggle]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                const turn = this.turns.find((item) => item.id === button.dataset.traceToggle);
                if (turn) {
                    turn.traceOpen = !turn.traceOpen;
                    this.renderConversation();
                }
            }));
            thread.querySelectorAll('[data-followup]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                this.send(button.dataset.followup);
            }));
            thread.querySelectorAll('[data-retry]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                const turn = this.turns.find((item) => item.id === button.dataset.retry);
                if (!turn) return;
                // An edited turn retries in place (keeping its "edited" mark); a
                // normal turn retries as an ordinary re-ask.
                if (turn.edited) this.send(turn.question, { replaceTurnId: turn.id });
                else this.send(turn.question);
            }));
            thread.querySelectorAll('[data-report-gap]').forEach((button) => button.addEventListener('click', async (event) => {
                event.stopPropagation();
                const turn = this.turns.find((item) => item.id === button.dataset.reportGap);
                if (!turn || button.disabled) return;
                button.disabled = true;
                try {
                    // The error text travels as ``notes`` (the field the API stores);
                    // _postJson throws on a non-2xx so a rejected report is never
                    // shown as "Reported".
                    await this._postFeedback(turn, 'catalog_gap', turn.error);
                    button.textContent = t('conversation.turn.reported');
                } catch (_) {
                    button.disabled = false;
                    button.textContent = t('conversation.turn.retryReport');
                }
            }));
            thread.querySelectorAll('[data-feedback]').forEach((button) => button.addEventListener('click', async (event) => {
                event.stopPropagation();
                const [turnId, value] = String(button.dataset.feedback).split(':');
                const turn = this.turns.find((item) => item.id === turnId);
                if (!turn || turn.feedbackSaving) return;
                await this.sendThumb(turn, value);
            }));
            thread.querySelectorAll('[data-feedback-open]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                const turn = this.turns.find((item) => item.id === button.dataset.feedbackOpen);
                if (turn) this.openFeedbackDialog(turn);
            }));
            thread.querySelectorAll('[data-edit]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                this.startEdit(button.dataset.edit);
            }));
            const editHead = thread.querySelector('.v3-turn-head--editing');
            const editInput = thread.querySelector('[data-edit-input]');
            if (editHead) {
                // Esc anywhere in the editing header (textarea, Cancel, Save)
                // leaves edit mode and never reaches the document Escape handler
                // that closes the mobile drawer.
                editHead.addEventListener('keydown', (event) => {
                    if (event.key === 'Escape') {
                        event.preventDefault();
                        event.stopPropagation();
                        this._cancelEdit();
                    }
                });
            }
            if (editInput) {
                editInput.addEventListener('click', (event) => event.stopPropagation());
                const trackSel = () => { this._editSel = [editInput.selectionStart, editInput.selectionEnd]; };
                editInput.addEventListener('input', () => {
                    this.editDraft = editInput.value;
                    trackSel();
                    editInput.style.height = 'auto';
                    editInput.style.height = `${Math.min(editInput.scrollHeight, 200)}px`;
                    const save = thread.querySelector('[data-edit-save]');
                    if (save) save.disabled = !this._editDirty(editInput.dataset.editInput);
                });
                editInput.addEventListener('keyup', trackSel);
                editInput.addEventListener('select', trackSel);
                editInput.addEventListener('keydown', (event) => {
                    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
                        event.preventDefault();
                        this._saveEdit(editInput.dataset.editInput);
                    }
                });
            }
            thread.querySelectorAll('[data-edit-cancel]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                this._cancelEdit();
            }));
            thread.querySelectorAll('[data-edit-save]').forEach((button) => button.addEventListener('click', (event) => {
                event.stopPropagation();
                this._saveEdit(button.dataset.editSave);
            }));
            // A re-render (enrichment, trace toggle, …) rebuilds the thread and
            // would drop an open edit field; restore focus + the prior selection,
            // but only if the field actually had focus (don't grab it back from
            // the composer).
            if (this.editingTurnId && editInput && editHadFocus) {
                this._focusEditInput(this.editingTurnId, !this._editSel);
            }
        },

        /**
         * Whether the "Edit" affordance is offered on a completed turn. Hidden
         * while any turn is streaming, on read-only conversations, on an ML
         * proposal (paused run), a running/rerunning turn, or a restored answer
         * with no rerunnable query behind it.
         */
        _editAvailable(turn) {
            if (this.readOnly || this.sending || turn.rerunning) return false;
            if (turn.status === 'running' || turn.status === 'streaming') return false;
            if (turn.result && turn.result.proposal) return false;
            if (turn.restored && !(turn.result && turn.result.sql)) return false;
            return true;
        },

        /** "You · {time}" (or the edit time) · edited — the grey header meta line. */
        _turnMetaHtml(turn) {
            const bits = [h('conversation.turn.you')];
            if (turn.edited && turn.editedAt != null) {
                // Mockup 3c: edited turns show only the edit time (no date).
                bits.push(esc(this._formatStamp(turn.editedAt, 'time')));
                bits.push(h('conversation.turn.edited'));
            } else if (turn.askedAt != null) {
                // Mockup 3a: "Sep 21, 5:47 PM" — month/day/time, no year.
                bits.push(esc(this._formatStamp(turn.askedAt, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })));
            }
            return `<div class="v3-turn-meta">${bits.join(' · ')}</div>`;
        },

        /** Localized timestamp for the turn header; falls back to the OS locale. */
        _formatStamp(value, style) {
            if (window.I18n && typeof window.I18n.formatDate === 'function') return window.I18n.formatDate(value, style) || '';
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) return '';
            const opts = typeof style === 'object' ? style : { hour: '2-digit', minute: '2-digit' };
            return date.toLocaleString(undefined, opts);
        },

        /** Grey question header: avatar, meta, question, favorite star, Edit. */
        _turnHeadHtml(turn) {
            if (this.editingTurnId === turn.id) return this._turnEditHeadHtml(turn);
            const star = turn.isFavorite ? `<span class="v3-turn-favorite" title="${h('favorite.saved')}">${ICON.star}</span>` : '';
            // On an already-edited error turn the body's "Edit again" covers it,
            // so the header pill would be a duplicate.
            const showEdit = this._editAvailable(turn) && !(turn.status === 'error' && turn.edited);
            const edit = showEdit
                ? `<button type="button" class="v3-turn-edit" data-edit="${turn.id}"><span class="v3-turn-edit-icon" aria-hidden="true">${ICON.pencil}</span>${h('conversation.turn.edit')}</button>`
                : '';
            return `<div class="v3-turn-head">
              <span class="v3-mini-avatar">${esc(this._initials())}</span>
              <div class="v3-turn-headmain">
                ${this._turnMetaHtml(turn)}
                <div class="v3-question" dir="${directionOf(turn.question)}">${esc(turn.question)}</div>
              </div>
              ${star}${edit}
            </div>`;
        },

        /** Edit-mode header (mockup 3b): meta "Editing", a textarea, then Cancel / Save & rerun. */
        _turnEditHeadHtml(turn) {
            const draft = this.editDraft != null ? this.editDraft : String(turn.question || '');
            const dirty = this._editDirty(turn.id);
            return `<div class="v3-turn-head v3-turn-head--editing">
              <span class="v3-mini-avatar">${esc(this._initials())}</span>
              <div class="v3-turn-headmain">
                <div class="v3-turn-meta">${h('conversation.turn.editing')}</div>
                <textarea class="v3-edit-input" data-edit-input="${turn.id}" dir="auto" rows="1" aria-label="${h('conversation.turn.editing')}">${esc(draft)}</textarea>
                <div class="v3-edit-actions">
                  <button type="button" class="v3-edit-cancel" data-edit-cancel="${turn.id}">${h('common.cancel')}</button>
                  <button type="button" class="v3-edit-save" data-edit-save="${turn.id}"${dirty ? '' : ' disabled'}>${h('conversation.turn.saveRerun')}</button>
                </div>
              </div>
            </div>`;
        },

        /** Agent label row that opens every answer body: Jeen mark + "Jeen". */
        _agentLabelHtml() {
            return `<div class="v3-turn-agent"><img class="v3-agent-mark" src="/static/images/jeen-mark.png" alt="">${h('conversation.turn.agent')}</div>`;
        },

        /** The trimmed draft differs from the stored question (Save is enabled). */
        _editDirty(turnId) {
            const turn = this.turns.find((item) => item.id === turnId);
            if (!turn) return false;
            const draft = String(this.editDraft || '').trim();
            return Boolean(draft) && draft !== String(turn.question || '').trim();
        },

        /** Enter edit mode for one turn, cancelling any edit already open. */
        startEdit(turnId) {
            const turn = this.turns.find((item) => item.id === turnId);
            if (!turn || !this._editAvailable(turn)) return;
            this.editingTurnId = turnId;
            this.editDraft = String(turn.question || '');
            this._editSel = null;
            this.renderConversation();
            this._focusEditInput(turnId, true);
        },

        /** Leave edit mode without changes and hand focus back to the Edit button. */
        _cancelEdit() {
            if (!this.editingTurnId) return;
            const id = this.editingTurnId;
            this.editingTurnId = null;
            this.editDraft = '';
            this._editSel = null;
            this.renderConversation();
            document.querySelector(`[data-edit="${id}"]`)?.focus();
        },

        /**
         * Commit an edit: replace the turn's question, mark it edited, wipe every
         * result-derived field so it renders as a fresh running answer, then
         * re-run it in place (later turns are kept). Frontend-only for now — the
         * rerun goes through /api/ask/stream with the full conversation as
         * context and the server appends a new turn (see the handoff open
         * questions), so a reload shows the edit as an appended turn.
         */
        _saveEdit(turnId) {
            const turn = this.turns.find((item) => item.id === turnId);
            if (!turn || !this._editDirty(turnId)) return;
            const draft = String(this.editDraft || '').trim();
            // Mark the edit; send() (via _resetTurnForRerun) wipes the old result
            // and re-runs the turn in place, keeping later turns.
            turn.edited = true;
            turn.editedAt = Date.now();
            this.editingTurnId = null;
            this.editDraft = '';
            this._editSel = null;
            this.selectedTurnId = turn.id;
            this.selectedResultId = turn.id;
            this.send(draft, { replaceTurnId: turn.id });
            if (this.input) this.input.focus();
        },

        /**
         * Wipe every result-derived field on a turn that is about to be re-run in
         * place (edit / edited-retry) and bump its revision so any late async
         * writeback (empty-hint, artifact, analysis chart) from the previous run
         * is dropped instead of landing on the new answer. Question / edited /
         * editedAt are set by the caller.
         */
        _resetTurnForRerun(turn) {
            turn.rev = (turn.rev || 0) + 1;
            turn.status = 'running';
            turn.startedAt = performance.now();
            turn.durationMs = null;
            turn.phaseState = Object.fromEntries(PHASES.map((phase) => [phase.id, 'pending']));
            turn.trace = [];
            turn.traceOpen = false;
            turn.result = null;
            turn.provisionalRevision = null;
            turn.timeline = {};
            turn.error = null;
            turn.restored = false;
            turn.snapshotAt = null;
            turn.resultKind = undefined;
            turn.hasChart = false;
            turn.canLoadData = false;
            turn.artifactState = undefined;
            turn.rerunError = null;
            turn.chartState = null;
            turn.chartLoading = false;
            turn.chartUnavailable = false;
            turn.chartCollapsed = undefined;
            turn.isFavorite = false;
            turn.feedback = null;
            turn.feedbackSent = false;
        },

        /** Focus the edit textarea (after a render), size it, and restore the caret. */
        _focusEditInput(turnId, toEnd) {
            requestAnimationFrame(() => {
                const input = document.querySelector(`[data-edit-input="${turnId}"]`);
                if (!input) return;
                input.style.height = 'auto';
                input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
                input.focus();
                if (!toEnd && Array.isArray(this._editSel)) {
                    const [start, end] = this._editSel;
                    try { input.setSelectionRange(start, end); } catch (_) { /* not focusable yet */ }
                } else {
                    const len = input.value.length;
                    try { input.setSelectionRange(len, len); } catch (_) { /* not focusable yet */ }
                }
            });
        },

        _turnHtml(turn) {
            const selected = turn.id === this.selectedTurnId;
            if (turn.status === 'running' || turn.status === 'streaming') {
                const streaming = turn.status === 'streaming';
                const rowCount = streaming ? normalizeRows(turn.result && turn.result.results).length : 0;
                return `<article class="v3-turn is-running${streaming ? ' is-streaming' : ''}${selected ? ' is-selected' : ''}" data-turn="${turn.id}"${streaming ? ` data-show-label="${h('conversation.turn.showAnswerBadge')}" tabindex="0" aria-current="${selected ? 'true' : 'false'}"` : ''}>
                  ${this._turnHeadHtml(turn)}
                  <div class="v3-turn-body">
                    ${this._agentLabelHtml()}
                    ${streaming ? `<div class="v3-data-ready" role="status" dir="auto"><span class="v3-dot is-ok"></span><span>${h('conversation.turn.dataReady', { rows: rowCount })}</span>${this._timelineHtml(turn)}</div>` : ''}
                    <div class="v3-running-list">${PHASES.map((phase) => {
                    const status = turn.phaseState[phase.id];
                    const label = status === 'done' ? h('conversation.turn.statusOk') : status === 'running' ? h('conversation.turn.statusRunning') : status === 'error' ? h('conversation.turn.statusFailed') : '';
                    return `<div class="v3-running-row is-${status}"><span class="v3-dot is-${status === 'done' ? 'ok' : status}"></span><span>${esc(phase.label)}</span><span class="v3-running-status">${label}</span></div>`;
                }).join('')}</div>
                    ${streaming ? `<div class="v3-skeleton-group" aria-label="${h('conversation.turn.summaryPending')}" aria-busy="true">
                      <div class="v3-skeleton v3-skeleton-summary"></div>
                      <div class="v3-skeleton v3-skeleton-line"></div>
                      <div class="v3-skeleton v3-skeleton-line is-short"></div>
                      <div class="v3-skeleton-chips"><span class="v3-skeleton v3-skeleton-chip"></span><span class="v3-skeleton v3-skeleton-chip"></span></div>
                    </div>` : ''}
                  </div>
                </article>`;
            }
            if (turn.status === 'error') {
                const failed = [...turn.trace].reverse().find((item) => item.status === 'node_failed');
                const isNewest = this.turns[this.turns.length - 1]?.id === turn.id;
                const meta = turn.restored
                    ? `${esc(turn.executionStatus || t('conversation.turn.error'))} · ${h('conversation.turn.restoredFromHistory')}`
                    : h('conversation.turn.failedAt', { node: iso(failed?.node || t('conversation.turn.query')), duration: formatMs(turn.durationMs) });
                return `<article class="v3-turn${selected || (!turn.restored && isNewest) ? ' is-selected' : ''}${this.editingTurnId === turn.id ? ' is-editing' : ''}" data-turn="${turn.id}" data-show-label="${h('conversation.turn.showAnswerBadge')}" tabindex="0" aria-label="${h('conversation.turn.showAnswer', { question: iso(turn.question) })}">
                  ${this._turnHeadHtml(turn)}
                  <div class="v3-turn-body">
                    ${this._agentLabelHtml()}
                    <div class="v3-error-block" dir="auto">${esc(turn.error)}
                      <div class="v3-error-meta">${meta}</div>
                    </div>
                    ${isNewest ? `<div class="v3-summary" style="color:var(--muted)">${h('conversation.turn.newestFailed')}</div>` : ''}
                    <div class="v3-error-actions">
                      <button data-retry="${turn.id}">${h('common.retry')}</button>
                      ${turn.edited ? `<button data-edit="${turn.id}">${h('conversation.turn.editAgain')}</button>` : ''}
                      ${turn.restored ? '' : `<button title="${h('conversation.turn.editSqlTitle')}">${h('conversation.turn.editSql')}</button>`}
                      <button data-report-gap="${turn.id}">${h('conversation.turn.reportGap')}</button>
                    </div>
                  </div>
                </article>`;
            }

            const result = turn.result || {};
            const finished = turn.trace.filter((item) => item.status === 'node_finished');
            const summary = textOf(result.answer);
            const findings = result.findings || [];
            const followups = result.followups || [];
            // A successful query that returned no rows: show an explicit,
            // localized "no records" line plus a likely-cause hint, and never
            // paint findings/followups (which only make sense for real rows).
            // Restored turns can have unloaded rows, so trust only the server's
            // empty_result flag for them (never the row-count heuristic).
            const emptyRows = normalizeRows(result.results);
            const hasRows = emptyRows.length > 0;
            const isEmpty = Boolean(result.empty_result)
                || (!turn.restored && Boolean(result.sql && !result.error && !result.proposal && !hasRows));
            const emptyMessage = isEmpty ? h('results.grid.noRecordsFromDb') : '';
            // Findings + follow-ups are authoritative text saved with the turn, so
            // a restored answer shows them even before its rows are (re)loaded; a
            // live turn still needs real rows, and neither paints on an empty result.
            const showAnalytics = (hasRows || turn.restored) && !isEmpty;
            const skillLabel = window.JeenAnalysisUI ? window.JeenAnalysisUI.SKILL_LABEL : {};
            if (result.proposal) {
                // A stopped ML run: the card lives in the answer pane; the thread
                // shows the question, the message and what the run is waiting for.
                const proposal = result.proposal;
                const kind = proposal.kind || result.status;
                const expired = window.JeenAnalysisUI
                    ? window.JeenAnalysisUI.proposalExpired(proposal)
                    : !proposal.proposal_id;
                const waiting = expired
                    ? t('conversation.turn.expiredProposal')
                    : kind === 'guard' ? t('conversation.turn.waitingGuard') : kind === 'clarify' ? t('conversation.turn.waitingChoice') : t('conversation.turn.waitingConfirm');
                return `<article class="v3-turn is-proposal${expired ? ' is-expired' : ''}${selected ? ' is-selected' : ''}${turn.restored ? ' is-restored' : ''}" data-turn="${turn.id}" data-show-label="${h('conversation.turn.showCardBadge')}" data-route-path="ml" data-route-source="${esc((result.routing || {}).source || '')}" tabindex="0" aria-label="${h('conversation.turn.showCard', { question: iso(turn.question) })}" aria-current="${selected ? 'true' : 'false'}">
                  ${this._turnHeadHtml(turn)}
                  <div class="v3-turn-body">
                    ${this._agentLabelHtml()}
                    <div class="v3-run-strip">${this._routePillHtml(result)}<span class="v3-skill-chip">${esc(skillLabel[proposal.skill] || proposal.skill || t('conversation.empty.analysis'))}</span><span class="v3-run-meta">${esc(waiting)}</span></div>
                    <div class="v3-summary" dir="${directionOf(summary)}">${esc(summary || proposal.message || '')}</div>
                  </div>
                </article>`;
            }
            const analysis = result.analysis && result.analysis.skill ? result.analysis : null;
            const mlPill = analysis
                ? `<span class="v3-skill-chip">${esc(skillLabel[analysis.skill] || analysis.skill)}</span>${result.low_confidence || analysis.low_confidence ? `<span class="v3-lowconf-pill">${h('conversation.turn.lowConfidence')}</span>` : ''}`
                : '';
            const insightsDirection = directionOf(findings.map(textOf).join(' '));
            const dots = PHASES.map((phase) => {
                const events = finished.filter((item) => (NODE_PHASE[item.node] || 'execution') === phase.id);
                const elapsed = events.reduce((total, item) => total + Number(item.elapsed_ms || 0), 0);
                const ran = events.length > 0;
                return `<span class="v3-dot${ran ? ' is-done' : ''}" title="${escFull(phase.label)}${ran ? ` · ${formatMs(elapsed)}` : ` · ${h('conversation.turn.notRun')}`}"></span>`;
            }).join('');
            const trace = turn.trace.filter((item) => item.status !== 'node_started');
            const routePath = (result.routing || {}).path || (analysis ? 'ml' : 'sql');
            const strip = turn.restored ? this._restoredStripHtml(turn) : `<div class="v3-run-strip">${dots}${this._routePillHtml(result)}${mlPill}<span class="v3-run-meta">${h('conversation.turn.runMeta', { duration: formatMs(turn.durationMs), count: trace.length })}</span>${this._timelineHtml(turn)}
                <button class="v3-text-btn" data-trace-toggle="${turn.id}">${turn.traceOpen ? h('conversation.turn.hideRun') : h('conversation.turn.runDetails')}</button>
              </div>`;
            return `<article class="v3-turn${selected ? ' is-selected' : ''}${turn.restored ? ' is-restored' : ''}${this.editingTurnId === turn.id ? ' is-editing' : ''}" data-turn="${turn.id}" data-show-label="${h('conversation.turn.showAnswerBadge')}" data-route-path="${esc(routePath)}" data-route-source="${esc((result.routing || {}).source || '')}" tabindex="0" aria-label="${h('conversation.turn.showAnswer', { question: iso(turn.question) })}" aria-current="${selected ? 'true' : 'false'}">
              ${this._turnHeadHtml(turn)}
              <div class="v3-turn-body">
                ${this._agentLabelHtml()}
                ${strip}
                ${!turn.restored && turn.traceOpen ? this._traceHtml(turn, trace) : ''}
                ${isEmpty
                    ? `<div class="v3-summary" dir="${directionOf(emptyMessage)}">${esc(emptyMessage)}</div>
                       <div class="v3-empty-hint${result.empty_hint ? '' : ' is-loading'}" dir="${directionOf(textOf(result.empty_hint || emptyMessage))}">${result.empty_hint ? esc(textOf(result.empty_hint)) : h('conversation.turn.emptyHintLoading')}</div>`
                    : (summary ? (wantsMarkdown(result) ? markdownDiv(summary) : `<div class="v3-summary" dir="${directionOf(summary)}">${esc(summary)}</div>`) : '')}
                ${(findings.length && showAnalytics) ? `<section class="v3-insights" aria-label="${h('conversation.turn.keyInsights')}" dir="${insightsDirection}">
                  <div class="v3-insights-title"><span class="v3-insights-mark" aria-hidden="true">✦</span>${h('conversation.turn.keyInsights')}</div>
                  <div class="v3-insights-list">${findings.map((finding, index) => `<div class="v3-finding">
                    <span class="v3-insight-index" aria-hidden="true">${index + 1}</span>
                    <span dir="${directionOf(finding)}">${esc(textOf(finding))}</span>
                  </div>`).join('')}</div>
                </section>` : ''}
                ${(followups.length && showAnalytics) ? `<div class="v3-followups">${followups.map((question) => `<button class="v3-chip" dir="${directionOf(question)}" data-followup="${esc(textOf(question))}">${esc(textOf(question))}</button>`).join('')}</div>` : ''}
                ${isEmpty ? '' : this._feedbackHtml(turn)}
              </div>
            </article>`;
        },

        /**
         * "table 3.0s · insights 5.2s · chart 10.1s": how long after the
         * question each part of the answer became visible. Only the stages
         * that happened are listed; nothing for restored turns.
         */
        _timelineHtml(turn) {
            const timeline = turn.timeline || {};
            const parts = [
                ['tableMs', 'conversation.turn.timelineTable'],
                ['insightsMs', 'conversation.turn.timelineInsights'],
                ['chartMs', 'conversation.turn.timelineChart'],
            ].filter(([key]) => Number.isFinite(timeline[key]))
                .map(([key, label]) => `<span class="v3-timeline-part" data-timeline="${key.replace('Ms', '')}">${h(label, { time: formatMs(timeline[key]) })}</span>`);
            if (!parts.length) return '';
            return `<span class="v3-timeline" dir="ltr" title="${h('conversation.turn.timelineTitle')}">${parts.join('<span class="v3-timeline-sep" aria-hidden="true">·</span>')}</span>`;
        },

        _traceHtml(turn, trace) {
            const rows = trace.map((item) => {
                let html = `<div class="v3-trace-row">
                  <span class="v3-dot ${item.status === 'node_failed' ? '' : 'is-ok'}"></span>
                  <span>${esc(item.node)}</span><span class="v3-trace-note">${esc(safeTraceNote(item))}</span>
                  <span class="v3-trace-ms">${formatMs(item.elapsed_ms)}</span></div>`;
                const timing = item.node === 'pre_graph_setup' && item.mcp_timing;
                if (timing && typeof timing === 'object') {
                    const parts = [
                        ['mcpFiltered', timing.filtered_tool_ms],
                        ['mcpReusable', timing.full_restore_ms],
                        ['mcpConnection', timing.connection_ms],
                        ['mcpParse', timing.parse_ms],
                    ];
                    html += parts
                        .filter(([, value]) => Number.isFinite(Number(value)))
                        .map(([label, value]) => `<div class="v3-trace-row v3-trace-row--breakdown">
                          <span></span><span>${h(`conversation.trace.${label}`)}</span>
                          <span class="v3-trace-note">${h('conversation.trace.includedInPreGraph')}</span>
                          <span class="v3-trace-ms">${formatMs(Number(value))}</span></div>`)
                        .join('');
                }
                return html;
            }).join('');
            const graphMs = trace.reduce((total, item) => total + Number(item.elapsed_ms || 0), 0);
            const wallMs = Number(turn.durationMs || 0);
            const overheadMs = Math.max(0, wallMs - graphMs);
            const reconcile = wallMs > 0 && graphMs > 0
                ? `<div class="v3-trace-reconcile">${h('conversation.trace.reconcile', {
                    wall: formatMs(wallMs),
                    graph: formatMs(graphMs),
                    overhead: formatMs(overheadMs),
                })}</div>`
                : '';
            return `<div class="v3-trace">${rows}${reconcile}</div>`;
        },

        _routePillHtml(result) {
            // A small, always-present badge naming the path this answer took —
            // "ML skill" or "SQL" — so it is obvious (and testable) which engine ran.
            const routing = (result && result.routing) || {};
            const path = routing.path || (result && result.analysis && result.analysis.skill ? 'ml' : result && result.proposal ? 'ml' : 'sql');
            if (path !== 'ml' && path !== 'sql') return '';
            const label = path === 'ml' ? t('conversation.turn.routeMl') : t('conversation.turn.routeSql');
            const title = routing.reason ? `${label} · ${routing.reason}` : label;
            return `<span class="v3-route-pill is-${path}" data-route-path="${esc(path)}" data-route-source="${esc(routing.source || '')}" title="${esc(title)}">${esc(label)}</span>`;
        },

        _restoredStripHtml(turn) {
            const when = turn.snapshotAt ? this._formatWhen(turn.snapshotAt) : null;
            if (turn.resultKind === 'text') {
                return `<div class="v3-run-strip"><span class="v3-run-meta">${h('conversation.restored.textAnswer')}</span></div>`;
            }
            if (turn.rerunning) {
                return `<div class="v3-run-strip"><span class="v3-run-meta">${h('conversation.restored.reloading')}</span></div>`;
            }
            if (turn.result?.results) {
                const label = when
                    ? h('conversation.restored.snapshotFrom', { when })
                    : turn.snapshotStatus === 'too_large' ? h('conversation.restored.freshTooLarge') : h('conversation.restored.snapshot');
                const analysis = turn.result.analysis && turn.result.analysis.skill ? turn.result.analysis : null;
                const pill = analysis && window.JeenAnalysisUI
                    ? `<span class="v3-skill-chip">${esc(window.JeenAnalysisUI.SKILL_LABEL[analysis.skill] || analysis.skill)}</span>${turn.result.low_confidence || analysis.low_confidence ? `<span class="v3-lowconf-pill">${h('conversation.turn.lowConfidence')}</span>` : ''}`
                    : '';
                return `<div class="v3-run-strip">${pill}
                  <span class="v3-run-meta">${label}${turn.hasChart ? ` · ${h('conversation.restored.chart')}` : ''}</span>
                  ${turn.canLoadData || turn.result?.sql ? `<button class="v3-text-btn" data-load-data="${turn.id}">${h('common.refresh')}</button>` : ''}
                </div>`;
            }
            if (turn.artifactState === 'loading') {
                return `<div class="v3-run-strip"><span class="v3-run-meta">${h('conversation.restored.loadingRows')}</span></div>`;
            }
            const reason = turn.snapshotStatus === 'too_large'
                ? t('conversation.restored.tooLarge')
                : turn.artifactState === 'failed'
                    ? t('conversation.restored.loadFailed')
                    : t('conversation.restored.notKept');
            return `<div class="v3-run-strip">
              <span class="v3-run-meta">${esc(reason)}</span>
              ${turn.canLoadData || turn.result?.sql ? `<button class="v3-text-btn" data-load-data="${turn.id}">${h('conversation.restored.loadData')}</button>` : ''}
              ${turn.rerunError ? `<span class="v3-run-meta v3-run-error">${esc(turn.rerunError)}</span>` : ''}
            </div>`;
        },

        _formatWhen(iso) {
            if (window.I18n && typeof window.I18n.formatDate === 'function') return window.I18n.formatDate(iso, 'dateTime') || String(iso);
            const date = new Date(iso);
            if (Number.isNaN(date.getTime())) return String(iso);
            return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
        },

        _placeholderDefault: null,

        /** Result pane for a restored table turn whose rows are not available. */
        _renderPendingResult(turn) {
            const placeholder = document.getElementById('v3-placeholder');
            if (this._placeholderDefault === null) this._placeholderDefault = placeholder.innerHTML;
            const loading = turn.artifactState === 'loading' || turn.rerunning;
            let title = t('conversation.pending.loadingTitle');
            let copy = t('conversation.pending.loadingCopy');
            let action = '';
            if (!loading) {
                if (turn.snapshotStatus === 'too_large') {
                    title = t('conversation.pending.tooLargeTitle');
                    copy = t('conversation.pending.tooLargeCopy');
                } else if (turn.artifactState === 'failed') {
                    title = t('conversation.pending.failedTitle');
                    copy = turn.rerunError || t('conversation.pending.failedCopy');
                } else {
                    title = t('conversation.pending.notKeptTitle');
                    copy = t('conversation.pending.notKeptCopy');
                }
                if (turn.canLoadData || turn.result?.sql) {
                    action = `<button class="v3-load-data" data-load-data="${turn.id}">${turn.rerunning ? h('common.loading') : h('conversation.restored.loadData')}</button>`;
                }
            }
            this._setResultTitle(turn.question);
            document.getElementById('v3-meta-row').innerHTML = `<span class="v3-status is-empty">${loading ? h('conversation.pending.loading') : h('conversation.pending.noRowsLoaded')}</span>`;
            placeholder.innerHTML = `<strong>${esc(title)}</strong><span>${esc(copy)}</span>${action}`;
            placeholder.querySelector('[data-load-data]')?.addEventListener('click', () => this.rerunTurn(turn.id));
            placeholder.hidden = false;
            document.getElementById('v3-chart-block').hidden = true;
            document.getElementById('v3-table-block').hidden = true;
            document.getElementById('v3-dock-meta').textContent = turn.result?.sql ? t('conversation.pending.storedQuery') : t('conversation.pending.noQueryText');
            this._setActionsEnabled(false);
            this.renderDock();
        },

        _restorePlaceholder() {
            const placeholder = document.getElementById('v3-placeholder');
            if (placeholder && this._placeholderDefault !== null && placeholder.innerHTML !== this._placeholderDefault) {
                placeholder.innerHTML = this._placeholderDefault;
            }
        },

        // ── ML skills ─────────────────────────────────────────────────────────

        _setModelTabVisible(visible) {
            const tab = document.querySelector('[data-dock="model"]');
            if (!tab) return;
            tab.hidden = !visible;
            if (!visible && this.dockTab === 'model') this.dockTab = 'sql';
        },

        _clearProposalExpiryTimer() {
            if (this._proposalExpiryTimer !== null) {
                clearTimeout(this._proposalExpiryTimer);
                this._proposalExpiryTimer = null;
            }
        },

        _scheduleNextProposalExpiry() {
            const now = Date.now();
            const expiries = this.turns
                .map((turn) => new Date(turn.result?.proposal?.expires_at).getTime())
                .filter((expiresAt) => Number.isFinite(expiresAt) && expiresAt > now);
            if (!expiries.length) return;
            const delay = Math.min(Math.min(...expiries) - now + 50, 2_147_483_647);
            this._proposalExpiryTimer = setTimeout(() => {
                this._proposalExpiryTimer = null;
                this.render();
            }, delay);
        },

        /** Answer pane for a stopped ML run: confirm card, clarification or guard refusal. */
        _renderProposal(turn) {
            const placeholder = document.getElementById('v3-placeholder');
            if (this._placeholderDefault === null) this._placeholderDefault = placeholder.innerHTML;
            const data = turn.result || {};
            const proposal = data.proposal || {};
            const kind = proposal.kind || data.status || 'confirm';
            const expired = window.JeenAnalysisUI
                ? window.JeenAnalysisUI.proposalExpired(proposal)
                : !proposal.proposal_id;
            const statusLabel = expired
                ? t('conversation.proposal.expired')
                : kind === 'guard' || data.status === 'blocked' ? t('conversation.proposal.blocked') : kind === 'clarify' ? t('conversation.proposal.clarify') : t('conversation.proposal.planning');
            const skill = (window.JeenAnalysisUI && window.JeenAnalysisUI.SKILL_LABEL[proposal.skill]) || proposal.skill || '';
            const failed = (proposal.guard_results || []).filter((g) => !g.passed);
            const meta = kind === 'guard'
                ? t('conversation.proposal.guardMeta', { names: iso(failed.map((g) => g.name).join(', ') || t('conversation.proposal.refused')) })
                : kind === 'clarify' ? t('conversation.proposal.clarifyMeta') : t('conversation.proposal.confirmMeta');
            this._setResultTitle(turn.question);
            // Guard keeps a status strip (it saves to history and reads as a result);
            // confirm/clarify lead with the Planning line inside the card, so the strip
            // stays out of the way — matching the skill-states mockup.
            document.getElementById('v3-meta-row').innerHTML = expired
                ? `<span class="v3-status is-expired">${esc(statusLabel)}</span>
                   ${skill ? `<span class="v3-skill-chip">${esc(skill)}</span>` : ''}
                   ${turn.restored ? `<span class="v3-result-meta">${h('conversation.restored.restored')}</span>` : ''}`
                : kind === 'guard'
                ? `<span class="v3-status is-blocked">${esc(statusLabel)}</span>
                   ${skill ? `<span class="v3-skill-chip">${esc(skill)}</span>` : ''}
                   <span class="v3-result-meta">${esc(meta)}${turn.restored ? ` · ${h('conversation.restored.restored')}` : ''}</span>`
                : (turn.restored ? `<span class="v3-result-meta">${h('conversation.restored.restored')}</span>` : '');
            placeholder.innerHTML = window.JeenAnalysisUI
                ? window.JeenAnalysisUI.proposalHtml(proposal)
                : `<strong>${esc(statusLabel)}</strong><span>${esc(textOf(data.answer))}</span>`;
            placeholder.hidden = false;
            this._hideDefinition();
            document.getElementById('v3-chart-block').hidden = true;
            document.getElementById('v3-table-block').hidden = true;
            const chart = document.getElementById('chart-view-container');
            if (chart) chart.style.display = 'none';
            this._setModelTabVisible(false);
            document.getElementById('v3-dock-meta').textContent = expired
                ? t('conversation.proposal.expiredDock')
                : kind === 'guard' ? t('conversation.proposal.blockedBeforeSql') : t('conversation.proposal.waitingForYou');
            this._setActionsEnabled(false);
            this.renderDock();
            this._bindProposalCard(turn, placeholder);
        },

        _bindProposalCard(turn, root) {
            const card = root.querySelector('.v3-ml-card');
            if (!card) return;
            const proposal = (turn.result || {}).proposal || {};
            const expired = window.JeenAnalysisUI
                ? window.JeenAnalysisUI.proposalExpired(proposal)
                : !proposal.proposal_id;
            if (expired) {
                const recreate = card.querySelector('[data-recreate]');
                const hint = card.querySelector('[data-recreate-hint]');
                const connection = this._analysisConnection();
                const unavailable = this.readOnly
                    ? t('conversation.readOnlySend')
                    : (!connection ? t('analysis.proposal.selectConnection') : '');
                if (recreate && unavailable) {
                    recreate.disabled = true;
                    recreate.title = unavailable;
                    if (hint) {
                        hint.hidden = false;
                        hint.textContent = unavailable;
                    }
                }
                recreate?.addEventListener('click', async () => {
                    if (recreate.disabled) return;
                    const label = recreate.textContent;
                    card.classList.add('is-busy');
                    recreate.disabled = true;
                    recreate.setAttribute('aria-busy', 'true');
                    recreate.textContent = t('analysis.proposal.askingAgain');
                    card.querySelector('[data-sql-instead]')?.setAttribute('disabled', '');
                    try {
                        await this.send(turn.question, { analysis: true });
                    } finally {
                        if (card.isConnected) {
                            card.classList.remove('is-busy');
                            recreate.disabled = false;
                            recreate.removeAttribute('aria-busy');
                            recreate.textContent = label;
                            card.querySelector('[data-sql-instead]')?.removeAttribute('disabled');
                        }
                    }
                });
                card.querySelector('[data-sql-instead]')?.addEventListener('click', () => this.send(turn.question, { analysis: false }));
                return;
            }
            // The setup form's local behaviour (summary, changed markers, validation)
            // lives in analysisPanel.js; the controller only runs and reports.
            const form = window.JeenAnalysisUI && window.JeenAnalysisUI.bindSetupForm && proposal.kind === 'confirm'
                ? window.JeenAnalysisUI.bindSetupForm(card, proposal.chips || [], { skill: proposal.skill, tier: proposal.tier })
                : null;
            const setBusy = (busy, label) => {
                card.classList.toggle('is-busy', busy);
                card.querySelectorAll('button, select, input').forEach((b) => { b.disabled = busy; });
                const run = card.querySelector('[data-run] > span, [data-run]');
                if (run && label) run.firstChild.textContent = label;
            };
            const fail = (error) => {
                setBusy(false);
                const detail = error && typeof error === 'object' ? error.detail : null;
                if (form && detail && form.showServerError(detail)) return;
                const message = error && error.message ? error.message : String(error);
                let note = card.querySelector('.v3-ml-error');
                if (!note) {
                    note = document.createElement('div');
                    note.className = 'v3-ml-error';
                    card.appendChild(note);
                }
                note.textContent = message;
            };
            const handleFailure = (error) => {
                if (error && error.status === 410) {
                    proposal.expires_at = new Date(0).toISOString();
                    this.render();
                    return;
                }
                fail(error);
            };
            card.querySelector('[data-run]')?.addEventListener('click', async () => {
                // Validate against the chips' declared bounds first: the first invalid
                // field gets focus and a message, and nothing is sent.
                if (form) {
                    const { ok, errors } = form.validate();
                    if (!ok) { form.reportInvalid(errors); return; }
                }
                const patch = window.JeenAnalysisUI ? window.JeenAnalysisUI.collectPatch(card) : {};
                const remember = Boolean(card.querySelector('[data-remember]')?.checked);
                setBusy(true, t('conversation.proposal.running'));
                try {
                    await this.runProposal(turn, { patch, remember });
                } catch (error) {
                    handleFailure(error);
                }
            });
            card.querySelectorAll('[data-exit]').forEach((button) => button.addEventListener('click', async () => {
                const option = (proposal.options || [])[Number(button.dataset.exit)];
                if (!option) return;
                if (option.kind === 'answer_with_sql') {
                    this.send(turn.question, { analysis: false });
                    return;
                }
                if (option.kind === 'switch_skill') {
                    this.input?.focus();
                    return;
                }
                setBusy(true);
                try {
                    await this.runProposal(turn, {
                        patch: option.params_patch || {},
                        override: option.kind === 'override',
                    });
                } catch (error) {
                    handleFailure(error);
                }
            }));
            card.querySelector('[data-sql-instead]')?.addEventListener('click', () => this.send(turn.question, { analysis: false }));
            // "Rephrase the question": return the cursor to the composer so the user can
            // ask toward another analysis (same intent as the clarify switch_skill exit).
            card.querySelector('[data-switch-skill]')?.addEventListener('click', () => this.input?.focus());
        },

        _analysisConnection() {
            return typeof window.getActiveConnection === 'function' ? window.getActiveConnection() : '';
        },

        async _postJson(url, body, { signal = null, timeoutMs = 0 } = {}) {
            let requestSignal = signal;
            if (timeoutMs > 0 && typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
                const timeoutSignal = AbortSignal.timeout(timeoutMs);
                requestSignal = signal && typeof AbortSignal.any === 'function'
                    ? AbortSignal.any([signal, timeoutSignal])
                    : (signal || timeoutSignal);
            }
            const response = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                signal: requestSignal,
            });
            let payload = {};
            try { payload = await response.json(); } catch (_) { payload = {}; }
            if (!response.ok) {
                // The BFF wraps upstream errors as {error: "<json>"}; surface the detail.
                let detail = payload.detail || payload.error || t('conversation.proposal.requestFailed', { status: response.status });
                if (typeof detail === 'string' && detail.trim().startsWith('{')) {
                    try { const inner = JSON.parse(detail); detail = inner.detail || detail; } catch (_) { /* keep text */ }
                }
                // A structured 422 ({message, field, loc}) travels on the error so
                // the setup card can put the message on the field it names.
                const structured = detail && typeof detail === 'object' ? detail : null;
                const message = structured
                    ? (structured.message || JSON.stringify(structured))
                    : errorText({ status: response.status, payload }, String(detail));
                const error = new Error(message);
                error.status = response.status;
                error.detail = structured;
                throw error;
            }
            return payload;
        },

        /**
         * POST one turn-row feedback value (``catalog_gap`` on a failed turn).
         * Throws on a non-2xx (via _postJson). Answer-quality feedback
         * (thumbs / dialog) goes through _postAnswerFeedback instead.
         */
        async _postFeedback(turn, value, notes = null) {
            const queryId = turn.result?.query_id || turn.turnId;
            if (!queryId) throw new Error('feedback: turn has no query id');
            const body = { query_id: String(queryId), feedback: value };
            const trimmed = notes == null ? '' : String(notes).trim();
            if (trimmed) body.notes = trimmed.slice(0, 4000);
            const connection = this._analysisConnection();
            if (connection) body.connection = connection;
            return this._postJson('/api/feedback', body);
        },

        /** Append one answer-feedback event (insights_answer_feedback). */
        async _postAnswerFeedback(turn, fields) {
            const queryId = turn.result?.query_id || turn.turnId;
            if (!queryId) throw new Error('feedback: turn has no query id');
            const body = { query_id: String(queryId) };
            if (fields.thumb) body.thumb = fields.thumb;
            if (Number.isInteger(fields.rating) && fields.rating >= 1) body.rating = fields.rating;
            if (fields.feedback_type) body.feedback_type = fields.feedback_type;
            const message = fields.message == null ? '' : String(fields.message).trim();
            if (message) body.message = message.slice(0, 4000);
            const connection = this._analysisConnection();
            if (connection) body.connection = connection;
            return this._postJson('/api/answer-feedback', body);
        },

        /** Answers that can take feedback: a SQL result or an ML analysis, not a greeting/capability text. */
        _feedbackEligible(turn) {
            if (!turn || turn.status !== 'success' || !turn.result || turn.result.proposal) return false;
            if (!(turn.result.query_id || turn.turnId)) return false;
            return Boolean(turn.result.sql || (turn.result.analysis && turn.result.analysis.skill));
        },

        /**
         * Quick signal: thumbs up / down on the answer card. Optimistic — the
         * thumb paints pressed at once and reverts if the save fails. Clicking
         * the pressed thumb again withdraws it (a ``cleared`` event).
         */
        async sendThumb(turn, value) {
            if (!turn || !['thumbs_up', 'thumbs_down'].includes(value)) return false;
            const prior = turn.feedback || null;
            const next = prior === value ? null : value;
            turn.feedback = next;
            turn.feedbackSaving = true;
            this.renderConversation();
            try {
                const saved = await this._postAnswerFeedback(turn, { thumb: next || 'cleared' });
                // Settle on the server's view of the current thumb.
                if (saved && Object.prototype.hasOwnProperty.call(saved, 'thumb')) turn.feedback = saved.thumb || null;
                return true;
            } catch (error) {
                console.warn('[Workspace] thumb feedback failed', error);
                turn.feedback = prior;
                if (typeof window.showToast === 'function') window.showToast(t('conversation.feedback.failed'), 'error');
                return false;
            } finally {
                turn.feedbackSaving = false;
                this.renderConversation();
            }
        },

        /**
         * Forecast tracking: compare a shown forecast with the actuals that have
         * arrived since. Result lives on the turn for this page session and is
         * drawn over the live chart; nothing saved is rewritten.
         */
        async checkForecastAccuracy(turn) {
            if (!turn || !turn.result?.analysis || turn.result.analysis.skill !== 'forecast') return;
            if (turn.tracking?.loading) return;
            const connection = this._analysisConnection();
            const queryId = turn.result.query_id || turn.turnId;
            if (!connection || !queryId) return;
            turn.tracking = { ...(turn.tracking || {}), loading: true, error: null };
            this.renderDock();
            try {
                const data = await this._postJson('/api/analysis/forecast/accuracy', { connection, query_id: String(queryId) }, { timeoutMs: 60_000 });
                turn.tracking = { loading: false, error: null, evaluation: data };
                if (this.selectedResultId === turn.id && window.JeenLegacyBridge?.overlayRealizedActuals) {
                    window.JeenLegacyBridge.overlayRealizedActuals(data.points || [], t('analysis.accuracy.realizedSeries'));
                }
            } catch (error) {
                turn.tracking = { loading: false, error: error.message || t('analysis.accuracy.failed'), evaluation: turn.tracking?.evaluation || null };
            } finally {
                this.renderDock();
            }
        },

        _feedbackHtml(turn) {
            if (!this._feedbackEligible(turn)) return '';
            const value = turn.feedback || null;
            const saving = Boolean(turn.feedbackSaving);
            const thumb = (kind, icon, labelKey) => `<button type="button" class="v3-feedback-btn${value === kind ? ' is-active' : ''}" data-feedback="${turn.id}:${kind}" aria-pressed="${value === kind ? 'true' : 'false'}" aria-label="${h(labelKey)}" title="${h(labelKey)}"${saving ? ' disabled' : ''}>${icon}</button>`;
            return `<div class="v3-feedback" data-feedback-row="${turn.id}" role="group" aria-label="${h('conversation.feedback.groupLabel')}">
              ${thumb('thumbs_up', ICON.thumbUp, 'conversation.feedback.helpful')}
              ${thumb('thumbs_down', ICON.thumbDown, 'conversation.feedback.notHelpful')}
              <button type="button" class="v3-feedback-btn v3-feedback-btn--open${turn.feedbackSent ? ' is-sent' : ''}" data-feedback-open="${turn.id}" aria-label="${h('conversation.feedback.open')}" title="${h('conversation.feedback.open')}" aria-haspopup="dialog">${ICON.feedback}</button>
            </div>`;
        },

        // ------------------------------------------------------------------
        // Give feedback dialog (Jeen UI composition: Modal + rating + Chip +
        // textarea + Cancel / Send). One instance, mounted on <body>.
        // ------------------------------------------------------------------
        FEEDBACK_TYPES: ['general', 'report_bug', 'ui_bug', 'other'],

        openFeedbackDialog(turn) {
            if (!this._feedbackEligible(turn)) return;
            this.feedbackDialog = {
                turnId: turn.id,
                rating: 0,
                hover: 0,
                type: null,
                message: '',
                saving: false,
                error: null,
                returnFocus: document.activeElement instanceof HTMLElement ? document.activeElement : null,
            };
            this._renderFeedbackDialog();
            requestAnimationFrame(() => {
                document.querySelector('#v3-feedback-dialog [data-fb-star="1"]')?.focus();
            });
        },

        closeFeedbackDialog() {
            const state = this.feedbackDialog;
            if (!state) return;
            this.feedbackDialog = null;
            document.getElementById('v3-feedback-dialog')?.remove();
            document.body.classList.remove('v3-feedback-dialog-open');
            if (this._feedbackKeydown) {
                document.removeEventListener('keydown', this._feedbackKeydown, true);
                this._feedbackKeydown = null;
            }
            const back = state.returnFocus;
            if (back && document.contains(back)) { try { back.focus(); } catch (_) { /* gone */ } }
        },

        _feedbackDialogCanSend(state) {
            return Boolean(state) && !state.saving
                && (state.rating > 0 || String(state.message || '').trim().length > 0);
        },

        _renderFeedbackDialog() {
            const state = this.feedbackDialog;
            if (!state) return;
            let host = document.getElementById('v3-feedback-dialog');
            if (!host) {
                host = document.createElement('div');
                host.id = 'v3-feedback-dialog';
                host.className = 'v3-fb-overlay is-entering';
                document.body.appendChild(host);
                document.body.classList.add('v3-feedback-dialog-open');
                this._bindFeedbackDialogOnce(host);
                setTimeout(() => host.classList.remove('is-entering'), 250);
            }
            const shown = state.hover || state.rating;
            const stars = [1, 2, 3, 4, 5].map((n) => `<button type="button" class="v3-fb-star${n <= shown ? ' is-on' : ''}" data-fb-star="${n}" role="radio" aria-checked="${state.rating === n ? 'true' : 'false'}" aria-label="${h('conversation.feedback.starLabel', { n })}" tabindex="${(state.rating === n || (!state.rating && n === 1)) ? '0' : '-1'}">${ICON.starFill}</button>`).join('');
            const chips = this.FEEDBACK_TYPES.map((type) => `<button type="button" class="v3-fb-chip${state.type === type ? ' is-selected' : ''}" data-fb-type="${type}" aria-pressed="${state.type === type ? 'true' : 'false'}">${h(`conversation.feedback.type.${type}`)}</button>`).join('');
            const canSend = this._feedbackDialogCanSend(state);
            host.innerHTML = `<div class="v3-fb-modal" role="dialog" aria-modal="true" aria-labelledby="v3-fb-title" aria-describedby="v3-fb-subtitle" dir="${isRtl() ? 'rtl' : 'ltr'}">
              <div class="v3-fb-head">
                <span class="v3-fb-mark" aria-hidden="true">${ICON.chatHeart}</span>
                <h2 class="v3-fb-title" id="v3-fb-title">${h('conversation.feedback.title')}</h2>
                <p class="v3-fb-subtitle" id="v3-fb-subtitle">${h('conversation.feedback.subtitle')}</p>
              </div>
              <div class="v3-fb-rating">
                <div class="v3-fb-rating-label" id="v3-fb-rate-label">${h('conversation.feedback.rate')}</div>
                <div class="v3-fb-stars" role="radiogroup" aria-labelledby="v3-fb-rate-label" data-fb-stars>${stars}</div>
              </div>
              <div class="v3-fb-section">
                <div class="v3-fb-label" id="v3-fb-type-label">${h('conversation.feedback.typeLabel')}</div>
                <div class="v3-fb-chips" role="group" aria-labelledby="v3-fb-type-label">${chips}</div>
              </div>
              <div class="v3-fb-section">
                <label class="v3-fb-label" for="v3-fb-message">${h('conversation.feedback.yourFeedback')}</label>
                <textarea id="v3-fb-message" class="v3-fb-textarea" rows="1" maxlength="4000" placeholder="${h('conversation.feedback.placeholder')}" dir="auto" data-fb-message>${esc(state.message || '')}</textarea>
              </div>
              ${state.error ? `<div class="v3-fb-error" role="alert">${esc(state.error)}</div>` : ''}
              <div class="v3-fb-footer">
                <button type="button" class="v3-fb-cancel" data-fb-cancel>${h('common.cancel')}</button>
                <button type="button" class="v3-fb-send" data-fb-send${canSend ? '' : ' disabled'}>${state.saving ? h('conversation.feedback.sending') : h('conversation.feedback.send')}</button>
              </div>
            </div>`;
            this._autosizeFeedbackMessage(host.querySelector('[data-fb-message]'));
        },

        _autosizeFeedbackMessage(textarea) {
            if (!textarea) return;
            textarea.style.height = 'auto';
            textarea.style.height = `${Math.min(Math.max(textarea.scrollHeight, 44), 180)}px`;
        },

        /**
         * Event delegation on the overlay, bound once per mount. Re-renders of
         * the dialog replace innerHTML, so nothing here holds element refs;
         * typing only patches state and the Send button (never re-renders
         * the textarea mid-typing).
         */
        _bindFeedbackDialogOnce(host) {
            host.addEventListener('click', (event) => {
                const state = this.feedbackDialog;
                if (!state) return;
                const target = event.target instanceof Element ? event.target : null;
                if (!target) return;
                if (target === host) { this.closeFeedbackDialog(); return; }
                const star = target.closest('[data-fb-star]');
                if (star) {
                    const n = Number(star.dataset.fbStar);
                    state.rating = state.rating === n ? 0 : n;
                    state.hover = 0;
                    this._renderFeedbackDialog();
                    host.querySelector(`[data-fb-star="${state.rating || 1}"]`)?.focus();
                    return;
                }
                const chip = target.closest('[data-fb-type]');
                if (chip) {
                    const type = chip.dataset.fbType;
                    state.type = state.type === type ? null : type;
                    this._renderFeedbackDialog();
                    host.querySelector(`[data-fb-type="${type}"]`)?.focus();
                    return;
                }
                if (target.closest('[data-fb-cancel]')) { this.closeFeedbackDialog(); return; }
                if (target.closest('[data-fb-send]')) { this.submitFeedbackDialog(); }
            });
            host.addEventListener('mouseover', (event) => {
                const state = this.feedbackDialog;
                const star = event.target instanceof Element ? event.target.closest('[data-fb-star]') : null;
                if (!state || !star) return;
                const n = Number(star.dataset.fbStar);
                if (state.hover === n) return;
                state.hover = n;
                host.querySelectorAll('[data-fb-star]').forEach((el) => el.classList.toggle('is-on', Number(el.dataset.fbStar) <= n));
            });
            host.addEventListener('mouseout', (event) => {
                const state = this.feedbackDialog;
                const stars = event.target instanceof Element ? event.target.closest('[data-fb-stars]') : null;
                if (!state || !stars) return;
                const to = event.relatedTarget instanceof Element ? event.relatedTarget : null;
                if (to && stars.contains(to)) return;
                state.hover = 0;
                host.querySelectorAll('[data-fb-star]').forEach((el) => el.classList.toggle('is-on', Number(el.dataset.fbStar) <= state.rating));
            });
            host.addEventListener('input', (event) => {
                const state = this.feedbackDialog;
                const area = event.target instanceof Element ? event.target.closest('[data-fb-message]') : null;
                if (!state || !area) return;
                state.message = area.value;
                this._autosizeFeedbackMessage(area);
                const send = host.querySelector('[data-fb-send]');
                if (send) send.disabled = !this._feedbackDialogCanSend(state);
            });
            this._feedbackKeydown = (event) => {
                const state = this.feedbackDialog;
                if (!state) return;
                if (event.key === 'Escape') {
                    event.preventDefault();
                    event.stopPropagation();
                    this.closeFeedbackDialog();
                    return;
                }
                const star = event.target instanceof Element ? event.target.closest('[data-fb-star]') : null;
                if (star && ['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
                    event.preventDefault();
                    const forward = event.key === 'ArrowUp' || (isRtl() ? event.key === 'ArrowLeft' : event.key === 'ArrowRight');
                    const current = state.rating || Number(star.dataset.fbStar) || 1;
                    state.rating = Math.min(5, Math.max(1, current + (forward ? 1 : -1)));
                    state.hover = 0;
                    this._renderFeedbackDialog();
                    host.querySelector(`[data-fb-star="${state.rating}"]`)?.focus();
                    return;
                }
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                    event.preventDefault();
                    this.submitFeedbackDialog();
                    return;
                }
                if (event.key === 'Tab') {
                    // Keep focus inside the dialog.
                    const focusable = [...host.querySelectorAll('button:not([disabled]):not([tabindex="-1"]), textarea')];
                    if (!focusable.length) return;
                    const first = focusable[0];
                    const last = focusable[focusable.length - 1];
                    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
                    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
                }
            };
            document.addEventListener('keydown', this._feedbackKeydown, true);
        },

        async submitFeedbackDialog() {
            const state = this.feedbackDialog;
            if (!this._feedbackDialogCanSend(state)) return false;
            const turn = this.turns.find((item) => item.id === state.turnId);
            if (!turn) { this.closeFeedbackDialog(); return false; }
            state.saving = true;
            state.error = null;
            this._renderFeedbackDialog();
            try {
                await this._postAnswerFeedback(turn, {
                    rating: state.rating || null,
                    feedback_type: state.type,
                    message: state.message,
                });
                turn.feedbackSent = true;
                // The user may have dismissed this dialog and opened another
                // while the request was in flight; only settle our own.
                if (this.feedbackDialog === state) {
                    state.returnFocus = null;
                    this.closeFeedbackDialog();
                }
                if (typeof window.showToast === 'function') window.showToast(t('conversation.feedback.sent'), 'success');
                this.renderConversation();
                // renderConversation() rebuilt the card, so focus the new trigger.
                if (!this.feedbackDialog) {
                    document.querySelector(`#v3-thread [data-feedback-open="${turn.id}"]`)?.focus();
                }
                return true;
            } catch (error) {
                console.warn('[Workspace] feedback dialog failed', error);
                if (this.feedbackDialog !== state) return false;
                state.saving = false;
                state.error = t('conversation.feedback.failed');
                this._renderFeedbackDialog();
                return false;
            }
        },

        /** Resume a persisted proposal (confirm / clarification pick / guard exit). */
        async runProposal(turn, { patch = {}, remember = false, override = false } = {}) {
            const proposal = (turn.result || {}).proposal || {};
            if (!proposal.proposal_id) throw new Error(t('conversation.proposal.expiredTitle'));
            const connection = this._analysisConnection();
            if (!connection) throw new Error(t('errors.selectConnectionFirst'));
            const sessionId = turn.result?.session_id || (typeof window._jeenGetSessionId === 'function' ? window._jeenGetSessionId() : null);
            const prefs = window.JeenPreferences ? window.JeenPreferences.getAll() : {};
            const body = {
                connection,
                proposal_id: proposal.proposal_id,
                session_id: sessionId,
                params_patch: patch,
                override_guards: Boolean(override),
                remember: Boolean(remember),
                idempotency_key: `${proposal.proposal_id}:${Date.now()}`,
                eval_analytics: insightsEnabled(prefs),
            };
            const data = await this._postJson('/api/analysis/run', body);
            this._appendServerTurn(data, { question: turn.question, parent: turn });
        },

        /**
         * "Edit setup" on a finished result: opens the run's parameter card
         * (measure, date column, grain, window, model, …) above the chart.
         * The card is per result; switching results closes it.
         */
        _hideDefinition() {
            const host = document.getElementById('v3-ml-definition');
            if (host) { host.hidden = true; host.innerHTML = ''; }
        },

        _bindDefinitionToggle(metaRow, turn) {
            const host = document.getElementById('v3-ml-definition');
            if (!host) return;
            const button = metaRow && metaRow.querySelector('[data-ml-edit]');
            const open = Boolean(button) && this._definitionOpenFor === turn.id;
            host.hidden = !open;
            if (!button) {
                host.innerHTML = '';
                this._definitionOpenFor = null;
                return;
            }
            if (open) this._renderDefinition(host, turn, button);
            button.setAttribute('aria-expanded', String(open));
            button.textContent = open ? t('conversation.proposal.hideSetup') : t('conversation.proposal.editSetup');
            button.addEventListener('click', () => {
                const nowOpen = this._definitionOpenFor !== turn.id;
                this._definitionOpenFor = nowOpen ? turn.id : null;
                host.hidden = !nowOpen;
                if (nowOpen) {
                    this._renderDefinition(host, turn, button);
                    // The card opens above the chart; the pane may be scrolled past it.
                    if (typeof host.scrollIntoView === 'function') host.scrollIntoView({ block: 'start' });
                } else {
                    host.innerHTML = '';
                }
                button.setAttribute('aria-expanded', String(nowOpen));
                button.textContent = nowOpen ? t('conversation.proposal.hideSetup') : t('conversation.proposal.editSetup');
            });
        },

        _renderDefinition(host, turn, toggle) {
            const analysis = (turn.result || {}).analysis || {};
            host.innerHTML = window.JeenAnalysisUI ? window.JeenAnalysisUI.definitionHtml(analysis) : '';
            const card = host.querySelector('.v3-ml-card');
            if (!card) return;
            const runButton = card.querySelector('[data-run]');
            const hint = card.querySelector('[data-note]');
            // Re-run has nothing to do until a value differs from what ran: the
            // button waits, with a muted hint rather than an error.
            const form = window.JeenAnalysisUI && window.JeenAnalysisUI.bindSetupForm
                ? window.JeenAnalysisUI.bindSetupForm(card, (analysis.definition || {}).chips || [], {
                    skill: analysis.skill, tier: (analysis.egress || {}).tier,
                    onChange: (state) => {
                        if (runButton) runButton.disabled = !state.dirty;
                        if (hint) hint.hidden = Boolean(state.dirty);
                    },
                })
                : null;
            const note = (message) => {
                let el = card.querySelector('.v3-ml-error');
                if (!el) {
                    el = document.createElement('div');
                    el.className = 'v3-ml-error';
                    card.appendChild(el);
                }
                el.textContent = message;
            };
            const setBusy = (busy) => {
                card.classList.toggle('is-busy', busy);
                card.querySelectorAll('button, select, input').forEach((el) => { el.disabled = busy; });
                if (runButton) {
                    runButton.textContent = busy ? t('conversation.proposal.running') : t('conversation.proposal.rerun');
                    if (!busy && form) runButton.disabled = !form.state.dirty;
                }
            };
            card.querySelector('[data-cancel]')?.addEventListener('click', () => {
                this._definitionOpenFor = null;
                host.hidden = true;
                host.innerHTML = '';
                toggle.setAttribute('aria-expanded', 'false');
                toggle.textContent = t('conversation.proposal.editSetup');
            });
            runButton?.addEventListener('click', async () => {
                if (form) {
                    const { ok, errors } = form.validate();
                    if (!ok) { form.reportInvalid(errors); return; }
                }
                const patch = window.JeenAnalysisUI ? window.JeenAnalysisUI.collectPatch(card) : {};
                if (!Object.keys(patch).length) {
                    if (hint) hint.hidden = false;
                    return;
                }
                setBusy(true);
                try {
                    if (this.selectedResultId !== turn.id) this.selectTurn(turn.id);
                    await this.rerunAnalysis(null, patch);
                    this._definitionOpenFor = null;  // the new answer is selected; its own card is a click away
                } catch (error) {
                    if (error && error.name === 'AbortError') return;
                    setBusy(false);
                    const detail = error && typeof error === 'object' ? error.detail : null;
                    if (form && detail && form.showServerError(detail)) return;
                    note(error && error.message ? error.message : String(error));
                }
            });
        },

        /** Re-run the selected ML result with an instruction or a structured patch. */
        async rerunAnalysis(instruction, patch) {
            if (this._analysisRerunInFlight) {
                throw new Error(t('charts.chat.rerunAlreadyRunning'));
            }
            const turn = this.turns.find((item) => item.id === this.selectedResultId);
            const analysis = turn && turn.result && turn.result.analysis;
            if (!turn || !analysis || !analysis.skill) throw new Error(t('conversation.proposal.selectAnalysis'));
            const connection = this._analysisConnection();
            if (!connection) throw new Error(t('errors.selectConnectionFirst'));
            const sessionId = turn.result.session_id || (typeof window._jeenGetSessionId === 'function' ? window._jeenGetSessionId() : null);
            const prefs = window.JeenPreferences ? window.JeenPreferences.getAll() : {};
            const body = {
                connection,
                parent_query_id: turn.result.query_id,
                session_id: sessionId,
                eval_analytics: insightsEnabled(prefs),
                idempotency_key: `${turn.result.query_id}:${Date.now()}`,
            };
            if (patch && Object.keys(patch).length) body.params_patch = patch;
            else body.instruction = String(instruction || '').trim();
            const run = {
                parentId: turn.id,
                startedAt: Date.now(),
                selectionVersion: this._selectionVersion,
            };
            const generation = this._generation;
            const abort = new AbortController();
            this._analysisRerunInFlight = run;
            this._analysisRerunAbort = abort;
            window.JeenLegacyBridge?.setAnalysisRerunBusy?.(true);
            this._syncNewConversationAction();
            try {
                const data = await this._postJson('/api/analysis/rerun', body, {
                    signal: abort.signal,
                    timeoutMs: 185_000,
                });
                if (generation !== this._generation || abort.signal.aborted) return;
                const label = body.instruction ? `${turn.question} — ${body.instruction}` : turn.question;
                const select = (
                    this.selectedResultId === turn.id
                    && this._selectionVersion === run.selectionVersion
                );
                this._appendServerTurn(data, { question: label, parent: turn, select });
                if (!select && typeof window.showToast === 'function') {
                    window.showToast(t('charts.chat.rerunCompleted'), 'info');
                }
            } finally {
                if (this._analysisRerunAbort === abort) this._analysisRerunAbort = null;
                if (this._analysisRerunInFlight === run) {
                    this._analysisRerunInFlight = null;
                    window.JeenLegacyBridge?.setAnalysisRerunBusy?.(false);
                    this._syncNewConversationAction();
                }
            }
        },

        /** Append a completed turn returned by /api/analysis/run|rerun (never mutates the parent). */
        _appendServerTurn(data, { question, parent, select = true } = {}) {
            // Appending a new turn leaves any open inline edit.
            if (this.editingTurnId) { this.editingTurnId = null; this.editDraft = ''; this._editSel = null; }
            const turn = {
                id: `turn-${Date.now()}-${++this.seq}`,
                question: question || data.question || (parent && parent.question) || '',
                status: 'success',
                startedAt: performance.now(),
                askedAt: Date.now(),
                phaseState: Object.fromEntries(PHASES.map((phase) => [phase.id, 'done'])),
                trace: (data.trace || []).map((raw) => ({ ...raw, status: 'node_finished' })),
                traceOpen: false,
                result: data,
                turnId: data.query_id || null,
                conversationId: data.session_id || null,
                isFavorite: false,
                feedback: null,
                error: null,
                parentId: parent ? parent.id : null,
                resultKind: data.proposal ? 'proposal' : undefined,
                durationMs: Number((data.metrics || {}).execution_time_ms || 0) + Number((data.metrics || {}).llm_latency_ms || 0),
            };
            if (data.error && !data.results && !data.proposal) {
                turn.status = 'error';
                turn.error = data.error;
            }
            this.turns.push(turn);
            if (select) {
                this._captureSelectedChart();
                this.selectedTurnId = turn.id;
                this.selectedResultId = turn.id;
                this.filter = '';
            }
            if (data.session_id && typeof window._jeenSetSessionId === 'function') window._jeenSetSessionId(data.session_id);
            this.render();
            if (select) this._scrollThread();
        },

        /** Build (server-side, deterministically) and attach the band chart for an ML result. */
        async _loadAnalysisChart(turn) {
            const data = turn.result || {};
            const spec = data.analysis && data.analysis.chart_spec;
            const connection = this._analysisConnection();
            if (!spec || !data.query_id || !connection) {
                turn.chartUnavailable = true;
                return;
            }
            // Drop a late chart if the turn was re-run (edited) meanwhile.
            const rev = turn.rev || 0;
            const body = { connection, query_id: String(data.query_id), chart_spec: spec };
            try {
                let payload;
                try {
                    payload = await this._postJson('/api/analysis/chart', body);
                } catch (error) {
                    if (!/cached|re-send/i.test(String(error && error.message))) throw error;
                    payload = await this._postJson('/api/analysis/chart', { ...body, results: data.results });
                }
                if ((turn.rev || 0) !== rev) return;
                if (payload && payload.chart_config) {
                    turn.chartState = { chart_spec: payload.chart_spec || spec, chart_config: payload.chart_config };
                    turn.hasChart = true;
                    turn.chartUnavailable = false;
                } else {
                    turn.chartUnavailable = true;
                }
            } catch (error) {
                if ((turn.rev || 0) !== rev) return;
                turn.chartUnavailable = true;
                console.warn('[Workspace] analysis chart failed', error);
            }
        },

        _mlSuggestionsCache: { connection: null, items: null, loading: false },

        /** One ML quick-start chip set per connection, fetched once. */
        _ensureMlSuggestions(connection) {
            const cache = this._mlSuggestionsCache;
            if (!connection || cache.connection === connection) return cache.items || [];
            if (cache.loading) return [];
            cache.loading = true;
            fetch(`/api/analysis/suggestions?connection=${encodeURIComponent(connection)}`)
                .then((response) => (response.ok ? response.json() : { suggestions: [] }))
                .then((payload) => {
                    cache.connection = connection;
                    cache.items = Array.isArray(payload.suggestions) ? payload.suggestions : [];
                })
                .catch(() => { cache.connection = connection; cache.items = []; })
                .finally(() => {
                    cache.loading = false;
                    if (!this.turns.length) this.renderConversation();
                });
            return [];
        },

        /** Result pane for a text-only answer (live or restored): no chart, no grid. */
        _renderTextResult(turn) {
            const placeholder = document.getElementById('v3-placeholder');
            if (this._placeholderDefault === null) this._placeholderDefault = placeholder.innerHTML;
            const data = turn.result || {};
            const answer = textOf(data.answer);
            const clarify = data.route_clarification;
            const filterClarify = data.filter_clarification;
            this._setResultTitle(turn.question);
            if (filterClarify && Array.isArray(filterClarify.options)) {
                // The grounder could not tell which field (or which value) a word
                // of the question meant: offer the candidates instead of guessing.
                document.getElementById('v3-meta-row').innerHTML = `
                  <span class="v3-status">${h('conversation.text.needsChoice')}</span>
                  <span class="v3-result-meta">${filterClarify.kind === 'column' ? h('conversation.text.whichField') : h('conversation.text.whichValue')}</span>`;
                placeholder.innerHTML = this._filterClarifyHtml(filterClarify, answer);
                this._bindFilterClarify(placeholder, turn, filterClarify);
            } else if (clarify) {
                // Ambiguous SQL-vs-ML: offer the two choices instead of guessing.
                const pct = clarify.confidence != null ? ` · ${h('conversation.text.percentSure', { percent: Math.round(clarify.confidence * 100) })}` : '';
                document.getElementById('v3-meta-row').innerHTML = `
                  <span class="v3-status">${h('conversation.text.needsChoice')}</span>
                  <span class="v3-result-meta">${h('conversation.text.directOrAnalysis')}${pct}</span>`;
                placeholder.innerHTML = `<div class="v3-route-clarify">
                  <p class="v3-ml-message" dir="${directionOf(answer)}">${esc(answer)}</p>
                  <div class="v3-ml-actions">
                    <button type="button" class="v3-ml-run" data-route-analysis>${h('conversation.text.runAnalysis')}</button>
                    <button type="button" class="v3-ml-alt" data-route-sql>${h('conversation.text.answerWithSql')}</button>
                  </div>
                </div>`;
                placeholder.querySelector('[data-route-analysis]')?.addEventListener('click', () => this.send(turn.question, { analysis: true }));
                placeholder.querySelector('[data-route-sql]')?.addEventListener('click', () => this.send(turn.question, { analysis: false }));
            } else {
                document.getElementById('v3-meta-row').innerHTML = `
                  <span class="v3-status">${h('conversation.text.answered')}</span>
                  <span class="v3-result-meta">${h('conversation.text.textAnswer')}${turn.restored ? ` · ${h('conversation.restored.restored')}` : ''}</span>`;
                const answerBody = wantsMarkdown(data)
                    ? markdownDiv(answer)
                    : `<div class="v3-text-answer" dir="${directionOf(answer)}">${esc(answer || t('conversation.text.noDataNeeded'))}</div>`;
                placeholder.innerHTML = `<strong>${h('conversation.text.answer')}</strong>${answerBody}`;
            }
            placeholder.hidden = false;
            this._hideDefinition();
            document.getElementById('v3-chart-block').hidden = true;
            document.getElementById('v3-table-block').hidden = true;
            const chart = document.getElementById('chart-view-container');
            if (chart) chart.style.display = 'none';
            if (this.lastAppliedResultId !== turn.id && window.JeenLegacyBridge) {
                // Keep the legacy panels (prompt/dev details, session id) in sync,
                // with the answer flattened to text for the legacy renderer.
                this.lastAppliedResultId = turn.id;
                const payload = { ...data, answer, results: null };
                if (turn.restored && typeof window.JeenLegacyBridge.applyRestoredResult === 'function') {
                    window.JeenLegacyBridge.applyRestoredResult(payload, null);
                } else {
                    window.JeenLegacyBridge.applyResult(payload);
                }
            }
            document.getElementById('v3-dock-meta').textContent = t('conversation.pending.noQueryText');
            this._setActionsEnabled(false);
            this.renderDock();
        },

        /** The result heading shows the user's question, so it takes the question's direction. */
        _setResultTitle(text) {
            const title = document.getElementById('v3-result-title');
            if (!title) return;
            title.textContent = text;
            title.setAttribute('dir', directionOf(text));
        },

        _renderUnavailableSavedAnswer(state) {
            const item = state && state.item;
            if (!item) return;
            const placeholder = document.getElementById('v3-placeholder');
            const chartBlock = document.getElementById('v3-chart-block');
            const tableBlock = document.getElementById('v3-table-block');
            this._placeChartInteraction(false);
            this._hideDefinition();
            this._setResultTitle(item.question || item.conversation_title || t('favorite.answerUnavailable'));
            document.getElementById('v3-meta-row').innerHTML = `
              <span class="v3-status is-empty">${h('favorite.answerUnavailable')}</span>`;
            placeholder.innerHTML = `<div class="v3-saved-unavailable" role="status">
              <strong>${h('favorite.answerUnavailable')}</strong>
              <span>${h('favorite.unavailableCopy')}</span>
              <div class="v3-saved-unavailable-actions">
                <button type="button" class="v3-text-btn" data-saved-retry>${h('favorite.retry')}</button>
                <button type="button" class="v3-text-btn" data-saved-remove>${h('favorite.remove')}</button>
              </div>
            </div>`;
            placeholder.hidden = false;
            chartBlock.hidden = true;
            tableBlock.hidden = true;
            document.getElementById('v3-dock-meta').textContent = t('shell.dock.noRunYet');
            this._setActionsEnabled(false);
            this.renderDock();
            placeholder.querySelector('[data-saved-retry]')?.addEventListener('click', () => this.openFavorite(item));
            placeholder.querySelector('[data-saved-remove]')?.addEventListener('click', async () => {
                const removed = await this.setFavoriteState(item.conversation_id, item.turn_id, false);
                if (!removed) return;
                this._unavailableSavedAnswer = null;
                this.setTab('saved');
                this.setSavedView('answers');
                this.render();
            });
        },

        renderWorkspace() {
            const turn = this.turns.find((item) => item.id === this.selectedResultId && turnShowsResult(item));
            this._renderFavoriteAction(turn);
            const placeholder = document.getElementById('v3-placeholder');
            const chartBlock = document.getElementById('v3-chart-block');
            const tableBlock = document.getElementById('v3-table-block');
            const chartStatus = document.getElementById('v3-chart-status');
            if (chartStatus) chartStatus.hidden = true;
            if (this._unavailableSavedAnswer) {
                this._renderUnavailableSavedAnswer(this._unavailableSavedAnswer);
                return;
            }
            if (!turn) {
                this._placeChartInteraction(false);
                this._restorePlaceholder();
                this._hideDefinition();
                const emptyTitle = document.getElementById('v3-result-title');
                emptyTitle.textContent = this.hydrating ? t('shell.result.restoring') : t('shell.result.getStarted');
                emptyTitle.removeAttribute('dir');
                document.getElementById('v3-meta-row').innerHTML = `<span class="v3-status is-empty">${h('shell.result.noResultYet')}</span>`;
                placeholder.hidden = false;
                chartBlock.hidden = true;
                tableBlock.hidden = true;
                document.getElementById('v3-dock-meta').textContent = t('shell.dock.noRunYet');
                this._setActionsEnabled(false);
                this.renderDock();
                return;
            }

            const data = turn.result;
            // ML skills: the run stopped to ask (confirm card, clarification,
            // guard refusal). The card owns the answer pane.
            if (data.proposal) {
                this._placeChartInteraction(false);
                this._renderProposal(turn);
                return;
            }
            // A restored table turn whose rows are not here yet (loading, pruned,
            // too large, failed): show the state instead of an empty table.
            if (turn.restored && turn.resultKind === 'table' && !data.results) {
                this._placeChartInteraction(false);
                this._renderPendingResult(turn);
                return;
            }
            // Text-only answers (greeting, memory answer, clarification) have no
            // dataset: never expose the chart/table blocks, which would otherwise
            // keep showing the previous turn's chart.
            const isText = turn.resultKind === 'text' || (!(data.results && data.results.columns) && !data.sql);
            if (isText) {
                this._placeChartInteraction(false);
                this._setModelTabVisible(false);
                this._renderTextResult(turn);
                return;
            }
            this._restorePlaceholder();
            const results = data.results || {};
            const rows = normalizeRows(results);
            const metrics = data.metrics || {};
            const cap = cappedMeta(results);
            const newest = this.turns[this.turns.length - 1];
            const stale = newest && newest.status === 'error' && newest.id !== turn.id;
            this._setResultTitle(turn.question);
            const restoredNote = turn.restored && turn.snapshotAt
                ? `<span class="v3-result-meta">${h('results.status.snapshotFrom', { when: this._formatWhen(turn.snapshotAt) })}</span>`
                : '';
            const isAnalysis = Boolean(data.analysis && data.analysis.skill);
            this._placeChartInteraction(isAnalysis);
            window.JeenLegacyBridge?.setChartAnalysisMode?.(isAnalysis);
            window.JeenLegacyBridge?.setAnalysisRerunBusy?.(Boolean(this._analysisRerunInFlight));
            const chartNeedsApply = this.lastAppliedResultId !== turn.id;
            if (chartNeedsApply) {
                this.chartOptionsOpen = false;
                this._renderChartOptionsOverflow();
            }
            if (chartNeedsApply && (!isAnalysis || rows.length > 0)) {
                window.JeenLegacyBridge?.setChartInteractionEnabled?.(false);
            } else if (isAnalysis && rows.length === 0) {
                window.JeenLegacyBridge?.setChartInteractionEnabled?.(true);
            }
            const mlStrip = isAnalysis && window.JeenAnalysisUI ? window.JeenAnalysisUI.stripSegments(data) : '';
            const metaRow = document.getElementById('v3-meta-row');
            const streaming = turn.status === 'streaming';
            const failedAfterRows = turn.status === 'error';
            const statusHtml = streaming
                ? `<span class="v3-status is-streaming">${h('results.status.analysing')}</span>`
                : failedAfterRows
                    ? `<span class="v3-status is-error">${h('results.status.failedAfterRows')}</span>`
                    : `<span class="v3-status">${cap.capped ? h('results.status.completedCapped') : h('results.status.completed')}</span>`;
            const metaHtml = streaming
                ? `<span class="v3-result-meta">${h('results.status.rowsReady', { rows: rows.length })}</span>`
                : `<span class="v3-result-meta">${h('results.status.meta', { rows: rows.length, exec: formatMs(metrics.execution_time_ms), llm: formatMs(metrics.llm_latency_ms) })}</span>`;
            metaRow.innerHTML = `
              ${statusHtml}
              ${mlStrip}
              ${metaHtml}
              ${restoredNote}
              ${stale ? `<span class="v3-stale-note">${h('results.status.staleNote')}</span>` : ''}`;
            this._bindDefinitionToggle(metaRow, turn);
            placeholder.hidden = true;
            // An empty result set has nothing to chart. ML charts stay hidden
            // until their deterministic server-built baseline is available;
            // never expose a stale prior chart or fall back to generic LLM charting.
            chartBlock.hidden = rows.length === 0 || (isAnalysis && !turn.chartState);
            tableBlock.hidden = false;
            this._setModelTabVisible(isAnalysis);

            // ML results never ask the LLM for a chart: the envelope's role-based
            // spec is built server-side once, stored as the turn's chart
            // baseline, and rendered through the restore path.
            if (isAnalysis && !turn.chartState && !turn.chartLoading && !turn.chartUnavailable && rows.length) {
                turn.chartLoading = true;
                const chartRev = turn.rev || 0;
                this._loadAnalysisChart(turn).finally(() => {
                    // A re-run (edit) since this load started owns its own flags now.
                    if ((turn.rev || 0) !== chartRev) return;
                    turn.chartLoading = false;
                    if (this.selectedResultId === turn.id) {
                        this.lastAppliedResultId = null;
                        this.renderWorkspace();
                    }
                });
            }
            if (isAnalysis && turn.chartUnavailable) {
                window.JeenLegacyBridge?.setChartInteractionEnabled?.(true);
                if (chartStatus) {
                    chartStatus.hidden = false;
                    const retry = chartStatus.querySelector('[data-chart-retry]');
                    if (retry) retry.onclick = () => {
                        turn.chartUnavailable = false;
                        turn.chartLoading = false;
                        this.renderWorkspace();
                    };
                }
            }

            if (chartNeedsApply && window.JeenLegacyBridge
                && !(isAnalysis && turn.chartLoading)
                && (!isAnalysis || Boolean(turn.chartState))) {
                this.lastAppliedResultId = turn.id;
                const showChart = () => {
                    const chart = document.getElementById('chart-view-container');
                    if (chart) chart.style.display = rows.length ? 'block' : 'none';
                    window.dispatchEvent(new Event('resize'));
                    if (typeof window.JeenLegacyBridge.setChartAnalysisMode === 'function') {
                        window.JeenLegacyBridge.setChartAnalysisMode(isAnalysis);
                    }
                    window.JeenLegacyBridge.setAnalysisRerunBusy?.(Boolean(this._analysisRerunInFlight));
                    window.JeenLegacyBridge.setChartInteractionEnabled?.(true);
                    this._placeChartInteraction(isAnalysis);
                };
                if ((turn.restored || turn.chartState) && typeof window.JeenLegacyBridge.applyRestoredResult === 'function') {
                    // Known rows (+ optional stored chart): render through the
                    // restore path and wait for the chart machinery instead of
                    // guessing with a timer. No LLM chart call when a baseline exists.
                    const applied = turn.id;
                    const appliedRev = turn.rev || 0;
                    window.JeenLegacyBridge.applyRestoredResult(data, turn.chartState)
                        .then(() => {
                            if (this.selectedResultId === applied && (turn.rev || 0) === appliedRev) showChart();
                        })
                        .catch((error) => {
                            console.warn('[Workspace] chart apply failed', error);
                            if (this.selectedResultId === applied && (turn.rev || 0) === appliedRev) {
                                this.lastAppliedResultId = null;
                                window.JeenLegacyBridge?.setChartInteractionEnabled?.(false);
                            }
                        });
                } else {
                    const applied = turn.id;
                    const appliedRev = turn.rev || 0;
                    window.JeenLegacyBridge.applyResult(data);
                    requestAnimationFrame(() => {
                        if (this.selectedResultId === applied && (turn.rev || 0) === appliedRev) showChart();
                    });
                }
            }
            // Export / share / save act on the finished turn (the artifact row
            // is written when the graph completes), so they stay off while the
            // narrative is still streaming.
            this._setActionsEnabled(!streaming);
            const captionColumns = isAnalysis && window.JeenAnalysisUI
                ? window.JeenAnalysisUI.chartCaption(data)
                : (results.columns && results.columns.length > 1
                    ? t('results.chart.captionBy', { first: iso(results.columns[0]), second: iso(results.columns[1]) })
                    : (results.columns && results.columns[0]) || t('results.chart.result'));
            document.getElementById('v3-chart-caption').textContent = t('results.chart.caption', { columns: iso(captionColumns), count: rows.length });
            const banner = document.getElementById('v3-cap-banner');
            banner.hidden = !cap.capped;
            if (cap.capped) {
                const totalCopy = cap.total ? t('results.cap.matched', { total: formatCompact(cap.total) }) : t('results.cap.totalUnavailable');
                banner.textContent = `${t('results.cap.banner', { cap: formatCompact(cap.cap) })} ${totalCopy} ${t('results.cap.exportsNote')}`;
            }
            const inputTokens = metrics.input_tokens == null ? '—' : formatCompact(metrics.input_tokens);
            const outputTokens = metrics.output_tokens == null ? '—' : formatCompact(metrics.output_tokens);
            const validated = turn.trace.some((event) => ['sqlglot_validate', 'dax_static_validate'].includes(event.node) && event.status === 'node_finished');
            document.getElementById('v3-dock-meta').textContent = t('results.dockMeta', {
                input: inputTokens,
                output: outputTokens,
                retries: metrics.retry_count == null ? '—' : metrics.retry_count,
                validation: validated ? t('results.validation.validated') : data.sql ? t('results.validation.generated') : t('results.validation.none'),
            });
            // The plot is the point of an ML/analysis result, so expand it by
            // default; plain SQL stays collapsed. A manual toggle on this result
            // (stored above) always wins.
            this.chartCollapsed = typeof turn.chartCollapsed === 'boolean'
                ? turn.chartCollapsed
                : !isAnalysis;
            this._renderChartCollapse();
            // The final `result` after a `partial` carries the same rows the grid
            // already shows: leave the DOM (and its scroll position) alone for
            // every re-render of that same table. Any change to the key (turn,
            // revision, filter, sort) or a direct renderTable() call repaints.
            const tableKey = this._tableKey(turn);
            if (this._tableRepaintHold === tableKey && this._paintedTableKey === tableKey) {
                // held
            } else {
                this._tableRepaintHold = null;
                this.renderTable();
            }
            this.renderDock();
        },

        /** Identity of what the grid currently paints: turn, revision, filter and sort. */
        _tableKey(turn) {
            const presentation = window.JeenLegacyBridge?.getTablePresentation?.() || {};
            return [
                turn.id,
                turn.rev || 0,
                this.filter || '',
                presentation.sortColumn == null ? '' : presentation.sortColumn,
                presentation.sortDirection || '',
            ].join('|');
        },

        renderTable() {
            const turn = this.turns.find((item) => item.id === this.selectedResultId);
            if (!turn || !turn.result?.results) return;
            this._paintedTableKey = this._tableKey(turn);
            const results = turn.result.results;
            const columns = results.columns || [];
            const allRows = normalizeRows(results);
            const mapActive = document.documentElement.dataset.jeenOsmMapActive === 'true';
            let filtered = filterResultRows(results, this.filter);
            const presentation = window.JeenLegacyBridge?.getTablePresentation?.() || {
                formats: {}, derived: [], sortColumn: null, sortDirection: 'asc',
            };
            if (presentation.sortColumn !== null && presentation.sortColumn !== undefined) {
                const index = presentation.sortColumn;
                const column = columns[index];
                const direction = presentation.sortDirection === 'desc' ? -1 : 1;
                filtered = filtered.slice().sort((left, right) => {
                    const a = rowValue(left, column, index);
                    const b = rowValue(right, column, index);
                    if (a == null) return 1;
                    if (b == null) return -1;
                    const an = numericValue(a);
                    const bn = numericValue(b);
                    if (Number.isFinite(an) && Number.isFinite(bn)) return (an - bn) * direction;
                    return String(a).localeCompare(String(b), undefined, { sensitivity: 'base' }) * direction;
                });
            }
            window.JeenLegacyBridge?.setVisibleRows?.(filtered);
            const cap = cappedMeta(results);
            document.getElementById('v3-row-caption').textContent = this.filter
                ? t('results.grid.filteredOf', { shown: filtered.length, total: allRows.length })
                : cap.total ? t('results.grid.loadedMatched', { loaded: allRows.length, total: formatCompact(cap.total) }) : t('results.grid.rowsLoaded', { count: allRows.length });
            const grid = document.getElementById('v3-grid');
            const wrap = document.getElementById('v3-grid-wrap');
            const columnMetadata = resultColumnMetadata(results);
            const descriptors = [];
            columns.forEach((name, index) => {
                const meta = columnMetadata[index] || {
                    name, sourceIndex: index, numeric: false, plain: false, type: 'text', omitMidnightTime: false,
                };
                descriptors.push({
                    ...meta,
                    plain: meta.plain && !presentation.formats?.[index],
                });
                const derived = (presentation.derived || []).find((item) => item.sourceIndex === index);
                if (derived) descriptors.push({ name: derived.name, sourceIndex: index, numeric: true, derived });
            });
            const derivedValues = new Map();
            (presentation.derived || []).forEach((derived) => {
                const source = columns[derived.sourceIndex];
                const values = filtered.map((row) => numericValue(rowValue(row, source, derived.sourceIndex)));
                const sum = values.reduce((total, value) => total + (Number.isFinite(value) ? value : 0), 0);
                let running = 0;
                derivedValues.set(derived.sourceIndex, values.map((value, rowIndex) => {
                    if (!Number.isFinite(value)) return null;
                    if (derived.type === 'pct_total') return sum ? (value / sum) * 100 : 0;
                    if (derived.type === 'running_total') return (running += value);
                    if (derived.type === 'delta') return rowIndex ? value - values[rowIndex - 1] : null;
                    return value;
                }));
            });
            const columnTemplate = descriptors.length
                ? `minmax(180px, 1.4fr)${descriptors.slice(1).map(() => ' minmax(120px, 1fr)').join('')}`
                : 'minmax(180px, 1fr)';
            grid.style.setProperty('--v3-columns', columnTemplate);
            const head = `<div class="v3-grid-row v3-grid-head">${descriptors.map((descriptor) => {
                const sort = !descriptor.derived && presentation.sortColumn === descriptor.sourceIndex
                    ? (presentation.sortDirection === 'desc' ? ' ↓' : ' ↑') : '';
                return `<button class="v3-grid-cell${descriptor.numeric ? ' is-numeric' : ''}${descriptor.derived ? ' is-derived' : ''}" ${descriptor.derived ? '' : `data-col="${descriptor.sourceIndex}"`} title="${esc(descriptor.name)}">${esc(descriptor.name)}${sort}</button>`;
            }).join('')}</div>`;
            const rowHtml = (row, visibleIndex) => {
                const sourceIndex = allRows.indexOf(row);
                const selected = mapActive && this.mapSelectedRows.has(sourceIndex);
                return `<div class="v3-grid-row${selected ? ' is-map-selected' : ''}" data-row="${visibleIndex}" data-source-row="${sourceIndex}"${mapActive ? ` title="${h('results.grid.focusMap')}"` : ''}>${descriptors.map((descriptor) => {
                const raw = descriptor.derived
                    ? derivedValues.get(descriptor.sourceIndex)?.[visibleIndex]
                    : rowValue(row, columns[descriptor.sourceIndex], descriptor.sourceIndex);
                const rendered = descriptor.derived
                    ? (descriptor.derived.type === 'pct_total' && raw != null ? `${formatCompact(raw)}%` : formatCompact(raw))
                    : (descriptor.type === 'datetime' && raw != null && raw !== '')
                        ? formatResultDateTime(raw, descriptor.omitMidnightTime)
                    : (descriptor.plain && raw != null && raw !== '')
                        ? String(raw)
                        : (window.JeenLegacyBridge?.formatTableValue?.(raw, descriptor.sourceIndex, descriptor.numeric) ?? (raw ?? '—'));
                    return `<div class="v3-grid-cell${descriptor.numeric ? ' is-numeric' : ''}${descriptor.derived ? ' is-derived' : ''}"${descriptor.numeric ? ' dir="ltr"' : ''} title="${esc(rendered)}">${esc(rendered)}</div>`;
                }).join('')}</div>`;
            };
            const bindGridActions = () => {
                grid.querySelectorAll('[data-col]').forEach((header) => {
                    const index = Number(header.dataset.col);
                    header.addEventListener('click', () => {
                        const next = presentation.sortColumn === index && presentation.sortDirection === 'asc' ? 'desc' : 'asc';
                        window.sortTableDir?.(index, next);
                    });
                    header.addEventListener('contextmenu', (event) => window.showColMenu?.(event, index));
                });
                grid.querySelectorAll('[data-row]').forEach((row) => {
                    row.addEventListener('contextmenu', (event) => window.showRowMenu?.(event, Number(row.dataset.row)));
                    row.addEventListener('click', (event) => {
                        if (event.defaultPrevented || !mapActive) return;
                        const sourceRow = Number(row.dataset.sourceRow);
                        if (Number.isInteger(sourceRow) && sourceRow >= 0) {
                            this.mapSelectedRows = new Set([sourceRow]);
                            this.renderTable();
                            document.dispatchEvent(new CustomEvent('jeen:osm-table-focus', {
                                detail: { rowIndexes: [sourceRow] },
                            }));
                        }
                    });
                });
            };
            wrap.onscroll = null;
            if (filtered.length <= 500) {
                grid.innerHTML = head + filtered.map((row, index) => rowHtml(row, index)).join('');
                bindGridActions();
                return;
            }

            const rowHeight = 45;
            const overscan = 10;
            const renderWindow = () => {
                const visible = Math.ceil(wrap.clientHeight / rowHeight);
                const start = Math.max(0, Math.floor(Math.max(0, wrap.scrollTop - 43) / rowHeight) - overscan);
                const end = Math.min(filtered.length, start + visible + overscan * 2);
                grid.innerHTML = head
                    + `<div class="v3-virtual-spacer" style="height:${start * rowHeight}px"></div>`
                    + filtered.slice(start, end).map((row, index) => rowHtml(row, start + index)).join('')
                    + `<div class="v3-virtual-spacer" style="height:${(filtered.length - end) * rowHeight}px"></div>`;
                bindGridActions();
            };
            wrap.onscroll = renderWindow;
            renderWindow();
        },

        _renderChartOptionsOverflow() {
            const secondary = document.getElementById('v3-chart-secondary');
            const toggle = document.getElementById('v3-chart-more');
            if (secondary) secondary.classList.toggle('is-open', this.chartOptionsOpen);
            if (toggle) toggle.setAttribute('aria-expanded', String(this.chartOptionsOpen));
        },

        _renderChartCollapse() {
            const controls = document.getElementById('v3-chart-types');
            const frame = document.getElementById('v3-chart-frame');
            const edit = document.getElementById('v3-chart-edit');
            const toggle = document.getElementById('v3-chart-toggle');
            if (controls) controls.hidden = this.chartCollapsed;
            if (this.chartCollapsed) {
                this.chartOptionsOpen = false;
                this._renderChartOptionsOverflow();
            }
            if (frame) frame.hidden = this.chartCollapsed;
            if (edit) edit.hidden = this.chartCollapsed;
            if (toggle) {
                toggle.textContent = this.chartCollapsed ? t('common.expand') : t('common.collapse');
                toggle.setAttribute('aria-expanded', String(!this.chartCollapsed));
                toggle.setAttribute(
                    'aria-label',
                    t(this.chartCollapsed ? 'results.chart.expandLabel' : 'results.chart.collapseLabel')
                );
            }
            window.JeenLegacyBridge?.setChartCollapsed?.(this.chartCollapsed);
        },

        toggleDock(tab) {
            if (this.dockOpen && this.dockTab === tab) this.dockOpen = false;
            else {
                this.dockTab = tab;
                this.dockOpen = true;
            }
            this.renderDock();
            setTimeout(() => window.dispatchEvent(new Event('resize')), 0);
        },

        renderDock() {
            // Onboarding signal: fire once on the rising edge of opening the SQL
            // dock for an answered turn (covers both the button and keyboard paths).
            const sqlOpen = this.dockOpen && this.dockTab === 'sql';
            if (sqlOpen && !this._sqlDockWasOpen
                && this.turns.some((item) => item.id === this.selectedResultId && item.status === 'success')) {
                document.dispatchEvent(new CustomEvent('jeen:onboarding:open_sql'));
            }
            this._sqlDockWasOpen = sqlOpen;
            document.querySelectorAll('[data-dock]').forEach((button) => {
                button.classList.toggle('is-active', this.dockOpen && button.dataset.dock === this.dockTab);
                button.setAttribute('aria-selected', String(this.dockOpen && button.dataset.dock === this.dockTab));
                button.tabIndex = button.dataset.dock === this.dockTab ? 0 : -1;
            });
            const body = document.getElementById('v3-dock-body');
            const toggle = document.getElementById('v3-dock-toggle');
            body.hidden = !this.dockOpen;
            toggle.textContent = this.dockOpen ? t('common.hide') : t('common.show');
            toggle.setAttribute('aria-expanded', String(this.dockOpen));
            if (!this.dockOpen) return;
            const turn = this.turns.find((item) => item.id === this.selectedResultId);
            if (!turn) {
                body.innerHTML = `<div class="v3-dock-empty">${h('shell.dock.empty')}</div>`;
                return;
            }
            body.innerHTML = this.dockTab === 'profiling'
                ? this._profileHtml(turn)
                : this.dockTab === 'model' && window.JeenAnalysisUI
                    ? window.JeenAnalysisUI.modelDetailsHtml(turn.result || {}, { tracking: turn.tracking })
                    : this._sqlHtml(turn);
            body.querySelector('[data-copy-sql]')?.addEventListener('click', () => navigator.clipboard.writeText(turn.result.sql || ''));
            body.querySelector('[data-ml-accuracy]')?.addEventListener('click', () => this.checkForecastAccuracy(turn));
            // "Adjustments to try": one click re-runs the analysis with the
            // server-validated patch as a child turn (same path as Edit setup).
            body.querySelectorAll('[data-ml-adjust]').forEach((chip) => chip.addEventListener('click', async () => {
                const items = (turn.result?.analysis || {}).adjustments || [];
                const item = items[Number(chip.dataset.mlAdjust)];
                if (!item || !item.params_patch) return;
                body.querySelectorAll('[data-ml-adjust]').forEach((other) => { other.disabled = true; });
                try {
                    await this.rerunAnalysis(null, item.params_patch);
                } catch (error) {
                    if (typeof window.showToast === 'function') window.showToast(error.message || t('charts.chat.rerunFailed'), 'error');
                } finally {
                    body.querySelectorAll('[data-ml-adjust]').forEach((other) => { other.disabled = false; });
                }
            }));
            body.querySelector('[data-full-profile]')?.addEventListener('click', () => this._openFullProfile());
            body.querySelector('[data-dev-details]')?.addEventListener('click', () => document.getElementById('dev-panel-btn')?.click());
            body.querySelectorAll('[data-filter-switch]').forEach((button) => button.addEventListener('click', () => {
                const [i, j] = String(button.dataset.filterSwitch).split(':').map(Number);
                const filter = ((turn.result || {}).filters || {}).resolved?.[i];
                const alt = filter && (filter.alternatives || [])[j];
                if (!filter || !alt) return;
                const literal = filter.raw_value != null ? String(filter.raw_value) : (Array.isArray(filter.value) ? filter.value[0] : String(filter.value));
                this.send(turn.question, { filterChoice: { literal, table: alt.table, column: alt.column } });
            }));
        },

        _sqlHtml(turn) {
            const data = turn.result;
            const metrics = data.metrics || {};
            const rows = normalizeRows(data.results).length;
            const nodes = new Set(turn.trace.filter((event) => event.status === 'node_finished').map((event) => event.node));
            const validation = nodes.has('sqlglot_validate')
                ? t('results.sql.sqlglotValidated')
                : nodes.has('dax_static_validate') ? t('results.sql.daxValidated') : t('results.sql.validationUnavailable');
            const stats = [
                [t('results.sql.rows'), rows],
                [t('results.sql.exec'), formatMs(metrics.execution_time_ms)],
                [t('results.sql.llm'), formatMs(metrics.llm_latency_ms)],
                [t('results.sql.tokens'), formatCompact(metrics.total_tokens)],
                [t('results.sql.retries'), metrics.retry_count == null ? '—' : metrics.retry_count],
            ];
            return `<div class="v3-stats">${stats.map(([label, value]) => `<div class="v3-stat"><div class="v3-stat-label">${escFull(label)}</div><div class="v3-stat-value">${esc(value)}</div></div>`).join('')}</div>
              ${this._filtersHtml(data)}
              <div class="v3-sql-card">
                <div class="v3-sql-provenance">${h('results.sql.provenance', { validation })} <button data-dev-details>${h('results.sql.developerDetails')}</button><button data-copy-sql>${h('common.copy')}</button></div>
                <pre dir="ltr">${esc(data.sql || t('results.sql.noSql'))}</pre>
              </div>`;
        },

        _profileHtml(turn) {
            const profiles = compactProfile(turn.result.results);
            if (!profiles.length) return `<div class="v3-dock-empty">${h('results.profile.empty')}</div>`;
            return `<div class="v3-profile-list">${profiles.map((profile) => `<div class="v3-profile-row">
              <div class="v3-profile-name"><strong title="${esc(profile.name)}"><bdi>${esc(profile.name)}</bdi></strong><span>${h(`results.profile.type.${profile.type}`)}</span></div>
              <div class="v3-profile-range"><div class="v3-profile-track"><div class="v3-profile-fill" style="width:${profile.fillPct}%"></div></div><span dir="auto">${esc(profile.range)}</span></div>
              <div class="v3-profile-figure">${h('results.profile.distinct', { count: profile.distinct })}</div><div class="v3-profile-figure">${h('results.profile.nulls', { percent: profile.nullPct })}</div>
            </div>`).join('')}</div><div class="v3-profile-foot">${h('results.profile.footQuestion')} <button class="v3-text-btn" data-full-profile>${h('results.profile.openFull')}</button></div>`;
        },

        _openFullProfile() {
            const section = document.getElementById('profiling-section');
            if (!section) return;
            document.getElementById('v3-profile-overlay')?.remove();
            const overlay = document.createElement('div');
            overlay.id = 'v3-profile-overlay';
            overlay.className = 'v3-profile-overlay';
            overlay.innerHTML = `<div class="v3-profile-modal"><button class="v3-profile-close" aria-label="${h('results.profile.close')}">×</button><div class="v3-profile-modal-body"></div></div>`;
            document.body.appendChild(overlay);
            const opener = document.activeElement;
            overlay.querySelector('.v3-profile-modal-body').appendChild(section);
            section.style.display = 'block';
            const close = () => {
                section.style.display = 'none';
                document.body.appendChild(section);
                overlay.remove();
                if (opener && typeof opener.focus === 'function') opener.focus();
            };
            overlay.querySelector('.v3-profile-close').addEventListener('click', close);
            overlay.addEventListener('click', (event) => { if (event.target === overlay) close(); });
            document.getElementById('profiling-header')?.click();
        },

        _setActionsEnabled(enabled) {
            this._actionsEnabled = enabled;
            ['export-btn', 'copy-results-btn', 'send-result-btn', 'describe-btn'].forEach((id) => {
                const button = document.getElementById(id);
                if (!button) return;
                button.disabled = !enabled;
                button.setAttribute('aria-disabled', String(!enabled));
                button.style.display = '';
                if (id === 'send-result-btn') {
                    const me = window._currentUser || {};
                    // Without Entra and a delivery connector Send can never work: hide it
                    // instead of showing a permanently disabled primary button.
                    const eligible = Boolean(me.connectors_enabled && me.is_entra);
                    const canSend = enabled && eligible && Boolean(window._resultHandle);
                    button.style.display = eligible ? '' : 'none';
                    button.disabled = !canSend;
                    button.setAttribute('aria-disabled', String(!canSend));
                    if (canSend) button.title = t('results.actions.sendResult');
                    else button.title = enabled ? t('send.noSnapshot') : t('results.actions.sendLabel');
                }
            });
        },

        _initials() {
            const name = String(window._currentUser?.name || window._currentUser?.email || t('common.you')).trim();
            const parts = name.split(/\s+/);
            return (parts.length > 1 ? `${parts[0][0]}${parts[parts.length - 1][0]}` : name.slice(0, 2)).toUpperCase();
        },

        _scrollThread() {
            // The thread was just re-rendered synchronously, so scroll now; the
            // frame callback catches layout that settles afterwards (fonts,
            // images). Relying on the frame alone left the thread at the top
            // when frames were delayed and a later render intervened.
            const scroll = () => {
                const thread = document.getElementById('v3-thread');
                if (thread) thread.scrollTop = thread.scrollHeight;
            };
            scroll();
            requestAnimationFrame(scroll);
        },

        _scrollTurnIntoView(turnId) {
            requestAnimationFrame(() => {
                const card = [...document.querySelectorAll('#v3-thread [data-turn]')]
                    .find((node) => node.dataset.turn === turnId);
                card?.scrollIntoView({ block: 'nearest' });
            });
        },
    };

    window.WorkspaceV3Utils = {
        PHASES,
        NODE_PHASE,
        compactProfile,
        cappedMeta,
        textOf,
        directionOf,
        safeTraceNote,
        filterResultRows,
        selectionForTurn,
        turnShowsResult,
    };
    window.WorkspaceController = WorkspaceController;
    if (typeof document !== 'undefined' && document.body) {
        WorkspaceController.init();
    } else {
        window.addEventListener('DOMContentLoaded', () => WorkspaceController.init());
    }
})();
