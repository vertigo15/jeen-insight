// @ts-check
// Deterministic coverage for the mini chat fast path, structural rebuilds,
// what-if scenarios and code-owned rejection messages.
const { test, expect } = require('@playwright/test');
const { openHarness, mountChartManager } = require('./_helpers');

test.beforeEach(async ({ page }) => {
  await openHarness(page);
});

test.afterEach(async ({ page }) => {
  await page.evaluate(() => window.__chartTestManager?.dispose?.()).catch(() => {});
});

const PIE_REBUILD = {
  chart_spec: {
    chart_type: 'pie', x: 'region', y: ['sales'], series: null, aggregate: 'sum',
    sort: 'none', title: 'Sales by region', value_format: 'number',
  },
  chart_config: {
    tooltip: { trigger: 'item' },
    legend: { orient: 'vertical', left: 'left' },
    series: [{
      name: 'Sales', type: 'pie', radius: '62%', center: ['58%', '54%'],
      data: [{ name: 'North', value: 10 }, { name: 'South', value: 30 }, { name: 'West', value: 20 }],
    }],
  },
};

test('common intents apply instantly with no model call', async ({ page }) => {
  await mountChartManager(page, { edit: [] });
  await page.evaluate(() => { window.__calls = []; });

  const refine = page.locator('#v3-chart-edit .chart-refine');
  await refine.locator('.chart-refine-input').fill('hide the legend and show labels');
  await refine.locator('.chart-refine-apply').click();
  await expect(refine.locator('.chart-refine-applied')).toBeVisible();
  await expect(refine.locator('.chart-refine-applied bdi')).toHaveText('hide the legend and show labels');

  await expect.poll(() => page.evaluate(() => ({
    legend: window.__chartTestManager.currentEchartsOptions?.legend?.show,
    labels: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.label?.show,
  }))).toEqual({ legend: false, labels: true });

  const calls = await page.evaluate(() => window.__calls || []);
  expect(calls.filter((call) => call.url.includes('/api/edit-chart'))).toHaveLength(0);
});

test('a pie request rebuilds from cached rows and the palette colours the slices', async ({ page }) => {
  await mountChartManager(page, {
    edit: [{
      response: {
        contract_version: 2,
        operations: [
          { op: 'set_chart_type', chart_type: 'pie' },
          { op: 'set_palette', colors: ['#ff9933', '#ffffff', '#138808'] },
        ],
        notes: 'Pie chart in saffron, white and green.',
        out_of_scope: false,
        reason_code: null,
      },
    }],
    rebuild: [{ response: PIE_REBUILD }],
  });

  await page.evaluate(() => { window.__calls = []; });
  const refine = page.locator('#v3-chart-edit .chart-refine');
  await refine.locator('.chart-refine-input').fill('change to a pie chart with the colours of the indian flag');
  await refine.locator('.chart-refine-apply').click();
  await expect(refine.locator('.chart-refine-applied')).toBeVisible();

  await expect.poll(() => page.evaluate(() => ({
    type: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.type,
    color: window.__chartTestManager.currentEchartsOptions?.color,
    sliceColor: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.itemStyle?.color,
    spec: window.__chartTestManager.getSaveState()?.chart_spec?.chart_type,
  }))).toEqual({ type: 'pie', color: ['#ff9933', '#ffffff', '#138808'], sliceColor: undefined, spec: 'pie' });

  const calls = await page.evaluate(() => window.__calls || []);
  const rebuilds = calls.filter((call) => call.url.includes('/api/edit-chart/rebuild'));
  expect(rebuilds).toHaveLength(1);
  expect(rebuilds[0].body.operations).toEqual([{ op: 'set_chart_type', chart_type: 'pie' }]);
  expect(rebuilds[0].body.chart_spec.chart_type).toBe('bar');
});

test('a what-if draws a labelled scenario next to the real data and Reset removes it', async ({ page }) => {
  await mountChartManager(page, {
    edit: [{
      response: {
        contract_version: 2,
        operations: [
          { op: 'scenario_set_point', target: 'all', category: 'South', value: 45, label: 'South at 45' },
          { op: 'add_reference_line', axis: 'y', value: 25, label: 'Target 25' },
        ],
        notes: 'Scenario with South at 45 and a target line.',
        out_of_scope: false,
        reason_code: null,
      },
    }],
  });

  const refine = page.locator('#v3-chart-edit .chart-refine');
  const badge = refine.locator('.chart-scenario-badge');
  await expect(badge).toBeHidden();
  await refine.locator('.chart-refine-input').fill('what if South sold 45, and add a target line at 25');
  await refine.locator('.chart-refine-apply').click();
  await expect(refine.locator('.chart-refine-applied')).toBeVisible();
  await expect(badge).toBeVisible();

  await expect.poll(() => page.evaluate(() => {
    const option = window.__chartTestManager.currentEchartsOptions;
    const titles = Array.isArray(option?.title) ? option.title : [option?.title];
    return {
      names: option?.series?.map((series) => series.name),
      realData: option?.series?.[0]?.data,
      scenarioData: option?.series?.[1]?.data,
      dashed: option?.series?.[1]?.lineStyle?.type,
      target: option?.series?.[0]?.markLine?.data?.[0]?.yAxis,
      cue: titles.some((title) => title && title.text === 'Scenario'),
      saved: window.__chartTestManager.getSaveState()?.chart_config?.series?.length,
    };
  })).toEqual({
    names: ['sales', 'Scenario: South at 45'],
    realData: [10, 30, 20],
    scenarioData: [10, 45, 20],
    dashed: 'dashed',
    target: 25,
    cue: true,
    saved: 1,
  });

  await refine.locator('.chart-refine-reset').click();
  await expect(refine.locator('.chart-refine-applied')).toBeHidden();
  await expect(badge).toBeHidden();
  await expect.poll(() => page.evaluate(() => ({
    count: window.__chartTestManager.currentEchartsOptions?.series?.length,
    markLine: window.__chartTestManager.currentEchartsOptions?.series?.[0]?.markLine,
  }))).toEqual({ count: 1, markLine: undefined });
});

test('rejections show catalogued copy for reason codes and rebuild errors', async ({ page }) => {
  await mountChartManager(page, {
    edit: [
      {
        response: {
          contract_version: 2,
          operations: [],
          notes: 'Model-written text that should not be shown.',
          out_of_scope: true,
          reason_code: 'needs_new_query',
        },
      },
      {
        response: {
          contract_version: 2,
          operations: [{ op: 'set_chart_type', chart_type: 'scatter' }],
          out_of_scope: false,
          reason_code: null,
        },
      },
    ],
    rebuild: [{ status: 422, response: { detail: { code: 'chart_rebuild_failed', message: 'nope' } } }],
  });
  const messages = await page.evaluate(() => window.I18n.t('charts.chat.reasons.needs_new_query'));
  const rebuildMessage = await page.evaluate(() => window.I18n.t('charts.chat.reasons.chart_rebuild_failed'));

  const refine = page.locator('#v3-chart-edit .chart-refine');
  const status = refine.locator('.chart-refine-status');
  await refine.locator('.chart-refine-input').fill('group by quarter instead');
  await refine.locator('.chart-refine-apply').click();
  await expect(status).toHaveText(messages);
  await expect(status).toHaveAttribute('data-kind', 'warn');

  // "with a trend" keeps this off the code-only fast path so the model answer
  // and the rebuild failure are both exercised.
  await refine.locator('.chart-refine-input').fill('turn this into a scatter plot with a trend');
  await refine.locator('.chart-refine-apply').click();
  await expect(status).toHaveText(rebuildMessage);
  await expect(status).toHaveAttribute('data-kind', 'error');
  await expect.poll(() => page.evaluate(() => (
    window.__chartTestManager.currentEchartsOptions?.series?.[0]?.type
  ))).toBe('bar');
});
