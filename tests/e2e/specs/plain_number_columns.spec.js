// @ts-check
// Edge cases for columns whose numbers are labels (years, ids): the year check
// covers every row, and Hebrew identifier names are recognised.
const { test, expect } = require('@playwright/test');
const { openHarness, ask } = require('./_helpers');

test.beforeEach(async ({ page }) => {
  // The harness does not load script.js; stand in for its locale formatter.
  await page.addInitScript(() => {
    window.JeenLegacyBridge = {
      formatTableValue: (value, index, numeric) => (numeric
        ? Number(value).toLocaleString('en-US', { maximumFractionDigits: 0 })
        : String(value)),
      applyResult() {},
    };
  });
  await openHarness(page);
});

/** Make the fake backend answer `question` with this table. */
async function serveTable(page, question, columns, rows) {
  await page.evaluate(([q, cols, data]) => {
    const F = window.__FIXTURES__;
    const id = `custom_${Object.keys(F.SCENARIOS).length}`;
    F.QUESTION_TO_SCENARIO[q] = id;
    F.SCENARIOS[id] = () => ({
      question: q, query_id: id, session_id: F.SESSION, sql: 'SELECT 1',
      results: { columns: cols, rows: data, row_count: data.length },
      answer: 'Here are the rows.', status: 'completed',
      metrics: { route: 'needs_query' },
      routing: { route: 'needs_query', path: 'sql', source: 'router_llm', reason: 'lookup', skill: null },
      trace: [{ node: 'execute_query', status: 'node_finished' }],
    });
  }, [question, columns, rows]);
}

const cell = (page, row, col) => page.locator(`#v3-grid [data-row="${row}"] .v3-grid-cell`).nth(col);

test('a year-named column is checked on every row, not only the first 200', async ({ page }) => {
  const rows = Array.from({ length: 250 }, (_, i) => [2000 + (i % 20), i]);
  rows[240][0] = 15000; // cannot be a year, and sits past row 200
  await serveTable(page, 'Orders per year bucket', ['year_bucket', 'orders'], rows);
  await ask(page, 'Orders per year bucket');
  await expect(cell(page, 0, 0)).toHaveText('2,000');
});

test('Hebrew identifier names are shown without separators', async ({ page }) => {
  await serveTable(page, 'סכום לפי לקוח', ['מזהה_לקוח', 'סכום'], [[11000, 25000], [11001, 18000]]);
  await ask(page, 'סכום לפי לקוח');
  await expect(cell(page, 0, 0)).toHaveText('11000');
  await expect(cell(page, 0, 1)).toHaveText('25,000');
});
