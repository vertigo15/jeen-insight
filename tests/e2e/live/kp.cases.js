// @ts-check
// The curated knowledge-pair test set (@kp). Each `question` is a knowledge
// pair registered in the metadata for the live connection — the gold SQL is
// fetched from the stack at run time (live/knowledgePairs.js), never copied
// here, so the test can't drift from what the LLM is shown. This file only
// says WHICH pairs are in the set and what the UI must show for each.
//
// Add a case: pick a question from live/.generated/knowledge_pairs.json (run
// the suite once) and describe the surfaces. Keep the question text exactly
// as registered (matching is whitespace/case-insensitive, punctuation-blind).
//
// Sweep every pair instead: LIVE_KP_ALL=1 (LIVE_KP_LIMIT=n to cap).

/**
 * @typedef {Object} KpCase
 * @property {string} id
 * @property {string} question              a registered knowledge-pair question
 * @property {boolean|'auto'} chart         a chart must render; 'auto' = when the result has >1 row and a numeric column
 * @property {[number, number]} [rows]      inclusive row-count range; default: at least one row
 * @property {boolean} [insights]           Key insights expected in the thread
 * @property {string} [chartTypeSwitch]     also switch the chart to this type (bar | line | pie | ...)
 * @property {boolean} [rtl]                question is Hebrew: the thread must render it right-to-left
 * @property {boolean} [smoke]              the one case the smoke suite (smoke.live.spec.js) asks
 */

/** @type {KpCase[]} */
const KP_CASES = [
  {
    id: 'sales_by_year', question: 'What is internet sales amount by calendar year',
    chart: true, rows: [3, 5], insights: true, chartTypeSwitch: 'line', smoke: true,
  },
  {
    id: 'customers_count', question: 'How many customers do we have?',
    chart: false, rows: [1, 1],
  },
  {
    id: 'top_customers', question: 'Who are our top 10 customers by revenue?',
    chart: 'auto', rows: [10, 10],
  },
  {
    id: 'revenue_by_country', question: 'Show me revenue by country',
    chart: true, rows: [3, 10],
  },
  {
    // Not "What is our revenue by sales territory?": that pair LEFT JOINs both
    // fact tables to the territory dimension (a cartesian product) and cannot
    // finish inside the statement timeout — the sweep (LIVE_KP_ALL=1) still
    // surfaces it; the curated set stays a signal about the app, not the data.
    id: 'top_territories', question: 'What are our top 5 territories by revenue?',
    chart: true, rows: [5, 5],
  },
  {
    id: 'sales_by_category', question: 'Show me sales by product category',
    chart: true, rows: [1, 5], insights: true,
  },
  {
    id: 'total_profit', question: 'What is our total profit?',
    chart: false, rows: [1, 1],
  },
  {
    id: 'revenue_by_quarter', question: 'What is our revenue by quarter?',
    chart: true, rows: [8, 20],
  },
  {
    id: 'top_products_internet', question: 'What are the top 10 products by internet revenue',
    chart: true, rows: [10, 10],
  },
  {
    id: 'avg_order_value', question: 'What is the average order value?',
    chart: false, rows: [1, 1],
  },
  {
    id: 'gross_margin_pct', question: 'What is our gross margin percentage?',
    chart: false, rows: [1, 1],
  },
  {
    id: 'heb_sales_by_territory', question: 'סכום מכירות אינטרנט לפי אזור מכירות?',
    chart: 'auto', rows: [1, 20], rtl: true,
  },
];

module.exports = { KP_CASES };
