// @ts-check
// Chart disclosure and mode-specific controls in the deterministic browser harness.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask } = require('./_helpers');

test.beforeEach(async ({ page }) => { await openHarness(page); });

test('SQL chart disclosure hides every chart-scoped control until expanded', async ({ page }) => {
  await ask(page, Q.sqlAggregate);

  const toggle = page.locator('#v3-chart-toggle');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(toggle).toHaveAccessibleName('Expand chart');
  await expect(page.locator('#v3-chart-types')).toBeHidden();
  await expect(page.locator('#v3-chart-frame')).toBeHidden();
  await expect(page.locator('#v3-chart-edit')).toBeHidden();
  await expect(page.getByLabel('Chart edit test control')).toBeHidden();
  await expect(page.locator('#v3-actions')).toBeVisible();

  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(toggle).toHaveAccessibleName('Collapse chart');
  await expect(page.locator('#v3-chart-types')).not.toHaveAttribute('hidden', '');
  await expect(page.locator('#v3-chart-frame')).not.toHaveAttribute('hidden', '');
  await expect(page.locator('#v3-chart-edit')).not.toHaveAttribute('hidden', '');
  await expect(page.getByLabel('Chart edit test control')).toBeVisible();
});

test('table toolbar has a top separator and Describe sits below the grid', async ({ page }) => {
  await ask(page, Q.sqlAggregate);
  const toolbar = page.locator('#v3-table-block > .v3-toolbar');
  await expect(toolbar).toHaveCSS('border-top-width', '1px');
  await expect(toolbar.locator('#v3-row-caption')).toBeVisible();
  await expect(toolbar.locator('#v3-result-filter')).toBeVisible();
  await expect(toolbar.locator('#v3-describe-slot')).toHaveCount(0);

  const positions = await page.evaluate(() => {
    const grid = document.getElementById('v3-grid-wrap').getBoundingClientRect();
    const describe = document.getElementById('v3-describe-slot').getBoundingClientRect();
    return { gridBottom: grid.bottom, describeTop: describe.top };
  });
  expect(positions.describeTop).toBeGreaterThanOrEqual(positions.gridBottom);
});

test('ML keeps analysis adjustment outside the chart and locks unsafe chart controls', async ({ page }) => {
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');

  await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('forecast');
  await expect(page.locator('#v3-analysis-adjust')).toBeVisible();
  await expect(page.locator('#v3-analysis-adjust')).toContainText('current result is kept');
  await expect(page.locator('#v3-chart-edit')).toBeHidden();

  await page.locator('#v3-chart-toggle').click();
  await expect(page.locator('#v3-chart-types')).toBeHidden();
  await expect(page.locator('#v3-chart-frame')).toBeHidden();
  await expect(page.locator('#v3-analysis-adjust')).toBeVisible();
});

test('narrow layouts keep chart type primary and put secondary controls behind an overflow', async ({ page }) => {
  await page.setViewportSize({ width: 760, height: 900 });
  await ask(page, Q.sqlAggregate);
  await page.evaluate(() => window.WorkspaceController.setConversation(false));
  await page.locator('#v3-chart-toggle').click();

  const more = page.locator('#v3-chart-more');
  await expect(more).toBeVisible();
  await expect(more).toHaveAccessibleName('Chart options');
  await expect(more).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('#v3-chart-secondary')).toHaveCSS('display', 'none');

  await more.click();
  await expect(more).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#v3-chart-secondary')).toHaveClass(/is-open/);
  await expect(page.locator('#v3-chart-secondary')).toHaveCSS('display', 'flex');
});

test('ML chart components lock semantic controls and keep local visual controls available', async ({ page }) => {
  await page.evaluate(async () => {
    const optionsHost = document.createElement('div');
    optionsHost.id = 'component-options-test';
    document.body.appendChild(optionsHost);
    const typeHost = document.createElement('div');
    typeHost.id = 'component-type-test';
    document.body.appendChild(typeHost);

    const [{ ChartOptionsPanel }, { ChartTypeSelector }] = await Promise.all([
      import('/src/static/chart-feature/components/ChartOptionsPanel.js'),
      import('/src/static/chart-feature/components/ChartTypeSelector.js'),
    ]);
    const panel = new ChartOptionsPanel('component-options-test', {});
    panel.setColumns([
      { name: 'ts', type: 'date' },
      { name: 'actual', type: 'numeric' },
    ]);
    panel.render();
    panel.setAnalysisMode(true);
    const selector = new ChartTypeSelector('component-type-test', () => {});
    selector.render();
    selector.setDisabled(true, 'Chart type is defined by this analysis.');
  });

  await expect(page.locator('#component-type-test .ctype-btn')).toBeDisabled();
  await expect(page.locator('#component-type-test .ctype-btn')).toHaveAttribute('aria-describedby', 'chart-analysis-lock-note');
  await expect(page.locator('#component-options-test #chart-cols-btn')).toBeDisabled();
  await expect(page.locator('#component-options-test #chart-cols-btn')).toHaveAttribute('aria-describedby', 'chart-analysis-lock-note');
  await expect(page.locator('#component-options-test [data-key="sortDesc"]')).toBeDisabled();
  await expect(page.locator('#component-options-test #chart-palette-btn')).toBeEnabled();
  await expect(page.locator('#component-options-test [data-key="dataZoom"]')).toBeEnabled();
  await expect(page.locator('#component-options-test .chart-analysis-lock-note')).toHaveText('Layout set by the analysis');
});

test('collapsing closes open chart menus and expanding requests a resize', async ({ page }) => {
  await page.evaluate(async () => {
    const optionsHost = document.createElement('div');
    optionsHost.id = 'collapse-options-test';
    document.body.appendChild(optionsHost);
    const typeHost = document.createElement('div');
    typeHost.id = 'collapse-type-test';
    document.body.appendChild(typeHost);

    const [{ ChartManager }, { ChartOptionsPanel }, { ChartTypeSelector }] = await Promise.all([
      import('/src/static/chart-feature/chartManager.js'),
      import('/src/static/chart-feature/components/ChartOptionsPanel.js'),
      import('/src/static/chart-feature/components/ChartTypeSelector.js'),
    ]);
    const panel = new ChartOptionsPanel('collapse-options-test', {});
    panel.setColumns([{ name: 'region', type: 'category' }, { name: 'sales', type: 'numeric' }]);
    panel.render();
    const selector = new ChartTypeSelector('collapse-type-test', () => {});
    selector.render();
    selector.open();
    document.querySelector('#collapse-options-test #chart-cols-btn').click();

    const target = {
      chartTypeSelector: selector,
      chartOptionsPanel: panel,
      chartContainer: { resize() { window.__chartResizeRequested = true; } },
    };
    ChartManager.prototype.setCollapsed.call(target, true);
    window.__collapseState = {
      menuHidden: selector.menuEl.hidden,
      columnsExpanded: document.querySelector('#collapse-options-test #chart-cols-btn').getAttribute('aria-expanded'),
      columnsHidden: document.querySelector('#collapse-options-test #chart-cols-expand').hidden,
    };
    ChartManager.prototype.setCollapsed.call(target, false);
  });

  expect(await page.evaluate(() => window.__collapseState)).toEqual({
    menuHidden: true,
    columnsExpanded: 'false',
    columnsHidden: true,
  });
  await expect.poll(() => page.evaluate(() => Boolean(window.__chartResizeRequested))).toBe(true);
});

test('chart edits survive apply and Reset restores the original baseline', async ({ page }) => {
  const state = await page.evaluate(async () => {
    const { ChartManager } = await import('/src/static/chart-feature/chartManager.js');
    const manager = new ChartManager({ workspaceMode: false });
    const baseline = {
      xAxis: { type: 'category', data: ['A', 'B'] },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: [1, 2], label: { show: false } }],
    };
    manager.chartContainer = { async init() {}, render() {}, dispose() {} };
    manager.state.isEChartsLoaded = true;
    manager.state.currentData = { columns: ['region', 'sales'], rows: [['A', 1], ['B', 2]] };
    const spec = { chart_type: 'bar', x: 'region', y: 'sales' };
    manager._adoptBaseline(structuredClone(baseline), spec);

    const edited = structuredClone(baseline);
    edited.series[0].label.show = true;
    await manager.applyEditedConfig(edited, [], null, { chart_spec: spec });
    const applied = manager.currentEchartsOptions.series[0].label.show;
    await manager.resetChartEdits();
    const reset = manager.currentEchartsOptions.series[0].label.show;
    const type = manager.getSaveState().chart_spec.chart_type;
    manager.dispose();
    return { applied, reset, type };
  });

  expect(state).toEqual({ applied: true, reset: false, type: 'bar' });
});

test('analysis reruns reject duplicates and do not steal selection after navigation', async ({ page }) => {
  await ask(page, Q.sqlAggregate);
  const sqlTurn = await page.evaluate(() => window.WorkspaceController.selectedResultId);
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');
  await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('forecast');

  const duplicateMessage = await page.evaluate(async () => {
    const originalFetch = window.fetch.bind(window);
    window.__releaseRerun = null;
    window.fetch = (url, options) => {
      if (String(url).includes('/api/analysis/rerun')) {
        return new Promise((resolve, reject) => {
          window.__releaseRerun = () => originalFetch(url, options).then(resolve, reject);
        });
      }
      return originalFetch(url, options);
    };
    window.__pendingRerun = window.WorkspaceController.rerunAnalysis('extend the horizon');
    try {
      await window.WorkspaceController.rerunAnalysis('extend the horizon');
      return '';
    } catch (error) {
      return String(error && error.message ? error.message : error);
    }
  });
  expect(duplicateMessage).toContain('already being re-run');

  await page.evaluate((id) => window.WorkspaceController.selectTurn(id), sqlTurn);
  await page.evaluate(async () => {
    window.__releaseRerun();
    await window.__pendingRerun;
  });
  expect(await page.evaluate(() => window.WorkspaceController.selectedResultId)).toBe(sqlTurn);
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(4);
});
