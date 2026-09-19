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

    const SKILL_LABEL = {
        anomaly_detection: 'anomaly detection', forecast: 'forecast', changepoint: 'changepoint', seasonality: 'seasonality',
        correlation: 'correlation', contribution: 'contribution', clustering: 'clustering', driver_analysis: 'driver analysis',
        regression: 'regression', classification: 'classification', cohort_retention: 'cohort retention',
        experiment_test: 'A/B test',
    };
    const GRAIN_ADJ = { day: 'daily', week: 'weekly', month: 'monthly' };
    const GRAIN_UNIT = { day: 'days', week: 'weeks', month: 'months' };
    const SERIES_SKILLS = ['anomaly_detection', 'forecast', 'changepoint', 'seasonality', 'correlation'];
    // Plain-language egress tier. The letter stays on hover (and in Model details).
    const TIER_META = { A: 'Aggregates only', B: 'Row-level, capped' };
    const TIER_TITLE = {
        A: 'Tier A: SQL rolls the data up first; only per-period totals reach the analysis service.',
        B: 'Tier B: entity rows are sent to the analysis service, capped and audited with the column list.',
    };
    // Shown in place of the server's egress sentence while a field is changed:
    // its numbers ("about 24 monthly totals") belong to the values that were
    // planned, and are recomputed by the run.
    const TIER_NEUTRAL_EGRESS = {
        A: 'Only aggregated totals are sent to the analysis service; no row-level data leaves the database. The exact count is recomputed when you run.',
        B: 'Entity rows are sent to the analysis service, capped and audited with the column list. The exact count is recomputed when you run.',
    };

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
            return validation.coverage != null ? `coverage ${fmtPct(validation.coverage, 0)}` : '';
        }
        if (metric === 'WAPE') return `WAPE ${fmtPct(validation.value)}`;
        if (metric === 'MASE') return `MASE ${Number(validation.value).toFixed(2)}`;
        if (metric === 'r') return `r ${Number(validation.value).toFixed(2)}`;
        if (metric === 'R2') return `R² ${Number(validation.value).toFixed(2)}`;
        if (metric === 'silhouette') return `silhouette ${Number(validation.value).toFixed(2)}`;
        if (metric === 'explained') return `explains ${fmtPct(validation.value, 0)}`;
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
        if (points != null) bits.push(`${points} ${facts.n_entities != null || facts.n_rows != null ? 'rows' : 'pts'}`);
        if (facts.series_count != null) bits.push(`${facts.series_count} series`);
        if (analysis.skill === 'anomaly_detection' && facts.n_flagged != null) bits.push(`${facts.n_flagged} flagged`);
        if (analysis.skill === 'forecast' && facts.horizon != null) bits.push(`horizon ${facts.horizon}`);
        if (analysis.skill === 'changepoint' && facts.n_changepoints != null) bits.push(`${facts.n_changepoints} shift${facts.n_changepoints === 1 ? '' : 's'}`);
        if (analysis.skill === 'seasonality' && facts.strength != null) bits.push(`strength ${Number(facts.strength).toFixed(2)}`);
        if (analysis.skill === 'clustering' && facts.k != null) bits.push(`${facts.k} segments`);
        if (analysis.skill === 'contribution' && facts.delta_pct != null) bits.push(`Δ ${fmtPct(facts.delta_pct)}`);
        if ((analysis.egress || {}).tier === 'B') bits.push('row-level');
        const metric = metricText(analysis.validation);
        if (metric) bits.push(metric);
        const sent = analysis.egress && analysis.egress.rows_sent_to_model;
        if (sent != null) bits.push(`${sent} rows sent to model`);
        const low = Boolean(result.low_confidence || analysis.low_confidence);
        // The method is named in the strip (never in the headline), kept short;
        // the full label is on hover and in Model details.
        const method = String(analysis.method_used || '');
        const shortMethod = method.length > 34 ? `${method.slice(0, 32)}…` : method;
        return `<span class="v3-skill-chip" title="${esc(method)}">${esc(SKILL_LABEL[analysis.skill] || analysis.skill)}</span>
          ${method ? `<span class="v3-result-meta v3-ml-method" title="${esc(method)}">${esc(shortMethod)}</span>` : ''}
          <span class="v3-result-meta v3-ml-meta">${esc(bits.join(' · '))}</span>
          ${low ? '<span class="v3-lowconf-pill" title="A guard was overridden for this run; treat the result as indicative.">low confidence</span>' : ''}
          ${hasDefinition(analysis) ? '<button type="button" class="v3-text-btn v3-ml-edit" data-ml-edit aria-expanded="false">Edit setup</button>' : ''}`;
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
        c.group = c.group ? String(c.group) : 'Setup';
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
            return GRAIN_UNIT[source] || (source ? String(source) : '');
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
        return c.min != null ? `at least ${c.min}` : `at most ${c.max}`;
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
                <span class="v3-ml-count" data-count>${chosen.length}${boundsHint(c) ? ` of ${esc(boundsHint(c))}` : ''}</span>
              </div>`;
        } else if (isSelect(c)) {
            const opts = c.options.map((o) => `<option value="${esc(o)}"${String(o) === String(value) ? ' selected' : ''}>${esc(optionLabel(c, o))}</option>`).join('');
            const extra = c.options.some((o) => String(o) === String(value)) || value === ''
                ? '' : `<option value="${esc(value)}" selected>${esc(optionLabel(c, value))}</option>`;
            const placeholder = value === '' ? '<option value="" selected disabled>Choose…</option>' : '';
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
          <label class="v3-ml-label" id="${id}-label" for="${id}">${esc(c.label)}<span class="v3-ml-badge" data-badge hidden>changed</span></label>
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
            const unit = GRAIN_UNIT[grain] || (grain ? `${grain}s` : 'periods');
            let head = `${esc(agg)}(<bdi>${esc(v.measure_column || '…')}</bdi>)`;
            if (skill === 'correlation' && filled('other_measure_column')) head += ` vs <bdi>${esc(v.other_measure_column)}</bdi>`;
            parts.push(head);
            if (grain) parts.push(`per ${esc(grain)}`);
            if (filled('window')) parts.push(`last ${esc(v.window)} ${esc(unit)}`);
            if (skill === 'forecast') {
                if (filled('horizon')) parts.push(`${esc(v.horizon)} ${esc(unit)} ahead`);
                if (filled('interval')) parts.push(`${esc(show('interval'))} interval`);
            }
            if (skill === 'anomaly_detection' && filled('sensitivity')) parts.push(`${esc(show('sensitivity'))} sensitivity`);
            if (skill === 'changepoint' && filled('max_changepoints')) parts.push(`up to ${esc(v.max_changepoints)} breaks`);
            if (skill === 'correlation' && filled('max_lag')) parts.push(`lag up to ${esc(v.max_lag)} ${esc(unit)}`);
            if (filled('method')) parts.push(`${esc(show('method'))} model`);
            if (filled('group_by') && String(v.group_by).toLowerCase() !== 'none') parts.push(`per <bdi>${esc(v.group_by)}</bdi>`);
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
                if (c.required) errors[c.key] = 'Required.';
                return;
            }
            const unit = unitText(c, v);
            const withUnit = (n) => `${n}${unit ? ` ${unit}` : ''}`;
            if (c.kind === 'multiselect') {
                const n = listOf(value).length;
                if (c.min != null && n < c.min) errors[c.key] = `Pick at least ${c.min}.`;
                else if (c.max != null && n > c.max) errors[c.key] = `Pick at most ${c.max}.`;
                return;
            }
            if (isNumber(c)) {
                const n = Number(value);
                if (!Number.isFinite(n)) { errors[c.key] = 'Enter a number.'; return; }
                if (c.min != null && n < c.min) errors[c.key] = `At least ${withUnit(c.min)}.`;
                else if (c.max != null && n > c.max) errors[c.key] = `At most ${withUnit(c.max)}.`;
                return;
            }
            if (c.kind === 'date' && Number.isNaN(Date.parse(String(value)))) errors[c.key] = 'Enter a date.';
        });
        return { ok: Object.keys(errors).length === 0, errors };
    }

    function tierMetaHtml(tier, seconds) {
        const bits = [];
        if (tier) bits.push(TIER_META[tier] || `tier ${tier}`);
        if (seconds) bits.push(`~${seconds}s`);
        if (!bits.length) return '';
        return `<span class="v3-ml-tiermeta" title="${esc(TIER_TITLE[tier] || '')}">${esc(bits.join(' · '))}</span>`;
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
            <span class="v3-ml-phase">Setup</span>
            <span class="v3-skill-chip">${esc(SKILL_LABEL[skill] || skill || 'analysis')}</span>
            <span class="v3-ml-summary" data-summary>${summarySentence(skill, valuesOf(chips), chips)}</span>
          </div>
          <div class="v3-ml-panel">
            <p class="v3-ml-reading">This is how the answer above was produced. Change anything and re-run; the current answer stays.</p>
            ${setupFormHtml(chips, 'ml-setup')}
            ${definition.egress_summary ? `<p class="v3-ml-egress" data-egress data-egress-original="${esc(definition.egress_summary)}">${esc(definition.egress_summary)}</p>` : ''}
            <div class="v3-ml-actions">
              <button type="button" class="v3-ml-run" data-run>Re-run</button>
              <button type="button" class="v3-text-btn" data-cancel>Cancel</button>
              <span class="v3-ml-note" data-note aria-live="polite">Change a value to re-run.</span>
              <span class="v3-ml-errsum" data-errsum aria-live="polite"></span>
              <span class="v3-ml-footmeta">
                <button type="button" class="v3-text-btn v3-ml-resetall" data-reset-all hidden>Reset all</button>
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

    function guardList(guardResults, onlyFailed) {
        const rows = (guardResults || []).filter((g) => !onlyFailed || !g.passed);
        if (!rows.length) return '';
        return `<ul class="v3-ml-guards">${rows.map((g) => `<li class="${g.passed ? 'is-ok' : 'is-fail'}"><span class="v3-ml-guard-name">${esc(g.name)}</span><span>${esc(g.detail)}</span></li>`).join('')}</ul>`;
    }

    function exitButtons(options) {
        return (options || []).map((o, i) => {
            const kind = o.kind || 'patch';
            const cls = kind === 'override' ? 'v3-ml-exit is-override' : o.recommended ? 'v3-ml-exit is-recommended' : 'v3-ml-exit';
            return `<button type="button" class="${cls}" data-exit="${i}" data-exit-kind="${esc(kind)}" title="${esc(o.description || '')}">${esc(o.label)}${o.recommended ? ' <small>recommended</small>' : ''}</button>`;
        }).join('');
    }

    /**
     * The card for a stopped run. `kind` = confirm | clarify | guard.
     * Handlers are bound by the controller on `[data-run]`, `[data-exit]`,
     * `[data-sql-instead]`, `[data-remember]`.
     */
    function proposalHtml(proposal) {
        const kind = proposal.kind;
        const series = (proposal.params || {}).series || {};
        const skill = esc(SKILL_LABEL[proposal.skill] || proposal.skill || 'analysis');
        const head = `<div class="v3-ml-card-head">
            <span class="v3-skill-chip">${skill}</span>
            <span class="v3-ml-kind">${kind === 'confirm' ? 'confirm before running' : kind === 'clarify' ? 'one thing to clarify' : 'guard'}</span>
            ${proposal.tier ? `<span class="v3-ml-tier">tier ${esc(proposal.tier)} · ${proposal.tier === 'A' ? 'aggregate' : 'row-level'}</span>` : ''}
          </div>`;
        if (kind === 'confirm') {
            const chips = (proposal.chips || []).map(normalizeChip);
            const grain = GRAIN_ADJ[series.grain] || series.grain || '';
            const summary = summarySentence(proposal.skill, valuesOf(chips), chips);
            const egress = proposal.egress_summary || `SQL rolls the measure up to ${grain} totals; only those rows are sent to the analysis service.`;
            return `<div class="v3-ml-card is-confirm" data-skill="${esc(proposal.skill || '')}" data-tier="${esc(proposal.tier || '')}">
              <div class="v3-ml-plan" title="${esc(proposal.message)}">
                <span class="v3-ml-phase">Planning</span>
                <span class="v3-skill-chip">${skill}</span>
                <span class="v3-ml-summary" data-summary>${summary || esc(proposal.message)}</span>
              </div>
              <div class="v3-ml-panel">
                ${setupFormHtml(chips, 'ml-confirm')}
                <p class="v3-ml-egress" data-egress data-egress-original="${esc(egress)}">${esc(egress)}</p>
                <div class="v3-ml-actions">
                  <button type="button" class="v3-ml-run" data-run>Run</button>
                  <button type="button" class="v3-ml-alt" data-switch-skill title="Return to the composer and ask in other words">Rephrase the question</button>
                  <button type="button" class="v3-text-btn v3-ml-sql" data-sql-instead>Answer with SQL instead</button>
                  <span class="v3-ml-errsum" data-errsum aria-live="polite"></span>
                  <span class="v3-ml-footmeta">
                    <button type="button" class="v3-text-btn v3-ml-resetall" data-reset-all hidden>Reset all</button>
                    ${tierMetaHtml(proposal.tier, proposal.estimated_seconds)}
                  </span>
                </div>
                <label class="v3-ml-remember"><input type="checkbox" data-remember> Don't ask again for ${skill} on this connection</label>
              </div>
            </div>`;
        }
        if (kind === 'clarify') {
            return `<div class="v3-ml-card is-clarify">${head}
              <p class="v3-ml-message">${esc(proposal.message)}</p>
              <div class="v3-ml-exits">${exitButtons(proposal.options)}</div>
              <div class="v3-ml-actions"><button type="button" class="v3-text-btn" data-sql-instead>Answer with SQL instead</button></div>
            </div>`;
        }
        return `<div class="v3-ml-card is-guard">${head}
          <p class="v3-ml-message v3-ml-refusal">${esc(proposal.message)}</p>
          ${guardList(proposal.guard_results, true)}
          <div class="v3-ml-exits">${exitButtons(proposal.options)}</div>
          <p class="v3-ml-egress">0 rows sent to the model — a blocked run reads no series.</p>
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
                egressEl.textContent = dirty ? (TIER_NEUTRAL_EGRESS[tier] || TIER_NEUTRAL_EGRESS.A) : egressEl.dataset.egressOriginal;
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
                if (errsum) errsum.textContent = keys.length ? `${keys.length} field${keys.length === 1 ? '' : 's'} need${keys.length === 1 ? 's' : ''} a look.` : '';
                const first = list.find((c) => keys.includes(c.key));
                if (first) focusField(fieldOf(cardEl, first.key));
                return keys.length > 0;
            },
            /** A 422 from the server: on its field when it names one, else false. */
            showServerError(detail) {
                const field = detail && typeof detail === 'object' ? detail.field : null;
                const message = detail && typeof detail === 'object' ? detail.message : String(detail || '');
                if (field && setFieldError(cardEl, field, message || 'Invalid value.')) {
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

    function modelDetailsHtml(result) {
        const analysis = result && result.analysis;
        if (!analysis || !analysis.skill) {
            return '<div class="v3-dock-empty">Model details appear here for anomaly detection and forecast answers.</div>';
        }
        const v = analysis.validation || {};
        const d = analysis.details || {};
        const p = analysis.provenance || {};
        const e = analysis.engine || {};
        const params = analysis.params || {};
        const series = params.series || {};
        const stats = [
            kv('Method', analysis.method_used || d.method_used || '—'),
            kv(v.metric || 'Metric', v.value == null ? '—' : (v.metric === 'WAPE' ? fmtPct(v.value) : Number(v.value).toFixed(3))),
            kv('Coverage', v.coverage == null ? '—' : `${fmtPct(v.coverage, 0)} of ${v.coverage_n || 0}`),
            kv('Season', (d.seasonal_periods || []).length ? `m=${d.seasonal_periods.join(',')}` : 'none confirmed'),
            kv('Rows sent', (analysis.egress || {}).rows_sent_to_model == null ? '—' : analysis.egress.rows_sent_to_model),
            kv('Engine', `${e.name || '—'} ${e.version || ''}`.trim()),
        ].join('');
        const candidates = (d.candidates || []).map((c) => `<tr class="${c.selected ? 'is-selected' : ''}">
            <td>${esc(c.name)}${c.is_baseline ? ' <small>baseline</small>' : ''}${c.selected ? ' <small>selected</small>' : ''}</td>
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
        const notes = (d.notes || []).concat(analysis.caveats || []);
        const diff = analysis.param_diff && Object.keys(analysis.param_diff).length
            ? `<div class="v3-ml-section"><div class="v3-ml-section-title">Changed from the previous run</div><div class="v3-ml-diff">${Object.entries(analysis.param_diff).map(([k, c]) => `<span class="v3-ml-diff-chip">${esc(k)}: <s>${esc(c.from == null ? '—' : c.from)}</s> → ${esc(c.to == null ? '—' : c.to)}</span>`).join('')}</div></div>`
            : '';
        return `<div class="v3-stats">${stats}</div>
          ${result.low_confidence || analysis.low_confidence ? '<div class="v3-ml-lowconf-note">Low confidence: a guard was overridden for this run. The flag travels with pins, exports and history.</div>' : ''}
          ${diff}
          <div class="v3-ml-grid">
            <div class="v3-ml-section"><div class="v3-ml-section-title">Candidates${v.basis ? ` · ${esc(v.basis)}` : ''}</div>
              <table class="v3-ml-table"><thead><tr><th>model</th><th>metric</th><th>value</th></tr></thead><tbody>${candidates || '<tr><td colspan="3">—</td></tr>'}</tbody></table></div>
            <div class="v3-ml-section"><div class="v3-ml-section-title">Parameters</div>
              <table class="v3-ml-table"><tbody>${paramRows}</tbody></table></div>
          </div>
          <div class="v3-ml-section"><div class="v3-ml-section-title">Guards</div>${guards || '<div class="v3-dock-empty">none recorded</div>'}</div>
          <div class="v3-ml-section"><div class="v3-ml-section-title">Data</div>
            <div class="v3-ml-prov">${esc(p.missing_policy || '')}${p.filters_summary ? ` · filters: ${esc(p.filters_summary)}` : ''}${p.span_start ? ` · span ${esc(p.span_start)} → ${esc(p.span_end)}` : ''}${p.query_ts ? ` · queried ${esc(p.query_ts)}` : ''}</div></div>
          ${notes.length ? `<div class="v3-ml-section"><div class="v3-ml-section-title">Notes</div><ul class="v3-ml-notes">${notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul></div>` : ''}`;
    }

    /** Short caption under the chart for an ML result. */
    function chartCaption(result) {
        const analysis = result && result.analysis;
        if (!analysis) return '';
        const params = analysis.params || {};
        if (params.entity) {
            const e = params.entity;
            return `${e.table} by ${e.entity_key} · ${(e.features || []).join(', ')} · ${analysis.method_used || ''}`;
        }
        const series = params.series || {};
        const label = `${String(series.agg || 'sum').toUpperCase()}(${series.measure_column || ''})`;
        if (analysis.skill === 'contribution') {
            return `Δ ${label} by ${(params.dimensions || []).join(', ')} · ${analysis.method_used || ''}`;
        }
        const grain = GRAIN_ADJ[series.grain] || series.grain || '';
        const split = series.group_by ? ` · per ${series.group_by}` : '';
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
        chartCaption,
        metricText,
        fmtNum,
        fmtPct,
        SKILL_LABEL,
    };
})();
