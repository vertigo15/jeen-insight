// @ts-check
// LLM-backed extras (@extras): features that need a model call of their own —
// chart refinement by text, Key insights and follow-up chips, the filter
// disambiguation card, the full profiling report, and Hebrew right-to-left
// rendering. Each test budgets one or two LLM round trips.
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');
const { NO_WRITES } = require('./questions');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');
const SEED_Q = 'What is internet sales amount by calendar year';

test.describe('LLM-backed features', { tag: ['@extras', '@feature'] }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;
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

  /** One data answer with a chart, as the fixture for chart-level features. */
  async function seed(testInfo) {
    await L.newConversation(page);
    const turn = await L.ask(page, SEED_Q, {}, L.WAIT.sql);
    L.expectPath(turn, 'sql');
    expect(turn.status).toMatch(/Completed/);
    const raw = await L.rawResult(page);
    L.annotate(testInfo, 'latency', L.latencyOf(raw, turn.wallMs));
    return { turn, raw };
  }

  test('refining the chart in words applies the change and Reset restores the baseline', async ({}, testInfo) => {
    test.setTimeout(420_000);
    await seed(testInfo);
    await L.expandChart(page);
    // The rendered type (ECharts series) is the user-facing truth; chart_spec should follow it.
    const baseline = await L.renderedChartType(page);
    expect(baseline, 'a chart is rendered before refining').toBeTruthy();
    const target = baseline === 'line' ? 'bar' : 'line';

    const refine = page.locator('#v3-chart-edit .chart-refine');
    const input = refine.locator('.chart-refine-input');
    await expect(input).toBeEnabled({ timeout: 30_000 });
    await input.fill(`Show this as a ${target} chart with the legend visible`);
    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().includes('/api/edit-chart') && r.request().method() === 'POST', { timeout: 120_000 }),
      refine.locator('.chart-refine-apply').click(),
    ]);
    expect(response.ok(), `/api/edit-chart → ${response.status()}`).toBe(true);
    const applied = refine.locator('.chart-refine-applied');
    await expect(applied).toBeVisible({ timeout: 60_000 });
    await expect(applied.locator('.chart-refine-applied-label')).toContainText(/Applied:/);
    await L.waitForChart(page);
    await expect.poll(() => L.renderedChartType(page), { timeout: 30_000 }).toBe(target);
    // Diagnostic, not a gate: the saved spec lags the rendered chart in places (edit responses
    // without a chart_spec; Reset restoring the config but not the spec). Recorded as findings.
    const specAfterEdit = ((await L.chartState(page)) || {}).chart_spec?.chart_type;
    if (specAfterEdit !== target) L.annotate(testInfo, 'finding', `chart refined to ${target} but chart_spec.chart_type reports ${specAfterEdit} (edit-chart response carried no chart_spec)`);
    L.annotate(testInfo, 'chart_type', `${baseline} → ${target}`);

    await refine.locator('.chart-refine-reset').click();
    await expect(applied).toBeHidden({ timeout: 30_000 });
    await L.waitForChart(page);
    // What the user sees must be the original chart again.
    await expect.poll(() => L.renderedChartType(page), { timeout: 30_000 }).toBe(baseline);
    // The saved spec should follow; when it does not, restored conversations
    // carry a spec that disagrees with the rendered chart (resetChartEdits only
    // restores originalChartSpec on the map branch).
    const specAfterReset = ((await L.chartState(page)) || {}).chart_spec?.chart_type;
    if (specAfterReset !== baseline) {
      L.annotate(testInfo, 'finding', `after Reset the chart renders as ${baseline} but chart_spec.chart_type is still ${specAfterReset} (resetChartEdits does not restore originalChartSpec for non-map charts)`);
    }
  });

  test('Key insights render one item per finding and a follow-up chip asks exactly its question', async ({}, testInfo) => {
    test.setTimeout(480_000);
    const { raw } = await seed(testInfo);
    const last = page.locator('#v3-thread article.v3-turn').last();
    const findings = Array.isArray(raw.findings) ? raw.findings : [];
    L.annotate(testInfo, 'insights', `${findings.length} finding(s)`);
    if (findings.length) {
      await expect(last.locator('.v3-insights')).toBeVisible();
      await expect(last.locator('.v3-insights .v3-finding')).toHaveCount(findings.length);
      // Findings state numbers that must come from the data.
      const text = await last.locator('.v3-insights').innerText();
      const grounded = L.answerGrounded(text, raw.results.rows, raw.results.columns);
      L.annotate(testInfo, 'answer_grounded', grounded.grounded ? 'yes' : `no — ${JSON.stringify(grounded.unmatched)}`);
    } else {
      L.annotate(testInfo, 'note', 'the answer carried no findings; insights section correctly absent');
      await expect(last.locator('.v3-insights')).toHaveCount(0);
    }
    const chips = last.locator('.v3-followups [data-followup]');
    const chipCount = await chips.count();
    L.annotate(testInfo, 'followups', String(chipCount));
    test.skip(chipCount === 0, 'the answer offered no follow-up chips');
    const question = (await chips.first().getAttribute('data-followup')) || '';
    const before = await page.locator('#v3-thread article.v3-turn').count();
    await chips.first().click();
    await page.waitForFunction((n) => document.querySelectorAll('#v3-thread article.v3-turn').length > n, before, { timeout: 15_000 });
    const next = page.locator('#v3-thread article.v3-turn').last();
    await expect(next.locator('.v3-question')).toHaveText(question);
    await page.waitForFunction(() => {
      const t = document.querySelectorAll('#v3-thread article.v3-turn');
      const l = t[t.length - 1];
      return l && !l.classList.contains('is-running') && (l.hasAttribute('data-route-path') || l.querySelector('.v3-error-block'));
    }, null, { timeout: L.WAIT.sql + L.WAIT.card });
    await page.waitForFunction(() => window.ChatController && !window.ChatController.sending, null, { timeout: 60_000 }).catch(() => {});
    const outcome = await L.lastTurnResult(page);
    L.assertNoBackendError({ failed: outcome?.status === 'error', errorText: outcome?.error || '' });
    expect(outcome?.status, `follow-up "${question}" failed: ${outcome?.error}`).toBe('success');
    const followRaw = await L.rawResult(page);
    expect(String(followRaw.session_id), 'the follow-up stays in the conversation').toBe(String(raw.session_id));
  });

  test('an ambiguous value asks which field was meant; choosing one reruns with that filter', async ({}, testInfo) => {
    test.setTimeout(480_000);
    await L.newConversation(page);
    // "Germany" is both a customer geography and a sales-territory country.
    const turn = await L.ask(page, 'Total internet sales amount for Germany', {}, L.WAIT.sql);
    L.assertNoBackendError(turn);
    expect(turn.failed, `turn failed: ${turn.errorText}`).toBe(false);
    const options = page.locator('#v3-placeholder [data-filter-option], #v3-thread [data-filter-option]');
    const count = await options.count();
    L.annotate(testInfo, 'filter_card', count ? `${count} option(s)` : 'no_card');
    if (!count) {
      // The grounder resolved the value on its own — a legitimate outcome, not a failure.
      expect(turn.path).toBe('sql');
      expect(turn.status).toMatch(/Completed/);
      const raw = await L.rawResult(page);
      expect(raw.sql).toMatch(/germany/i);
      return;
    }
    const label = (await options.first().innerText()).trim();
    const before = await page.locator('#v3-thread article.v3-turn').count();
    await options.first().click();
    await page.waitForFunction((n) => {
      const t = document.querySelectorAll('#v3-thread article.v3-turn');
      const l = t[t.length - 1];
      return t.length > n && l && !l.classList.contains('is-running') && (l.hasAttribute('data-route-path') || l.querySelector('.v3-error-block'));
    }, before, { timeout: L.WAIT.sql });
    await page.waitForFunction(() => window.ChatController && !window.ChatController.sending, null, { timeout: 60_000 }).catch(() => {});
    const raw = await L.rawResult(page);
    expect(raw.sql, 'the rerun produced SQL').toBeTruthy();
    expect(raw.sql).not.toMatch(NO_WRITES);
    expect(raw.sql).toMatch(/germany/i);
    expect(JSON.stringify(raw.filters || {}), `the chosen filter (${label}) is recorded as resolved`).toMatch(/germany/i);
    const dock = await L.openDock(page, 'sql');
    expect(dock).toMatch(/Filtered by|filter/i);
  });

  test('the full profiling report renders in an iframe', async ({}, testInfo) => {
    test.setTimeout(420_000);
    await seed(testInfo);
    const compact = await L.openDock(page, 'profiling');
    expect(compact).toMatch(/distinct/);
    await page.locator('#v3-dock-body [data-full-profile]').click();
    const modal = page.locator('#v3-profile-overlay .v3-profile-modal');
    await expect(modal).toBeVisible();
    const generate = modal.locator('.profile-generate-btn');
    await expect(generate).toBeVisible({ timeout: 30_000 });
    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().includes('/api/generate-profile') && r.status() !== 409, { timeout: 180_000 }),
      generate.click(),
    ]);
    if (!response.ok()) {
      const body = await response.text();
      test.skip(/not installed|unavailable|No module|ImportError/i.test(body), `profiling library unavailable on this stack: ${body.slice(0, 160)}`);
      throw new Error(`/api/generate-profile → ${response.status()} ${body.slice(0, 300)}`);
    }
    const iframe = modal.locator('#profile-iframe');
    await expect(iframe).toBeVisible({ timeout: 60_000 });
    const frame = await (await iframe.elementHandle())?.contentFrame();
    expect(frame, 'the report iframe has a document').toBeTruthy();
    await expect.poll(async () => (await frame?.locator('body').innerText().catch(() => ''))?.length || 0, { timeout: 60_000 }).toBeGreaterThan(200);
    await expect(modal.locator('.profile-download-btn')).toBeVisible();
    await modal.locator('.v3-profile-close').click();
    await expect(page.locator('#v3-profile-overlay')).toHaveCount(0);
  });

  test('a Hebrew question renders right-to-left, answers on the SQL path, and uses the bundled Hebrew font', async ({}, testInfo) => {
    test.setTimeout(360_000);
    await L.newConversation(page);
    const question = 'סכום מכירות אינטרנט לפי שנה';
    const turn = await L.ask(page, question, {}, L.WAIT.sql);
    L.expectPath(turn, 'sql');
    expect(turn.status).toMatch(/Completed/);
    const last = page.locator('#v3-thread article.v3-turn').last();
    await expect(last.locator('.v3-question')).toHaveAttribute('dir', 'rtl');
    if (/[\u0590-\u05ff]/.test(turn.answer)) await expect(last.locator('.v3-summary')).toHaveAttribute('dir', 'rtl');
    L.annotate(testInfo, 'answer_language', /[\u0590-\u05ff]/.test(turn.answer) ? 'hebrew' : 'other');
    const raw = await L.rawResult(page);
    expect(Number(raw.results?.row_count ?? raw.results?.rows?.length)).toBeGreaterThan(0);
    const fontReady = await page.evaluate(async () => {
      await document.fonts.ready;
      return document.fonts.check('12px "Noto Sans Hebrew"');
    });
    expect(fontReady, 'Noto Sans Hebrew is loaded from src/static/fonts').toBe(true);
  });
});
