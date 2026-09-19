// @ts-check
// The live question set. Expectations are STRUCTURAL — the LLM writes the SQL
// and picks the model, so a test asserts the path, the shape of the SQL, a row
// range and which surfaces render; never a number from the data.
//
// Data: AdventureWorksDW on Postgres (FactInternetSales 2005–mid 2008,
// DimProduct, DimCustomer with YearlyIncome / TotalChildren / NumberCarsOwned).

const NO_WRITES = /\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE)\b/i;

/**
 * @typedef {Object} SqlCase
 * @property {string} id
 * @property {string} q
 * @property {[number, number]} rows        inclusive row-count range in the grid
 * @property {RegExp[]} sqlMust            every regex must match the SQL tab
 * @property {boolean} chart                a chart is expected to render
 * @property {boolean} [insights]           Key insights expected in the thread
 * @property {string} [followupOf]          ask after this case, in the same conversation
 */

/** @type {SqlCase[]} */
const SQL_CASES = [
  {
    id: 'by_month_range',
    q: 'Show sales by month from 2006 to 2008',
    rows: [24, 36], sqlMust: [/GROUP BY/i, /FactInternetSales/i], chart: true, insights: true,
  },
  {
    id: 'followup_history',
    q: 'Same but only for 2007',
    rows: [1, 12], sqlMust: [/GROUP BY/i, /2007/], chart: true, followupOf: 'by_month_range',
  },
  {
    id: 'by_year',
    q: 'Total sales amount by year',
    rows: [3, 5], sqlMust: [/GROUP BY/i, /SUM/i], chart: true, insights: true,
  },
  {
    id: 'single_total',
    q: 'What was the total profit in 2007?',
    rows: [1, 1], sqlMust: [/SUM/i, /2007/], chart: false,
  },
  {
    id: 'top_n',
    q: 'Top 10 products by sales amount',
    // LIMIT/FETCH/TOP or a window rank — any way of taking ten is fine.
    rows: [10, 10], sqlMust: [/DimProduct/i, /\b(LIMIT|FETCH|TOP|ROW_NUMBER|RANK)\b/i, /ORDER BY/i], chart: true,
  },
  {
    id: 'join_dimension',
    q: 'Sales amount by sales territory in 2007',
    rows: [2, 15], sqlMust: [/GROUP BY/i, /Territory/i], chart: true,
  },
  {
    id: 'count_distinct',
    q: 'How many distinct customers placed an order in 2008?',
    // COUNT(DISTINCT …) or COUNT(*) over a DISTINCT subquery.
    rows: [1, 1], sqlMust: [/COUNT/i, /DISTINCT/i, /2008/], chart: false,
  },
  {
    id: 'average',
    q: 'Average order quantity per month in 2008',
    rows: [1, 12], sqlMust: [/AVG/i, /GROUP BY/i], chart: true,
  },
  {
    id: 'by_product_line',
    q: 'Sales amount by product line',
    rows: [1, 10], sqlMust: [/ProductLine/i, /GROUP BY/i], chart: true,
  },
  {
    id: 'date_span',
    q: 'What are the earliest and latest order dates?',
    rows: [1, 1], sqlMust: [/MIN\s*\(/i, /MAX\s*\(/i], chart: false,
  },
];

/**
 * @typedef {Object} MlCase
 * @property {string} id
 * @property {string} q
 * @property {string} skill                 contracts.py skill name
 * @property {'confirm'|'guard'} card       the stop card expected first
 * @property {'A'|'B'} [tier]               egress tier the card must declare (data-tier)
 * @property {string[]} [sections]          fieldset legends on the confirm card, in order
 * @property {string[]} [noSections]        legends that must NOT appear
 * @property {RegExp} [tierMeta]            plain-language egress meta on the card
 * @property {RegExp[]} metaMust            status strip after the run (a number, not just a word)
 * @property {RegExp[]} modelTabMust        Model details tab after the run
 * @property {boolean} chart
 * @property {boolean} [rerun]              exercise Edit setup → Re-run afterwards
 * @property {'recommended'|'override'} [exit]  which guard exit to take
 * @property {Array<{chip: string, value: string}>} [edits]  fields to set on the confirm card before Run
 * @property {import('./mlEnvelope').EnvelopeChecks} [envelope]  data-level bounds on the result envelope (raw.analysis)
 */

// Sandbox caps (docker-compose.yml: ANALYSIS_MAX_SERIES_ROWS / ANALYSIS_MAX_ENTITY_ROWS).
const SERIES_CAP = 1500;
const ENTITY_CAP = 50_000;

// Status-strip fragments as stripSegments() renders them: a number every time,
// so a regression that drops the metric cannot pass on the word alone.
const NUM = '-?\\d+(?:\\.\\d+)?';
const META = {
  points: new RegExp(`\\d+ (?:pts|rows)`),
  horizon: (n) => new RegExp(`horizon ${n}\\b`),
  metric: /WAPE \d+(?:\.\d+)?%|MASE \d+(?:\.\d+)?/,
  flagged: /\d+ flagged/,
  shifts: /\d+ shifts?/,
  strength: new RegExp(`strength ${NUM}`),
  r: new RegExp(`\\br ${NUM}`),
  delta: /Δ -?\d+(?:\.\d+)?%/,
  segments: /\d+ segments/,
  fit: new RegExp(`R² ${NUM}|explains \\d+%`),
  rowsSent: /\d+ rows sent to model/,
};

/** @type {MlCase[]} */
const ML_CASES = [
  {
    id: 'forecast', skill: 'forecast', card: 'confirm', tier: 'A',
    q: 'Forecast total SalesAmount by month for the next 6 months',
    sections: ['Data', 'Model', 'Output'], tierMeta: /Aggregates only/,
    metaMust: [/forecast/, META.horizon(6), META.metric, META.rowsSent], modelTabMust: [/Candidates/i, /Parameters/i], chart: true, rerun: true,
    envelope: {
      params: { horizon: 6, 'series.grain': 'month' }, metric: /WAPE|MASE/i, tier: 'A', maxRowsSent: SERIES_CAP,
      guardsPassed: true, lowConfidence: false, facts: { horizon: 6 }, chartTypes: ['band'],
    },
  },
  {
    id: 'anomaly', skill: 'anomaly_detection', card: 'confirm', tier: 'A',
    q: 'Flag any abnormal spikes or drops in weekly SalesAmount during 2007',
    sections: ['Data', 'Model'], tierMeta: /Aggregates only/,
    metaMust: [/anomaly detection/, META.flagged, META.points], modelTabMust: [/Method/i, /Guards/i], chart: true,
    envelope: {
      params: { 'series.grain': 'week' }, tier: 'A', maxRowsSent: SERIES_CAP, guardsPassed: true, lowConfidence: false,
      // A year of weeks: ~52 points, a handful flagged at 95 % sensitivity, never most of them.
      facts: { n_points: [40, 60], n_flagged: [0, 15], 'sensitivity': [0.8, 0.99] }, chartTypes: ['band'],
    },
  },
  {
    id: 'changepoint', skill: 'changepoint', card: 'confirm', tier: 'A',
    q: 'When did the trend in monthly SalesAmount shift?',
    sections: ['Data', 'Model', 'Output'],
    metaMust: [/changepoint/, META.shifts], modelTabMust: [/Method/i], chart: true,
    envelope: {
      params: { 'series.grain': 'month' }, tier: 'A', maxRowsSent: SERIES_CAP, guardsPassed: true,
      facts: { n_points: [24, 60], n_changepoints: [0, 20] },
    },
  },
  {
    id: 'seasonality', skill: 'seasonality', card: 'confirm', tier: 'A',
    q: 'Is there a seasonal pattern in monthly SalesAmount?',
    sections: ['Data', 'Model'],
    metaMust: [/seasonality/, META.strength], modelTabMust: [/Season/i], chart: true,
    envelope: {
      params: { 'series.grain': 'month' }, tier: 'A', maxRowsSent: SERIES_CAP, guardsPassed: true,
      facts: { strength: [0, 1] },
    },
  },
  {
    id: 'correlation', skill: 'correlation', card: 'confirm', tier: 'A',
    q: 'Is OrderQuantity correlated with Profit by month?',
    sections: ['Data', 'Model'],
    metaMust: [/correlation/, META.r], modelTabMust: [/Method/i], chart: true,
    envelope: { tier: 'A', maxRowsSent: SERIES_CAP, guardsPassed: true, facts: { n_points: [12, 60] } },
  },
  {
    // No literal year range ("2008 compared to 2007" reads to the filter
    // grounder as a numeric range filter) and a real measure: this Postgres
    // port has no profit column, so "profit" sent the planner to
    // factfinance.amount. Slices are named so the planner stays on the fact
    // table. "Most recent quarter" is relative to today for the planner; the
    // guard re-anchors periods that miss the data to its end (Model details
    // records it under Guards as "periods").
    id: 'contribution', skill: 'contribution', card: 'confirm', tier: 'A',
    q: 'What drove the change in SalesAmount in the most recent quarter compared with the quarter before, by sales territory and promotion?',
    sections: ['Data', 'Comparison'], noSections: ['Model'],
    metaMust: [/contribution/, META.delta], modelTabMust: [/Parameters/i], chart: true,
    envelope: { tier: 'A', maxRowsSent: SERIES_CAP, guardsPassed: true },
  },
  {
    // A small entity table with well-filled integer/float features (606 rows —
    // DimCustomer's 18k exceed the sandbox's 30 s budget, and the row cap is a
    // hard limit, not a sample). DimProduct's list price / standard cost /
    // weight are NULL for about a third of the rows and are refused as
    // "mostly empty", which is correct.
    id: 'clustering', skill: 'clustering', card: 'confirm', tier: 'B',
    q: 'Segment products by weight, safety stock level and reorder point',
    sections: ['Data', 'Model'], tierMeta: /Row-level, capped/,
    metaMust: [/clustering/, META.segments, /row-level/], modelTabMust: [/silhouette|Method/i], chart: true,
    envelope: {
      tier: 'B', maxRowsSent: ENTITY_CAP, guardsPassed: true,
      // DimProduct has 606 rows; k is chosen by silhouette, which is bounded in [-1, 1].
      facts: { n_entities: [100, 1000], k: [2, 10], silhouette: [-1, 1] }, chartTypes: ['scatter', 'bar', 'horizontal_bar'],
    },
  },
  {
    // Postgres `money` measures (DimReseller.AnnualSales / AnnualRevenue, 701
    // rows, no NULLs): the entity SQL casts them so the engine sees numbers,
    // not "$1,234.00" text.
    id: 'clustering_money', skill: 'clustering', card: 'confirm', tier: 'B',
    q: 'Segment resellers by annual sales, annual revenue and number of employees',
    sections: ['Data', 'Model'], tierMeta: /Row-level, capped/,
    metaMust: [/clustering/, META.segments, /row-level/], modelTabMust: [/silhouette|Method/i], chart: true,
    envelope: {
      tier: 'B', maxRowsSent: ENTITY_CAP, guardsPassed: true,
      // DimReseller has 701 rows; the money columns must reach the engine as numbers.
      facts: { n_entities: [500, 1000], k: [2, 10], silhouette: [-1, 1] }, chartTypes: ['scatter', 'bar', 'horizontal_bar'],
    },
  },
  {
    id: 'driver', skill: 'driver_analysis', card: 'confirm', tier: 'B',
    q: 'What drives the days to manufacture of a product? Use weight, safety stock level and reorder point as candidates',
    sections: ['Data', 'Model'], tierMeta: /Row-level, capped/,
    metaMust: [/driver analysis/, META.fit, META.points], modelTabMust: [/Method/i], chart: false,
    envelope: { tier: 'B', maxRowsSent: ENTITY_CAP, guardsPassed: true, facts: { n_rows: [50, 1000], r2_holdout: [-1, 1] } },
  },
  {
    // 100 is inside the contract's horizon bound (1–104) but well past what
    // ~190 weeks of history support (max_horizon caps at n/3), so the guard
    // refuses and offers a shorter horizon as the recommended exit.
    id: 'guard_horizon', skill: 'forecast', card: 'guard', exit: 'recommended',
    q: 'Forecast weekly SalesAmount for the next 100 weeks',
    metaMust: [/forecast/, META.metric], modelTabMust: [/Guards/i], chart: true,
    // The recommended exit re-plans within the guard: a shorter horizon, no override, full confidence.
    envelope: { params: { horizon: [1, 99], 'series.grain': 'week' }, tier: 'A', guardsPassed: true, lowConfidence: false, chartTypes: ['band'] },
  },
  {
    // The same refusal, overridden: the run happens and is flagged low confidence
    // everywhere it is shown.
    id: 'guard_override', skill: 'forecast', card: 'guard', exit: 'override',
    q: 'Forecast weekly SalesAmount for the next 100 weeks',
    metaMust: [/forecast/, /low confidence/], modelTabMust: [/Low confidence: a guard was overridden/], chart: true,
    envelope: { params: { horizon: 100 }, tier: 'A', lowConfidence: true, chartTypes: ['band'] },
  },
];

/** Skills this schema cannot serve well: the outcome only has to be graceful. */
const GRACEFUL_CASES = [
  { id: 'cohort', skill: 'cohort_retention', q: 'Show customer retention by signup cohort' },
  { id: 'experiment', skill: 'experiment_test', q: 'Did variant B beat control in our A/B test on order quantity?' },
];

module.exports = { NO_WRITES, META, SERIES_CAP, ENTITY_CAP, SQL_CASES, ML_CASES, GRACEFUL_CASES };
