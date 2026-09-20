// @ts-check
// Scripted multi-turn conversations (@conv). Each conversation runs in ONE
// session (the controller keeps session_id across turns) and every turn
// declares what must hold. Memory turns are graded deterministically: the
// expected text is computed from the rows the previous data turn returned,
// so "which year was highest?" is checked against the actual maximum.
//
// Data: AdventureWorksDW (internet sales 2005 – mid 2008).

/**
 * @typedef {Object} ConvTurn
 * @property {'sql'|'followup'|'memory'|'ml'|'sql_instead'} kind
 * @property {string} q
 * @property {RegExp[]} [sqlMust]                 SQL must match each — applied when the turn ran SQL (sql / followup)
 * @property {[number, number]} [rows]            inclusive row-count range (sql / followup / sql_instead)
 * @property {(rows: any[], columns: string[]) => string|null} [rowsMust]  returns a problem, or null when the rows are right
 * @property {boolean} [chart]                    a chart must render (sql / followup)
 * @property {string[]} [paths]                   acceptable data-route-path values; defaults: sql → ['sql'], followup/memory → ['sql', 'from_memory']
 * @property {(prev: PrevTurn) => string|RegExp|null} [derive]   memory: expected text, from the previous data turn
 * @property {string} [skill]                     ml: skill on the confirm card
 * @property {'confirm'|'guard'} [card]           ml: which stop card
 * @property {import('./mlEnvelope').EnvelopeChecks} [envelope]  ml: envelope bounds after Run
 */
/** @typedef {{ rows: any[], columns: string[], raw: any }} PrevTurn */

/** @typedef {{ id: string, title: string, turns: ConvTurn[], tags?: string[] }} Conversation */

// ── derive helpers (pure) ────────────────────────────────────────────────────

const cell = (row, col) => (Array.isArray(row) ? row[Number(col)] : row[col]);
const asNumber = (v) => (typeof v === 'number' ? v : Number(String(v ?? '').replace(/[$,]/g, '')));

/** The column whose values look like calendar years. */
function yearColumn(prev) {
  return prev.columns.find((c) => prev.rows.every((r) => { const n = asNumber(cell(r, c)); return Number.isInteger(n) && n >= 1990 && n <= 2100; })) || null;
}

/** The numeric column with the largest spread that is not the year column. */
function measureColumn(prev, exclude = []) {
  const candidates = prev.columns.filter((c) => !exclude.includes(c) && prev.rows.every((r) => Number.isFinite(asNumber(cell(r, c)))));
  let best = null; let spread = -1;
  for (const c of candidates) {
    const values = prev.rows.map((r) => asNumber(cell(r, c)));
    const s = Math.max(...values) - Math.min(...values);
    if (s > spread) { spread = s; best = c; }
  }
  return best;
}

/** Label (year) of the row with the highest measure. */
function labelOfMax(prev) {
  const year = yearColumn(prev);
  const measure = measureColumn(prev, year ? [year] : []);
  if (!year || !measure || !prev.rows.length) return null;
  const top = prev.rows.reduce((a, b) => (asNumber(cell(b, measure)) > asNumber(cell(a, measure)) ? b : a));
  return String(asNumber(cell(top, year)));
}

/** rowsMust: every row's year is one of `years` (works for SQL and for memory-filtered results). */
function onlyYears(years) {
  return (rows, columns) => {
    const prev = { rows, columns, raw: null };
    const year = yearColumn(prev);
    if (!year) return `no year column among ${JSON.stringify(columns)}`;
    const bad = rows.map((r) => asNumber(cell(r, year))).filter((y) => !years.includes(y));
    return bad.length ? `rows for years ${JSON.stringify([...new Set(bad)])} should have been filtered out` : null;
  };
}

// ── The set ──────────────────────────────────────────────────────────────────

/** @type {Conversation[]} */
const CONVERSATIONS = [
  {
    id: 'analytics_followups',
    title: 'SQL answer → refinement follow-up → memory question',
    turns: [
      // A registered knowledge pair; the @kp suite grades its SQL, here it seeds the conversation.
      { kind: 'sql', q: 'What is internet sales amount by calendar year', rows: [3, 5], sqlMust: [/GROUP BY/i, /factinternetsales/i], chart: true },
      // Refinement right after the grouped answer: "same but…" inherits the measure and the
      // grouping — either as a new query (SQL mentions both years) or by filtering the prior
      // rows in memory. (Keep it directly after turn 1: after a "which year was highest?"
      // question, "same" would legitimately mean "the highest of those two".)
      { kind: 'followup', q: 'Same but only for 2007 and 2008', rows: [2, 2], sqlMust: [/2007/, /2008/], rowsMust: onlyYears([2007, 2008]) },
      // Memory: the answer must name the year with the higher amount in the previous result.
      { kind: 'memory', q: 'Which of those two years had the higher sales amount?', derive: labelOfMax },
    ],
  },
  {
    id: 'analytics_to_ml',
    title: 'SQL time series → forecast in the same conversation',
    turns: [
      { kind: 'sql', q: 'Internet sales trends by month', rows: [30, 48], sqlMust: [/GROUP BY/i], chart: true },
      {
        kind: 'ml', q: 'Forecast the monthly internet sales amount for the next 6 months', skill: 'forecast', card: 'confirm',
        envelope: {
          params: { horizon: 6, 'series.grain': 'month' },
          metric: /WAPE|MASE/i, band: ['good', 'fair', 'poor', 'n/a'],
          tier: 'A', maxRowsSent: 1500, guardsPassed: true, lowConfidence: false,
          facts: { horizon: 6 }, chartTypes: ['band'],
        },
      },
    ],
  },
  {
    id: 'ml_to_sql',
    title: 'ML confirm card → "Answer with SQL instead" → follow-up keeps working',
    turns: [
      { kind: 'sql_instead', q: 'Forecast total SalesAmount by month for the next 6 months', skill: 'forecast', rows: [1, 60] },
      { kind: 'followup', q: 'Now show the same by year instead of by month', rows: [3, 5], sqlMust: [/GROUP BY/i] },
    ],
  },
];

module.exports = { CONVERSATIONS, yearColumn, measureColumn, labelOfMax, onlyYears };
