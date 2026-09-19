// @ts-check
// Result-surface features (@features): ONE real answer is fetched in
// beforeAll, then every feature is exercised on it — filter, sort, column and
// row tools, Describe, export/copy, chart type/columns/palette/toggles/PNG,
// docks, developer panel, inline trace. Expected values are computed from the
// raw rows so assertions are exact. No test here spends an LLM call.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');
// A registered knowledge pair with a text dimension and a numeric measure (a
// handful of rows, one join — fast and stable on every stack).
const QUESTION = 'Show me revenue by country';

/** Number in a cell, accepting "$1,234.56" / "12.5%" money and percent strings; NaN for text. */
const asNumber = (v) => {
  if (typeof v === 'number') return v;
  const s = String(v ?? '').trim();
  if (!/^[-+]?[$€£¥]?\s*[-+]?\d[\d,]*(\.\d+)?\s*%?$/.test(s)) return NaN;
  return Number(s.replace(/[^0-9.\-]/g, ''));
};
const cellOf = (row, column, index) => (Array.isArray(row) ? row[index] : row[column]);

test.describe('Result & chart features', { tag: ['@features', '@feature'] }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  /** @type {any} */
  let raw;
  /** @type {string[]} */
  let columns = [];
  /** @type {any[]} */
  let rows = [];
  /** Columns whose cells parse as numbers once $ and , are stripped (what a user calls numeric). */
  /** @type {number[]} */
  let numericIdx = [];
  /** Columns the table tools treat as numeric (script.js profileColumns / parseCellNumber):
   *  70 % of the cells parse, currency strings such as "$1,234.56" included. */
  /** @type {number[]} */
  let appNumericIdx = [];
  /** @type {number[]} */
  let textIdx = [];

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;
    const context = await browser.newContext({
      storageState: AUTH_FILE, viewport: { width: 1440, height: 900 },
      permissions: ['clipboard-read', 'clipboard-write'], acceptDownloads: true,
    });
    page = await context.newPage();
    await L.openApp(page);
    await L.selectConnection(page);
    await L.newConversation(page);
    const turn = await L.ask(page, QUESTION, {}, L.WAIT.sql);
    L.expectPath(turn, 'sql');
    expect(turn.status, `fixture question did not complete: ${turn.status} — ${turn.answer}`).toMatch(/Completed/);
    raw = await L.rawResult(page);
    columns = (raw.results.columns || []).map((c) => (typeof c === 'string' ? c : c.name));
    rows = raw.results.rows || [];
    expect(rows.length, 'the fixture answer has rows to work with').toBeGreaterThan(2);
    columns.forEach((column, index) => {
      const values = rows.map((row) => cellOf(row, column, index)).filter((v) => v !== null && v !== undefined && v !== '');
      const numeric = values.length > 0 && values.every((v) => Number.isFinite(asNumber(v)));
      (numeric ? numericIdx : textIdx).push(index);
      const parsable = values.filter((v) => Number.isFinite(asNumber(v))).length;
      if (values.length && parsable / values.length >= 0.7) appNumericIdx.push(index);
    });
  });

  /** ECharts option of the chart on screen (palette, legend, zoom live here, not in the saved spec). */
  async function echartsOption() {
    return page.evaluate(() => {
      const el = document.getElementById('chart-display-container');
      const inst = el && window.echarts && window.echarts.getInstanceByDom ? window.echarts.getInstanceByDom(el) : null;
      if (!inst) return null;
      const option = inst.getOption();
      const legend = Array.isArray(option.legend) ? option.legend[0] : option.legend;
      return {
        color: option.color || null,
        legendShow: legend ? legend.show !== false : null,
        dataZoom: Array.isArray(option.dataZoom) ? option.dataZoom.length : 0,
        firstSeriesData: (option.series && option.series[0] && option.series[0].data) || null,
      };
    });
  }

  test.beforeEach(async ({}, testInfo) => {
    L.skipUnlessLive();
    await L.noteCatalogSource(page, testInfo);
    L.annotate(testInfo, 'question', QUESTION);
  });

  test.afterEach(async ({}, testInfo) => {
    if (page && testInfo.status !== testInfo.expectedStatus) {
      await testInfo.attach('workspace', { body: await page.screenshot(), contentType: 'image/png' });
    }
  });

  test.afterAll(async () => { await page?.context().close(); });

  /** Visible grid cell texts for a source column. */
  async function columnCells(index) {
    return page.evaluate((i) => {
      const head = [...document.querySelectorAll('#v3-grid .v3-grid-head .v3-grid-cell')];
      const position = head.findIndex((cell) => cell.getAttribute('data-col') === String(i));
      if (position < 0) return [];
      return [...document.querySelectorAll('#v3-grid .v3-grid-row[data-row]')].map((row) => row.children[position]?.textContent?.trim() ?? '');
    }, index);
  }

  // ── Table ──────────────────────────────────────────────────────────────────

  test('filter box narrows the grid to matching rows and the caption says so', async () => {
    test.skip(!textIdx.length, 'no text column to filter on');
    const column = columns[textIdx[0]];
    const needle = String(cellOf(rows[0], column, textIdx[0])).slice(0, 4);
    const expected = rows.filter((row) => columns.some((c, i) => String(cellOf(row, c, i) ?? '').toLowerCase().includes(needle.toLowerCase()))).length;
    const filter = page.locator('#v3-result-filter');
    await filter.fill(needle);
    await expect(page.locator('#v3-grid .v3-grid-row[data-row]')).toHaveCount(expected);
    await expect(page.locator('#v3-row-caption')).toHaveText(`${expected} of ${rows.length} loaded`);
    await filter.fill('');
    await expect(page.locator('#v3-grid .v3-grid-row[data-row]')).toHaveCount(rows.length);
    await expect(page.locator('#v3-row-caption')).toContainText(`${rows.length} rows loaded`);
  });

  test('clicking a numeric header sorts ascending, again descending', async () => {
    test.skip(!numericIdx.length, 'no numeric column to sort');
    const index = numericIdx[numericIdx.length - 1];
    const header = page.locator(`#v3-grid .v3-grid-head [data-col="${index}"]`);
    await header.click();
    await expect(header).toContainText('↑');
    const asc = (await columnCells(index)).map(asNumber).filter(Number.isFinite);
    expect(asc.length).toBe(rows.length);
    for (let i = 1; i < asc.length; i += 1) expect(asc[i], `ascending at row ${i}`).toBeGreaterThanOrEqual(asc[i - 1]);
    await header.click();
    await expect(header).toContainText('↓');
    const desc = (await columnCells(index)).map(asNumber).filter(Number.isFinite);
    for (let i = 1; i < desc.length; i += 1) expect(desc[i], `descending at row ${i}`).toBeLessThanOrEqual(desc[i - 1]);
  });

  test('column menu: number format changes the cells, "% of total" adds a derived column with the right values', async ({}, testInfo) => {
    test.skip(!numericIdx.length, 'no numeric column');
    const header0 = page.locator(`#v3-grid .v3-grid-head [data-col="${numericIdx[numericIdx.length - 1]}"]`);
    await header0.click({ button: 'right' });
    const menu = page.locator('#col-ctx-menu');
    await expect(menu).toBeVisible();
    await expect(menu).toContainText('Ascending');
    await expect(menu).toContainText('Filter non-null');
    await expect(menu).toContainText('Ask about this column');
    // Money columns reach the browser as "$1,234.56" strings and must still get Format / Calculate.
    await expect(menu, `Format/Calculate offered for ${columns[numericIdx[numericIdx.length - 1]]}`).toContainText('Add % of total');
    if (!appNumericIdx.length) {
      L.annotate(testInfo, 'finding', `no column parses as numeric in the test's own rule: ${JSON.stringify(columns)}`);
      await page.keyboard.press('Escape');
      test.skip(true, 'no numeric-looking column to format');
    }
    await page.keyboard.press('Escape');
    const index = appNumericIdx[appNumericIdx.length - 1];
    const column = columns[index];
    const header = page.locator(`#v3-grid .v3-grid-head [data-col="${index}"]`);

    await header.click({ button: 'right' });
    await expect(menu).toBeVisible();
    await menu.locator('.col-ctx-item', { hasText: 'Integer' }).click();
    const formatted = await columnCells(index);
    for (const text of formatted) expect(text, 'integer format leaves no decimals').not.toMatch(/\.\d/);

    await header.click({ button: 'right' });
    await expect(menu).toBeVisible();
    await menu.locator('.col-ctx-item', { hasText: 'Add % of total' }).click();
    const derivedHeader = page.locator('#v3-grid .v3-grid-head .v3-grid-cell.is-derived', { hasText: `${column} %` });
    await expect(derivedHeader).toHaveCount(1);
    // Values: the derived column follows its source; % of total against the raw rows (current sort order is irrelevant: compare as sets).
    const values = rows.map((row) => asNumber(cellOf(row, column, index)));
    const total = values.reduce((a, b) => a + (Number.isFinite(b) ? b : 0), 0);
    const expected = values.map((v) => Math.round((v / total) * 1000) / 10).sort((a, b) => a - b);
    const shown = (await page.evaluate(() => {
      const head = [...document.querySelectorAll('#v3-grid .v3-grid-head .v3-grid-cell')];
      const position = head.findIndex((cell) => cell.classList.contains('is-derived'));
      return [...document.querySelectorAll('#v3-grid .v3-grid-row[data-row]')].map((row) => row.children[position]?.textContent ?? '');
    })).map((t) => Number(String(t).replace(/[^0-9.\-]/g, ''))).sort((a, b) => a - b);
    expect(shown.length).toBe(expected.length);
    shown.forEach((v, i) => expect(Math.abs(v - expected[i]), `% of total row ${i}: ${v} vs ${expected[i]}`).toBeLessThanOrEqual(0.15));

    // Clean up so later tests see the plain grid.
    await header.click({ button: 'right' });
    await menu.locator('.col-ctx-item', { hasText: 'Remove derived column' }).click();
    await expect(page.locator('#v3-grid .v3-grid-head .v3-grid-cell.is-derived')).toHaveCount(0);
    await header.click({ button: 'right' });
    await menu.locator('.col-ctx-item', { hasText: 'Reset format' }).click();
  });

  test('column menu: "Ask about this column" fills the composer without sending', async () => {
    const index = textIdx[0] ?? 0;
    const captured = await L.captureRequest(page, '**/api/ask/stream', async () => {
      await page.locator(`#v3-grid .v3-grid-head [data-col="${index}"]`).click({ button: 'right' });
      await page.locator('#col-ctx-menu .col-ctx-item', { hasText: 'Ask about this column' }).click();
      await expect(page.locator('#question-input')).toHaveValue(new RegExp(columns[index]));
    }, 1_500);
    expect(captured, 'nothing was sent').toBeNull();
    await page.locator('#question-input').fill('');
  });

  test('row menu offers actions for the clicked row', async () => {
    const row = page.locator('#v3-grid .v3-grid-row[data-row="0"]');
    await row.click({ button: 'right' });
    const menu = page.locator('#row-ctx-menu');
    await expect(menu).toBeVisible();
    expect((await menu.innerText()).trim().length, 'the row menu lists actions').toBeGreaterThan(10);
    // The close listeners attach on the next tick after opening; give them that tick.
    await page.waitForTimeout(50);
    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
  });

  test('Describe shows per-column statistics that match the rows', async () => {
    test.skip(!numericIdx.length, 'no numeric column');
    const button = page.locator('#describe-btn');
    await expect(button).toBeVisible();
    await button.click();
    const section = page.locator('#describe-section');
    await expect(section).toBeVisible();
    const text = (await section.innerText()).replace(/\s+/g, ' ');
    const index = numericIdx[numericIdx.length - 1];
    const column = columns[index];
    const values = rows.map((row) => asNumber(cellOf(row, column, index))).filter(Number.isFinite);
    expect(text, 'the measure column is described').toContain(column);
    // The row count is the one statistic with a single exact rendering.
    expect(text).toMatch(new RegExp(`\\b${values.length}\\b`));
    // Describe parses currency strings: Min / Max / Mean of the measure must be stated (2 decimals).
    const max = Math.max(...values);
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    const shown = L.numbersIn(text);
    expect(shown.some((n) => Math.abs(n - max) <= 0.011), `Describe states Max ${max.toFixed(2)}`).toBe(true);
    expect(shown.some((n) => Math.abs(n - mean) <= 0.011), `Describe states Mean ${mean.toFixed(2)}`).toBe(true);
    await button.click();
    await expect(section).toBeHidden();
  });

  test('Export downloads a CSV with the result header and every row', async () => {
    const { name, buffer } = await L.download(page, () => page.locator('#export-btn').click());
    expect(name).toMatch(/\.csv$/i);
    const lines = buffer.toString('utf8').split(/\r?\n/).filter((line) => line.length);
    expect(lines[0].split(',').map((s) => s.trim())).toEqual(columns);
    expect(lines.length - 1, 'one CSV line per result row').toBe(rows.length);
  });

  test('Copy puts a tab-separated table on the clipboard', async () => {
    await page.locator('#copy-results-btn').click();
    const text = await L.clipboardText(page);
    const lines = text.split(/\r?\n/).filter((line) => line.length);
    expect(lines[0].split('\t').map((s) => s.trim())).toEqual(columns);
    expect(lines.length - 1).toBe(rows.length);
  });

  // ── Chart ──────────────────────────────────────────────────────────────────

  test('chart expands, has a spec bound to result columns, and collapses again', async () => {
    await L.expandChart(page);
    const state = await L.chartState(page);
    expect(state && state.chart_spec && state.chart_spec.chart_type, 'chart_spec.chart_type').toBeTruthy();
    const lower = columns.map((c) => c.toLowerCase());
    const spec = state.chart_spec;
    for (const bound of [spec.x, ...(Array.isArray(spec.y) ? spec.y : [spec.y])].filter(Boolean)) {
      expect(lower, `encoding ${bound} is a result column`).toContain(String(bound).toLowerCase());
    }
    await expect(page.locator('#v3-chart-caption')).toContainText(`${rows.length} points`);
    const toggle = page.locator('#v3-chart-toggle');
    await toggle.click();
    await expect(toggle).toHaveText(/Expand/);
    await toggle.click();
    await L.waitForChart(page);
  });

  test('chart type menu switches bar → line → pie and the spec follows', async () => {
    await L.expandChart(page);
    for (const type of ['line', 'pie', 'bar']) {
      const state = await L.selectChartType(page, type);
      expect(await L.renderedChartType(page)).toBe(type);
      expect(state && state.chart_spec && state.chart_spec.chart_type).toBe(type);
      await expect(page.locator('#chart-type-selector-container .ctype-btn-label')).not.toBeEmpty();
    }
  });

  test('Columns picker rebinds the Y axis and the spec records it', async () => {
    test.skip(numericIdx.length < 2, 'needs two numeric columns to swap Y');
    await L.expandChart(page);
    await page.locator('#chart-cols-btn').click();
    const y = page.locator('#chart-opt-y');
    await expect(y).toBeVisible();
    const current = await y.inputValue();
    const options = await y.locator('option').evaluateAll((els) => els.map((el) => /** @type {HTMLOptionElement} */ (el).value).filter(Boolean));
    const next = options.find((o) => o !== current);
    test.skip(!next, 'only one Y candidate');
    await y.selectOption(/** @type {string} */ (next));
    await L.waitForChart(page);
    await expect.poll(async () => {
      const state = await L.chartState(page);
      const spec = (state && state.chart_spec) || {};
      return [].concat(spec.y || []).map((s) => String(s).toLowerCase());
    }, { timeout: 15_000 }).toContain(String(next).toLowerCase());
    await y.selectOption(current);
    await L.waitForChart(page);
  });

  test('palette chips and quick toggles change the rendered chart', async ({}, testInfo) => {
    await L.expandChart(page);
    const before = await echartsOption();
    const beforeDots = await page.locator('#chart-palette-dots').innerHTML();

    await page.locator('#chart-palette-btn').click();
    const chips = page.locator('#chart-palette-expand .chart-palette-chip');
    await expect(chips.first()).toBeVisible();
    // Chips re-render on selection, so hold on to the palette id, not the element.
    const chosen = await page.locator('#chart-palette-expand .chart-palette-chip[aria-checked="false"]').first().getAttribute('data-palette');
    const chip = page.locator(`#chart-palette-expand .chart-palette-chip[data-palette="${chosen}"]`);
    await chip.click();
    await expect(chip).toHaveClass(/is-active/);
    await expect(chip).toHaveAttribute('aria-checked', 'true');
    // The toolbar swatches follow the palette; the chart's own colour list follows when ECharts is reachable.
    await expect.poll(() => page.locator('#chart-palette-dots').innerHTML(), { timeout: 10_000 }).not.toBe(beforeDots);
    if (before && before.color) {
      await expect.poll(async () => JSON.stringify(((await echartsOption()) || {}).color), { timeout: 10_000 }).not.toBe(JSON.stringify(before.color));
    } else {
      L.annotate(testInfo, 'note', 'window.echarts not reachable: palette verified on the toolbar swatches only');
    }
    L.annotate(testInfo, 'palette', String(chosen));

    const legend = page.locator('.chart-opt-toggle[data-key="legend"]');
    if (await legend.count()) {
      const wasOn = await legend.evaluate((el) => el.classList.contains('is-on'));
      await legend.click();
      await expect(legend).toHaveClass(wasOn ? /^(?!.*is-on)/ : /is-on/);
      if (before) await expect.poll(async () => ((await echartsOption()) || {}).legendShow, { timeout: 10_000 }).toBe(!wasOn);
      await legend.click();
      await expect(legend).toHaveClass(wasOn ? /is-on/ : /^(?!.*is-on)/);
    }
    const zoom = page.locator('.chart-opt-toggle[data-key="dataZoom"]');
    if (await zoom.count()) {
      const wasOn = await zoom.evaluate((el) => el.classList.contains('is-on'));
      await zoom.click();
      await expect(zoom).toHaveClass(wasOn ? /^(?!.*is-on)/ : /is-on/);
      if (before) await expect.poll(async () => (((await echartsOption()) || {}).dataZoom || 0) > 0, { timeout: 10_000 }).toBe(!wasOn);
      await zoom.click();
    }
    const sort = page.locator('.chart-opt-toggle[data-key="sortDesc"]');
    if (await sort.count() && before && Array.isArray(before.firstSeriesData) && before.firstSeriesData.length > 2) {
      await sort.click();
      await expect(sort).toHaveClass(/is-on/);
      await expect.poll(async () => {
        const data = (((await echartsOption()) || {}).firstSeriesData || []).map((d) => Number(typeof d === 'object' && d !== null ? d.value : d));
        return data.every((v, i) => i === 0 || !Number.isFinite(v) || !Number.isFinite(data[i - 1]) || v <= data[i - 1]);
      }, { timeout: 10_000 }).toBe(true);
      await sort.click();
    }
  });

  test('Save PNG produces a real PNG of the chart', async ({}, testInfo) => {
    await L.expandChart(page);
    const button = page.locator('#chart-save-png-btn');
    await expect(button).toBeVisible();
    await expect(button).toBeEnabled();
    // Chromium reports the anchor download; when the event is not surfaced the
    // success toast plus the generated data URL prove the same thing.
    const toastText = page.evaluate(() => new Promise((resolve) => {
      const seen = () => [...document.querySelectorAll('.toast')].map((t) => t.textContent || '').join(' | ');
      const observer = new MutationObserver(() => { const text = seen(); if (text) { observer.disconnect(); resolve(text); } });
      observer.observe(document.body, { childList: true });
      setTimeout(() => { observer.disconnect(); resolve(seen()); }, 8_000);
    }));
    const download = page.waitForEvent('download', { timeout: 15_000 }).then((dl) => dl.path().then((file) => ({ name: dl.suggestedFilename(), buffer: fs.readFileSync(file) }))).catch(() => null);
    await button.click();
    const saved = await download;
    const toast = await toastText;
    L.annotate(testInfo, 'toast', toast || '(none)');
    if (/No chart to export|Could not generate/.test(toast)) {
      L.annotate(testInfo, 'finding', `Save PNG refused ("${toast}") while a chart is rendered — the toolbar button is bound to a ChartManager that no longer owns the chart`);
    }
    expect(toast, `Save PNG must export the chart on screen, but the app said "${toast}"`).not.toMatch(/No chart to export|Could not generate/);
    if (saved) {
      expect(saved.name).toMatch(/\.png$/i);
      expect(saved.buffer.subarray(0, 8).toString('hex')).toBe('89504e470d0a1a0a');
      expect(saved.buffer.length).toBeGreaterThan(2_000);
      return;
    }
    L.annotate(testInfo, 'note', 'no download event surfaced; verified via toast + data URL');
    expect(toast).toMatch(/Chart saved as PNG/);
    const dataUrl = await page.evaluate(() => {
      const el = document.getElementById('chart-display-container');
      const inst = el && window.echarts && window.echarts.getInstanceByDom ? window.echarts.getInstanceByDom(el) : null;
      return inst ? inst.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' }) : null;
    });
    expect(dataUrl, 'the chart can be exported as a PNG data URL').toMatch(/^data:image\/png;base64,/);
    const bytes = Buffer.from(String(dataUrl).split(',')[1], 'base64');
    expect(bytes.subarray(0, 8).toString('hex')).toBe('89504e470d0a1a0a');
    expect(bytes.length).toBeGreaterThan(2_000);
  });

  // ── Docks and diagnostics ─────────────────────────────────────────────────

  test('SQL dock shows the generated SQL, its provenance, and copies it', async () => {
    const text = await L.openDock(page, 'sql');
    const shown = (await page.locator('#v3-dock-body pre').innerText()).replace(/\s+/g, ' ').trim();
    expect(shown).toBe(String(raw.sql).replace(/\s+/g, ' ').trim());
    expect(text).toMatch(/read-only/);
    expect(text).toMatch(/validated|generated/);
    await page.locator('#v3-dock-body [data-copy-sql]').click();
    expect((await L.clipboardText(page)).replace(/\s+/g, ' ').trim()).toBe(shown);
  });

  test('profiling dock renders compact statistics for the result', async () => {
    const text = await L.openDock(page, 'profiling');
    expect(text.length, 'the profiling dock has content').toBeGreaterThan(20);
    expect(text).toContain(columns[numericIdx[0] ?? 0]);
  });

  test('developer panel shows SQL, prompts and the execution trace of the answer', async () => {
    // The top-bar button is hidden in the v3 workspace; the SQL dock's
    // "Developer details" is the user's way in.
    await L.openDock(page, 'sql');
    await page.locator('#v3-dock-body [data-dev-details]').click();
    await expect(page.locator('#dev-drawer')).toHaveAttribute('aria-hidden', 'false');
    await page.locator('#tab-sql').click();
    await expect(page.locator('#content-sql')).toBeVisible();
    expect((await page.locator('#content-sql').innerText()).replace(/\s+/g, ' ')).toContain(String(raw.sql).split(/\s+/).slice(0, 3).join(' '));
    await page.locator('#tab-prompts').click();
    await expect(page.locator('#content-prompts')).toBeVisible();
    expect(await page.locator('#content-prompts').innerText()).toMatch(/Knowledge Pairs|System|Prompt/i);
    await page.locator('#tab-trace').click();
    await expect(page.locator('#content-trace')).toBeVisible();
    const traceText = await page.locator('#content-trace').innerText();
    const nodes = (raw.trace || []).map((e) => e.node).filter(Boolean);
    expect(nodes.length, 'the answer carries a node trace').toBeGreaterThan(3);
    // The Execution tab labels nodes for humans (router.py → "Router", …) and states the route.
    expect(traceText).toMatch(/nodes:\s*\d+/);
    expect(traceText).toMatch(/route:\s*needs_query/);
    for (const [node, label] of [['fused_router', /Router/], ['sql_generator', /SQL generation/], ['execute_query', /Run SQL/], ['sqlglot_validate', /SQL validate/], ['dlp_check', /Governance check/]]) {
      if (nodes.includes(node)) expect(traceText, `${node} appears as "${label}"`).toMatch(label);
    }
    await page.locator('#dev-drawer-close').click();
    await expect(page.locator('#dev-drawer')).toHaveAttribute('aria-hidden', 'true');
  });

  test('inline "run details" lists the nodes with their timings', async () => {
    const last = page.locator('#v3-thread article.v3-turn').last();
    const toggle = last.locator('[data-trace-toggle]');
    await toggle.click();
    const trace = last.locator('.v3-trace');
    await expect(trace).toBeVisible();
    const lines = await trace.locator('.v3-trace-row').count();
    expect(lines).toBeGreaterThan(3);
    await expect(trace.locator('.v3-trace-ms').first()).toHaveText(/\d/);
    await expect(trace).toContainText('execute_query');
    await toggle.click();
    await expect(trace).toBeHidden();
  });
});
