// @ts-check
// Node side of the SQL/DAX grader: runs live/sql_equivalence.py (the project's
// sqlglot + dialect mapping) with one JSON document on stdin. See that file
// for the tier definitions. The Python interpreter is the repo's .venv when
// present (same rule as tests/e2e/globalSetup.js), else python3.
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const SCRIPT = path.join(__dirname, 'sql_equivalence.py');

/** Strongest first; `mismatch`, `ungraded` (unparsable side) and `error` never pass. */
const TIERS = ['exact', 'equivalent', 'structural', 'execution_match', 'mismatch', 'ungraded', 'error'];

/**
 * @typedef {Object} Grade
 * @property {string} tier                        exact | equivalent | structural | execution_match | mismatch | error
 * @property {{exact: boolean, equivalent: boolean, structural: boolean, execution_match: boolean|null}} [verdicts]
 * @property {number} [overlap]                   Jaccard overlap of the structural fingerprints (0..1)
 * @property {{gold: string, generated: string}} [normalized]
 * @property {{gold: string|null, generated: string|null}} [canonical]
 * @property {{gold: object|null, generated: object|null}} [fingerprint]
 * @property {object} [delta]                     what differs, per fingerprint key
 * @property {string[]} [diff]                    sqlglot edit script (diagnostics)
 * @property {{match: boolean|null, detail: string}} [execution]
 * @property {string|null} [error]
 */

function pythonBin() {
  const venv = path.join(REPO_ROOT, '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : 'python3';
}

/**
 * Grade `generated` against `gold`.
 * @param {{ gold: string, generated: string, dialect?: string|null, language?: 'sql'|'dax',
 *   goldDsn?: string|null, appRows?: any[]|null, appColumns?: string[]|null, appTruncated?: boolean }} args
 * @returns {Grade}
 */
function gradeSql(args) {
  const payload = {
    gold: args.gold,
    generated: args.generated,
    dialect: args.dialect ?? null,
    language: args.language || 'sql',
    gold_dsn: args.goldDsn || process.env.LIVE_GOLD_DSN || null,
    app_rows: args.appRows ?? null,
    app_columns: args.appColumns ?? null,
    app_truncated: Boolean(args.appTruncated),
  };
  const run = spawnSync(pythonBin(), [SCRIPT], {
    cwd: REPO_ROOT,
    input: JSON.stringify(payload),
    encoding: 'utf8',
    timeout: 120_000,
    maxBuffer: 16 * 1024 * 1024,
  });
  if (run.error) return { tier: 'error', error: `grader did not start: ${run.error.message}` };
  if (run.status !== 0) return { tier: 'error', error: `grader exited ${run.status}: ${(run.stderr || '').trim().slice(-800)}` };
  try {
    return JSON.parse(run.stdout);
  } catch (error) {
    return { tier: 'error', error: `grader returned non-JSON: ${(run.stdout || '').slice(0, 300)}` };
  }
}

/**
 * Does `tier` satisfy the required strictness? `execution_match` always passes
 * (the rows are right, whatever the text looks like).
 * @param {string} tier
 * @param {string} [strict]  exact | equivalent | structural (default from LIVE_KP_STRICT, else structural)
 */
function passes(tier, strict = process.env.LIVE_KP_STRICT || 'structural') {
  if (tier === 'execution_match') return true;
  const want = TIERS.indexOf(strict);
  const have = TIERS.indexOf(tier);
  if (want < 0) throw new Error(`LIVE_KP_STRICT must be one of exact|equivalent|structural, got ${strict}`);
  return have >= 0 && have <= want && !['mismatch', 'ungraded', 'error'].includes(tier);
}

/** True when the grader could not compute a tier (unparsable SQL, grader crash): report, don't judge. */
function ungraded(tier) {
  return tier === 'ungraded' || tier === 'error';
}

/** `database_type` → sqlglot dialect, mirroring src/connectors/dialects.py (null = permissive default). */
function sqlglotDialect(databaseType) {
  const key = String(databaseType || '').trim().toLowerCase();
  const map = { postgres: 'postgres', postgresql: 'postgres', trino: 'trino', presto: 'presto', databricks: 'databricks', spark: 'spark', spark2: 'spark2' };
  return map[key] || null;
}

/** One-paragraph explanation for a failed grade, for the assertion message and the report. */
function explain(grade) {
  const parts = [`tier=${grade.tier}`];
  if (typeof grade.overlap === 'number') parts.push(`overlap=${grade.overlap}`);
  if (grade.delta && Object.keys(grade.delta).length) parts.push(`delta=${JSON.stringify(grade.delta)}`);
  if (grade.execution && grade.execution.match !== null && grade.execution.match !== undefined) parts.push(`execution=${grade.execution.detail}`);
  if (grade.error) parts.push(`error=${grade.error}`);
  return parts.join(' | ');
}

module.exports = { gradeSql, passes, ungraded, explain, sqlglotDialect, TIERS, pythonBin };
