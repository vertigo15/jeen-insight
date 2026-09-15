/**
 * ML skills — answer-pane rendering helpers.
 *
 * Pure functions that turn the API's `proposal` / `analysis` objects into
 * markup for the existing workspace surfaces (status strip, placeholder card,
 * dock). No fetch, no state: the WorkspaceController owns I/O and binds the
 * handlers. Tokens only — colours come from design-tokens.css classes.
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
          ${low ? '<span class="v3-lowconf-pill" title="A guard was overridden for this run; treat the result as indicative.">low confidence</span>' : ''}`;
    }

    /** True when a stored proposal can no longer be resumed. */
    function proposalExpired(proposal, now) {
        if (!proposal || !proposal.proposal_id) return true;
        if (!proposal.expires_at) return false;
        const t = new Date(proposal.expires_at).getTime();
        return Number.isFinite(t) && t <= (now || Date.now());
    }

    // ── Confirm / clarify / guard card ────────────────────────────────────────

    function chipControl(chip) {
        const key = esc(chip.key);
        const value = chip.value == null ? '' : chip.value;
        if (Array.isArray(chip.options) && chip.options.length) {
            const opts = chip.options.map((o) => `<option value="${esc(o)}"${String(o) === String(value) ? ' selected' : ''}>${esc(o)}</option>`).join('');
            const extra = chip.options.some((o) => String(o) === String(value)) ? '' : `<option value="${esc(value)}" selected>${esc(value)}</option>`;
            return `<label class="v3-ml-chip"><span>${esc(chip.label)}</span><select data-chip="${key}" data-original="${esc(value)}">${extra}${opts}</select></label>`;
        }
        const numeric = typeof value === 'number';
        return `<label class="v3-ml-chip"><span>${esc(chip.label)}</span><input data-chip="${key}" data-original="${esc(value)}" type="${numeric ? 'number' : 'text'}" value="${esc(value)}"${numeric ? ' step="any"' : ''}></label>`;
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
            const chips = (proposal.chips || []).map(chipControl).join('');
            const grain = GRAIN_ADJ[series.grain] || series.grain || '';
            return `<div class="v3-ml-card is-confirm">${head}
              <p class="v3-ml-message">${esc(proposal.message)}</p>
              <div class="v3-ml-chips" data-chip-row>${chips}</div>
              <p class="v3-ml-egress">${esc(proposal.egress_summary || `SQL rolls the measure up to ${grain} totals; only those rows are sent to the analysis service.`)}</p>
              ${guardList(proposal.guard_results, false)}
              <div class="v3-ml-actions">
                <button type="button" class="v3-ml-run" data-run>Run ${proposal.estimated_seconds ? `<small>~${esc(proposal.estimated_seconds)}s</small>` : ''}</button>
                <button type="button" class="v3-text-btn" data-sql-instead>Answer with SQL instead</button>
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

    /** Read edited chips back into an allowlisted params_patch. */
    function collectPatch(cardEl) {
        const patch = {};
        if (!cardEl) return patch;
        cardEl.querySelectorAll('[data-chip]').forEach((el) => {
            const key = el.dataset.chip;
            const original = el.dataset.original;
            let value = el.value;
            if (String(value) === String(original)) return;
            if (el.type === 'number' || ['window', 'horizon'].includes(key)) {
                const n = Number(value);
                if (!Number.isFinite(n)) return;
                value = ['window', 'horizon'].includes(key) ? Math.round(n) : n;
            }
            patch[key] = value;
        });
        return patch;
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
        const nested = params.series || params.entity || {};
        const paramRows = Object.entries({ ...nested, ...Object.fromEntries(Object.entries(params).filter(([k]) => k !== 'series' && k !== 'entity')) })
            .filter(([k, val]) => val != null && val !== '' && !(Array.isArray(val) && !val.length) && !['schema_name', 'catalog', 'timezone'].includes(k))
            .map(([k, val]) => `<tr><td>${esc(k)}</td><td class="v3-mono">${esc(Array.isArray(val) ? val.map((f) => `${f.column} ${f.op} ${Array.isArray(f.value) ? f.value.join(', ') : f.value}`).join('; ') : val)}</td></tr>`).join('');
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
        collectPatch,
        modelDetailsHtml,
        chartCaption,
        metricText,
        fmtNum,
        fmtPct,
        SKILL_LABEL,
    };
})();
