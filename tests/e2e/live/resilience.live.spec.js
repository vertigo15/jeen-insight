// @ts-check
// The edges around a question: greetings take their own path, a confirm card
// can be answered with SQL instead, consent is remembered and can be forgotten,
// an unanswerable question stays graceful, and Retry re-sends a failed turn.
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');
const FORECAST_Q = 'Forecast total SalesAmount by month for the next 6 months';

test.describe('Resilience', { tag: ['@resilience', '@e2e'] }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  let connection = '';

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;  // each test skips itself; nothing to set up
    const context = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
    page = await context.newPage();
    await L.openApp(page);
    connection = await L.selectConnection(page);
  });

  test.beforeEach(async () => { L.skipUnlessLive(); });

  test.afterEach(async ({}, testInfo) => {
    if (page && testInfo.status !== testInfo.expectedStatus) {
      await testInfo.attach('workspace', { body: await page.screenshot(), contentType: 'image/png' });
    }
  });

  test.afterAll(async () => {
    // Never leave consent behind for the next run.
    if (page) await L.forgetSkill(page, connection, 'forecast').catch(() => {});
    await page?.context().close();
  });

  test('a greeting takes the greeting path and loads no data', { tag: ['@sql', '@smoke'] }, async () => {
    test.setTimeout(240_000);
    await L.newConversation(page);
    const turn = await L.ask(page, 'hello', {}, L.WAIT.sql);
    L.expectPath(turn, 'greeting');
    expect(turn.answer.length).toBeGreaterThan(0);
    const last = page.locator('#v3-thread article.v3-turn').last();
    await expect(last.locator('.v3-route-pill')).toHaveCount(0);  // neither SQL nor ML
    expect(await L.gridRows(page)).toBe(0);
    await expect(L.metaRow(page).locator('.v3-status')).toHaveText(/Answered|No result/);
    await L.shot(page, 'res-greeting');
  });

  test('an unanswerable question stays graceful and the next question still works', { tag: '@sql' }, async () => {
    test.setTimeout(360_000);
    await L.newConversation(page);
    const odd = await L.ask(page, "What colour is the CEO's car?", {}, L.WAIT.sql);
    L.assertNoBackendError(odd);
    if (odd.failed) {
      // A failed turn is allowed, as long as it is presented as one the user can act on.
      const last = page.locator('#v3-thread article.v3-turn').last();
      await expect(last.locator('.v3-error-block')).toBeVisible();
      await expect(last.locator('button[data-retry]')).toBeVisible();
      await expect(last.locator('.v3-error-meta')).toContainText('query_failed');
    } else {
      expect(odd.answer.length, 'a text answer or a data answer, but never an exception').toBeGreaterThan(0);
    }
    await L.shot(page, 'res-unanswerable');

    const next = await L.ask(page, 'Total sales amount by year', {}, L.WAIT.sql);
    L.expectPath(next, 'sql');
    expect(next.status).toMatch(/Completed/);
    expect(await L.gridRows(page)).toBeGreaterThan(0);
  });

  test.describe('ML consent and exits', { tag: '@ml' }, () => {
    // Consent is server-side state: start and end every test without it, so an
    // interrupted run cannot leak "don't ask again" into the next one.
    test.beforeEach(async ({}, testInfo) => {
      await L.noteCatalogSource(page, testInfo);
      await L.forgetSkill(page, connection, 'forecast');
    });
    test.afterEach(async () => { await L.forgetSkill(page, connection, 'forecast').catch(() => {}); });

    test('"Answer with SQL instead" on a confirm card switches to the SQL path', async () => {
      test.setTimeout(420_000);
      await L.requireMlRoute(page, FORECAST_Q);
      await L.newConversation(page);
      const turn = await L.ask(page, FORECAST_Q, {}, L.WAIT.card);
      L.expectPath(turn, 'ml');
      expect(turn.card).toBe('confirm');

      const before = await page.locator('#v3-thread article.v3-turn').count();
      await L.confirmCard(page).locator('[data-sql-instead]').click();
      await page.waitForFunction((n) => {
        const turns = document.querySelectorAll('#v3-thread article.v3-turn');
        const last = turns[turns.length - 1];
        return turns.length > n && last && !last.classList.contains('is-running')
          && (last.hasAttribute('data-route-path') || last.querySelector('.v3-error-block'));
      }, before, { timeout: L.WAIT.sql });
      const last = page.locator('#v3-thread article.v3-turn').last();
      await expect(last).toHaveAttribute('data-route-path', 'sql');
      await expect(last.locator('.v3-route-pill.is-sql')).toHaveText('SQL');
      await expect(L.metaRow(page).locator('.v3-status')).toHaveText(/Completed|Answered/);
      await L.shot(page, 'res-sql-instead');
    });

    test('"Don\'t ask again" skips the card next time; forgetting it brings the card back', async () => {
      // Card + run, a remembered run, then a card again: three LLM round trips.
      test.setTimeout(900_000);
      await L.requireMlRoute(page, FORECAST_Q);
      await L.newConversation(page);
      const first = await L.ask(page, FORECAST_Q, {}, L.WAIT.card);
      L.expectPath(first, 'ml');
      expect(first.card).toBe('confirm');
      await L.confirmCard(page).locator('[data-remember]').check();
      await L.runCard(page, L.WAIT.run);
      await expect(L.metaRow(page).locator('.v3-status')).toHaveText(/Completed/);
      const skills = await L.listSkills(page, connection);
      expect(skills.find((s) => s.name === 'forecast')?.remembered, 'consent is recorded server-side').toBe(true);

      // Remembered: the same question runs straight through, no card.
      await L.newConversation(page);
      const second = await L.ask(page, FORECAST_Q, {}, L.WAIT.card + L.WAIT.run);
      L.expectPath(second, 'ml');
      expect(second.card, 'no confirm card when consent is remembered').toBeNull();
      expect(second.status).toMatch(/Completed/);
      await L.shot(page, 'res-remembered');

      // Forgotten: the card is back.
      await L.forgetSkill(page, connection, 'forecast');
      await L.newConversation(page);
      const third = await L.ask(page, FORECAST_Q, {}, L.WAIT.card);
      L.expectPath(third, 'ml');
      expect(third.card).toBe('confirm');
    });
  });

  test('Retry on a failed turn re-sends the question', { tag: '@sql' }, async () => {
    test.setTimeout(360_000);
    // Force a failure the product must present gracefully: a question about a
    // table that does not exist on this connection cannot succeed as SQL.
    await L.newConversation(page);
    const turn = await L.ask(page, 'Sum the column zzz_missing in table zzz_nonexistent_table_42', {}, L.WAIT.sql);
    L.assertNoBackendError(turn);
    const last = page.locator('#v3-thread article.v3-turn').last();
    if (!turn.failed) {
      // The app may decline politely instead of failing; that is also graceful.
      expect(turn.answer.length).toBeGreaterThan(0);
      test.info().annotations.push({ type: 'note', description: `declined instead of failing: ${turn.answer.slice(0, 120)}` });
      return;
    }
    await expect(last.locator('button[data-retry]')).toBeVisible();
    const before = await page.locator('#v3-thread article.v3-turn').count();
    await last.locator('button[data-retry]').click();
    // Retry appends a fresh attempt at the same question.
    await page.waitForFunction((n) => document.querySelectorAll('#v3-thread article.v3-turn').length > n, before, { timeout: 10_000 });
    const retried = page.locator('#v3-thread article.v3-turn').last();
    await expect(retried.locator('.v3-question')).toHaveText(turn.question);
    // …and reaches a state the user can act on: an answer, or another Retry.
    await page.waitForFunction(() => {
      const turns = document.querySelectorAll('#v3-thread article.v3-turn');
      const last = turns[turns.length - 1];
      return !last.classList.contains('is-running') && (last.hasAttribute('data-route-path') || last.querySelector('button[data-retry]'));
    }, null, { timeout: L.WAIT.sql });
    const terminal = (await retried.getAttribute('data-route-path')) || (await retried.locator('button[data-retry]').count() ? 'error+retry' : 'unknown');
    expect(['sql', 'ml', 'greeting', 'error+retry']).toContain(terminal);
    await L.shot(page, 'res-retry');
  });
});
