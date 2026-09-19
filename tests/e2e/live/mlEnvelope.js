// @ts-check
// Data-level checks on an ML answer: the `analysis` field of the QueryResponse
// is the artifact view of ResultEnvelope (src/analysis/contracts.py) —
// params, validation, guard_results, low_confidence, egress, engine, details,
// facts, chart_spec, headline, caveats. A case declares bounds; this module
// returns the list of violations (empty = pass) so one assertion can print
// them all.

/**
 * @typedef {Object} EnvelopeChecks
 * @property {Record<string, any>} [params]      dotted path → exact value, or [min, max] for a numeric range
 * @property {RegExp} [metric]                   validation.metric must match (e.g. /WAPE|MASE/)
 * @property {string[]} [band]                   validation.band must be one of (good | fair | poor | n/a)
 * @property {number} [maxMetric]                validation.value must be <= (e.g. WAPE 60 = 60 %)
 * @property {'A'|'B'} [tier]                    egress.tier
 * @property {number} [maxRowsSent]              egress.rows_sent_to_model <=
 * @property {boolean} [lowConfidence]           expected low_confidence flag
 * @property {boolean} [guardsPassed]            every guard_results[].passed must be true
 * @property {Record<string, any>} [facts]       facts key (dotted) → exact value or [min, max]
 * @property {string[]} [chartTypes]             chart_spec.chart_type must be one of
 * @property {number} [minRows]                  the result grid (rows in the QueryResponse) has at least this many rows
 */

function getPath(object, dotted) {
  return String(dotted).split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), object);
}

function inRange(value, range) {
  return typeof value === 'number' && Number.isFinite(value) && value >= range[0] && value <= range[1];
}

/**
 * @param {any} analysis   `raw.analysis`
 * @param {EnvelopeChecks} checks
 * @param {{ rows?: any[] }} [context]
 * @returns {string[]} problems
 */
function checkEnvelope(analysis, checks = {}, context = {}) {
  const problems = [];
  if (!analysis || typeof analysis !== 'object') return ['no `analysis` envelope on the answer'];

  // Contract shape the UI depends on, regardless of case.
  if (!analysis.skill) problems.push('envelope.skill is empty');
  if (!analysis.details || !analysis.details.method_used) problems.push('details.method_used is empty');
  if (!analysis.validation || typeof analysis.validation.metric !== 'string') problems.push('validation.metric is missing');
  if (!analysis.egress || typeof analysis.egress.rows_sent_to_model !== 'number') problems.push('egress.rows_sent_to_model is missing');
  if (!Array.isArray(analysis.guard_results)) problems.push('guard_results is not a list');
  if (!analysis.chart_spec || !analysis.chart_spec.chart_type) problems.push('chart_spec.chart_type is missing');
  if (typeof analysis.headline !== 'string' || !analysis.headline.trim()) problems.push('headline is empty');

  for (const [dotted, want] of Object.entries(checks.params || {})) {
    const have = getPath(analysis.params, dotted);
    if (Array.isArray(want) && want.length === 2 && typeof want[0] === 'number') {
      if (!inRange(Number(have), want)) problems.push(`params.${dotted}=${JSON.stringify(have)} not in [${want}]`);
    } else if (have !== want) {
      problems.push(`params.${dotted}=${JSON.stringify(have)} != ${JSON.stringify(want)}`);
    }
  }
  const validation = analysis.validation || {};
  if (checks.metric && !checks.metric.test(String(validation.metric || ''))) problems.push(`validation.metric "${validation.metric}" !~ ${checks.metric}`);
  if (checks.band && !checks.band.includes(String(validation.band))) problems.push(`validation.band "${validation.band}" not in ${JSON.stringify(checks.band)}`);
  if (typeof checks.maxMetric === 'number' && typeof validation.value === 'number' && validation.value > checks.maxMetric) {
    problems.push(`validation.value ${validation.value} > ${checks.maxMetric}`);
  }
  const egress = analysis.egress || {};
  if (checks.tier && egress.tier !== checks.tier) problems.push(`egress.tier "${egress.tier}" != "${checks.tier}"`);
  if (typeof checks.maxRowsSent === 'number' && egress.rows_sent_to_model > checks.maxRowsSent) {
    problems.push(`egress.rows_sent_to_model ${egress.rows_sent_to_model} > ${checks.maxRowsSent}`);
  }
  if (typeof checks.lowConfidence === 'boolean' && Boolean(analysis.low_confidence) !== checks.lowConfidence) {
    problems.push(`low_confidence=${Boolean(analysis.low_confidence)} != ${checks.lowConfidence}`);
  }
  if (checks.guardsPassed) {
    const failed = (analysis.guard_results || []).filter((g) => g && g.passed === false).map((g) => `${g.name}: ${g.detail}`);
    if (failed.length) problems.push(`guards failed: ${failed.join('; ')}`);
  }
  for (const [dotted, want] of Object.entries(checks.facts || {})) {
    const have = getPath(analysis.facts, dotted);
    if (Array.isArray(want) && want.length === 2 && typeof want[0] === 'number') {
      if (!inRange(Number(have), want)) problems.push(`facts.${dotted}=${JSON.stringify(have)} not in [${want}]`);
    } else if (have !== want) {
      problems.push(`facts.${dotted}=${JSON.stringify(have)} != ${JSON.stringify(want)}`);
    }
  }
  if (checks.chartTypes && analysis.chart_spec && !checks.chartTypes.includes(analysis.chart_spec.chart_type)) {
    problems.push(`chart_spec.chart_type "${analysis.chart_spec.chart_type}" not in ${JSON.stringify(checks.chartTypes)}`);
  }
  if (typeof checks.minRows === 'number') {
    const rows = Array.isArray(context.rows) ? context.rows.length : 0;
    if (rows < checks.minRows) problems.push(`result rows ${rows} < ${checks.minRows}`);
  }
  return problems;
}

/** Compact summary of an envelope for annotations and the scorecard. */
function summarizeEnvelope(analysis) {
  if (!analysis || typeof analysis !== 'object') return null;
  const v = analysis.validation || {};
  const e = analysis.egress || {};
  return {
    skill: analysis.skill,
    method: analysis.details && analysis.details.method_used,
    metric: v.metric, value: v.value, band: v.band,
    tier: e.tier, rows_sent: e.rows_sent_to_model,
    low_confidence: Boolean(analysis.low_confidence),
    guards: (analysis.guard_results || []).map((g) => `${g.name}:${g.passed ? 'ok' : 'FAIL'}`),
    chart: analysis.chart_spec && analysis.chart_spec.chart_type,
  };
}

module.exports = { checkEnvelope, summarizeEnvelope, getPath };
