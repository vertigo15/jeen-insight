// @ts-check
// ML questions through the real UI: routing → confirm (or guard) card with the
// grouped setup form → Run → completed result with the skill in the strip,
// Model details and a chart. The forecast case also edits the setup and re-runs.
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');
const { ML_CASES, GRACEFUL_CASES } = require('./questions');
const { checkEnvelope, summarizeEnvelope } = require('./mlEnvelope');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');
// Budgets, not hopes: card (≤150 s) + run (≤180 s) + assertions; a re-run or a
// guard exit followed by the patched card's run adds another run.
const BUDGET = { case: 420_000, rerun: 660_000, guard: 600_000, graceful: 240_000 };
// How the UI names each skill (analysisPanel.js SKILL_LABEL).
const SKILL_LABEL = {
  anomaly_detection: 'anomaly detection', forecast: 'forecast', changepoint: 'changepoint', seasonality: 'seasonality',
  correlation: 'correlation', contribution: 'contribution', clustering: 'clustering', driver_analysis: 'driver analysis',
  regression: 'regression', classification: 'classification', cohort_retention: 'cohort retention', experiment_test: 'A/B test',
};

test.describe('ML questions', { tag: ['@ml', '@e2e', '@regression'] }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  let connection = '';

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;  // each test skips itself; nothing to set up
    const context = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
    page = await context.newPage();
    await L.openApp(page);
    connection = await L.selectConnection(page);
    // The confirm card only shows while consent is not remembered.
    const skills = new Set([...ML_CASES, ...GRACEFUL_CASES].map((c) => c.skill));
    for (const skill of skills) await L.forgetSkill(page, connection, skill);
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

  /** Assert the finished result the way the user sees it. */
  async function expectCompleted(c) {
    const meta = L.metaRow(page);
    await expect(meta.locator('.v3-status')).toHaveText(/Completed/);
    // "Completed" without a skill chip is the SQL fallback for an empty read
    // (the thread says why, e.g. "the query returned no rows for this request").
    if (!(await meta.locator('.v3-skill-chip').count())) {
      const why = (await page.locator('#v3-thread article.v3-turn').last().locator('.v3-summary').innerText().catch(() => '')).trim();
      throw new Error(`the run completed without an analysis for "${c.q}" — ${why || 'no explanation in the thread'}`);
    }
    await expect(meta.locator('.v3-skill-chip').first()).toBeVisible();
    const metaText = (await meta.innerText()).replace(/\s+/g, ' ');
    for (const must of c.metaMust) expect(metaText, `status strip should match ${must}`).toMatch(must);
    const last = page.locator('#v3-thread article.v3-turn').last();
    await expect(last).toHaveAttribute('data-route-path', 'ml');
    await expect(last.locator('.v3-route-pill.is-ml')).toHaveText('ML skill');
    // Model details is the one extra dock tab an ML answer earns; it must
    // name a real method and say how many rows left the database.
    await expect(page.locator('[data-dock="model"]')).toBeVisible();
    const details = await L.openDock(page, 'model');
    expect(details, 'Model details names the method').not.toMatch(/Method\s*\n\s*—/);
    expect(details, 'Model details states rows sent').toMatch(/Rows sent\s*\n\s*\d+/);
    for (const must of c.modelTabMust) expect(details, `Model details should match ${must}`).toMatch(must);
    if (c.chart) await L.waitForChart(page);

    // Data level: the envelope behind the strip must be sane, not just rendered.
    const raw = await L.rawResult(page);
    expect(raw && raw.analysis, 'the completed ML turn carries an analysis envelope').toBeTruthy();
    expect(raw.analysis.skill, 'envelope skill').toBe(c.skill);
    const info = test.info();
    L.annotate(info, 'ml_envelope', summarizeEnvelope(raw.analysis));
    L.annotate(info, 'latency', L.latencyOf(raw));
    const problems = checkEnvelope(raw.analysis, c.envelope || {}, { rows: (raw.results || {}).rows || [] });
    await info.attach('ml-envelope.json', { body: JSON.stringify(raw.analysis, null, 2), contentType: 'application/json' });
    expect(problems, `ML envelope violations for "${c.q}":\n- ${problems.join('\n- ')}`).toEqual([]);
  }

  for (const c of ML_CASES) {
    test(`${c.id}: ${c.q}`, async () => {
      test.setTimeout(c.rerun ? BUDGET.rerun : c.card === 'guard' ? BUDGET.guard : BUDGET.case);
      // The deterministic router must send the question to ML before we spend an LLM run on it.
      await L.requireMlRoute(page, c.q);
      await L.newConversation(page);

      const turn = await L.ask(page, c.q, {}, L.WAIT.card);
      L.expectPath(turn, 'ml');
      expect(turn.card, `expected a ${c.card} card, got ${turn.card || turn.status} — ${turn.answer}`).toBe(c.card);
      await L.shot(page, `ml-${c.id}-card`);

      if (c.card === 'guard') {
        // A refusal names the shortfall in numbers, sends nothing, and offers
        // exits: one recommended, and an override that runs anyway flagged low-confidence.
        const guard = L.guardCard(page);
        await expect(guard.locator('.v3-ml-card-head .v3-skill-chip')).toHaveText(SKILL_LABEL[c.skill] || c.skill);
        await expect(guard.locator('.v3-ml-refusal')).toContainText(/\d/);
        await expect(guard.locator('.v3-ml-egress')).toContainText('0 rows sent');
        const exit = guard.locator(c.exit === 'override' ? '.v3-ml-exit.is-override' : '.v3-ml-exit.is-recommended');
        if (c.exit === 'override' && !(await exit.count())) {
          test.info().annotations.push({ type: 'note', description: 'this guard offers no override exit' });
          return;
        }
        await expect(exit).toBeVisible();
        let before = await page.locator('#v3-thread article.v3-turn').count();
        await exit.click();
        await L.waitForCompleted(page, before, L.WAIT.run);
        // A guard exit re-runs the guard and, on a first run, still asks for
        // consent: the patched plan comes back as a confirm card. Run it.
        if (await L.confirmCard(page).count()) {
          await expect(L.confirmCard(page).locator('[data-run]')).toBeEnabled();
          await L.shot(page, `ml-${c.id}-patched-card`);
          before = await page.locator('#v3-thread article.v3-turn').count();
          await L.confirmCard(page).locator('[data-run]').click();
          await L.waitForCompleted(page, before, L.WAIT.run);
        }
        await expectCompleted(c);
        if (c.exit === 'override') {
          await expect(L.metaRow(page).locator('.v3-lowconf-pill')).toBeVisible();
          await expect(page.locator('#v3-thread article.v3-turn').last().locator('.v3-lowconf-pill')).toBeVisible();
        }
        await L.shot(page, `ml-${c.id}-result`);
        return;
      }

      // Confirm card: skill, tier, grouped setup form, live summary, plain-language egress.
      const card = L.confirmCard(page);
      await expect(card).toHaveAttribute('data-skill', c.skill);
      if (c.tier) await expect(card).toHaveAttribute('data-tier', c.tier);
      if (c.sections) await expect(card.locator('.v3-ml-group legend')).toHaveText(c.sections);
      for (const legend of c.noSections || []) {
        await expect(card.locator('.v3-ml-group legend', { hasText: legend })).toHaveCount(0);
      }
      await expect(card.locator('[data-summary]')).not.toBeEmpty();
      await expect(card.locator('.v3-ml-egress')).toContainText(/analysis service/);
      if (c.tierMeta) await expect(card.locator('.v3-ml-tiermeta')).toHaveText(c.tierMeta);
      await expect(card.locator('[data-run]')).toBeEnabled();
      // Every field the planner filled has a value; none is flagged invalid before the user touches it.
      await expect(card.locator('.v3-ml-field.is-invalid')).toHaveCount(0);
      // Fields the case sets before running (e.g. a smaller row cap); the change
      // must be marked and the card must still validate.
      for (const edit of c.edits || []) {
        const control = card.locator(`[data-chip="${edit.chip}"]`);
        if ((await control.evaluate((el) => el.tagName)) === 'SELECT') await control.selectOption(edit.value);
        else await control.fill(edit.value);
        await expect(card.locator(`.v3-ml-field[data-field="${edit.chip}"]`)).toHaveClass(/is-changed/);
      }
      if (c.edits?.length) await expect(card.locator('.v3-ml-field.is-invalid')).toHaveCount(0);

      await L.runCard(page, L.WAIT.run);
      await expectCompleted(c);
      await L.shot(page, `ml-${c.id}-result`);

      if (c.rerun) {
        // Edit setup on the finished answer → shorten the horizon → Re-run →
        // child turn with a diff. Shorter, not longer: the max_horizon guard
        // caps the horizon at a third of the history, so a longer one is a
        // legitimate refusal, not a re-run.
        const edit = L.metaRow(page).locator('[data-ml-edit]');
        await expect(edit).toBeVisible();
        await edit.click();
        const setup = page.locator('#v3-ml-definition .v3-ml-card.is-definition');
        await expect(setup).toBeVisible();
        await expect(setup.locator('[data-summary]')).toBeInViewport();
        await expect(setup.locator('[data-run]')).toBeDisabled();
        const horizon = setup.locator('[data-chip="horizon"]');
        const original = Number(await horizon.inputValue());
        const next = Math.max(1, original - 2);
        await horizon.fill(String(next));
        await expect(setup.locator('.v3-ml-field[data-field="horizon"]')).toHaveClass(/is-changed/);
        await expect(setup.locator('[data-run]')).toBeEnabled();
        const before = await page.locator('#v3-thread article.v3-turn').count();
        await setup.locator('[data-run]').click();
        await L.waitForCompleted(page, before, L.WAIT.run);
        await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(before + 1);
        await expect(L.metaRow(page).locator('.v3-status')).toHaveText(/Completed/);
        await expect(L.metaRow(page)).toContainText(`horizon ${next}`);
        // Section titles render upper-case via CSS and innerText reports the rendered text.
        const details = await L.openDock(page, 'model');
        expect(details).toMatch(/changed from the previous run/i);
        expect(details).toMatch(new RegExp(`horizon:\\s*${original}\\s*→\\s*${next}`));
        await L.shot(page, `ml-${c.id}-rerun`);
      }
    });
  }

  for (const c of GRACEFUL_CASES) {
    test(`${c.id} (graceful): ${c.q}`, async () => {
      test.setTimeout(BUDGET.graceful);
      // This schema has no clean cohort/experiment tables. Whatever the app does —
      // a confirm/clarify/guard card, or falling back to SQL — it must not error.
      await L.newConversation(page);
      const turn = await L.ask(page, c.q, {}, L.WAIT.card);
      L.assertNoBackendError(turn);
      expect(turn.failed, `graceful outcome expected, got an error: ${turn.errorText}`).toBe(false);
      expect(['ml', 'sql']).toContain(turn.path);
      if (turn.path === 'ml') expect(['confirm', 'clarify', 'guard']).toContain(turn.card);
      else expect(turn.status).toMatch(/Completed|Answered/);
      await L.shot(page, `ml-${c.id}-graceful`);
    });
  }
});
