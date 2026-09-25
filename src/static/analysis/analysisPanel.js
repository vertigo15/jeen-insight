/**
 * ML skills — answer-pane rendering helpers.
 *
 * Pure functions that turn the API's `proposal` / `analysis` objects into
 * markup for the existing workspace surfaces (status strip, placeholder card,
 * dock), plus the setup form's local behaviour (`bindSetupForm`: live summary,
 * changed markers, validation). No fetch: the WorkspaceController owns I/O and
 * binds the run/cancel handlers. Tokens only — colours come from
 * design-tokens.css classes.
 *
 * Copy rules (docs/ml_skills_handoff/README.md §9): the headline is the
 * finding; the method lives in the strip; numbers, never adjectives.
 */
(function () {
    'use strict';

    const esc = (value) => String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

    // Interface strings come from the locale catalog (static/i18n/i18n.js).
    const t = (key, args) => (window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
    const h = (key, args) => esc(t(key, args));
    const has = (key) => Boolean(window.I18n && typeof window.I18n.has === 'function' && window.I18n.has(key));

    const SKILL_KEYS = ['anomaly_detection', 'forecast', 'changepoint', 'seasonality', 'correlation', 'contribution',
        'clustering', 'driver_analysis', 'regression', 'classification', 'cohort_retention', 'experiment_test'];
    // Skill labels resolve lazily through the catalog; the object shape is kept
    // for callers that index it (`SKILL_LABEL[skill]`).
    const SKILL_LABEL = new Proxy({}, {
        get: (_target, skill) => (typeof skill === 'string' && SKILL_KEYS.includes(skill) ? t(`analysis.skills.${skill}`) : undefined),
        has: (_target, skill) => SKILL_KEYS.includes(skill),
        ownKeys: () => SKILL_KEYS.slice(),
        getOwnPropertyDescriptor: (_target, skill) => (SKILL_KEYS.includes(skill)
            ? { enumerable: true, configurable: true, value: t(`analysis.skills.${skill}`) } : undefined),
    });
    const grainAdj = (grain) => (has(`analysis.grain.adj.${grain}`) ? t(`analysis.grain.adj.${grain}`) : undefined);
    const grainUnit = (grain) => (has(`analysis.grain.unit.${grain}`) ? t(`analysis.grain.unit.${grain}`) : undefined);
    const SERIES_SKILLS = ['anomaly_detection', 'forecast', 'changepoint', 'seasonality', 'correlation'];
    // Plain-language egress tier. The letter stays on hover (and in Model details).
    const tierMeta = (tier) => (has(`analysis.tier.meta.${tier}`) ? t(`analysis.tier.meta.${tier}`) : undefined);
    const tierTitle = (tier) => (has(`analysis.tier.title.${tier}`) ? t(`analysis.tier.title.${tier}`) : '');
    // Shown in place of the server's egress sentence while a field is changed:
    // its numbers ("about 24 monthly totals") belong to the values that were
    // planned, and are recomputed by the run.
    const tierNeutralEgress = (tier) => t(`analysis.tier.neutralEgress.${has(`analysis.tier.neutralEgress.${tier}`) ? tier : 'A'}`);

    function fmtNum(value, digits) {
        if (value == null || Number.isNaN(Number(value))) return '—';
        const v = Number(value);
        const a = Math.abs(v);
        if (a >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
        if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
        if (a >= 1e4) return `${Math.round(v / 1e3)}k`;
        if (a >= 1e3) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
        return v.toLocaleString(undefined, { maximumFractionDigits: digits == null ? 2 : digits });
    }

    function fmtPct(value, digits) {
        if (value == null || Number.isNaN(Number(value))) return '—';
        return `${(Number(value) * 100).toFixed(digits == null ? 1 : digits).replace(/\.0$/, '')}%`;
    }

    function metricText(validation) {
        if (!validation) return '';
        const metric = validation.metric;
        if (validation.value == null) {
            return validation.coverage != null ? t('analysis.metric.coverage', { value: fmtPct(validation.coverage, 0) }) : '';
        }
        if (metric === 'WAPE') return `WAPE ${fmtPct(validation.value)}`;
        if (metric === 'MASE') return `MASE ${Number(validation.value).toFixed(2)}`;
        if (metric === 'r') return `r ${Number(validation.value).toFixed(2)}`;
        if (metric === 'R2') return `R² ${Number(validation.value).toFixed(2)}`;
        if (metric === 'silhouette') return `silhouette ${Number(validation.value).toFixed(2)}`;
        if (metric === 'explained') return t('analysis.metric.explains', { value: fmtPct(validation.value, 0) });
        return `${metric} ${fmtNum(validation.value)}`;
    }

    /** Segments appended to #v3-meta-row for a completed ML result. */
    function stripSegments(result) {
        const analysis = result && result.analysis;
        if (!analysis || !analysis.skill) return '';
        const facts = analysis.facts || {};
        const bits = [];
        const points = facts.n_points != null ? facts.n_points : facts.n_entities != null ? facts.n_entities
            : facts.n_rows != null ? facts.n_rows : (analysis.egress || {}).rows_sent_to_model;
        if (points != null) bits.push(facts.n_entities != null || facts.n_rows != null ? t('analysis.strip.rows', { count: Number(points) }) : t('analysis.strip.points', { count: Number(points) }));
        if (facts.series_count != null) bits.push(t('analysis.strip.series', { count: Number(facts.series_count) }));
        if (analysis.skill === 'anomaly_detection' && facts.n_flagged != null) bits.push(t('analysis.strip.flagged', { count: Number(facts.n_flagged) }));
        if (analysis.skill === 'forecast' && facts.horizon != null) bits.push(t('analysis.strip.horizon', { value: facts.horizon }));
        if (analysis.skill === 'changepoint' && facts.n_changepoints != null) bits.push(t('analysis.strip.shifts', { count: Number(facts.n_changepoints) }));
        if (analysis.skill === 'seasonality' && facts.strength != null) bits.push(t('analysis.strip.strength', { value: Number(facts.strength).toFixed(2) }));
        if (analysis.skill === 'clustering' && facts.k != null) bits.push(t('analysis.strip.segments', { count: Number(facts.k) }));
        if (analysis.skill === 'contribution' && facts.delta_pct != null) bits.push(`Δ ${fmtPct(facts.delta_pct)}`);
        if ((analysis.egress || {}).tier === 'B') bits.push(t('analysis.tier.rowLevel'));
        const metric = metricText(analysis.validation);
        if (metric) bits.push(metric);
        const sent = analysis.egress && analysis.egress.rows_sent_to_model;
        if (sent != null) bits.push(t('analysis.strip.rowsSent', { count: Number(sent) }));
        const low = Boolean(result.low_confidence || analysis.low_confidence);
        // The method is named in the strip (never in the headline), kept short;
        // the full label is on hover and in Model details.
        const method = String(analysis.method_used || '');
        const shortMethod = method.length > 34 ? `${method.slice(0, 32)}…` : method;
        return `<span class="v3-skill-chip" title="${esc(method)}">${esc(SKILL_LABEL[analysis.skill] || analysis.skill)}</span>
          ${method ? `<span class="v3-result-meta v3-ml-method" title="${esc(method)}">${esc(shortMethod)}</span>` : ''}
          <span class="v3-result-meta v3-ml-meta">${esc(bits.join(' · '))}</span>
          ${low ? `<span class="v3-lowconf-pill" title="${h('analysis.strip.lowConfidenceTitle')}">${h('analysis.strip.lowConfidence')}</span>` : ''}
          ${hasDefinition(analysis) ? `<button type="button" class="v3-text-btn v3-ml-edit" data-ml-edit aria-expanded="false">${h('analysis.strip.editSetup')}</button>` : ''}`;
    }

    function hasDefinition(analysis) {
        const chips = analysis && analysis.definition && analysis.definition.chips;
        return Array.isArray(chips) && chips.length > 0;
    }

    // ── Setup form (confirm card + "Edit setup") ──────────────────────────────
    //
    // The server sends one ParamChip per parameter (src/analysis/contracts.py):
    // key/value/options are the patch contract; group/unit/help/min/max/
    // option_labels are presentation. Everything presentational is optional so
    // chips persisted before it existed still render (one "Setup" section).

    const listOf = (value) => Array.isArray(value)
        ? value.map((v) => String(v).trim()).filter(Boolean)
        : String(value == null ? '' : value).split(',').map((s) => s.trim()).filter(Boolean);

    function normalizeChip(chip) {
        const c = { ...(chip || {}) };
        c.key = String(c.key || '');
        c.label = c.label == null ? c.key : String(c.label);
        c.options = Array.isArray(c.options) ? c.options : [];
        c.option_labels = c.option_labels && typeof c.option_labels === 'object' ? c.option_labels : {};
        c.group = c.group ? String(c.group) : t('analysis.form.setupGroup');
        c.editable = c.editable !== false;
        if (!c.kind) {
            if (Array.isArray(c.value)) c.kind = 'multiselect';
            else if (typeof c.value === 'number') c.kind = 'number';
            else c.kind = 'text';
        }
        if (c.kind === 'multiselect') c.value = listOf(c.value);
        return c;
    }

    /** `{key: value}` from chips (the values the card was rendered with). */
    function valuesOf(chips) {
        const values = {};
        (chips || []).forEach((chip) => { const c = normalizeChip(chip); values[c.key] = c.value; });
        return values;
    }

    function unitText(chip, values) {
        if (chip.unit) return String(chip.unit);
        if (chip.unit_from) {
            const source = values ? values[chip.unit_from] : null;
            return grainUnit(source) || (source ? String(source) : '');
        }
        return '';
    }

    function optionLabel(chip, value) {
        const label = chip.option_labels[String(value)];
        return label == null ? String(value == null ? '' : value) : String(label);
    }

    /** Human display of a chip's value: option label, joined list, or the raw value. */
    function displayValue(chip, value) {
        if (Array.isArray(value)) return value.join(', ');
        if (value == null || value === '') return '';
        return optionLabel(chip, value);
    }

    const isSelect = (c) => c.kind !== 'multiselect' && c.options.length > 0;
    const isNumber = (c) => c.kind === 'number' && !c.options.length;

    function boundsHint(c) {
        if (c.min == null && c.max == null) return '';
        if (c.min != null && c.max != null) return `${c.min}–${c.max}`;
        return c.min != null ? t('analysis.form.atLeast', { value: c.min }) : t('analysis.form.atMost', { value: c.max });
    }

    function fieldControl(chip, idPrefix, values) {
        const c = normalizeChip(chip);
        const key = esc(c.key);
        const id = `${esc(idPrefix || 'ml')}-${key}`;
        const helpId = `${id}-help`;
        const errId = `${id}-err`;
        const value = c.value == null ? '' : c.value;
        const unit = unitText(c, values);
        const described = `${c.help || boundsHint(c) ? `${helpId} ` : ''}${errId}`;
        const common = `data-chip="${key}" data-kind="${esc(c.kind)}" aria-describedby="${described}"${c.required ? ' aria-required="true"' : ''}`;
        let control;
        if (!c.editable) {
            control = `<div class="v3-ml-static" id="${id}"><bdi>${esc(displayValue(c, value))}</bdi></div>`;
        } else if (c.kind === 'multiselect') {
            const chosen = listOf(value);
            const pool = c.options.map(String);
            chosen.forEach((v) => { if (!pool.includes(v)) pool.push(v); });
            const joined = chosen.join(',');
            const pills = pool.map((o) => `<button type="button" class="v3-ml-toggle${chosen.includes(o) ? ' is-on' : ''}" data-toggle="${esc(o)}" aria-pressed="${chosen.includes(o)}"><bdi>${esc(o)}</bdi></button>`).join('');
            control = `<div class="v3-ml-multi" id="${id}" role="group" aria-labelledby="${id}-label">
                <input type="hidden" ${common} data-original="${esc(joined)}" value="${esc(joined)}">
                ${pills}
                <span class="v3-ml-count" data-count>${chosen.length}${boundsHint(c) ? ` ${h('analysis.form.of')} ${esc(boundsHint(c))}` : ''}</span>
              </div>`;
        } else if (isSelect(c)) {
            const opts = c.options.map((o) => `<option value="${esc(o)}"${String(o) === String(value) ? ' selected' : ''}>${esc(optionLabel(c, o))}</option>`).join('');
            const extra = c.options.some((o) => String(o) === String(value)) || value === ''
                ? '' : `<option value="${esc(value)}" selected>${esc(optionLabel(c, value))}</option>`;
            const placeholder = value === '' ? `<option value="" selected disabled>${h('analysis.form.choose')}</option>` : '';
            control = `<select id="${id}" ${common} data-original="${esc(value)}" title="${esc(displayValue(c, value))}" dir="auto">${placeholder}${extra}${opts}</select>`;
        } else if (isNumber(c)) {
            const attrs = [
                c.min != null ? `min="${esc(c.min)}"` : '', c.max != null ? `max="${esc(c.max)}"` : '',
                `step="${c.step != null ? esc(c.step) : 'any'}"`, c.step === 1 ? 'inputmode="numeric"' : 'inputmode="decimal"',
            ].filter(Boolean).join(' ');
            control = `<input id="${id}" ${common} data-original="${esc(value)}" type="number" ${attrs}${c.step === 1 ? ' data-step="1"' : ''} value="${esc(value)}">`;
        } else if (c.kind === 'date') {
            control = `<input id="${id}" ${common} data-original="${esc(value)}" type="date" value="${esc(value)}">`;
        } else {
            control = `<input id="${id}" ${common} data-original="${esc(value)}" type="text" value="${esc(value)}" dir="auto">`;
        }
        const help = c.help ? esc(c.help) : '';
        const hint = isNumber(c) && boundsHint(c) ? `<span class="v3-ml-bounds">(${esc(boundsHint(c))})</span>` : '';
        return `<div class="v3-ml-field${c.required ? ' is-required' : ''}${c.editable ? '' : ' is-static'}" data-field="${key}">
          <label class="v3-ml-label" id="${id}-label" for="${id}">${esc(c.label)}<span class="v3-ml-badge" data-badge hidden>${h('analysis.form.changed')}</span></label>
          <div class="v3-ml-input">${control}${unit ? `<span class="v3-ml-unit" data-unit>${esc(unit)}</span>` : ''}</div>
          ${help || hint ? `<small class="v3-ml-help" id="${helpId}">${help}${help && hint ? ' ' : ''}${hint}</small>` : ''}
          <small class="v3-ml-fielderr" id="${errId}" hidden></small>
        </div>`;
    }

    /** The grouped fields: one fieldset per section label, in first-occurrence order. */
    function setupFormHtml(chips, idPrefix) {
        const list = (chips || []).map(normalizeChip);
        const values = valuesOf(list);
        const groups = [];
        list.forEach((c) => {
            let group = groups.find((g) => g.label === c.group);
            if (!group) { group = { label: c.group, chips: [] }; groups.push(group); }
            group.chips.push(c);
        });
        return groups.map((g) => `<fieldset class="v3-ml-group" data-group="${esc(g.label)}">
            <legend class="v3-ml-section-title">${esc(g.label)}</legend>
            <div class="v3-ml-fields">${g.chips.map((c) => fieldControl(c, idPrefix, values)).join('')}</div>
          </fieldset>`).join('');
    }

    /**
     * One line that reads the setup back: `SUM(<measure>) per month · last 24
     * months · 6 months ahead · 80% interval · Auto model`. Bespoke for the
     * time-series family; every other skill lists its filled fields.
     * Returns HTML (column names are isolated in <bdi> for mixed-direction text).
     */
    function summarySentence(skill, values, chips) {
        const list = (chips || []).map(normalizeChip);
        const byKey = Object.fromEntries(list.map((c) => [c.key, c]));
        const v = values || valuesOf(list);
        if (!list.length && !Object.keys(v).length) return '';  // nothing to read back; the caller keeps its own sentence
        const show = (key) => (byKey[key] ? displayValue(byKey[key], v[key]) : String(v[key] == null ? '' : v[key]));
        const filled = (key) => v[key] != null && v[key] !== '' && !(Array.isArray(v[key]) && !v[key].length);
        const parts = [];
        if (SERIES_SKILLS.includes(skill)) {
            const agg = String(v.agg || 'sum').toUpperCase();
            const grain = v.grain || '';
            const unit = grainUnit(grain) || (grain ? `${grain}s` : t('analysis.grain.periods'));
            let head = `${esc(agg)}(<bdi>${esc(v.measure_column || '…')}</bdi>)`;
            if (skill === 'correlation' && filled('other_measure_column')) head += ` ${h('analysis.summary.vs')} <bdi>${esc(v.other_measure_column)}</bdi>`;
            parts.push(head);
            if (grain) parts.push(t('analysis.summary.per', { grain: esc(grain) }));
            if (filled('window')) parts.push(t('analysis.summary.last', { count: esc(v.window), unit: esc(unit) }));
            if (skill === 'forecast') {
                if (filled('horizon')) parts.push(t('analysis.summary.ahead', { count: esc(v.horizon), unit: esc(unit) }));
                if (filled('interval')) parts.push(t('analysis.summary.interval', { value: esc(show('interval')) }));
            }
            if (skill === 'anomaly_detection' && filled('sensitivity')) parts.push(t('analysis.summary.sensitivity', { value: esc(show('sensitivity')) }));
            if (skill === 'changepoint' && filled('max_changepoints')) parts.push(t('analysis.summary.breaks', { count: esc(v.max_changepoints) }));
            if (skill === 'correlation' && filled('max_lag')) parts.push(t('analysis.summary.lag', { count: esc(v.max_lag), unit: esc(unit) }));
            if (filled('method')) parts.push(t('analysis.summary.model', { value: esc(show('method')) }));
            if (filled('group_by') && String(v.group_by).toLowerCase() !== 'none') parts.push(t('analysis.summary.split', { column: `<bdi>${esc(v.group_by)}</bdi>` }));
            return parts.join(' · ');
        }
        list.forEach((c) => {
            if (!filled(c.key)) return;
            const text = displayValue(c, v[c.key]);
            if (!text) return;
            const unit = unitText(c, v);
            parts.push(`${esc(c.label)} <bdi>${esc(text)}</bdi>${unit ? ` ${esc(unit)}` : ''}`);
        });
        return parts.join(' · ');
    }

    /**
     * Pure validation against the chips' declared bounds — the same numbers
     * `parse_params` enforces server-side. `values` is `{key: current value}`.
     */
    function validateValues(chips, values) {
        const errors = {};
        const list = (chips || []).map(normalizeChip);
        const v = values || {};
        list.forEach((c) => {
            if (!c.editable) return;
            const value = v[c.key];
            const empty = value == null || value === '' || (Array.isArray(value) && !value.length);
            if (empty) {
                if (c.required) errors[c.key] = t('analysis.validation.required');
                return;
            }
            const unit = unitText(c, v);
            const withUnit = (n) => `${n}${unit ? ` ${unit}` : ''}`;
            if (c.kind === 'multiselect') {
                const n = listOf(value).length;
                if (c.min != null && n < c.min) errors[c.key] = t('analysis.validation.pickAtLeast', { count: c.min });
                else if (c.max != null && n > c.max) errors[c.key] = t('analysis.validation.pickAtMost', { count: c.max });
                return;
            }
            if (isNumber(c)) {
                const n = Number(value);
                if (!Number.isFinite(n)) { errors[c.key] = t('analysis.validation.enterNumber'); return; }
                if (c.min != null && n < c.min) errors[c.key] = t('analysis.validation.atLeast', { value: withUnit(c.min) });
                else if (c.max != null && n > c.max) errors[c.key] = t('analysis.validation.atMost', { value: withUnit(c.max) });
                return;
            }
            if (c.kind === 'date' && Number.isNaN(Date.parse(String(value)))) errors[c.key] = t('analysis.validation.enterDate');
        });
        return { ok: Object.keys(errors).length === 0, errors };
    }

    function tierMetaHtml(tier, seconds) {
        const bits = [];
        if (tier) bits.push(tierMeta(tier) || t('analysis.tier.label', { tier }));
        if (seconds) bits.push(`~${seconds}s`);
        if (!bits.length) return '';
        return `<span class="v3-ml-tiermeta" title="${esc(tierTitle(tier))}">${esc(bits.join(' · '))}</span>`;
    }

    /**
     * The setup card for a finished result: the confirm card's fields with the
     * values that ran (measure, grain, window, model, …), plus "Re-run" /
     * "Cancel". Re-running appends a new child turn; the controller binds
     * `[data-run]` and `[data-cancel]`. This is the one place to change the model.
     */
    function definitionHtml(analysis) {
        if (!hasDefinition(analysis)) return '';
        const definition = analysis.definition;
        const chips = definition.chips.map(normalizeChip);
        const skill = String(analysis.skill || '');
        const tier = (analysis.egress || {}).tier || '';
        return `<div class="v3-ml-card is-confirm is-definition" data-skill="${esc(skill)}" data-tier="${esc(tier)}">
          <div class="v3-ml-plan">
            <span class="v3-ml-phase">${h('analysis.definition.setup')}</span>
            <span class="v3-skill-chip">${esc(SKILL_LABEL[skill] || skill || t('conversation.empty.analysis'))}</span>
            <span class="v3-ml-summary" data-summary>${summarySentence(skill, valuesOf(chips), chips)}</span>
          </div>
          <div class="v3-ml-panel">
            <p class="v3-ml-reading">${h('analysis.definition.reading')}</p>
            ${setupFormHtml(chips, 'ml-setup')}
            ${definition.egress_summary ? `<p class="v3-ml-egress" data-egress data-egress-original="${esc(definition.egress_summary)}">${esc(definition.egress_summary)}</p>` : ''}
            <div class="v3-ml-actions">
              <button type="button" class="v3-ml-run" data-run>${h('analysis.definition.rerun')}</button>
              <button type="button" class="v3-text-btn" data-cancel>${h('common.cancel')}</button>
              <span class="v3-ml-note" data-note aria-live="polite">${h('analysis.definition.changeToRerun')}</span>
              <span class="v3-ml-errsum" data-errsum aria-live="polite"></span>
              <span class="v3-ml-footmeta">
                <button type="button" class="v3-text-btn v3-ml-resetall" data-reset-all hidden>${h('analysis.definition.resetAll')}</button>
                ${tierMetaHtml(tier)}
              </span>
            </div>
          </div>
        </div>`;
    }

    /** True when a stored proposal can no longer be resumed. */
    function proposalExpired(proposal, now) {
        if (!proposal || !proposal.proposal_id) return true;
        if (!proposal.expires_at) return false;
        const t = new Date(proposal.expires_at).getTime();
        return Number.isFinite(t) && t <= (now || Date.now());
    }

    // ── Confirm / clarify / guard card ────────────────────────────────────────

    function expiredNoticeHtml() {
        return `<section class="v3-ml-expired-notice" role="status" aria-labelledby="v3-ml-expired-title">
          <span class="v3-ml-expired-icon" aria-hidden="true">!</span>
          <div class="v3-ml-expired-copy">
            <span class="v3-ml-expired-badge">${h('analysis.proposal.expiredBadge')}</span>
            <strong id="v3-ml-expired-title">${h('analysis.proposal.expiredHeading')}</strong>
            <p>${h('analysis.proposal.expiredBody')}</p>
            <div class="v3-ml-expired-actions">
              <button type="button" class="v3-ml-run" data-recreate>${h('analysis.proposal.askAgain')}</button>
              <button type="button" class="v3-text-btn v3-ml-sql" data-sql-instead>${h('analysis.proposal.sqlInstead')}</button>
            </div>
            <span class="v3-ml-expired-hint" data-recreate-hint aria-live="polite" hidden></span>
          </div>
        </section>`;
    }

    function staticExitOptions(options) {
        return (options || []).map((o) => `<span class="v3-ml-exit is-static">${esc(o.label)}</span>`).join('');
    }

    function guardList(guardResults, onlyFailed) {
        const rows = (guardResults || []).filter((g) => !onlyFailed || !g.passed);
        if (!rows.length) return '';
        return `<ul class="v3-ml-guards">${rows.map((g) => `<li class="${g.passed ? 'is-ok' : 'is-fail'}"><span class="v3-ml-guard-name">${esc(g.name)}</span><span>${esc(g.detail)}</span></li>`).join('')}</ul>`;
    }

    function exitButtons(options) {
        return (options || []).map((o, i) => {
            const kind = o.kind || 'patch';
            const cls = kind === 'override' ? 'v3-ml-exit is-override' : o.recommended ? 'v3-ml-exit is-recommended' : 'v3-ml-exit';
            return `<button type="button" class="${cls}" data-exit="${i}" data-exit-kind="${esc(kind)}" title="${esc(o.description || '')}">${esc(o.label)}${o.recommended ? ` <small>${h('analysis.proposal.recommended')}</small>` : ''}</button>`;
        }).join('');
    }

    /**
     * The card for a stopped run. `kind` = confirm | clarify | guard.
     * Handlers are bound by the controller on `[data-run]`, `[data-exit]`,
     * `[data-sql-instead]`, `[data-remember]`.
     */
    function proposalHtml(proposal) {
        const kind = proposal.kind;
        const expired = proposalExpired(proposal);
        const series = (proposal.params || {}).series || {};
        const skill = esc(SKILL_LABEL[proposal.skill] || proposal.skill || t('conversation.empty.analysis'));
        const head = `<div class="v3-ml-card-head">
            <span class="v3-skill-chip">${skill}</span>
            <span class="v3-ml-kind">${kind === 'confirm' ? h('analysis.proposal.confirmKind') : kind === 'clarify' ? h('analysis.proposal.clarifyKind') : h('analysis.proposal.guardKind')}</span>
            ${proposal.tier ? `<span class="v3-ml-tier">${h('analysis.proposal.tier', { tier: proposal.tier, mode: proposal.tier === 'A' ? t('analysis.tier.aggregate') : t('analysis.tier.rowLevel') })}</span>` : ''}
          </div>`;
        if (kind === 'confirm') {
            const chips = (proposal.chips || []).map(normalizeChip);
            const displayChips = expired ? chips.map((chip) => ({ ...chip, editable: false })) : chips;
            const grain = grainAdj(series.grain) || series.grain || '';
            const summary = summarySentence(proposal.skill, valuesOf(chips), chips);
            const egress = proposal.egress_summary || t('analysis.proposal.egressDefault', { grain });
            return `<div class="v3-ml-card is-confirm${expired ? ' is-expired' : ''}" data-skill="${esc(proposal.skill || '')}" data-tier="${esc(proposal.tier || '')}">
              ${expired ? expiredNoticeHtml() : ''}
              <div class="v3-ml-plan" title="${esc(proposal.message)}">
                ${expired ? '' : `<span class="v3-ml-phase">${h('analysis.proposal.planning')}</span>`}
                <span class="v3-skill-chip">${skill}</span>
                <span class="v3-ml-summary" data-summary>${summary || esc(proposal.message)}</span>
              </div>
              <div class="v3-ml-panel">
                ${setupFormHtml(displayChips, 'ml-confirm')}
                <p class="v3-ml-egress" data-egress data-egress-original="${esc(egress)}">${esc(egress)}</p>
                ${expired ? '' : `<div class="v3-ml-actions">
                  <button type="button" class="v3-ml-run" data-run>${h('analysis.proposal.run')}</button>
                  <button type="button" class="v3-ml-alt" data-switch-skill title="${h('analysis.proposal.rephraseTitle')}">${h('analysis.proposal.rephrase')}</button>
                  <button type="button" class="v3-text-btn v3-ml-sql" data-sql-instead>${h('analysis.proposal.sqlInstead')}</button>
                  <span class="v3-ml-errsum" data-errsum aria-live="polite"></span>
                  <span class="v3-ml-footmeta">
                    <button type="button" class="v3-text-btn v3-ml-resetall" data-reset-all hidden>${h('analysis.definition.resetAll')}</button>
                    ${tierMetaHtml(proposal.tier, proposal.estimated_seconds)}
                  </span>
                </div>
                <label class="v3-ml-remember"><input type="checkbox" data-remember> ${t('analysis.proposal.dontAsk', { skill })}</label>`}
              </div>
            </div>`;
        }
        if (kind === 'clarify') {
            return `<div class="v3-ml-card is-clarify${expired ? ' is-expired' : ''}">
              ${expired ? expiredNoticeHtml() : ''}
              ${head}
              <p class="v3-ml-message" dir="auto">${esc(proposal.message)}</p>
              <div class="v3-ml-exits">${expired ? staticExitOptions(proposal.options) : exitButtons(proposal.options)}</div>
              ${expired ? '' : `<div class="v3-ml-actions"><button type="button" class="v3-text-btn" data-sql-instead>${h('analysis.proposal.sqlInstead')}</button></div>`}
            </div>`;
        }
        return `<div class="v3-ml-card is-guard${expired ? ' is-expired' : ''}">
          ${expired ? expiredNoticeHtml() : ''}
          ${head}
          <p class="v3-ml-message v3-ml-refusal" dir="auto">${esc(proposal.message)}</p>
          ${guardList(proposal.guard_results, true)}
          <div class="v3-ml-exits">${expired ? staticExitOptions(proposal.options) : exitButtons(proposal.options)}</div>
          <p class="v3-ml-egress">${h('analysis.proposal.guardEgress')}</p>
        </div>`;
    }

    /** Read edited fields back into an allowlisted params_patch. */
    function collectPatch(cardEl) {
        const patch = {};
        if (!cardEl) return patch;
        cardEl.querySelectorAll('[data-chip]').forEach((el) => {
            const key = el.dataset.chip;
            const original = el.dataset.original;
            let value = el.value;
            if (String(value) === String(original)) return;
            if (el.dataset.kind === 'multiselect') {
                patch[key] = listOf(value);
                return;
            }
            // Integer fields declare step=1; window/horizon stay integers for
            // chips persisted before the metadata existed.
            const integer = el.dataset.step === '1' || ['window', 'horizon'].includes(key);
            if (el.type === 'number' || el.dataset.kind === 'number' || integer) {
                if (String(value).trim() === '') return;  // an emptied number is not a change to 0
                const n = Number(value);
                if (!Number.isFinite(n)) return;
                value = integer ? Math.round(n) : n;
            }
            patch[key] = value;
        });
        return patch;
    }

    // ── Setup form behaviour (DOM-local, no I/O) ──────────────────────────────

    /** Current `{key: value}` of a rendered card, typed like the patch. */
    function currentValues(cardEl, chips) {
        const values = valuesOf(chips);
        if (!cardEl) return values;
        cardEl.querySelectorAll('[data-chip]').forEach((el) => {
            const key = el.dataset.chip;
            let value = el.value;
            if (el.dataset.kind === 'multiselect') value = listOf(value);
            else if (el.type === 'number' || el.dataset.kind === 'number') value = String(value).trim() === '' ? '' : Number(value);
            values[key] = value;
        });
        return values;
    }

    function fieldOf(cardEl, key) {
        const fields = cardEl.querySelectorAll('.v3-ml-field[data-field]');
        for (const field of fields) if (field.dataset.field === key) return field;
        return null;
    }

    function setFieldError(cardEl, key, message) {
        const field = fieldOf(cardEl, key);
        if (!field) return false;
        const err = field.querySelector('.v3-ml-fielderr');
        const control = field.querySelector('[data-chip]');
        field.classList.toggle('is-invalid', Boolean(message));
        if (err) { err.hidden = !message; err.textContent = message || ''; }
        if (control) control.setAttribute('aria-invalid', message ? 'true' : 'false');
        return true;
    }

    function focusField(field) {
        if (!field) return;
        const target = field.querySelector('.v3-ml-toggle, select, input:not([type="hidden"])');
        if (typeof field.scrollIntoView === 'function') field.scrollIntoView({ block: 'nearest' });
        if (target && typeof target.focus === 'function') target.focus();
    }

    function syncMulti(multi) {
        const hidden = multi.querySelector('input[type="hidden"][data-chip]');
        const on = Array.from(multi.querySelectorAll('[data-toggle]')).filter((b) => b.classList.contains('is-on')).map((b) => b.dataset.toggle);
        if (hidden) hidden.value = on.join(',');
        const count = multi.querySelector('[data-count]');
        if (count) count.textContent = count.textContent.replace(/^\d+/, String(on.length));
    }

    /**
     * Wire a rendered card's form: live summary, "changed" markers + Reset all,
     * the grain rule, unit labels, inline validation, and the neutral egress
     * line while values differ from what was planned. Returns helpers the
     * controller uses around Run/Re-run.
     *
     * Grain rule: a window still at a default moves to the new grain's default
     * (day 90 / week 26 / month 24) so "26 weeks" never silently becomes
     * "26 months"; a number the user typed is kept and only its unit changes.
     */
    function bindSetupForm(cardEl, chips, options) {
        const opts = options || {};
        const list = (chips || []).map(normalizeChip);
        const skill = opts.skill || (cardEl.dataset ? cardEl.dataset.skill : '') || '';
        const tier = opts.tier || (cardEl.dataset ? cardEl.dataset.tier : '') || 'A';
        const summaryEl = cardEl.querySelector('[data-summary]');
        const egressEl = cardEl.querySelector('[data-egress]');
        const resetAll = cardEl.querySelector('[data-reset-all]');
        const errsum = cardEl.querySelector('[data-errsum]');
        const windowChip = list.find((c) => c.key === 'window');

        const state = { dirty: 0, values: valuesOf(list), errors: {} };

        const refresh = () => {
            const values = currentValues(cardEl, list);
            let dirty = 0;
            list.forEach((c) => {
                const field = fieldOf(cardEl, c.key);
                if (!field) return;
                const control = field.querySelector('[data-chip]');
                const changed = Boolean(control) && String(control.value) !== String(control.dataset.original);
                field.classList.toggle('is-changed', changed);
                const badge = field.querySelector('[data-badge]');
                if (badge) badge.hidden = !changed;
                if (changed) dirty += 1;
                const unitEl = field.querySelector('[data-unit]');
                if (unitEl && c.unit_from) unitEl.textContent = unitText(c, values);
                if (control && control.tagName === 'SELECT') control.title = displayValue(c, control.value);
            });
            if (summaryEl) {
                const text = summarySentence(skill, values, list);
                if (text) summaryEl.innerHTML = text;
            }
            if (egressEl && egressEl.dataset.egressOriginal) {
                egressEl.textContent = dirty ? tierNeutralEgress(tier) : egressEl.dataset.egressOriginal;
            }
            if (resetAll) resetAll.hidden = !dirty;
            const { errors } = validateValues(list, values);
            list.forEach((c) => setFieldError(cardEl, c.key, errors[c.key] || ''));
            if (errsum) errsum.textContent = '';
            state.dirty = dirty;
            state.values = values;
            state.errors = errors;
            if (typeof opts.onChange === 'function') opts.onChange(state);
        };

        const applyGrainRule = (newGrain) => {
            if (!windowChip || !windowChip.defaults_by_grain) return;
            const field = fieldOf(cardEl, 'window');
            const control = field && field.querySelector('[data-chip]');
            if (!control) return;
            const defaults = windowChip.defaults_by_grain;
            const atDefault = String(control.value) === String(control.dataset.original)
                || Object.values(defaults).some((d) => String(d) === String(control.value));
            if (atDefault && defaults[newGrain] != null) control.value = String(defaults[newGrain]);
        };

        const resetAllFields = () => {
            cardEl.querySelectorAll('[data-chip]').forEach((el) => {
                el.value = el.dataset.original == null ? '' : el.dataset.original;
                if (el.dataset.kind === 'multiselect') {
                    const multi = el.closest('.v3-ml-multi');
                    const chosen = listOf(el.value);
                    multi.querySelectorAll('[data-toggle]').forEach((b) => {
                        const on = chosen.includes(b.dataset.toggle);
                        b.classList.toggle('is-on', on);
                        b.setAttribute('aria-pressed', String(on));
                    });
                    syncMulti(multi);
                }
            });
        };

        cardEl.addEventListener('change', (event) => {
            const el = event.target.closest ? event.target.closest('[data-chip]') : null;
            if (!el) return;
            if (el.dataset.chip === 'grain') applyGrainRule(el.value);
            refresh();
        });
        cardEl.addEventListener('input', (event) => {
            if (event.target.closest && event.target.closest('[data-chip]')) refresh();
        });
        cardEl.addEventListener('click', (event) => {
            const toggle = event.target.closest ? event.target.closest('[data-toggle]') : null;
            if (toggle) {
                const on = !toggle.classList.contains('is-on');
                toggle.classList.toggle('is-on', on);
                toggle.setAttribute('aria-pressed', String(on));
                syncMulti(toggle.closest('.v3-ml-multi'));
                refresh();
                return;
            }
            if (event.target.closest && event.target.closest('[data-reset-all]')) {
                resetAllFields();
                refresh();
            }
        });
        refresh();

        return {
            refresh,
            state,
            values: () => currentValues(cardEl, list),
            validate: () => validateValues(list, currentValues(cardEl, list)),
            /** Before a run: mark every invalid field, focus the first, and say how many. */
            reportInvalid(errors) {
                const keys = Object.keys(errors || {});
                keys.forEach((key) => setFieldError(cardEl, key, errors[key]));
                if (errsum) errsum.textContent = keys.length ? t('analysis.validation.fieldsNeedLook', { count: keys.length }) : '';
                const first = list.find((c) => keys.includes(c.key));
                if (first) focusField(fieldOf(cardEl, first.key));
                return keys.length > 0;
            },
            /** A 422 from the server: on its field when it names one, else false. */
            showServerError(detail) {
                const field = detail && typeof detail === 'object' ? detail.field : null;
                const message = detail && typeof detail === 'object' ? detail.message : String(detail || '');
                if (field && setFieldError(cardEl, field, message || t('analysis.validation.invalidValue'))) {
                    focusField(fieldOf(cardEl, field));
                    return true;
                }
                return false;
            },
        };
    }

    // ── Model details dock ─────────────────────────────────────────────────────

    function kv(label, value) {
        return `<div class="v3-stat"><div class="v3-stat-label">${esc(label)}</div><div class="v3-stat-value">${esc(value)}</div></div>`;
    }

    /**
     * "Adjustments to try": the server's validated one-click re-run patches
     * (analysis.adjustments), rendered as chips. Each carries an index the dock
     * resolves back to its params_patch; the label is built here from the
     * server's code + args so it reads in the user's language.
     */
    function adjustmentLabel(item) {
        const a = item.args || {};
        switch (item.code) {
            case 'coarser_grain':
            case 'coarser_grain_for_season':
                return t(`analysis.adjust.${item.code}`, { grain: grainAdj(a.grain) || a.grain || '' });
            case 'wider_window_for_season':
            case 'wider_window_for_band':
                return t(`analysis.adjust.${item.code}`, { window: a.window, unit: grainUnit(a.unit) || `${a.unit || ''}s`, coverage: a.coverage, level: a.level });
            case 'try_theta':
                return t('analysis.adjust.try_theta', { baseline: a.baseline || '' });
            case 'guard_exit':
                return a.label || t('analysis.adjust.guard_exit', { guard: a.guard || '' });
            default:
                return a.label || item.code;
        }
    }

    function adjustmentsHtml(analysis) {
        const items = Array.isArray(analysis && analysis.adjustments) ? analysis.adjustments : [];
        if (!items.length) return '';
        const chips = items.map((item, index) => `<button type="button" class="v3-chip v3-ml-adjust${item.recommended ? ' is-recommended' : ''}" data-ml-adjust="${index}" title="${esc(patchText(item.params_patch))}">${esc(adjustmentLabel(item))}${item.recommended ? ` <small>${h('analysis.adjust.recommended')}</small>` : ''}</button>`).join('');
        return `<div class="v3-ml-section v3-ml-adjustments">
          <div class="v3-ml-section-title">${h('analysis.adjust.title')}</div>
          <div class="v3-ml-section-copy">${h('analysis.adjust.copy')}</div>
          <div class="v3-ml-adjust-list">${chips}</div>
        </div>`;
    }

    function patchText(patch) {
        const flat = [];
        Object.entries(patch || {}).forEach(([k, v]) => {
            if (v && typeof v === 'object' && !Array.isArray(v)) Object.entries(v).forEach(([nk, nv]) => flat.push(`${nk}=${nv}`));
            else flat.push(`${k}=${v}`);
        });
        return flat.join(' · ');
    }

    /**
     * Forecast tracking: "check against actuals" and, once fetched, the
     * realized error / coverage beside what the model claimed, per period.
     * ``tracking`` is the page-local state: {loading, error, evaluation}.
     */
    function accuracyHtml(analysis, tracking) {
        if (!analysis || analysis.skill !== 'forecast') return '';
        const params = analysis.params || {};
        if ((params.series || {}).group_by || (analysis.facts || {}).series) return '';
        const state = tracking || {};
        const ev = state.evaluation;
        const fmtMetric = (metric, value) => (value == null ? '—' : metric === 'WAPE' ? fmtPct(value) : Number(value).toFixed(3));
        const button = `<button type="button" class="v3-text-btn" data-ml-accuracy${state.loading ? ' disabled' : ''}>${state.loading ? h('analysis.accuracy.checking') : (ev ? h('analysis.accuracy.recheck') : h('analysis.accuracy.check'))}</button>`;
        let body = '';
        if (state.error) {
            body = `<div class="v3-ml-accuracy-note is-error">${esc(state.error)}</div>`;
        } else if (ev) {
            const r = ev.realized || {};
            const c = ev.claimed || {};
            const scored = Number.isFinite(Number(ev.scored_periods)) ? Number(ev.scored_periods) : Number(r.n || 0);
            const unscored = Array.isArray(ev.unscored_periods) ? ev.unscored_periods : [];
            if (!ev.elapsed_periods) {
                body = `<div class="v3-ml-accuracy-note">${h('analysis.accuracy.nothingElapsed', { pending: ev.pending_periods, dataEnd: ev.data_end })}</div>`;
            } else if (!scored) {
                body = `<div class="v3-ml-accuracy-note">${h('analysis.accuracy.nothingScored', { elapsed: ev.elapsed_periods, dataEnd: ev.data_end })}</div>`;
            } else {
                const stats = [
                    kv(t('analysis.accuracy.realizedError', { metric: r.metric || '' }), `${fmtMetric(r.metric, r.value)}${r.band && r.band !== 'n/a' ? ` · ${r.band}` : ''}`),
                    kv(t('analysis.accuracy.claimedError', { metric: c.metric || '' }), `${fmtMetric(c.metric, c.value)}${c.band && c.band !== 'n/a' ? ` · ${c.band}` : ''}`),
                    kv(t('analysis.accuracy.realizedCoverage'), r.coverage == null ? '—' : t('analysis.details.coverageOf', { value: fmtPct(r.coverage, 0), total: r.coverage_n || 0 })),
                    kv(t('analysis.accuracy.target'), fmtPct(ev.realized && ev.realized.interval_level != null ? ev.realized.interval_level : (c.interval_level || 0), 0)),
                ].join('');
                const rows = (ev.points || []).map((p) => `<tr class="${p.inside === false ? 'is-outside' : ''}">
                    <td class="v3-mono">${esc(p.ts)}</td><td class="v3-mono">${esc(fmtNum(p.actual))}</td><td class="v3-mono">${esc(fmtNum(p.forecast))}</td>
                    <td class="v3-mono">${p.lower == null || p.upper == null ? '—' : `${esc(fmtNum(p.lower))} – ${esc(fmtNum(p.upper))}`}</td>
                    <td>${p.inside == null ? '—' : p.inside ? h('analysis.accuracy.inside') : h('analysis.accuracy.outside')}</td></tr>`).join('');
                body = `<div class="v3-stats">${stats}</div>
                  <div class="v3-ml-accuracy-note">${h('analysis.accuracy.scored', { elapsed: scored, pending: ev.pending_periods, dataEnd: ev.data_end })}${unscored.length ? ` · ${h('analysis.accuracy.unscored', { count: unscored.length })}` : ''}</div>
                  <table class="v3-ml-table"><thead><tr><th>${h('analysis.accuracy.period')}</th><th>${h('analysis.accuracy.actual')}</th><th>${h('analysis.accuracy.forecast')}</th><th>${h('analysis.accuracy.band')}</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
            }
        } else {
            body = `<div class="v3-ml-section-copy">${h('analysis.accuracy.copy')}</div>`;
        }
        return `<div class="v3-ml-section v3-ml-accuracy">
          <div class="v3-ml-section-title">${h('analysis.accuracy.title')} ${button}</div>
          ${body}
        </div>`;
    }

    function modelDetailsHtml(result, extras) {
        const analysis = result && result.analysis;
        if (!analysis || !analysis.skill) {
            return `<div class="v3-dock-empty">${h('analysis.details.empty')}</div>`;
        }
        const tracking = extras && extras.tracking;
        const v = analysis.validation || {};
        const d = analysis.details || {};
        const p = analysis.provenance || {};
        const e = analysis.engine || {};
        const params = analysis.params || {};
        const series = params.series || {};
        const stats = [
            kv(t('analysis.details.method'), analysis.method_used || d.method_used || '—'),
            kv(v.metric || t('analysis.details.metric'), v.value == null ? '—' : (v.metric === 'WAPE' ? fmtPct(v.value) : Number(v.value).toFixed(3))),
            kv(t('analysis.details.coverage'), v.coverage == null ? '—' : t('analysis.details.coverageOf', { value: fmtPct(v.coverage, 0), total: v.coverage_n || 0 })),
            kv(t('analysis.details.season'), (d.seasonal_periods || []).length ? `m=${d.seasonal_periods.join(',')}` : t('analysis.details.noneConfirmed')),
            kv(t('analysis.details.rowsSent'), (analysis.egress || {}).rows_sent_to_model == null ? '—' : analysis.egress.rows_sent_to_model),
            kv(t('analysis.details.engine'), `${e.name || '—'} ${e.version || ''}`.trim()),
        ].join('');
        const candidates = (d.candidates || []).map((c) => `<tr class="${c.selected ? 'is-selected' : ''}">
            <td>${esc(c.name)}${c.is_baseline ? ` <small>${h('analysis.details.baseline')}</small>` : ''}${c.selected ? ` <small>${h('analysis.details.selected')}</small>` : ''}</td>
            <td>${esc(c.metric)}</td><td class="v3-mono">${c.value == null ? '—' : c.metric === 'WAPE' || c.metric === 'fit WAPE' ? fmtPct(c.value) : Number(c.value).toFixed(3)}</td></tr>`).join('');
        const nested = params.series || params.entity || params.cohort || params.experiment || {};
        // Arrays are either filter specs ({column, op, value}) or plain lists (features, dimensions).
        const item0IsObject = (val) => val.length > 0 && val[0] != null && typeof val[0] === 'object';
        const listText = (val) => val.map((item) => (item && typeof item === 'object'
            ? `${item.column} ${item.op} ${Array.isArray(item.value) ? item.value.join(', ') : item.value}`
            : String(item))).join(item0IsObject(val) ? '; ' : ', ');
        const paramRows = Object.entries({ ...nested, ...Object.fromEntries(Object.entries(params).filter(([k]) => !['series', 'entity', 'cohort', 'experiment'].includes(k))) })
            .filter(([k, val]) => val != null && val !== '' && !(Array.isArray(val) && !val.length) && !['schema_name', 'catalog', 'timezone'].includes(k))
            .map(([k, val]) => `<tr><td>${esc(k)}</td><td class="v3-mono">${esc(Array.isArray(val) ? listText(val) : val)}</td></tr>`).join('');
        const guards = guardList(analysis.guard_results, false);
        // Engines copy their notes into the caveats; show each sentence once.
        const notes = [...new Set([...(d.notes || []), ...(analysis.caveats || [])].map((n) => String(n)))];
        const diff = analysis.param_diff && Object.keys(analysis.param_diff).length
            ? `<div class="v3-ml-section"><div class="v3-ml-section-title">${h('analysis.details.changedFrom')}</div><div class="v3-ml-diff">${Object.entries(analysis.param_diff).map(([k, c]) => `<span class="v3-ml-diff-chip" dir="ltr">${esc(k)}: <s>${esc(c.from == null ? '—' : c.from)}</s> → ${esc(c.to == null ? '—' : c.to)}</span>`).join('')}</div></div>`
            : '';
        return `<div class="v3-stats">${stats}</div>
          ${result.low_confidence || analysis.low_confidence ? `<div class="v3-ml-lowconf-note">${h('analysis.details.lowConfidenceNote')}</div>` : ''}
          ${adjustmentsHtml(analysis)}
          ${accuracyHtml(analysis, tracking)}
          ${diff}
          <div class="v3-ml-grid">
            <div class="v3-ml-section"><div class="v3-ml-section-title">${h('analysis.details.candidates')}${v.basis ? ` · ${esc(v.basis)}` : ''}</div>
              <table class="v3-ml-table"><thead><tr><th>${h('analysis.details.model')}</th><th>${h('analysis.details.metricCol')}</th><th>${h('analysis.details.value')}</th></tr></thead><tbody>${candidates || '<tr><td colspan="3">—</td></tr>'}</tbody></table></div>
            <div class="v3-ml-section"><div class="v3-ml-section-title">${h('analysis.details.parameters')}</div>
              <table class="v3-ml-table"><tbody>${paramRows}</tbody></table></div>
          </div>
          <div class="v3-ml-section"><div class="v3-ml-section-title">${h('analysis.details.guards')}</div>${guards || `<div class="v3-dock-empty">${h('analysis.details.noneRecorded')}</div>`}</div>
          <div class="v3-ml-section"><div class="v3-ml-section-title">${h('analysis.details.data')}</div>
            <div class="v3-ml-prov">${esc(p.missing_policy || '')}${p.filters_summary ? ` · ${h('analysis.details.filters', { value: esc(p.filters_summary) })}` : ''}${p.span_start ? ` · ${h('analysis.details.span', { start: esc(p.span_start), end: esc(p.span_end) })}` : ''}${p.query_ts ? ` · ${h('analysis.details.queried', { value: esc(p.query_ts) })}` : ''}</div></div>
          ${notes.length ? `<div class="v3-ml-section"><div class="v3-ml-section-title">${h('analysis.details.notes')}</div><ul class="v3-ml-notes">${notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul></div>` : ''}`;
    }

    /** Short caption under the chart for an ML result. */
    function chartCaption(result) {
        const analysis = result && result.analysis;
        if (!analysis) return '';
        const params = analysis.params || {};
        if (params.entity) {
            const e = params.entity;
            return `${t('analysis.caption.by', { table: e.table, key: e.entity_key })} · ${(e.features || []).join(', ')} · ${analysis.method_used || ''}`;
        }
        const series = params.series || {};
        const label = `${String(series.agg || 'sum').toUpperCase()}(${series.measure_column || ''})`;
        if (analysis.skill === 'contribution') {
            return `Δ ${t('analysis.caption.by', { table: label, key: (params.dimensions || []).join(', ') })} · ${analysis.method_used || ''}`;
        }
        const grain = grainAdj(series.grain) || series.grain || '';
        const split = series.group_by ? ` · ${t('analysis.summary.split', { column: series.group_by })}` : '';
        return `${label} · ${grain}${split} · ${analysis.method_used || ''}`;
    }

    window.JeenAnalysisUI = {
        stripSegments,
        proposalExpired,
        proposalHtml,
        definitionHtml,
        setupFormHtml,
        summarySentence,
        normalizeChip,
        valuesOf,
        validateValues,
        bindSetupForm,
        collectPatch,
        modelDetailsHtml,
        adjustmentLabel,
        adjustmentsHtml,
        accuracyHtml,
        chartCaption,
        metricText,
        fmtNum,
        fmtPct,
        SKILL_LABEL,
    };
})();
