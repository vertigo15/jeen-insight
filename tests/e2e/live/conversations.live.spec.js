// @ts-check
// Multi-turn conversations (@conv): every turn of a conversation runs in one
// session and is checked against what the previous turn actually returned —
// follow-ups inherit the grouping, memory questions name the right value, an
// ML turn after a SQL turn produces a sane envelope, "Answer with SQL instead"
// leaves the conversation usable. Cases: conversations.js.
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');
const { CONVERSATIONS } = require('./conversations');
const { NO_WRITES } = require('./questions');
const { checkEnvelope, summarizeEnvelope } = require('./mlEnvelope');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');
const PER_TURN = { sql: 300_000, followup: 300_000, memory: 240_000, ml: 420_000, sql_instead: 420_000 };

const columnNames = (results) => (results && Array.isArray(results.columns) ? results.columns : [])
  .map((c) => (typeof c === 'string' ? c : (c && (c.name || c.column)) || String(c)));

test.describe('Conversations', { tag: ['@conv', '@e2e', '@regression'] }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  let connection = '';

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;
    const context = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
    page = await context.newPage();
    await L.openApp(page);
    connection = await L.selectConnection(page);
    // ML turns must show the confirm card, so consent must not be remembered.
    const skills = new Set(CONVERSATIONS.flatMap((c) => c.turns.map((t) => t.skill).filter(Boolean)));
    for (const skill of skills) await L.forgetSkill(page, connection, /** @type {string} */ (skill)).catch(() => {});
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

  /**
   * A completed data answer — a fresh query (sql) or, for follow-ups, the prior
   * result filtered in memory (from_memory). Returns { turn, raw, prev } for the next turn.
   */
  async function dataTurn(t, testInfo, index) {
    const turn = await L.ask(page, t.q, {}, L.WAIT.sql);
    L.assertNoBackendError(turn);
    expect(turn.failed, `turn ${index} failed: ${turn.errorText}`).toBe(false);
    const allowed = t.paths || (t.kind === 'followup' ? ['sql', 'from_memory'] : ['sql']);
    expect(allowed, `turn ${index} "${t.q}" took path "${turn.path}" (${turn.pillTitle})`).toContain(turn.path);
    if (!/Completed|Answered/.test(turn.status)) {
      // A clarification instead of an answer to a plain refinement is worth a line in the report on its own.
      L.annotate(testInfo, 'finding', `follow-up "${t.q}" got "${turn.status}": ${turn.answer}`);
    }
    expect(turn.status, `turn ${index} "${t.q}": expected a data answer, got "${turn.status}" — ${turn.answer}`).toMatch(/Completed|Answered/);
    const raw = await L.rawResult(page);
    expect(raw, `turn ${index}: the controller holds the result`).toBeTruthy();
    const results = raw.results || {};
    const rowCount = Number(results.row_count ?? (results.rows ? results.rows.length : 0));
    if (turn.path === 'sql') {
      expect(raw.sql, `turn ${index}: SQL generated`).toBeTruthy();
      expect(raw.sql).not.toMatch(NO_WRITES);
      for (const must of t.sqlMust || []) expect(raw.sql, `turn ${index} SQL should match ${must}`).toMatch(must);
    } else if (raw.sql) {
      expect(raw.sql, `turn ${index}: memory computation is read-only`).not.toMatch(NO_WRITES);
    }
    if (t.rows) {
      const why = `turn ${index} rows for "${t.q}" (${turn.path}) — ${rowCount} row(s):\n${raw.sql || '(no SQL: answered from memory)'}`;
      expect(rowCount, why).toBeGreaterThanOrEqual(t.rows[0]);
      expect(rowCount, why).toBeLessThanOrEqual(t.rows[1]);
    }
    if (t.rowsMust) {
      const problem = t.rowsMust(results.rows || [], columnNames(results));
      expect(problem, `turn ${index} rows check`).toBeNull();
    }
    if (t.chart) await L.expandChart(page);
    L.annotate(testInfo, `turn_${index}`, { kind: t.kind, q: t.q, path: turn.path, rows: rowCount, latency: L.latencyOf(raw, turn.wallMs) });
    return { turn, raw, prev: { rows: results.rows || [], columns: columnNames(results), raw } };
  }

  for (const conv of CONVERSATIONS) {
    test(`${conv.id}: ${conv.title}`, { tag: ['@conv', ...(conv.tags || [])] }, async ({}, testInfo) => {
      test.setTimeout(conv.turns.reduce((total, t) => total + (PER_TURN[t.kind] || 300_000), 60_000));
      // An ML turn only makes sense when the deterministic router will send it to ML.
      for (const t of conv.turns) if (t.kind === 'ml' || t.kind === 'sql_instead') await L.requireMlRoute(page, t.q);

      await L.newConversation(page);
      /** @type {{ rows: any[], columns: string[], raw: any } | null} */
      let prev = null;
      let sessionId = null;

      for (const [i, t] of conv.turns.entries()) {
        const index = i + 1;
        await test.step(`turn ${index} (${t.kind}): ${t.q}`, async () => {
          if (t.kind === 'sql' || t.kind === 'followup') {
            const done = await dataTurn(t, testInfo, index);
            prev = done.prev;
            // Same conversation: the server keeps the session across turns.
            if (sessionId) expect(done.raw.session_id, 'follow-up stays in the same session').toBe(sessionId);
            sessionId = done.raw.session_id;
            return;
          }

          if (t.kind === 'memory') {
            expect(prev, 'a memory turn needs a previous data turn').toBeTruthy();
            const want = t.derive ? t.derive(/** @type {any} */ (prev)) : null;
            expect(want, `derive() computed an expectation from ${prev.rows.length} previous row(s)`).toBeTruthy();
            const turn = await L.ask(page, t.q, {}, L.WAIT.sql);
            L.assertNoBackendError(turn);
            expect(turn.failed, `memory turn failed: ${turn.errorText}`).toBe(false);
            const paths = t.paths || ['from_memory', 'sql'];
            expect(paths, `memory question took path "${turn.path}" (${turn.pillTitle})`).toContain(turn.path);
            const raw = await L.rawResult(page);
            const last = await L.lastTurnResult(page);
            const answer = String((last && last.result && last.result.answer) || turn.answer || '');
            const rows = (last && last.result && last.result.results && last.result.results.rows) || [];
            const haystack = `${answer}\n${JSON.stringify(rows)}`;
            const hit = want instanceof RegExp ? want.test(haystack) : haystack.includes(String(want));
            L.annotate(testInfo, `turn_${index}`, { kind: t.kind, q: t.q, path: turn.path, expected: String(want), answer: answer.slice(0, 200), latency: L.latencyOf(raw, turn.wallMs) });
            expect(hit, `memory answer should name ${JSON.stringify(String(want))} (from the previous rows) — got: "${answer}" rows=${JSON.stringify(rows).slice(0, 200)}`).toBe(true);
            return;
          }

          if (t.kind === 'ml') {
            const turn = await L.ask(page, t.q, {}, L.WAIT.card);
            L.expectPath(turn, 'ml');
            expect(turn.card, `expected a ${t.card} card, got ${turn.card || turn.status} — ${turn.answer}`).toBe(t.card || 'confirm');
            const card = L.confirmCard(page);
            if (t.skill) await expect(card).toHaveAttribute('data-skill', t.skill);
            await expect(card.locator('[data-run]')).toBeEnabled();
            await L.runCard(page, L.WAIT.run);
            await expect(L.metaRow(page).locator('.v3-status')).toHaveText(/Completed/);
            const raw = await L.rawResult(page);
            expect(raw && raw.analysis, 'the completed ML turn carries an analysis envelope').toBeTruthy();
            if (sessionId) expect(raw.session_id, 'ML turn stays in the same session').toBe(sessionId);
            const problems = checkEnvelope(raw.analysis, t.envelope || {}, { rows: (raw.results || {}).rows || [] });
            L.annotate(testInfo, `turn_${index}`, { kind: t.kind, q: t.q, envelope: summarizeEnvelope(raw.analysis), latency: L.latencyOf(raw, turn.wallMs) });
            L.annotate(testInfo, 'ml_envelope', summarizeEnvelope(raw.analysis));
            expect(problems, `ML envelope violations:\n- ${problems.join('\n- ')}`).toEqual([]);
            if (!t.envelope || t.envelope.chartTypes) await L.waitForChart(page);
            return;
          }

          if (t.kind === 'sql_instead') {
            const turn = await L.ask(page, t.q, {}, L.WAIT.card);
            L.expectPath(turn, 'ml');
            expect(turn.card, `expected a confirm card, got ${turn.card || turn.status}`).toBe('confirm');
            const before = await page.locator('#v3-thread article.v3-turn').count();
            await L.confirmCard(page).locator('[data-sql-instead]').click();
            await page.waitForFunction((n) => {
              const turns = document.querySelectorAll('#v3-thread article.v3-turn');
              const last = turns[turns.length - 1];
              return turns.length > n && last && !last.classList.contains('is-running')
                && (last.hasAttribute('data-route-path') || last.querySelector('.v3-error-block'));
            }, before, { timeout: L.WAIT.sql });
            await page.waitForFunction(() => window.ChatController && !window.ChatController.sending, null, { timeout: 60_000 }).catch(() => {});
            const last = page.locator('#v3-thread article.v3-turn').last();
            await expect(last).toHaveAttribute('data-route-path', 'sql');
            await expect(L.metaRow(page).locator('.v3-status')).toHaveText(/Completed|Answered/);
            const raw = await L.rawResult(page);
            expect(raw && raw.sql, 'SQL generated for the "answer with SQL instead" turn').toBeTruthy();
            const results = raw.results || {};
            const rowCount = Number(results.row_count ?? (results.rows ? results.rows.length : 0));
            if (t.rows) {
              expect(rowCount).toBeGreaterThanOrEqual(t.rows[0]);
              expect(rowCount).toBeLessThanOrEqual(t.rows[1]);
            }
            sessionId = raw.session_id;
            prev = { rows: results.rows || [], columns: columnNames(results), raw };
            L.annotate(testInfo, `turn_${index}`, { kind: t.kind, q: t.q, path: 'ml→sql', rows: rowCount, latency: L.latencyOf(raw, turn.wallMs) });
            return;
          }

          throw new Error(`unknown turn kind ${t.kind}`);
        });
      }
      await L.shot(page, `conv-${conv.id}`);
    });
  }
});
