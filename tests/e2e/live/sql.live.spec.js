// @ts-check
// SQL questions through the real UI: each one must take the SQL path, complete,
// show a plausible number of rows, generate read-only SQL of the expected shape,
// and render the surfaces a user expects (grid, chart, answer, insights).
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');
const { SQL_CASES, NO_WRITES } = require('./questions');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');

// One page for the whole file (login and app boot happen once); the tests run
// in order on one worker but are independent — a failing question never skips
// the rest, which is what you want from a live smoke run.
test.describe('SQL questions', { tag: '@sql' }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;  // each test skips itself; nothing to set up
    const context = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
    page = await context.newPage();
    await L.openApp(page);
    await L.selectConnection(page);
  });

  test.beforeEach(async ({}, testInfo) => {
    L.skipUnlessLive();
    await L.noteCatalogSource(page, testInfo);
  });

  test.afterEach(async ({}, testInfo) => {
    if (page && testInfo.status !== testInfo.expectedStatus) {
      await testInfo.attach('workspace', { body: await page.screenshot(), contentType: 'image/png' });
    }
  });

  test.afterAll(async () => { await page?.context().close(); });

  /** Ask `c.q` in the current conversation and assert everything the user sees. */
  async function askAndVerify(c) {
    const turn = await L.ask(page, c.q, {}, L.WAIT.sql);
    L.expectPath(turn, 'sql');
    // A data answer, not a text reply ("Answered · text answer" is how the
    // app asks for clarification or declines); say what came back instead.
    expect(turn.status, `expected a data answer, got "${turn.status}" — ${turn.answer}`).toMatch(/Completed/);

    // Thread: SQL badge on the new turn, an answer sentence, insights where expected.
    const last = page.locator('#v3-thread article.v3-turn').last();
    await expect(last.locator('.v3-route-pill.is-sql')).toHaveText('SQL');
    // The answer sentence is LLM-written and occasionally empty; the data is what
    // the test guards, so an empty sentence is recorded, not failed.
    if (!turn.answer) test.info().annotations.push({ type: 'note', description: `no answer sentence for "${c.q}"` });
    if (c.insights) await expect(last.locator('.v3-insights')).toBeVisible();

    // Answer pane: title, then the SQL (read first so a row-count miss can quote it).
    await expect(page.locator('#v3-result-title')).toHaveText(c.q);
    const sql = await L.sqlText(page);
    const rows = await L.gridRows(page);
    const why = `rows for "${c.q}" — the query ran and returned ${rows} row(s):\n${sql}`;
    expect(rows, why).toBeGreaterThanOrEqual(c.rows[0]);
    expect(rows, why).toBeLessThanOrEqual(c.rows[1]);

    // SQL tab: read-only SELECT of the expected shape.
    expect(sql).toMatch(/\b(SELECT|WITH)\b/i);
    expect(sql).not.toMatch(NO_WRITES);
    for (const must of c.sqlMust) expect(sql, `SQL should match ${must}`).toMatch(must);

    if (c.chart) await L.expandChart(page);
    await L.shot(page, `sql-${c.id}`);
    return turn;
  }

  for (const c of SQL_CASES) {
    test(`${c.id}: ${c.q}`, async () => {
      // One SQL answer is ≤180 s; a follow-up case asks two questions.
      test.setTimeout(c.followupOf ? 480_000 : 300_000);
      await L.newConversation(page);
      if (c.followupOf) {
        // Conversation memory: the base question first, then the follow-up in the same thread.
        const base = SQL_CASES.find((item) => item.id === c.followupOf);
        if (!base) throw new Error(`followupOf "${c.followupOf}" is not a case`);
        await askAndVerify(base);
      }
      await askAndVerify(c);
    });
  }
});
