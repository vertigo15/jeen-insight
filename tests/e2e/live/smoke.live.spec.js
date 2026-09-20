// @ts-check
// Smoke (@smoke): is the stack alive and answering? Health, login, connection,
// one knowledge-pair question end to end (route, grid, chart), the ML router
// and confirm card (no run), and restore after reload. Two LLM calls, under
// eight minutes. Run after every deploy: `npm run test:live:smoke`.
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');
const KP = require('./knowledgePairs');
const { KP_CASES } = require('./kp.cases');
const { NO_WRITES } = require('./questions');
const { gradeSql, passes, sqlglotDialect } = require('./sqlEquivalence');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');
const PAIRS = KP.loadPairs();
const CASE = KP_CASES.find((c) => c.smoke) || KP_CASES[0];
const FORECAST_Q = 'Forecast total SalesAmount by month for the next 6 months';

test.describe('Smoke', { tag: ['@smoke'] }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  let connection = '';

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;
    const context = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
    page = await context.newPage();
  });

  test.beforeEach(async ({}, testInfo) => {
    L.skipUnlessLive();
    if (connection) await L.noteCatalogSource(page, testInfo);
  });

  test.afterEach(async ({}, testInfo) => {
    if (page && testInfo.status !== testInfo.expectedStatus) {
      await testInfo.attach('workspace', { body: await page.screenshot(), contentType: 'image/png' });
    }
  });

  test.afterAll(async () => { await page?.context().close(); });

  test('the UI is healthy and the session is valid', async ({}, testInfo) => {
    const health = await page.request.get('/health', { timeout: 30_000 });
    expect(health.ok(), `/health → ${health.status()}`).toBe(true);
    const body = await health.json().catch(() => ({}));
    L.annotate(testInfo, 'health', JSON.stringify(body).slice(0, 300));
    const me = await page.request.get('/api/auth/me', { timeout: 30_000 });
    expect(me.status(), 'the saved session authenticates').toBe(200);
    const user = await me.json();
    expect(user.email).toBeTruthy();
    L.annotate(testInfo, 'user', `${user.email} (${user.role})`);
  });

  test('the workspace boots and the connection is selectable with metadata', async ({}, testInfo) => {
    await L.openApp(page);
    connection = await L.selectConnection(page);
    const info = await L.connectionInfo(page, connection);
    expect(info.is_active, 'connection is active').toBe(true);
    const summary = info.metadata_summary || {};
    L.annotate(testInfo, 'metadata_summary', JSON.stringify(summary));
    expect(Number(summary.tables || 0), 'tables registered').toBeGreaterThan(0);
    expect(Number(summary.knowledge_pairs || 0), 'knowledge pairs registered').toBeGreaterThan(0);
  });

  test(`one knowledge-pair question answers end to end: ${CASE.question}`, async ({}, testInfo) => {
    test.setTimeout(300_000);
    expect(connection, 'connection selected by the previous smoke step').toBeTruthy();
    L.annotate(testInfo, 'question', CASE.question);
    await L.newConversation(page);
    const turn = await L.ask(page, CASE.question, {}, L.WAIT.sql);
    L.expectPath(turn, 'sql');
    expect(turn.status).toMatch(/Completed/);
    const raw = await L.rawResult(page);
    expect(raw.sql).toBeTruthy();
    expect(raw.sql).not.toMatch(NO_WRITES);
    L.annotate(testInfo, 'latency', L.latencyOf(raw, turn.wallMs));
    const rowCount = Number(raw.results?.row_count ?? raw.results?.rows?.length ?? 0);
    expect(rowCount).toBeGreaterThan(0);
    expect(await L.gridRows(page)).toBe(Math.min(rowCount, 500));
    const gold = KP.findPair(PAIRS.pairs, CASE.question);
    if (gold) {
      const grade = gradeSql({ gold: gold.sql, generated: raw.sql, dialect: sqlglotDialect(PAIRS.database_type), language: PAIRS.is_power_bi ? 'dax' : 'sql' });
      L.annotate(testInfo, 'kp_tier', grade.tier);
      L.annotate(testInfo, 'kp_verdict', passes(grade.tier) ? 'pass' : 'fail');
      if (typeof grade.overlap === 'number') L.annotate(testInfo, 'kp_overlap', String(grade.overlap));
    } else {
      L.annotate(testInfo, 'kp_tier', 'no_gold');
      L.annotate(testInfo, 'kp_verdict', 'no_gold');
    }
    if (CASE.chart) {
      await L.expandChart(page);
      const state = await L.chartState(page);
      expect(state && state.chart_spec && state.chart_spec.chart_type).toBeTruthy();
      L.annotate(testInfo, 'chart_type', String(state.chart_spec.chart_type));
    }
  });

  test('the ML router recognises a forecast and shows the confirm card (no run)', async ({}, testInfo) => {
    test.setTimeout(240_000);
    expect(connection).toBeTruthy();
    const preview = await L.routingPreview(page, FORECAST_Q);
    L.annotate(testInfo, 'routing_preview', JSON.stringify(preview));
    test.skip(!preview.ml_skills_enabled, 'ML_SKILLS_ENABLED is off on this stack');
    expect(preview.would_route).toBe('needs_analysis');
    await L.forgetSkill(page, connection, 'forecast');
    await L.newConversation(page);
    const turn = await L.ask(page, FORECAST_Q, {}, L.WAIT.card);
    L.expectPath(turn, 'ml');
    // The planner stops for the user either way: a confirm card when the catalog
    // names the date axis, a clarify card (pick the date column) when it does not
    // — the db catalog source lacks the column roles the MCP catalog carries.
    // Both prove the router and planner; nothing runs until the user acts.
    expect(['confirm', 'clarify'], `expected a stop card, got ${turn.card || turn.status} — ${turn.answer}`).toContain(turn.card);
    const card = page.locator('#v3-placeholder .v3-ml-card');
    if (turn.card === 'confirm') {
      await expect(card).toHaveAttribute('data-skill', 'forecast');
      await expect(card.locator('[data-run]')).toBeEnabled();
    } else {
      const options = await card.locator('[data-exit]').count();
      expect(options, 'a clarification must offer choices').toBeGreaterThan(0);
      L.annotate(testInfo, 'finding', `planner asked for clarification instead of confirming: ${turn.answer || (await card.locator('.v3-ml-message').textContent())}`);
    }
    L.annotate(testInfo, 'ml_card', turn.card);
    const last = await L.lastTurnResult(page);
    L.annotate(testInfo, 'latency', L.latencyOf(last && last.result, turn.wallMs));
  });

  test('a reload restores the conversation', async () => {
    expect(connection).toBeTruthy();
    const before = await page.locator('#v3-thread article.v3-turn').count();
    expect(before).toBeGreaterThan(0);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await L.openApp(page);
    await L.settle(page, 90_000);
    await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(before, { timeout: 60_000 });
  });
});
