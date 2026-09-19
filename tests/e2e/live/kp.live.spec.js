// @ts-check
// Knowledge-pair correctness (@kp): ask a question that is registered as a
// knowledge pair, then grade what came back against the pair's gold SQL
// (exact → AST-equivalent → structural → execution match; see
// sql_equivalence.py) and check the surfaces the user sees — answer, grid,
// chart, insights. Curated set: kp.cases.js. Every pair: LIVE_KP_ALL=1.
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');
const KP = require('./knowledgePairs');
const { KP_CASES } = require('./kp.cases');
const { NO_WRITES } = require('./questions');
const { gradeSql, passes, explain, sqlglotDialect } = require('./sqlEquivalence');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');
const STRICT = process.env.LIVE_KP_STRICT || 'structural';
const STRICT_ANSWER = process.env.LIVE_STRICT_ANSWER === '1';

// Read at collection time (globalSetup wrote it before the workers loaded this file).
const PAIRS = KP.loadPairs();

/** @returns {import('./kp.cases').KpCase[]} */
function sweepCases() {
  const limit = Number(process.env.LIVE_KP_LIMIT || 0) || PAIRS.pairs.length;
  return PAIRS.pairs.slice(0, limit).map((pair, index) => ({
    id: `sweep_${String(index + 1).padStart(2, '0')}_${(pair.category || 'general').replace(/\W+/g, '_')}`,
    question: pair.question,
    chart: 'auto',
    rtl: /[\u0590-\u05ff]/.test(pair.question),
  }));
}

const CASES = process.env.LIVE_KP_ALL === '1' ? sweepCases() : KP_CASES;
const SWEEP = process.env.LIVE_KP_ALL === '1';

const columnNames = (results) => (results && Array.isArray(results.columns) ? results.columns : [])
  .map((c) => (typeof c === 'string' ? c : (c && (c.name || c.column)) || String(c)));

function hasNumericColumn(results) {
  const rows = (results && results.rows) || [];
  const first = rows[0];
  if (!first) return false;
  const values = Array.isArray(first) ? first : Object.values(first);
  return values.some((v) => typeof v === 'number' || (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v.replace(/[$,%]/g, '')))));
}

test.describe('Knowledge pairs', { tag: ['@kp', '@e2e', '@regression'] }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  let connection = '';

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;
    const context = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
    page = await context.newPage();
    await L.openApp(page);
    connection = await L.selectConnection(page);
  });

  test.beforeEach(async ({}, testInfo) => {
    L.skipUnlessLive();
    await L.noteCatalogSource(page, testInfo);
    L.annotate(testInfo, 'kp_pairs_available', String(PAIRS.pairs.length));
  });

  test.afterEach(async ({}, testInfo) => {
    if (page && testInfo.status !== testInfo.expectedStatus) {
      await testInfo.attach('workspace', { body: await page.screenshot(), contentType: 'image/png' });
    }
  });

  test.afterAll(async () => { await page?.context().close(); });

  test('the stack registers knowledge pairs for the connection', async ({}, testInfo) => {
    // Precondition of the whole suite, reported once instead of on every case.
    const info = await L.connectionInfo(page, connection);
    const registered = Number(info?.metadata_summary?.knowledge_pairs ?? 0);
    L.annotate(testInfo, 'kp_registered', String(registered));
    expect(registered, 'metadata_summary.knowledge_pairs for the connection').toBeGreaterThan(0);
    expect(PAIRS.pairs.length, `pairs fetched from the resolved prompt (${PAIRS.error || 'ok'})`).toBeGreaterThan(0);
    if (!SWEEP) {
      const missing = KP_CASES.filter((c) => !KP.findPair(PAIRS.pairs, c.question)).map((c) => `${c.id}: "${c.question}"`);
      expect(missing, 'every curated case matches a registered pair').toEqual([]);
    }
  });

  for (const c of CASES) {
    test(`${c.id}: ${c.question}`, async ({}, testInfo) => {
      test.setTimeout(c.chartTypeSwitch ? 360_000 : 300_000);
      const gold = KP.findPair(PAIRS.pairs, c.question);
      L.annotate(testInfo, 'question', c.question);
      L.annotate(testInfo, 'kp_gold', gold ? 'yes' : `no — ${PAIRS.error || 'no registered pair matches this question'}`);

      await L.newConversation(page);
      const turn = await L.ask(page, c.question, {}, L.WAIT.sql);
      L.expectPath(turn, 'sql');
      expect(turn.status, `expected a data answer, got "${turn.status}" — ${turn.answer}`).toMatch(/Completed/);

      const raw = await L.rawResult(page);
      expect(raw, 'the controller holds the QueryResponse for the turn').toBeTruthy();
      expect(raw.sql, 'a SQL/DAX statement was generated').toBeTruthy();
      expect(raw.sql).not.toMatch(NO_WRITES);
      L.annotate(testInfo, 'latency', L.latencyOf(raw, turn.wallMs));

      // ── SQL / DAX against the gold pair ────────────────────────────────────
      const results = raw.results || {};
      const rowCount = Number(results.row_count ?? (results.rows ? results.rows.length : 0));
      if (gold) {
        const grade = gradeSql({
          gold: gold.sql,
          generated: raw.sql,
          dialect: sqlglotDialect(PAIRS.database_type),
          language: PAIRS.is_power_bi ? 'dax' : 'sql',
          appRows: results.rows || null,
          appColumns: columnNames(results),
          appTruncated: Boolean(results.truncated),
        });
        L.annotate(testInfo, 'kp_tier', grade.tier);
        L.annotate(testInfo, 'kp_verdict', passes(grade.tier, STRICT) ? 'pass' : 'fail');
        if (typeof grade.overlap === 'number') L.annotate(testInfo, 'kp_overlap', String(grade.overlap));
        await testInfo.attach('kp-grade.json', {
          body: JSON.stringify({ question: c.question, gold: gold.sql, generated: raw.sql, grade }, null, 2),
          contentType: 'application/json',
        });
        expect(
          passes(grade.tier, STRICT),
          `generated ${PAIRS.is_power_bi ? 'DAX' : 'SQL'} does not match the knowledge pair at strictness "${STRICT}" — ${explain(grade)}\n--- gold ---\n${gold.sql}\n--- generated ---\n${raw.sql}`,
        ).toBe(true);
      } else {
        L.annotate(testInfo, 'kp_tier', 'no_gold');
        L.annotate(testInfo, 'kp_verdict', 'no_gold');
      }

      // ── Table ──────────────────────────────────────────────────────────────
      L.annotate(testInfo, 'row_count', String(rowCount));
      if (rowCount === 0 && gold && /^(exact|equivalent)$/.test(String(testInfo.annotations.find((a) => a.type === 'kp_tier')?.description))) {
        // The app reproduced the registered SQL and still got nothing: the pair, not the app, is the suspect.
        L.annotate(testInfo, 'finding', `the registered knowledge pair for "${c.question}" returned no rows (or timed out) when run as registered`);
      }
      if (c.rows) {
        const why = `rows for "${c.question}" — the query returned ${rowCount} row(s):\n${raw.sql}`;
        expect(rowCount, why).toBeGreaterThanOrEqual(c.rows[0]);
        expect(rowCount, why).toBeLessThanOrEqual(c.rows[1]);
      } else if (!SWEEP) {
        expect(rowCount, `at least one row for "${c.question}"`).toBeGreaterThanOrEqual(1);
      }
      if (rowCount > 0) {
        const names = columnNames(results);
        const headers = await L.gridHeaders(page);
        expect(headers.length, `grid header count vs result columns ${JSON.stringify(names)}`).toBe(names.length);
        const domRows = await L.gridRows(page);
        if (rowCount <= 200) expect(domRows, 'every result row is in the grid').toBe(rowCount);
        else expect(domRows, 'the grid shows a window of a large result').toBeGreaterThan(0);
        const exportBtn = page.locator('#export-btn');
        if (await exportBtn.count()) await expect(exportBtn, 'Export is enabled for a data answer').toBeEnabled();
      }

      // ── Answer sentence ────────────────────────────────────────────────────
      const grounded = L.answerGrounded(turn.answer, results.rows || [], columnNames(results));
      L.annotate(testInfo, 'answer_grounded', grounded.grounded ? 'yes' : `no — unmatched ${JSON.stringify(grounded.unmatched)}`);
      if (!turn.answer) L.annotate(testInfo, 'note', 'no answer sentence');
      if (STRICT_ANSWER) {
        expect(turn.answer.length, 'an answer sentence').toBeGreaterThan(0);
        expect(grounded.grounded, `answer states numbers not derivable from the rows: ${JSON.stringify(grounded.unmatched)} — "${turn.answer}"`).toBe(true);
      }
      if (c.rtl) {
        const question = page.locator('#v3-thread article.v3-turn').last().locator('.v3-question');
        await expect(question).toHaveAttribute('dir', 'rtl');
      }

      // ── Chart ──────────────────────────────────────────────────────────────
      const wantChart = c.chart === true || (c.chart === 'auto' && rowCount > 1 && hasNumericColumn(results));
      if (wantChart) {
        await L.expandChart(page);
        const state = await L.chartState(page);
        const spec = (state && state.chart_spec) || {};
        expect(spec.chart_type, 'the chart has a type').toBeTruthy();
        L.annotate(testInfo, 'chart_type', String(spec.chart_type));
        const names = columnNames(results).map((n) => n.toLowerCase());
        const encoded = [spec.x, ...(Array.isArray(spec.y) ? spec.y : [spec.y]), spec.series].filter(Boolean).map((n) => String(n).toLowerCase());
        for (const column of encoded) {
          expect(names, `chart encoding "${column}" is a result column`).toContain(column);
        }
        if (c.chartTypeSwitch) {
          const after = await L.selectChartType(page, c.chartTypeSwitch);
          expect(await L.renderedChartType(page)).toBe(c.chartTypeSwitch);
          expect(after && after.chart_spec && after.chart_spec.chart_type, 'chart_spec follows the type switch').toBe(c.chartTypeSwitch);
        }
      } else {
        L.annotate(testInfo, 'chart_type', rowCount <= 1 ? 'n/a (single value)' : 'not required');
      }

      // ── Insights ───────────────────────────────────────────────────────────
      const findings = Array.isArray(raw.findings) ? raw.findings.length : 0;
      const insightsVisible = await page.locator('#v3-thread article.v3-turn').last().locator('.v3-insights').isVisible().catch(() => false);
      L.annotate(testInfo, 'insights', `${findings} finding(s), ${insightsVisible ? 'rendered' : 'not rendered'}`);
      expect(insightsVisible, 'Key insights render exactly when the answer carries findings').toBe(findings > 0);
      if (c.insights) expect(findings, 'Key insights expected for this question').toBeGreaterThan(0);

      await L.shot(page, `kp-${c.id}`);
    });
  }
});
