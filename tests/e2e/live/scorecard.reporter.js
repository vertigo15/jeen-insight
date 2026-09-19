// @ts-check
// Scorecard reporter for the live suite: turns Playwright results plus the
// typed annotations the specs attach (kp_tier, kp_verdict, chart_type,
// latency, ml_envelope, …) into
//   test-results-live/scorecard.json   machine-readable, one record per test
//   test-results-live/scorecard.md     the human summary
// and enforces optional gates:
//   LIVE_MIN_PASS_RATE   fraction of non-infra, non-skipped tests that must pass (e.g. 0.9)
//   LIVE_MIN_SQL_EQUIV   fraction of graded @kp cases whose SQL must pass at the run's strictness
//   LIVE_MAX_P95_MS      p95 wall time of answered turns (ms) across the run
// Infrastructure failures ("Backend unavailable", 502/503, dead API) count as
// `infra`, not as product failures, and never satisfy nor break a gate.
const fs = require('fs');
const path = require('path');

const INFRA = /Infrastructure failure|Backend unavailable|Invalid internal token|ECONNREFUSED|Max retries exceeded|\b50[234]\b|live stack not reachable/i;
const AREA_TAGS = ['kp', 'conv', 'ml', 'sql', 'resilience', 'features', 'history', 'extras', 'admin'];
const TYPE_TAGS = ['smoke', 'e2e', 'feature', 'regression', 'security'];

function pct(n, d) { return d ? `${((n / d) * 100).toFixed(1)}%` : 'n/a'; }
function percentile(values, p) {
  const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[index];
}
function parseJson(text) { try { return JSON.parse(text); } catch (_) { return text; } }

class ScorecardReporter {
  constructor(options = {}) {
    this.outputDir = options.outputDir || 'test-results-live';
    /** @type {any[]} */
    this.records = [];
    this.startedAt = new Date();
    this.env = {
      app: process.env.LIVE_APP_URL || 'http://localhost:8501',
      connection: process.env.LIVE_CONNECTION || 'AdventureWorksDW',
      strict: process.env.LIVE_KP_STRICT || 'structural',
      only: process.env.LIVE_ONLY || '',
      sweep: process.env.LIVE_KP_ALL === '1',
    };
    this.gates = {
      minPassRate: process.env.LIVE_MIN_PASS_RATE ? Number(process.env.LIVE_MIN_PASS_RATE) : null,
      minSqlEquiv: process.env.LIVE_MIN_SQL_EQUIV ? Number(process.env.LIVE_MIN_SQL_EQUIV) : null,
      maxP95Ms: process.env.LIVE_MAX_P95_MS ? Number(process.env.LIVE_MAX_P95_MS) : null,
    };
    this.gateFailures = [];
  }

  printsToStdio() { return false; }

  onBegin(config) {
    const base = config.configFile ? path.dirname(config.configFile) : process.cwd();
    this.outputDir = path.resolve(base, this.outputDir);
  }

  /** Called once per attempt; the record for a test id is replaced by the latest attempt. */
  onTestEnd(test, result) {
    if (test.parent && test.parent.project() && test.parent.project().name === 'setup') return;
    const attempts = test.results.length;
    const final = result;
    // Static annotations live on the test, runtime ones on the result (and may be mirrored); dedupe.
    const seen = new Set();
    /** @type {Record<string, any>} */
    const scalars = {};
    /** @type {Record<string, any[]>} */
    const lists = { latency: [], finding: [], note: [] };
    for (const a of [...(test.annotations || []), ...(final.annotations || [])]) {
      if (!a || !a.type) continue;
      const key = `${a.type}\u0000${a.description ?? ''}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const value = parseJson(a.description ?? '');
      if (a.type in lists) lists[a.type].push(value);
      else scalars[a.type] = value; // last write wins (a retry overrides the earlier attempt)
    }
    const annotations = { ...scalars, ...lists };
    const tags = (test.tags || []).map((t) => String(t).replace(/^@/, ''));
    const errorText = (final.errors || []).map((e) => e.message || '').join('\n');
    let status = final.status === 'passed' ? 'passed' : final.status === 'skipped' ? 'skipped' : final.status === 'timedOut' ? 'failed' : final.status;
    if (status === 'failed' && INFRA.test(errorText)) status = 'infra';
    if (status === 'passed' && attempts > 1) status = 'flaky';
    const latency = annotations.latency.filter((l) => l && typeof l === 'object');
    const record = {
      id: test.id,
      title: test.titlePath().slice(2).join(' › '),
      file: path.basename(test.location.file),
      line: test.location.line,
      // Area tag when present; a test that only has a type tag (smoke, security) is grouped under it.
      area: AREA_TAGS.find((t) => tags.includes(t)) || TYPE_TAGS.find((t) => tags.includes(t)) || 'other',
      types: TYPE_TAGS.filter((t) => tags.includes(t)),
      tags,
      status,
      attempts,
      duration_ms: test.results.reduce((s, r) => s + (r.duration || 0), 0),
      question: annotations.question || null,
      catalog_source: annotations.catalog_source || null,
      kp_tier: annotations.kp_tier || null,
      kp_verdict: annotations.kp_verdict || null,
      kp_overlap: annotations.kp_overlap != null ? Number(annotations.kp_overlap) : null,
      answer_grounded: annotations.answer_grounded || null,
      chart_type: annotations.chart_type || null,
      ml_envelope: annotations.ml_envelope || null,
      latency,
      findings: annotations.finding,
      notes: annotations.note,
      // Strip ANSI colour codes so the markdown stays readable.
      error: status === 'passed' ? null : errorText.replace(/\u001b\[[0-9;]*m/g, '').split('\n').slice(0, 12).join('\n').slice(0, 1500),
    };
    const existing = this.records.findIndex((r) => r.id === record.id);
    if (existing >= 0) this.records[existing] = record; else this.records.push(record);
  }

  summarize() {
    const rows = this.records;
    const scored = rows.filter((r) => r.status !== 'skipped' && r.status !== 'infra');
    const passed = scored.filter((r) => r.status === 'passed' || r.status === 'flaky');
    const byArea = {};
    for (const area of [...AREA_TAGS, ...TYPE_TAGS, 'other']) {
      const set = rows.filter((r) => r.area === area);
      if (!set.length) continue;
      byArea[area] = this._rate(set);
    }
    const byType = {};
    for (const type of TYPE_TAGS) {
      const set = rows.filter((r) => r.types.includes(type));
      if (set.length) byType[type] = this._rate(set);
    }
    const graded = rows.filter((r) => r.kp_verdict === 'pass' || r.kp_verdict === 'fail');
    const tiers = {};
    for (const r of graded) tiers[r.kp_tier] = (tiers[r.kp_tier] || 0) + 1;
    const sqlPass = graded.filter((r) => r.kp_verdict === 'pass').length;
    const walls = rows.flatMap((r) => r.latency.map((l) => Number(l.wall_ms))).filter(Number.isFinite);
    const llm = rows.flatMap((r) => r.latency.map((l) => Number(l.llm_latency_ms))).filter(Number.isFinite);
    const exec = rows.flatMap((r) => r.latency.map((l) => Number(l.execution_time_ms))).filter(Number.isFinite);
    const latencyByArea = {};
    for (const area of Object.keys(byArea)) {
      const w = rows.filter((r) => r.area === area).flatMap((r) => r.latency.map((l) => Number(l.wall_ms))).filter(Number.isFinite);
      if (w.length) latencyByArea[area] = { n: w.length, p50_ms: percentile(w, 50), p95_ms: percentile(w, 95) };
    }
    const grounded = rows.filter((r) => r.answer_grounded);
    const summary = {
      generated_at: new Date().toISOString(),
      started_at: this.startedAt.toISOString(),
      env: this.env,
      totals: {
        tests: rows.length,
        passed: rows.filter((r) => r.status === 'passed').length,
        flaky: rows.filter((r) => r.status === 'flaky').length,
        failed: rows.filter((r) => r.status === 'failed').length,
        infra: rows.filter((r) => r.status === 'infra').length,
        skipped: rows.filter((r) => r.status === 'skipped').length,
        pass_rate: scored.length ? passed.length / scored.length : null,
      },
      by_area: byArea,
      by_type: byType,
      sql_equivalence: { graded: graded.length, pass: sqlPass, pass_rate: graded.length ? sqlPass / graded.length : null, by_tier: tiers, strictness: this.env.strict },
      answer_grounded: { checked: grounded.length, yes: grounded.filter((r) => String(r.answer_grounded).startsWith('yes')).length },
      latency: {
        answered_turns: walls.length,
        wall_p50_ms: percentile(walls, 50), wall_p95_ms: percentile(walls, 95),
        llm_p50_ms: percentile(llm, 50), llm_p95_ms: percentile(llm, 95),
        exec_p50_ms: percentile(exec, 50), exec_p95_ms: percentile(exec, 95),
        by_area: latencyByArea,
      },
      gates: this.gates,
      gate_failures: this.gateFailures,
    };
    return summary;
  }

  _rate(set) {
    const scored = set.filter((r) => r.status !== 'skipped' && r.status !== 'infra');
    const ok = scored.filter((r) => r.status === 'passed' || r.status === 'flaky').length;
    return {
      tests: set.length, scored: scored.length, passed: ok,
      failed: scored.length - ok,
      infra: set.filter((r) => r.status === 'infra').length,
      skipped: set.filter((r) => r.status === 'skipped').length,
      pass_rate: scored.length ? ok / scored.length : null,
    };
  }

  applyGates(summary) {
    const g = this.gates;
    if (g.minPassRate != null && summary.totals.pass_rate != null && summary.totals.pass_rate < g.minPassRate) {
      this.gateFailures.push(`pass rate ${pct(summary.totals.passed + summary.totals.flaky, summary.totals.tests - summary.totals.skipped - summary.totals.infra)} < required ${pct(g.minPassRate, 1)}`);
    }
    if (g.minSqlEquiv != null && summary.sql_equivalence.pass_rate != null && summary.sql_equivalence.pass_rate < g.minSqlEquiv) {
      this.gateFailures.push(`SQL equivalence ${pct(summary.sql_equivalence.pass, summary.sql_equivalence.graded)} < required ${pct(g.minSqlEquiv, 1)} at strictness ${this.env.strict}`);
    }
    if (g.maxP95Ms != null && summary.latency.wall_p95_ms != null && summary.latency.wall_p95_ms > g.maxP95Ms) {
      this.gateFailures.push(`p95 wall time ${Math.round(summary.latency.wall_p95_ms)} ms > allowed ${g.maxP95Ms} ms`);
    }
    summary.gate_failures = this.gateFailures;
  }

  markdown(summary) {
    const t = summary.totals;
    const lines = [];
    lines.push(`# Live scorecard — ${summary.generated_at}`);
    lines.push('');
    lines.push(`Stack: ${summary.env.app} · connection: ${summary.env.connection} · SQL strictness: ${summary.env.strict}${summary.env.only ? ` · LIVE_ONLY=${summary.env.only}` : ''}${summary.env.sweep ? ' · knowledge-pair sweep' : ''}`);
    lines.push('');
    lines.push(`**${t.passed} passed**, ${t.flaky} flaky, **${t.failed} failed**, ${t.infra} infra, ${t.skipped} skipped — pass rate ${pct(t.passed + t.flaky, t.tests - t.skipped - t.infra)}`);
    if (summary.gate_failures.length) {
      lines.push('');
      lines.push('## Gate failures');
      for (const f of summary.gate_failures) lines.push(`- ${f}`);
    }
    lines.push('');
    lines.push('## By area');
    lines.push('');
    lines.push('| area | tests | passed | failed | infra | skipped | pass rate | wall p50 | wall p95 |');
    lines.push('|---|---:|---:|---:|---:|---:|---:|---:|---:|');
    for (const [area, r] of Object.entries(summary.by_area)) {
      const lat = summary.latency.by_area[area];
      lines.push(`| ${area} | ${r.tests} | ${r.passed} | ${r.failed} | ${r.infra} | ${r.skipped} | ${pct(r.passed, r.scored)} | ${lat ? `${(lat.p50_ms / 1000).toFixed(0)}s` : ''} | ${lat ? `${(lat.p95_ms / 1000).toFixed(0)}s` : ''} |`);
    }
    lines.push('');
    lines.push('## By type');
    lines.push('');
    lines.push('| type | tests | passed | failed | infra | skipped | pass rate |');
    lines.push('|---|---:|---:|---:|---:|---:|---:|');
    for (const [type, r] of Object.entries(summary.by_type)) {
      lines.push(`| ${type} | ${r.tests} | ${r.passed} | ${r.failed} | ${r.infra} | ${r.skipped} | ${pct(r.passed, r.scored)} |`);
    }
    const sq = summary.sql_equivalence;
    if (sq.graded) {
      lines.push('');
      lines.push('## SQL vs knowledge pairs');
      lines.push('');
      lines.push(`${sq.pass}/${sq.graded} graded answers pass at strictness **${sq.strictness}** (${pct(sq.pass, sq.graded)}). Tiers: ${Object.entries(sq.by_tier).map(([k, v]) => `${k} ${v}`).join(', ')}.`);
      lines.push('');
      lines.push('| case | question | tier | verdict | overlap | rows/chart | grounded |');
      lines.push('|---|---|---|---|---:|---|---|');
      for (const r of this.records.filter((x) => x.kp_tier)) {
        lines.push(`| ${r.title.split(':')[0].replace(/^Knowledge pairs › /, '')} | ${String(r.question || '').replace(/\|/g, '\\|')} | ${r.kp_tier} | ${r.kp_verdict || ''} | ${r.kp_overlap ?? ''} | ${r.chart_type || ''} | ${String(r.answer_grounded || '').split(' — ')[0]} |`);
      }
    }
    const ml = this.records.filter((r) => r.ml_envelope);
    if (ml.length) {
      lines.push('');
      lines.push('## ML envelopes');
      lines.push('');
      lines.push('| test | skill | method | metric | band | tier | rows sent | low conf | status |');
      lines.push('|---|---|---|---|---|---|---:|---|---|');
      for (const r of ml) {
        const e = Array.isArray(r.ml_envelope) ? r.ml_envelope[r.ml_envelope.length - 1] : r.ml_envelope;
        if (!e || typeof e !== 'object') continue;
        lines.push(`| ${r.title.split(':')[0].replace(/^[^›]*› /, '')} | ${e.skill || ''} | ${e.method || ''} | ${e.metric || ''}${e.value != null ? ` ${e.value}` : ''} | ${e.band || ''} | ${e.tier || ''} | ${e.rows_sent ?? ''} | ${e.low_confidence ? 'yes' : 'no'} | ${r.status} |`);
      }
    }
    const lat = summary.latency;
    if (lat.answered_turns) {
      lines.push('');
      lines.push('## Latency');
      lines.push('');
      lines.push(`${lat.answered_turns} answered turns — wall p50 ${(lat.wall_p50_ms / 1000).toFixed(1)}s / p95 ${(lat.wall_p95_ms / 1000).toFixed(1)}s · LLM p50 ${lat.llm_p50_ms != null ? (lat.llm_p50_ms / 1000).toFixed(1) : '–'}s / p95 ${lat.llm_p95_ms != null ? (lat.llm_p95_ms / 1000).toFixed(1) : '–'}s · SQL exec p50 ${lat.exec_p50_ms != null ? (lat.exec_p50_ms / 1000).toFixed(1) : '–'}s / p95 ${lat.exec_p95_ms != null ? (lat.exec_p95_ms / 1000).toFixed(1) : '–'}s`);
    }
    const findings = this.records.filter((r) => r.findings.length);
    if (findings.length) {
      lines.push('');
      lines.push('## Findings (recorded, not failed)');
      lines.push('');
      for (const r of findings) for (const f of r.findings) lines.push(`- ${r.title}: ${typeof f === 'string' ? f : JSON.stringify(f)}`);
    }
    const failures = this.records.filter((r) => r.status === 'failed' || r.status === 'infra');
    if (failures.length) {
      lines.push('');
      lines.push('## Failures');
      lines.push('');
      for (const r of failures) {
        lines.push(`### ${r.status === 'infra' ? '[infra] ' : ''}${r.title}`);
        lines.push('');
        if (r.question) lines.push(`Question: ${r.question}`);
        lines.push('```');
        lines.push(String(r.error || '').trim());
        lines.push('```');
        lines.push('');
      }
    }
    return lines.join('\n');
  }

  async onEnd(result) {
    const summary = this.summarize();
    this.applyGates(summary);
    fs.mkdirSync(this.outputDir, { recursive: true });
    fs.writeFileSync(path.join(this.outputDir, 'scorecard.json'), JSON.stringify({ summary, tests: this.records }, null, 2));
    fs.writeFileSync(path.join(this.outputDir, 'scorecard.md'), this.markdown(summary));
    const t = summary.totals;
    process.stdout.write(`\n[scorecard] ${t.passed} passed, ${t.flaky} flaky, ${t.failed} failed, ${t.infra} infra, ${t.skipped} skipped → ${path.relative(process.cwd(), path.join(this.outputDir, 'scorecard.md'))}\n`);
    if (summary.sql_equivalence.graded) {
      process.stdout.write(`[scorecard] SQL vs knowledge pairs: ${summary.sql_equivalence.pass}/${summary.sql_equivalence.graded} at ${summary.env.strict}\n`);
    }
    if (this.gateFailures.length) {
      process.stdout.write(`[scorecard] GATE FAILURES:\n${this.gateFailures.map((f) => `  - ${f}`).join('\n')}\n`);
      return { status: 'failed' };
    }
    return undefined;
  }
}

module.exports = ScorecardReporter;
