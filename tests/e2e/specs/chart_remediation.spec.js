// @ts-check
// Deterministic coverage for chart session state, refinement UI and races.
const { test, expect } = require('@playwright/test');
const { openHarness, mountChartManager } = require('./_helpers');

test.beforeEach(async ({ page }) => {
  await openHarness(page);
});

test.afterEach(async ({ page }) => {
  await page.evaluate(() => window.__chartTestManager?.dispose?.()).catch(() => {});
});

test('small chat applies one compact operation response without any query path', async ({ page }) => {
  await mountChartManager(page, {
    edit: [{
      response: {
        contract_version: 2,
        operations: [
          { op: 'set_chart_type', chart_type: 'line' },
          { op: 'set_color', target: 'all', color: '#22c55e' },
          { op: 'set_toggle', key: 'dataLabels', value: true },
        ],
        notes: 'Line chart in green with labels.',
        out_of_scope: false,
        reason_code: null,
      },
    }],
  });

  await page.evaluate(() => { window.__calls = []; });
  const refine = page.locator('#v3-chart-edit .chart-refine');
  await refine.locator('.chart-refine-input').fill('show line chart in green with labels');
  await refine.locator('.chart-refine-apply').click();

  await expect.poll(() => page.evaluate(() => ({
    type: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.type,
    color: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.lineStyle?.color,
    labels: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.label?.show,
  }))).toEqual({ type: 'line', color: '#22c55e', labels: true });

  const calls = await page.evaluate(() => window.__calls || []);
  const edits = calls.filter((call) => call.url.includes('/api/edit-chart'));
  expect(edits).toHaveLength(1);
  expect(edits[0].body.contract_version).toBe(2);
  expect(edits[0].body.chart_manifest.series[0].data_summary.count).toBe(3);
  expect(JSON.stringify(edits[0].body)).not.toContain('"current_config"');
  expect(JSON.stringify(edits[0].body)).not.toContain('"sample_data"');
  expect(JSON.stringify(edits[0].body)).not.toContain('"all_data"');
  expect(JSON.stringify(edits[0].body).length).toBeLessThan(6000);
  expect(calls.some((call) => /\/api\/(?:ask|analysis\/rerun|generate-chart|analysis\/chart)/.test(call.url))).toBe(false);
});

test('binding edit rebuilds without repeating the LLM request', async ({ page }) => {
  const rebuilt = await page.evaluate(() => ({
    chart_config: structuredClone(window.__FIXTURES__.CHART.lineEdit.chart_config),
    chart_spec: structuredClone(window.__FIXTURES__.CHART.lineEdit.chart_spec),
  }));
  await mountChartManager(page, {
    edit: [{
      response: {
        contract_version: 2,
        operations: [{ op: 'set_binding', x: 'region', y: 'sales' }],
        notes: 'Updated chart bindings.',
        out_of_scope: false,
        reason_code: null,
      },
    }],
    rebuild: [
      { status: 409, response: { detail: 'cache_miss' } },
      { response: rebuilt },
    ],
  });

  await page.evaluate(() => { window.__calls = []; });
  const refine = page.locator('#v3-chart-edit .chart-refine');
  await refine.locator('.chart-refine-input').fill('use region on x and sales on y');
  await refine.locator('.chart-refine-apply').click();
  await expect(refine.locator('.chart-refine-applied')).toBeVisible();

  const calls = await page.evaluate(() => window.__calls || []);
  const edits = calls.filter((call) => call.url.endsWith('/api/edit-chart'));
  const rebuilds = calls.filter((call) => call.url.includes('/api/edit-chart/rebuild'));
  expect(edits).toHaveLength(1);
  expect(rebuilds).toHaveLength(2);
  expect(rebuilds[0].body.all_data).toBeUndefined();
  expect(rebuilds[1].body.column_names).toEqual(['region', 'sales']);
  expect(rebuilds[1].body.all_data).toHaveLength(3);
  expect(calls.some((call) => /\/api\/(?:ask|analysis\/rerun|generate-chart|analysis\/chart)/.test(call.url))).toBe(false);
});

test('a chart edit survives local presentation changes and Reset restores the baseline', async ({ page }) => {
  await mountChartManager(page, {
    edit: [{
      response: {
        contract_version: 2,
        operations: [
          { op: 'set_chart_type', chart_type: 'line' },
          { op: 'set_color', target: 'all', color: '#22c55e' },
          { op: 'set_toggle', key: 'dataLabels', value: true },
        ],
        notes: 'Changed to a line chart with labels.',
        out_of_scope: false,
        reason_code: null,
      },
    }],
  });

  const refine = page.locator('#v3-chart-edit .chart-refine');
  const input = refine.locator('.chart-refine-input');
  const apply = refine.locator('.chart-refine-apply');
  await expect(input).toBeEnabled();
  await expect(apply).toHaveAttribute('aria-disabled', 'true');

  await input.fill('show line chart with labels');
  await expect(apply).toHaveAttribute('aria-disabled', 'false');
  await apply.click();
  const editCalls = await page.evaluate(() => (
    (window.__calls || []).filter((call) => call.url.includes('/api/edit-chart'))
  ));
  expect(editCalls).toHaveLength(1);
  expect(editCalls[0].body.contract_version).toBe(2);
  expect(editCalls[0].body.all_data).toBeUndefined();

  await expect(refine.locator('.chart-refine-applied')).toBeVisible();
  await expect(refine.locator('.chart-refine-applied bdi')).toHaveText('show line chart with labels');
  await expect.poll(() => page.evaluate(() => (
    window.__chartTestManager.currentEchartsOptions?.series?.[0]?.type
  ))).toBe('line');

  const legend = page.locator('#chart-options-panel-container [data-key="legend"]');
  await legend.click();
  await page.evaluate(() => {
    document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  });
  await expect.poll(() => page.evaluate(() => ({
    type: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.type,
    labels: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.label?.show,
  }))).toEqual({ type: 'line', labels: true });

  await refine.locator('.chart-refine-reset').click();
  await expect(refine.locator('.chart-refine-applied')).toBeHidden();
  await expect(input).toBeFocused();
  await expect.poll(() => page.evaluate(() => ({
    type: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.type,
    labels: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.label?.show,
    spec: window.__chartTestManager.getSaveState()?.chart_spec?.chart_type,
  }))).toEqual({ type: 'bar', labels: false, spec: 'bar' });
});

test('the newest chart generation wins when an older request finishes last', async ({ page }) => {
  await mountChartManager(page);
  await page.evaluate(() => {
    const F = window.__FIXTURES__.CHART;
    const slow = structuredClone(F.barConfig);
    slow.series[0].data = [101, 102, 103];
    const fast = structuredClone(F.lineEdit.chart_config);
    fast.series[0].data = [201, 202, 203];
    window.__CHART_FIXTURES__.generate = [
      {
        delayMs: 120,
        response: { chart_type: 'bar', chart_spec: F.barSpec, chart_config: slow },
      },
      {
        delayMs: 5,
        response: { chart_type: 'line', chart_spec: F.lineEdit.chart_spec, chart_config: fast },
      },
    ];
  });

  await page.evaluate(async () => {
    const manager = window.__chartTestManager;
    const older = manager.handleChartTypeChange('bar');
    await new Promise((resolve) => setTimeout(resolve, 10));
    const newer = manager.handleChartTypeChange('line');
    await Promise.allSettled([older, newer]);
  });

  await expect.poll(() => page.evaluate(() => ({
    type: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.type,
    data: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.data,
  }))).toEqual({ type: 'line', data: [201, 202, 203] });
});

test('chart toggles expose accessible pressed state', async ({ page }) => {
  await mountChartManager(page);
  const labels = page.locator('#chart-options-panel-container [data-key="dataLabels"]');
  await expect(labels).toHaveAttribute('aria-pressed', 'false');
  await labels.focus();
  await page.keyboard.press('Space');
  await expect(labels).toHaveAttribute('aria-pressed', 'true');
  await page.keyboard.press('Enter');
  await expect(labels).toHaveAttribute('aria-pressed', 'false');
});
