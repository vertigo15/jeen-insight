// @ts-check
// Graded answers → the per-level summary (summary.json), the human report
// (report.md), the console tables and the gates. Times are over answered
// questions only; failures are counted by kind beside them, so a timeout
// reads as a failure, never as a fast answer.
const fs = require('fs');
const { failureOf, verdictOf } = require('./grade');

const TIMINGS = ['open', 'table', 'answer', 'done', 'chart'];
const TIERS = ['exact', 'equivalent', 'structural', 'execution_match', 'mismatch', 'ungraded', 'error', 'no_gold'];
const LIST_LIMIT = 30;
/** Graph steps faster than this at every level are left out of "Where the time goes". */
const STEP_FLOOR_MS = 100;

// ── Answer files ─────────────────────────────────────────────────────────────

/**
 * @param {string} file
 * @param {any} record
 */
function appendAnswer(file, record) {
  fs.appendFileSync(file, `${JSON.stringify(record)}\n`);
}

/**
 * Replace the file in one step (graded records supersede their raw lines).
 * @param {string} file
 * @param {any[]} records
 */
function writeAnswers(file, records) {
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, `${records.map((r) => JSON.stringify(r)).join('\n')}\n`);
  fs.renameSync(tmp, file);
}

/**
 * Records in file order; when an id appears twice the later line wins.
 * @param {string} file
 */
function readAnswers(file) {
  if (!fs.existsSync(file)) return [];
  const byId = new Map();
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    try {
      const record = JSON.parse(line);
      byId.set(record.id, record);
    } catch (_) {
      // A line cut off by an interrupted write.
    }
  }
  return [...byId.values()];
}

// ── Numbers ──────────────────────────────────────────────────────────────────

/**
 * Nearest-rank percentile, the same rule as the live scorecard.
 * @param {any[]} values
 * @param {number} p
 */
function percentile(values, p) {
  const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[index];
}

/** @param {any[]} values */
function stats(values) {
  const list = values.filter((v) => Number.isFinite(v));
  if (!list.length) return { n: 0, p50: null, p95: null, max: null };
  return { n: list.length, p50: percentile(list, 50), p95: percentile(list, 95), max: Math.max(...list) };
}

/** @param {any} value */
const num = (value) => (typeof value === 'number' ? value : value == null || value === '' ? NaN : Number(value));
/** @param {number[]} values */
const finite = (values) => values.filter((v) => Number.isFinite(v));
/** @param {number[]} values */
const sum = (values) => finite(values).reduce((a, b) => a + b, 0);
/** @param {number[]} values */
const mean = (values) => (finite(values).length ? sum(values) / finite(values).length : null);
/**
 * @param {number | null} a
 * @param {number | null} b
 */
const ratio = (a, b) => (a != null && b ? a / b : null);

/** @param {any[]} list */
function mode(list) {
  /** @type {Map<any, number>} */
  const counts = new Map();
  for (const item of list) counts.set(item, (counts.get(item) || 0) + 1);
  let best = null;
  let bestCount = 0;
  for (const [item, count] of counts) {
    if (count > bestCount) {
      best = item;
      bestCount = count;
    }
  }
  return best;
}

/**
 * @param {any} answer
 * @param {string} key
 */
function timing(answer, key) {
  if (key === 'chart') return answer.chart && answer.chart.status === 200 ? answer.chart.ready_ms : null;
  return answer.t ? answer.t[key] : null;
}

// ── Summary ──────────────────────────────────────────────────────────────────

/**
 * @param {any} run  run.json
 * @param {any[]} answers  every record, the warm-up included
 * @param {string} strict
 * @param {import('../config').Config['gates']} gates
 */
function summarize(run, answers, strict, gates) {
  const measured = answers.filter((a) => !a.warmup);
  /** @type {Map<string, { verdict: string, reason: string }>} */
  const verdicts = new Map(measured.map((a) => [a.id, verdictOf(a.grade, strict)]));
  /** @param {any} a */
  const verdict = (a) => /** @type {{ verdict: string, reason: string }} */ (verdicts.get(a.id));
  /** @param {any[]} list @param {string} v */
  const count = (list, v) => list.filter((a) => verdict(a).verdict === v).length;

  const levels = run.levels.map((/** @type {any} */ entry) => {
    const list = measured.filter((a) => a.level === entry.level);
    const answered = list.filter((a) => verdict(a).verdict !== 'failed');
    /** @type {Record<string, number>} */
    const failures = {};
    for (const a of list) if (verdict(a).verdict === 'failed') failures[verdict(a).reason] = (failures[verdict(a).reason] || 0) + 1;
    /** @type {Record<string, number>} */
    const tiers = {};
    for (const a of answered) if (a.grade && a.grade.tier) tiers[a.grade.tier] = (tiers[a.grade.tier] || 0) + 1;
    const correct = count(list, 'correct');
    const wrong = count(list, 'wrong');
    const failed = count(list, 'failed');
    const checked = answered.filter((a) => a.grade && a.grade.grounded && a.grade.grounded.checked > 0);
    const charts = list.filter((a) => a.chart);
    /** @param {string} key */
    const metric = (key) => answered.map((a) => num(a.result && a.result.metrics ? a.result.metrics[key] : null));
    return {
      level: entry.level,
      partial: Boolean(entry.partial),
      asked: list.length,
      answered: answered.length,
      correct,
      wrong,
      failed,
      ungraded: count(list, 'ungraded'),
      accuracy: correct + wrong ? correct / (correct + wrong) : null,
      success: list.length ? (list.length - failed) / list.length : null,
      failures,
      tiers,
      grounded: { grounded: checked.filter((a) => a.grade.grounded.grounded).length, checked: checked.length },
      charts: { requested: charts.length, ok: charts.filter((a) => a.chart.status === 200).length },
      timings: Object.fromEntries(TIMINGS.map((key) => [key, stats(answered.map((a) => timing(a, key)))])),
      server: {
        llm_latency_ms: stats(metric('llm_latency_ms')),
        execution_time_ms: stats(metric('execution_time_ms')),
        llm_calls_mean: mean(metric('llm_call_count')),
        retries_total: sum(metric('retry_count')),
        tokens_mean: mean(metric('total_tokens')),
        tokens_total: sum(metric('total_tokens')),
      },
      wall_ms: entry.wall_ms,
      throughput_per_min: entry.wall_ms ? answered.length / (entry.wall_ms / 60_000) : null,
      /** @type {any} */
      vs_first: null,
    };
  });

  const first = levels[0];
  for (const level of levels.slice(1)) {
    level.vs_first = {
      accuracy_change: level.accuracy != null && first.accuracy != null ? level.accuracy - first.accuracy : null,
      table_p95_x: ratio(level.timings.table.p95, first.timings.table.p95),
      answer_p50_x: ratio(level.timings.answer.p50, first.timings.answer.p50),
      answer_p95_x: ratio(level.timings.answer.p95, first.timings.answer.p95),
    };
  }

  /** @param {any} a */
  const answeredFingerprint = (a) => (verdict(a).verdict !== 'failed' && a.grade && a.grade.fingerprint) || null;
  const asked = new Set(measured.map((a) => a.case_id));
  const questions = run.cases.filter((/** @type {any} */ kase) => asked.has(kase.id)).map((/** @type {any} */ kase) => {
    const list = measured.filter((a) => a.case_id === kase.id);
    /** @type {Record<string, any>} */
    const perLevel = {};
    for (const entry of run.levels) {
      const at = list.filter((a) => a.level === entry.level);
      perLevel[entry.level] = {
        asked: at.length,
        correct: count(at, 'correct'),
        wrong: count(at, 'wrong'),
        failed: count(at, 'failed'),
        ungraded: count(at, 'ungraded'),
      };
    }
    const baseline = first ? mode(list.filter((a) => a.level === first.level).map(answeredFingerprint).filter(Boolean)) : null;
    const later = list.filter((a) => first && a.level !== first.level).map(answeredFingerprint).filter(Boolean);
    return {
      id: kase.id,
      question: kase.question,
      has_gold: Boolean(kase.gold_sql),
      levels: perLevel,
      baseline_fingerprint: baseline,
      same_rows: baseline && later.length ? { same: later.filter((f) => f === baseline).length, answered: later.length } : null,
      distinct_results: new Set(list.map(answeredFingerprint).filter(Boolean)).size,
    };
  });

  // Per answer, the time spent in each graph step (summed when a step ran twice, e.g. a retry).
  /** @type {Map<string, number[]>} */
  const stepTimes = new Map();
  /** @type {Set<string>} */
  const stepNames = new Set();
  for (const a of measured) {
    if (verdict(a).verdict === 'failed' || !a.result) continue;
    /** @type {Map<string, number>} */
    const perStep = new Map();
    for (const step of a.result.trace || []) perStep.set(step.node, (perStep.get(step.node) || 0) + step.elapsed_ms);
    for (const [node, ms] of perStep) {
      stepNames.add(node);
      const key = `${a.level}|${node}`;
      if (!stepTimes.has(key)) stepTimes.set(key, []);
      /** @type {number[]} */ (stepTimes.get(key)).push(ms);
    }
  }
  const lastLevel = levels.length ? levels[levels.length - 1].level : null;
  const steps = [...stepNames]
    .map((node) => ({
      node,
      levels: Object.fromEntries(run.levels.map((/** @type {any} */ e) => [e.level, stats(stepTimes.get(`${e.level}|${node}`) || [])])),
    }))
    .filter((step) => Object.values(step.levels).some((st) => (st.p95 ?? 0) >= STEP_FLOOR_MS))
    .sort((a, b) => ((b.levels[lastLevel]?.p95 ?? 0) - (a.levels[lastLevel]?.p95 ?? 0)));

  const failures = measured
    .filter((a) => verdict(a).verdict === 'failed')
    .map((a) => ({ id: a.id, level: a.level, user: a.user, case_id: a.case_id, kind: verdict(a).reason, detail: (failureOf(a) || {}).detail || '' }));
  const wrongAnswers = measured
    .filter((a) => verdict(a).verdict === 'wrong')
    .map((a) => ({ id: a.id, level: a.level, user: a.user, case_id: a.case_id, reason: verdict(a).reason, row_count: a.grade.row_count, sql: a.result.sql }));

  return { levels, questions, steps, failures, wrong: wrongAnswers, gates: evaluateGates(levels, gates) };
}

/**
 * Every gate that is set, for every level; a gate that cannot be evaluated fails.
 * @param {any[]} levels
 * @param {import('../config').Config['gates']} gates
 */
function evaluateGates(levels, gates) {
  /** @type {{ gate: string, level: number, value: number | null, limit: number, ok: boolean, note: string }[]} */
  const out = [];
  const first = levels[0];
  for (const level of levels) {
    if (gates.minAccuracy != null) {
      const value = level.accuracy;
      out.push({
        gate: 'STRESS_MIN_ACCURACY', level: level.level, value, limit: gates.minAccuracy,
        ok: value != null && value >= gates.minAccuracy,
        note: value == null ? 'no graded answers' : `accuracy ${pct(value)} (min ${pct(gates.minAccuracy)})`,
      });
    }
    if (gates.maxAccuracyDrop != null && level !== first) {
      const value = level.accuracy != null && first.accuracy != null ? first.accuracy - level.accuracy : null;
      out.push({
        gate: 'STRESS_MAX_ACCURACY_DROP', level: level.level, value, limit: gates.maxAccuracyDrop,
        ok: value != null && value <= gates.maxAccuracyDrop,
        note: value == null
          ? 'no graded answers to compare'
          : `accuracy ${value > 0 ? 'fell' : 'rose'} ${Math.abs(value * 100).toFixed(1)} pts against ${first.level} user(s) (max drop ${(gates.maxAccuracyDrop * 100).toFixed(1)})`,
      });
    }
    if (gates.maxP95Ms != null) {
      const value = level.timings.answer.p95;
      out.push({
        gate: 'STRESS_MAX_P95_MS', level: level.level, value, limit: gates.maxP95Ms,
        ok: value != null && value <= gates.maxP95Ms,
        note: value == null ? 'no answered questions' : `full answer p95 ${s(value)} s (max ${s(gates.maxP95Ms)} s)`,
      });
    }
    if (gates.maxErrorRate != null) {
      const value = level.asked ? level.failed / level.asked : null;
      out.push({
        gate: 'STRESS_MAX_ERROR_RATE', level: level.level, value, limit: gates.maxErrorRate,
        ok: value != null && value <= gates.maxErrorRate,
        note: value == null ? 'nothing was asked' : `${pct(value)} failed (max ${pct(gates.maxErrorRate)})`,
      });
    }
  }
  return out;
}

// ── Rendering ────────────────────────────────────────────────────────────────

/** @param {number | null | undefined} ms */
const s = (ms) => (ms == null ? '–' : (ms / 1000).toFixed(1));
/** @param {number | null | undefined} x */
const pct = (x) => (x == null ? '–' : `${(x * 100).toFixed(1)}%`);
/** @param {number | null | undefined} x */
const times = (x) => (x == null ? '–' : `${x.toFixed(1)}×`);
/** @param {number | null | undefined} x */
const pts = (x) => (x == null ? '–' : `${x >= 0 ? '+' : '−'}${Math.abs(x * 100).toFixed(1)} pts`);
/** @param {{ n: number, p50: number | null, p95: number | null, max: number | null }} st */
const trio = (st) => (st.n ? `${s(st.p50)} / ${s(st.p95)} / ${s(st.max)}` : '–');
/** @param {{ n: number, p50: number | null, p95: number | null }} st */
const duo = (st) => (st.n ? `${s(st.p50)} / ${s(st.p95)}` : '–');
/** @param {number | null} x @param {number} [digits] */
const fixed = (x, digits = 1) => (x == null ? '–' : x.toFixed(digits));
/** @param {any} level */
const usersLabel = (level) => `${level.level}${level.partial ? ' (interrupted)' : ''}`;

/**
 * @param {string[]} header
 * @param {(string | number)[][]} rows
 */
function table(header, rows) {
  return [`| ${header.join(' | ')} |`, `|${header.map(() => '---').join('|')}|`, ...rows.map((row) => `| ${row.join(' | ')} |`)].join('\n');
}

/** @param {string} text */
const cell = (text) => String(text).replace(/\|/g, '\\|').replace(/\s+/g, ' ').trim();

/** @param {any} summary */
function accuracyTable(summary) {
  return table(
    ['users', 'asked', 'correct', 'wrong', 'failed', 'ungraded', 'accuracy', 'answered', 'grounded sentence', 'accuracy vs first level'],
    summary.levels.map((/** @type {any} */ l) => [
      usersLabel(l), l.asked, l.correct, l.wrong, l.failed, l.ungraded, pct(l.accuracy), pct(l.success),
      l.grounded.checked ? `${l.grounded.grounded}/${l.grounded.checked}` : '–',
      l.vs_first ? pts(l.vs_first.accuracy_change) : 'baseline',
    ]),
  );
}

/** @param {any} summary */
function speedTable(summary) {
  return table(
    ['users', 'stream opened', 'first table', 'full answer', 'stream closed', 'chart ready', 'charts ok', 'answers/min', 'full answer p95 vs first level'],
    summary.levels.map((/** @type {any} */ l) => [
      usersLabel(l), trio(l.timings.open), trio(l.timings.table), trio(l.timings.answer), trio(l.timings.done), trio(l.timings.chart),
      l.charts.requested ? `${l.charts.ok}/${l.charts.requested}` : '–',
      fixed(l.throughput_per_min),
      l.vs_first ? times(l.vs_first.answer_p95_x) : 'baseline',
    ]),
  );
}

/** @param {any} gate */
const gateLine = (gate) => `- ${gate.ok ? 'PASS' : 'FAIL'} \`${gate.gate}\` at ${gate.level} user(s): ${gate.note}`;

/**
 * The human report.
 * @param {any} run
 * @param {any} summary
 * @param {string} strict
 */
function renderMarkdown(run, summary, strict) {
  const settings = run.settings;
  const levelIds = run.levels.map((/** @type {any} */ l) => l.level);
  const lines = [
    `# Stress run ${run.id}`,
    '',
    `- Target: ${run.target} · connection ${run.connection.name} (${run.connection.source_key}, ${run.connection.database_type || 'unknown type'}) · catalog ${run.catalog_source}`,
    `- App ${run.app.version || 'version unknown'} · model ${run.app.model || 'unknown'} · git ${run.git.sha || 'unknown'}${run.git.dirty ? ' (with uncommitted changes)' : ''} · node ${run.node}`,
    `- ${run.started_at} → ${run.ended_at || 'interrupted'} · ${settings.levels.join(', ')} user(s) · ${settings.questions_per_user} question(s) per user from ${settings.cases.length} case(s)`
      + ` · chart requests ${settings.chart ? 'on' : 'off'} · think time ${s(settings.think_ms)} s · question timeout ${s(settings.ask_timeout_ms)} s`,
    `- **Correct**: the generated SQL passes the knowledge pair's gold at \`${strict}\` and the row count is in the case's range. **Failed**: no usable answer`
      + ' (timeout, HTTP error, stream error, or not answered with SQL). Accuracy = correct / (correct + wrong); answered = not failed / asked.',
    '- Times are seconds from sending the question (p50 / p95 / max), over answered questions only.',
    '',
    '## Accuracy',
    '',
    accuracyTable(summary),
    '',
  ];
  const failureRows = summary.levels.filter((/** @type {any} */ l) => l.failed);
  if (failureRows.length) {
    lines.push('Failures by kind:', '');
    for (const l of failureRows) lines.push(`- ${l.level} user(s): ${Object.entries(l.failures).map(([kind, n]) => `${n} ${kind}`).join(', ')}`);
    lines.push('');
  }
  lines.push('SQL against the gold, per tier (answered questions):', '');
  lines.push(table(['users', ...TIERS], summary.levels.map((/** @type {any} */ l) => [usersLabel(l), ...TIERS.map((tier) => l.tiers[tier] || 0)])), '');

  lines.push('## Speed', '', speedTable(summary), '');
  lines.push('Server side (p50 / p95 seconds, from the answers\' metrics):', '');
  lines.push(table(
    ['users', 'LLM time', 'SQL execution', 'LLM calls per answer', 'retries', 'tokens per answer', 'tokens total'],
    summary.levels.map((/** @type {any} */ l) => [
      usersLabel(l), duo(l.server.llm_latency_ms), duo(l.server.execution_time_ms), fixed(l.server.llm_calls_mean),
      l.server.retries_total, fixed(l.server.tokens_mean, 0), l.server.tokens_total,
    ]),
  ), '');

  if (summary.steps.length) {
    lines.push('## Where the time goes', '', 'Per graph step (the answers\' `trace`), p50 / p95 seconds, slowest at the highest level first; steps under 0.1 s are left out:', '');
    lines.push(table(
      ['step', ...levelIds.map((/** @type {number} */ n) => `${n} user(s)`)],
      summary.steps.slice(0, 15).map((/** @type {any} */ step) => [step.node, ...levelIds.map((/** @type {number} */ n) => duo(step.levels[n]))]),
    ), '');
  }

  lines.push('## Per question', '', 'Correct / asked per level; "same rows" compares each later answer\'s result values with the most common result at the first level.', '');
  lines.push(table(
    ['case', ...levelIds.map((/** @type {number} */ n) => `${n} user(s)`), 'same rows as first level', 'distinct results'],
    summary.questions.map((/** @type {any} */ q) => [
      `${q.id}${q.has_gold ? '' : ' (no gold)'}`,
      ...levelIds.map((/** @type {number} */ n) => {
        const at = q.levels[n];
        if (!at || !at.asked) return '–';
        const extra = [at.failed ? `${at.failed} failed` : '', at.ungraded ? `${at.ungraded} ungraded` : ''].filter(Boolean).join(', ');
        return `${at.correct}/${at.asked}${extra ? ` (${extra})` : ''}`;
      }),
      q.same_rows ? `${q.same_rows.same}/${q.same_rows.answered}` : '–',
      q.distinct_results || '–',
    ]),
  ), '');

  if (summary.wrong.length) {
    lines.push(`## Wrong answers${summary.wrong.length > LIST_LIMIT ? ` (first ${LIST_LIMIT} of ${summary.wrong.length})` : ''}`, '');
    for (const w of summary.wrong.slice(0, LIST_LIMIT)) lines.push(`- \`${w.id}\` ${w.case_id}: ${w.reason} — \`${cell(w.sql || '').slice(0, 240)}\``);
    lines.push('');
  }
  if (summary.failures.length) {
    lines.push(`## Failures${summary.failures.length > LIST_LIMIT ? ` (first ${LIST_LIMIT} of ${summary.failures.length})` : ''}`, '');
    for (const f of summary.failures.slice(0, LIST_LIMIT)) lines.push(`- \`${f.id}\` ${f.case_id}: ${f.kind} — ${cell(f.detail).slice(0, 240)}`);
    lines.push('');
  }
  lines.push('## Gates', '');
  lines.push(...(summary.gates.length ? summary.gates.map(gateLine) : ['None set (report only). Set STRESS_MIN_ACCURACY, STRESS_MAX_ACCURACY_DROP, STRESS_MAX_P95_MS or STRESS_MAX_ERROR_RATE to enforce.']));
  return `${lines.join('\n')}\n`;
}

/**
 * The two headline tables and any failed gates, for the terminal.
 * @param {any} summary
 */
function renderConsole(summary) {
  const failed = summary.gates.filter((/** @type {any} */ g) => !g.ok);
  return [
    'Accuracy',
    accuracyTable(summary),
    '',
    'Speed (seconds from sending the question: p50 / p95 / max, answered questions only)',
    speedTable(summary),
    ...(summary.gates.length ? ['', `Gates: ${failed.length ? `${failed.length} failed` : 'all passed'}`, ...summary.gates.map(gateLine)] : []),
  ].join('\n');
}

module.exports = { appendAnswer, writeAnswers, readAnswers, percentile, stats, summarize, evaluateGates, renderMarkdown, renderConsole };
