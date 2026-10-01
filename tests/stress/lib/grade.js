// @ts-check
// Accuracy of one answer with the live suite's ground truth: the generated SQL
// against its knowledge pair's gold (tests/e2e/live/sql_equivalence.py), the
// row range the curated case declares (kp.cases.js), and whether the sentence
// states only numbers found in the rows (answerGrounded). The grader runs
// after a level, never while one is being timed.
const crypto = require('crypto');
const { gradeSql, passes, ungraded } = require('../../e2e/live/sqlEquivalence');
const { answerGrounded } = require('../../e2e/live/_live');
const { NO_WRITES } = require('../../e2e/live/questions');

const INFRA = /Backend unavailable|Invalid internal token|Name or service not known|ECONNREFUSED|Max retries exceeded/i;

/** @param {any} results */
function rowsOf(results) {
  return results && Array.isArray(results.rows) ? results.rows : [];
}

/** @param {any} results */
function columnNames(results) {
  return (results && Array.isArray(results.columns) ? results.columns : [])
    .map((/** @type {any} */ c) => (typeof c === 'string' ? c : (c && (c.name || c.column)) || String(c)));
}

/**
 * Why a record holds no answer to grade (the transport, the stream, or the
 * app not answering with SQL), or null when it does.
 * @param {any} record
 * @returns {{ kind: string, detail: string } | null}
 */
function failureOf(record) {
  if (record.error) return record.error;
  const result = record.result;
  if (!result) return { kind: 'no_result', detail: 'no result event' };
  if (result.clarification) return { kind: 'clarification', detail: 'the app asked a clarifying question instead of answering' };
  const route = result.routing && result.routing.path;
  if (result.proposal || ['confirm', 'clarify', 'blocked'].includes(result.status) || (route && route !== 'sql')) {
    return { kind: 'not_sql', detail: `answered on the ${route || result.status} path, not SQL` };
  }
  if (result.error && !rowsOf(result.results).length) {
    return { kind: INFRA.test(result.error) ? 'unavailable' : 'answer_error', detail: String(result.error).slice(0, 300) };
  }
  if (!result.sql) return { kind: 'no_sql', detail: 'no SQL was generated' };
  return null;
}

/** @param {any} value */
function normalizeValue(value) {
  if (value === null || value === undefined) return '∅';
  if (typeof value === 'number') return value.toFixed(2);
  if (typeof value === 'object') return JSON.stringify(value);
  const text = String(value).trim();
  const plain = text.replace(/[$,]/g, '');
  if (plain !== '' && Number.isFinite(Number(plain))) return Number(plain).toFixed(2);
  const midnight = text.match(/^(\d{4}-\d{2}-\d{2})[T ]00:00:00(?:\.0+)?(?:Z|[+-]00:?00)?$/);
  if (midnight) return midnight[1];
  return text.toLowerCase();
}

/**
 * A digest of the result values that ignores column names, column order and
 * row order, so two phrasings of the same answer match.
 * @param {any} results
 */
function fingerprint(results) {
  const rows = rowsOf(results);
  if (!rows.length) return 'empty';
  const lines = rows
    .map((/** @type {any} */ row) => (Array.isArray(row) ? row : Object.values(row)).map(normalizeValue).sort().join('\u0001'))
    .sort();
  return crypto.createHash('sha1').update(lines.join('\u0002')).digest('hex').slice(0, 12);
}

/**
 * The grade stored on a record. The verdict is derived from it (verdictOf),
 * so a report can apply another strictness without running the grader again.
 * @param {any} record
 * @param {{ kase: any, dialect: string | null, language: 'sql' | 'dax', goldDsn: string | null }} context
 */
function gradeAnswer(record, { kase, dialect, language, goldDsn }) {
  const failure = failureOf(record);
  if (failure) return { failure };
  const result = record.result;
  const rows = rowsOf(result.results);
  const columns = columnNames(result.results);
  const rowCount = Number(result.results?.row_count ?? rows.length);
  const [min, max] = kase && kase.rows ? kase.rows : [1, Number.MAX_SAFE_INTEGER];
  const grounded = answerGrounded(result.answer || '', rows, columns);
  /** @type {any} */
  const grade = {
    failure: null,
    tier: 'no_gold',
    row_count: rowCount,
    rows_ok: rowCount >= min && rowCount <= max,
    write_sql: NO_WRITES.test(result.sql),
    grounded: { grounded: grounded.grounded, checked: grounded.checked, unmatched: grounded.unmatched.slice(0, 5) },
    fingerprint: fingerprint(result.results),
  };
  if (kase && kase.gold_sql) {
    const graded = gradeSql({
      gold: kase.gold_sql,
      generated: result.sql,
      dialect,
      language,
      goldDsn,
      appRows: rows,
      appColumns: columns,
      appTruncated: Boolean(result.results?.truncated),
    });
    grade.tier = graded.tier;
    if (typeof graded.overlap === 'number') grade.overlap = graded.overlap;
    if (graded.error) grade.grade_error = String(graded.error).slice(0, 300);
    if (graded.delta && Object.keys(graded.delta).length) grade.delta = graded.delta;
    if (graded.execution) grade.execution = graded.execution;
  }
  return grade;
}

/**
 * correct | wrong | failed | ungraded, and why.
 * @param {any} grade
 * @param {string} strict  exact | equivalent | structural
 */
function verdictOf(grade, strict) {
  if (!grade) return { verdict: 'ungraded', reason: 'not graded yet' };
  if (grade.failure) return { verdict: 'failed', reason: grade.failure.kind };
  if (grade.write_sql) return { verdict: 'wrong', reason: 'write statement' };
  if (grade.tier === 'no_gold') return { verdict: 'ungraded', reason: 'no gold SQL for this question' };
  if (ungraded(grade.tier)) return { verdict: 'ungraded', reason: `grader: ${grade.grade_error || grade.tier}` };
  if (!passes(grade.tier, strict)) return { verdict: 'wrong', reason: `SQL ${grade.tier} at ${strict}` };
  if (!grade.rows_ok) return { verdict: 'wrong', reason: `${grade.row_count} row(s), outside the case's range` };
  return { verdict: 'correct', reason: grade.tier };
}

/**
 * Grade the records that have no grade yet (every one with `force`) and set
 * each record's verdict at `context.strict`.
 * @param {any[]} records
 * @param {{ cases: Map<string, any>, dialect: string | null, language: 'sql' | 'dax', goldDsn: string | null, strict: string }} context
 * @param {boolean} [force]
 */
function gradeAll(records, context, force = false) {
  for (const record of records) {
    if (!record.grade || force) record.grade = gradeAnswer(record, { ...context, kase: context.cases.get(record.case_id) });
    const { verdict, reason } = verdictOf(record.grade, context.strict);
    record.verdict = verdict;
    record.verdict_reason = reason;
  }
}

module.exports = { failureOf, fingerprint, gradeAnswer, verdictOf, gradeAll };
