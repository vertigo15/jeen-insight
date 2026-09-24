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

test('a chart edit survives local presentation changes and Reset restores the baseline', async ({ page }) => {
  await mountChartManager(page, {
    edit: [
      { status: 409, response: { detail: 'cache_miss' } },
      { response: await page.evaluate(() => structuredClone(window.__FIXTURES__.CHART.lineEdit)) },
    ],
  });

  const refine = page.locator('#v3-chart-edit .chart-refine');
  const input = refine.locator('.chart-refine-input');
  const apply = refine.locator('.chart-refine-apply');
  await expect(input).toBeEnabled();
  await expect(apply).toHaveAttribute('aria-disabled', 'true');

  await input.fill('הצג כגרף קו עם תוויות');
  await expect(apply).toHaveAttribute('aria-disabled', 'false');
  await apply.click();
  const editCalls = await page.evaluate(() => (
    (window.__calls || []).filter((call) => call.url.includes('/api/edit-chart'))
  ));
  expect(editCalls).toHaveLength(2);
  expect(editCalls[1].body.all_data).toHaveLength(3);

  await expect(refine.locator('.chart-refine-applied')).toBeVisible();
  await expect(refine.locator('.chart-refine-applied bdi')).toHaveText('Changed the chart to a line and enabled labels.');
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
