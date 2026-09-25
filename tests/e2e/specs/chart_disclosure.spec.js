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
  await expect(page.locator('#export-btn')).toBeVisible();
  await expect(page.locator('#copy-results-btn')).toBeVisible();

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

test('conversation scroll follows the newest card after its final answer expands', async ({ page }) => {
  await ask(page, Q.sqlAggregate);
  await page.addStyleTag({
    content: '#v3-thread .v3-turn:last-child .v3-turn-body { min-height: 520px; }',
  });
  await ask(page, Q.sqlCount);
  const scroll = await page.evaluate(() => {
    const thread = document.getElementById('v3-thread');
    return {
      bottom: thread.scrollTop + thread.clientHeight,
      height: thread.scrollHeight,
    };
  });
  expect(scroll.bottom).toBeGreaterThanOrEqual(scroll.height - 2);
  await expect(page.locator('#v3-thread article.v3-turn').last()).toBeInViewport();
  await expect(page.locator('#v3-new-conversation')).toBeInViewport();
});

test('run details separate MCP stages and reconcile traced time with browser wall time', async ({ page }) => {
  await ask(page, Q.sqlAggregate);
  await page.evaluate(() => {
    const controller = window.WorkspaceController;
    const turn = controller.turns[controller.turns.length - 1];
    turn.durationMs = 1500;
    turn.traceOpen = true;
    turn.trace = [
      {
        node: 'pre_graph_setup',
        status: 'node_finished',
        elapsed_ms: 1000,
        detail: 'catalog/history/audit pre-load in parallel',
        mcp_timing: {
          filtered_tool_ms: 600,
          full_restore_ms: 250,
          connection_ms: 100,
          parse_ms: 50,
        },
      },
      {
        node: 'sql_generator',
        status: 'node_finished',
        elapsed_ms: 200,
        type: 'llm',
      },
    ];
    controller.renderConversation();
  });

  await expect(page.locator('.v3-trace-row--breakdown')).toHaveCount(4);
  await expect(page.locator('.v3-trace')).toContainText('Question-specific MCP');
  await expect(page.locator('.v3-trace')).toContainText('Reusable catalog restore');
  await expect(page.locator('.v3-trace-reconcile')).toContainText('proxy/network 300ms');
});

test('ML small chat edits only the ECharts config and collapses with the chart', async ({ page }) => {
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');

  await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('forecast');
  await expect(page.locator('#v3-analysis-adjust')).toBeHidden();
  await expect(page.locator('#v3-chart-edit')).toBeVisible();
  await page.evaluate(async () => {
    const host = document.createElement('div');
    host.id = 'ml-chart-only-chat';
    document.getElementById('v3-chart-edit').appendChild(host);
    const { ChartChat } = await import('/src/static/chart-feature/components/ChartChat.js');
    const config = {
      xAxis: { type: 'category', data: ['Jan', 'Feb'] },
      yAxis: { type: 'value' },
      series: [{ id: 'actual', name: 'Actual', jeenRole: 'actual', type: 'line', data: [10, 12] }],
    };
    window.__CHART_FIXTURES__ = {
      edit: [{
        response: {
          contract_version: 2,
          operations: [{ op: 'set_color', target: 'role:actual', color: '#22c55e' }],
          notes: 'Updated the actual line.',
          out_of_scope: false,
          reason_code: null,
        },
      }],
    };
    window.__mlChartOnly = { applies: 0 };
    const chat = new ChartChat('ml-chart-only-chat', {
      getChartManifest: () => ({
        series: [{
          id: 'actual', name: 'Actual', role: 'actual', type: 'line',
          pointCount: 2, locked: false,
        }],
        axes: { x: [{ type: 'category', categories: { count: 2 } }], y: [{ type: 'value' }] },
        toggles: { dataLabels: false, legend: true, dataZoom: false },
        overlays: [],
        chart_spec: { chart_type: 'band' },
        columns: [],
      }),
      getChartKind: () => 'ml_band',
      getConnection: () => 'sales_db',
      getRevision: () => 1,
      isRevisionCurrent: (revision) => revision === 1,
      onApply: () => { window.__mlChartOnly.applies += 1; },
      onReset: () => {},
    });
    chat.mount();
    chat.enable();
    chat.setAnalysisMode(false);
    window.__calls = [];
  });
  await expect(page.locator('#ml-chart-only-chat .chart-refine-input')).toHaveAttribute('aria-label', 'Refine this chart');
  await expect(page.locator('#ml-chart-only-chat .chart-refine-apply')).toContainText('Apply');

  await page.locator('#ml-chart-only-chat .chart-refine-input').fill('show line chart in green with labels');
  await page.locator('#ml-chart-only-chat .chart-refine-apply').click();
  await expect.poll(() => page.evaluate(() => (
    (window.__calls || []).some((call) => call.url.includes('/api/edit-chart'))
  ))).toBe(true);
  expect(await page.evaluate(() => window.__mlChartOnly)).toEqual({ applies: 1 });

  await page.locator('#v3-chart-toggle').click();
  await expect(page.locator('#v3-chart-types')).toBeHidden();
  await expect(page.locator('#v3-chart-frame')).toBeHidden();
  await expect(page.locator('#v3-chart-edit')).toBeHidden();
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

test('restored SQL and ML turns keep collapse defaults and reuse saved chart baselines', async ({ page }) => {
  await page.evaluate(async () => {
    const originalFetch = window.fetch;
    window.__restoreCalls = [];
    const sqlConfig = {
      xAxis: { type: 'category', data: ['A', 'B'] },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: [10, 20] }],
    };
    const mlConfig = {
      xAxis: { type: 'category', data: ['2026-01', '2026-02'] },
      yAxis: { type: 'value' },
      series: [
        { type: 'line', name: 'Actual', jeenRole: 'actual', data: [10, 12] },
        { type: 'line', name: 'Forecast', jeenRole: 'forecast', data: [13, 14] },
      ],
    };
    window.fetch = (input, init) => {
      const url = String(input);
      window.__restoreCalls.push(url);
      if (url === '/api/conversations/restore-e2e') {
        return Promise.resolve(new Response(JSON.stringify({
          conversation: {
            id: 'restore-e2e',
            title: 'Restored analysis',
            source_key: 'sales_db',
            connection_available: true,
          },
          turns: [
            {
              turn_id: 'ml-turn',
              sequence_number: 2,
              question: 'Forecast sales',
              execution_status: 'success',
              result_kind: 'table',
              snapshot_status: 'stored',
              snapshot_at: '2026-09-22T09:00:00Z',
              has_chart: true,
              answer: 'Forecast restored.',
              analysis: {
                skill: 'forecast',
                params: {},
                chart_spec: { chart_type: 'band' },
              },
            },
            {
              turn_id: 'sql-turn',
              sequence_number: 1,
              question: 'Sales by region',
              execution_status: 'success',
              result_kind: 'table',
              snapshot_status: 'stored',
              snapshot_at: '2026-09-22T08:00:00Z',
              has_chart: true,
              answer: 'SQL result restored.',
            },
          ],
        }), { headers: { 'Content-Type': 'application/json' } }));
      }
      if (url.endsWith('/turns/ml-turn/artifact')) {
        return Promise.resolve(new Response(JSON.stringify({
          results: { columns: ['month', 'actual', 'forecast'], rows: [['2026-01', 10, 13], ['2026-02', 12, 14]] },
          snapshot_status: 'stored',
          chart_spec: { chart_type: 'band' },
          chart_config: mlConfig,
          analysis: { skill: 'forecast', params: {}, chart_spec: { chart_type: 'band' } },
        }), { headers: { 'Content-Type': 'application/json' } }));
      }
      if (url.endsWith('/turns/sql-turn/artifact')) {
        return Promise.resolve(new Response(JSON.stringify({
          results: { columns: ['region', 'sales'], rows: [['A', 10], ['B', 20]] },
          snapshot_status: 'stored',
          chart_spec: { chart_type: 'bar', x: 'region', y: 'sales' },
          chart_config: sqlConfig,
        }), { headers: { 'Content-Type': 'application/json' } }));
      }
      return originalFetch(input, init);
    };
    await window.WorkspaceController.hydrate('sales_db', { conversationId: 'restore-e2e' });
  });

  await expect(page.locator('#v3-result-title')).toHaveText('Forecast sales');
  await expect(page.locator('#v3-chart-toggle')).toHaveAttribute('aria-expanded', 'true');
  expect(await page.evaluate(() => {
    const turn = window.WorkspaceController.turns.find((item) => item.turnId === 'ml-turn');
    return turn.chartState.chart_config.series.map((series) => series.jeenRole);
  })).toEqual(['actual', 'forecast']);

  await page.evaluate(() => window.WorkspaceController.selectTurn('turn-sql-turn'));
  await expect.poll(() => page.evaluate(() => {
    const turn = window.WorkspaceController.turns.find((item) => item.turnId === 'sql-turn');
    return turn.artifactState;
  })).toBe('loaded');
  await expect(page.locator('#v3-result-title')).toHaveText('Sales by region');
  await expect(page.locator('#v3-chart-toggle')).toHaveAttribute('aria-expanded', 'false');
  expect(await page.evaluate(() => window.__restoreCalls.some((url) => url.includes('/api/generate-chart')))).toBe(false);
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

    await manager.applyEditedOperations(
      [{ op: 'set_toggle', key: 'dataLabels', value: true }],
      manager._chartEditToken(),
    );
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
  const analysisTurn = await page.evaluate(() => window.WorkspaceController.selectedResultId);

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

  await page.evaluate(({ sqlTurnId, analysisTurnId }) => {
    window.WorkspaceController.selectTurn(sqlTurnId);
    window.WorkspaceController.selectTurn(analysisTurnId);
  }, { sqlTurnId: sqlTurn, analysisTurnId: analysisTurn });
  await page.evaluate(async () => {
    window.__releaseRerun();
    await window.__pendingRerun;
  });
  expect(await page.evaluate(() => window.WorkspaceController.selectedResultId)).toBe(analysisTurn);
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(4);
});

test.skip('legacy local quick-intent path is superseded by typed chart operations', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const host = document.createElement('div');
    host.id = 'quick-edit-options';
    document.body.appendChild(host);
    const chatHost = document.createElement('div');
    chatHost.id = 'quick-edit-chat';
    document.body.appendChild(chatHost);

    const [{ ChartManager }, { ChartOptionsPanel }, { ChartChat }] = await Promise.all([
      import('/src/static/chart-feature/chartManager.js'),
      import('/src/static/chart-feature/components/ChartOptionsPanel.js'),
      import('/src/static/chart-feature/components/ChartChat.js'),
    ]);
    const panel = new ChartOptionsPanel('quick-edit-options', {});
    panel.setColumns([
      { name: 'month_number', type: 'numeric' },
      { name: 'month_name', type: 'category' },
      { name: 'revenue', type: 'numeric' },
    ]);
    panel.render();

    const manager = new ChartManager({ workspaceMode: false });
    manager.chartOptionsPanel = panel;
    manager.chartContainer = { async init() {}, render() {}, dispose() {} };
    manager.state.isEChartsLoaded = true;
    manager.state.currentData = {
      columns: ['month_number', 'month_name', 'revenue'],
      rows: [[1, 'January', 10], [2, 'February', 20]],
    };
    manager.currentEchartsOptions = {
      xAxis: { type: 'category', data: [1, 2] },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', jeenRole: 'actual', data: [10, 20], label: { show: false } }],
    };
    manager.originalConfig = structuredClone(manager.currentEchartsOptions);
    manager.currentChartSpec = { chart_type: 'bar', x: 'month_number', y: 'revenue' };
    manager.originalChartSpec = structuredClone(manager.currentChartSpec);
    panel.syncFromSpec(manager.currentChartSpec);

    let networkCalls = 0;
    const originalFetch = window.fetch;
    window.fetch = (...args) => {
      networkCalls += 1;
      return originalFetch(...args);
    };
    const chat = new ChartChat('quick-edit-chat', {
      getCurrentConfig: () => manager.currentEchartsOptions,
      getCurrentResults: () => manager.state.currentData,
      getConnection: () => 'test',
      onQuickEdit: (instruction) => manager.applyQuickInstruction(instruction),
      onApply: () => {},
      onReset: () => manager.resetChartEdits(),
    });
    chat.mount();
    chat.enable();
    chat._inputEl.value = 'all lables and change colore to red';
    await chat._handleSend();
    const firstSummary = chat._appliedInstructionEl.textContent;
    const first = {
      labels: manager.currentEchartsOptions.series[0].label.show,
      color: manager.currentEchartsOptions.series[0].itemStyle.color,
      customPalette: panel.customPalette?.name,
    };
    const greenConfig = JSON.parse(JSON.stringify(manager.currentEchartsOptions));
    greenConfig.series[0].itemStyle.color = '#228b22';
    const greenApplied = await manager.applyEditedConfig(greenConfig, [], 'Changed bars to green.');
    const greenState = {
      color: manager.currentEchartsOptions.series[0].itemStyle.color,
      customPalette: manager.customPalette?.name,
    };
    const greenRepeated = await manager.applyEditedConfig(
      JSON.parse(JSON.stringify(manager.currentEchartsOptions)),
      [],
      'Changed bars to green.',
    );
    chat._inputEl.value = 'show month names in the x axis';
    await chat._handleSend();
    const secondSummary = chat._appliedInstructionEl.textContent;
    const second = {
      categories: manager.currentEchartsOptions.xAxis.data,
      specX: manager.currentChartSpec.x,
      panelX: panel.getMapping().xColumn,
    };
    const repeated = await manager.applyQuickInstruction('show month names in the x axis');
    panel.setToggles({ sortDesc: true });
    await manager._reapplyQuickToggles({ key: 'sortDesc', value: true });
    await manager.applyQuickInstruction('show month numbers in the x axis');
    await manager.applyQuickInstruction('show month names in the x axis');
    const sortedBinding = {
      categories: manager.currentEchartsOptions.xAxis.data,
      values: manager.currentEchartsOptions.series[0].data,
    };
    panel.setToggles({ sortDesc: false });
    await manager._reapplyQuickToggles({ key: 'sortDesc', value: false });
    const sortOff = {
      categories: manager.currentEchartsOptions.xAxis.data,
      values: manager.currentEchartsOptions.series[0].data,
      specX: manager.currentChartSpec.x,
    };
    const unsupported = {
      dataset: manager._withXAxisBinding({
        dataset: { source: [] },
        xAxis: { type: 'category', data: [1, 2] },
        series: [{ type: 'bar', encode: { x: 0, y: 1 } }],
      }, 'month_name') === null,
      multiAxis: manager._withXAxisBinding({
        xAxis: [
          { type: 'category', data: [1, 2] },
          { type: 'category', data: [1, 2] },
        ],
        series: [{ type: 'bar', xAxisIndex: 1, data: [10, 20] }],
      }, 'month_name') === null,
    };
    const preservedRole = manager.currentEchartsOptions.series[0].jeenRole;
    const resetLabel = chat._resetBtnEl.getAttribute('aria-label');
    const resetHasSvg = Boolean(chat._resetBtnEl.querySelector('svg'));
    await chat._handleReset();
    const reset = {
      labels: manager.currentEchartsOptions.series[0].label.show,
      categories: manager.currentEchartsOptions.xAxis.data,
      customPalette: panel.customPalette,
    };
    window.fetch = originalFetch;
    manager.dispose();
    return {
      networkCalls, firstSummary, secondSummary, first, greenApplied, greenState,
      greenRepeated, second, repeated,
      sortedBinding, sortOff, unsupported, preservedRole, reset, resetLabel, resetHasSvg,
    };
  });

  expect(result.networkCalls).toBe(0);
  expect(result.first).toEqual({ labels: true, color: '#d4574a', customPalette: 'red' });
  expect(result.firstSummary).toBe('Labels on · Colour: red');
  expect(result.greenApplied).toEqual({ applied: true });
  expect(result.greenState).toEqual({ color: '#228b22', customPalette: 'custom' });
  expect(result.greenRepeated).toEqual({ applied: false, noChange: true });
  expect(result.second).toEqual({
    categories: ['January', 'February'],
    specX: 'month_name',
    panelX: 'month_name',
  });
  expect(result.secondSummary).toBe('X-axis: month_name');
  expect(result.repeated).toEqual({ applied: false, noChange: true });
  expect(result.sortedBinding).toEqual({
    categories: ['February', 'January'],
    values: [20, 10],
  });
  expect(result.sortOff).toEqual({
    categories: ['January', 'February'],
    values: [10, 20],
    specX: 'month_name',
  });
  expect(result.unsupported).toEqual({ dataset: true, multiAxis: true });
  expect(result.preservedRole).toBe('actual');
  expect(result.reset).toEqual({ labels: false, categories: [1, 2], customPalette: null });
  expect(result.resetLabel).toBe('Reset chart');
  expect(result.resetHasSvg).toBe(true);
});

test.skip('legacy full-config no-op path is superseded by typed chart operations', async ({ page }) => {
  const state = await page.evaluate(async () => {
    const { ChartChat } = await import('/src/static/chart-feature/components/ChartChat.js');
    const host = document.createElement('div');
    host.id = 'no-op-chat';
    document.body.appendChild(host);
    const config = {
      xAxis: { type: 'category', data: ['A'] },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: [1] }],
    };
    const originalFetch = window.fetch;
    window.fetch = async () => new Response(JSON.stringify({
      chart_config: structuredClone(config),
      derived_series: [],
      out_of_scope: false,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    let applies = 0;
    const chat = new ChartChat('no-op-chat', {
      getCurrentConfig: () => config,
      getCurrentResults: () => ({ columns: ['category', 'value'], rows: [['A', 1]] }),
      getConnection: () => 'test',
      onApply: () => { applies += 1; },
      onReset: () => {},
    });
    chat.mount();
    chat.enable();
    chat._showApplied('Previous edit');
    chat._inputEl.value = 'make this more polished';
    await chat._handleSend();
    window.fetch = originalFetch;
    return {
      applies,
      appliedHidden: chat._appliedEl.hidden,
      appliedLabelHidden: chat._appliedLabelEl.hidden,
      resetVisible: !chat._resetBtnEl.hidden,
      status: chat._statusEl.textContent,
      statusKind: chat._statusEl.dataset.kind,
    };
  });

  expect(state).toEqual({
    applies: 0,
    appliedHidden: true,
    appliedLabelHidden: false,
    resetVisible: true,
    status: 'No visible chart change was applied.',
    statusKind: 'warn',
  });
});

test.skip('legacy quick-render rollback is covered by chart operation session tests', async ({ page }) => {
  const state = await page.evaluate(async () => {
    const host = document.createElement('div');
    host.id = 'rollback-options';
    document.body.appendChild(host);
    const [{ ChartManager }, { ChartOptionsPanel }] = await Promise.all([
      import('/src/static/chart-feature/chartManager.js'),
      import('/src/static/chart-feature/components/ChartOptionsPanel.js'),
    ]);
    const panel = new ChartOptionsPanel('rollback-options', {});
    panel.setColumns([
      { name: 'category', type: 'category' },
      { name: 'value', type: 'numeric' },
    ]);
    panel.render();
    const manager = new ChartManager({ workspaceMode: false });
    manager.chartOptionsPanel = panel;
    manager.chartContainer = { async init() {}, render() { throw new Error('paint failed'); }, dispose() {} };
    manager.state.isEChartsLoaded = true;
    manager.state.currentData = {
      columns: ['category', 'value'],
      rows: [['A', 1], ['B', 2]],
    };
    manager.currentEchartsOptions = {
      xAxis: { type: 'category', data: ['A', 'B'] },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: [1, 2], label: { show: false } }],
    };
    manager.originalConfig = structuredClone(manager.currentEchartsOptions);
    let error = '';
    try {
      await manager.applyQuickInstruction('show labels and change color to red');
    } catch (caught) {
      error = caught.message;
    }
    return {
      error,
      labels: panel.getToggles().dataLabels,
      customPalette: panel.customPalette,
      managerCustom: manager.customPalette,
      displayedLabels: manager.currentEchartsOptions.series[0].label.show,
    };
  });

  expect(state.error).toBe('Got a config back but failed to render it. The chart was not changed.');
  expect(state.labels).toBe(false);
  expect(state.customPalette).toBeNull();
  expect(state.managerCustom).toBeNull();
  expect(state.displayedLabels).toBe(false);
});

test.skip('legacy local rebinding is superseded by the deterministic rebuild route', async ({ page }) => {
  const state = await page.evaluate(async () => {
    const host = document.createElement('div');
    host.id = 'duplicate-binding-options';
    document.body.appendChild(host);
    const [{ ChartManager }, { ChartOptionsPanel }] = await Promise.all([
      import('/src/static/chart-feature/chartManager.js'),
      import('/src/static/chart-feature/components/ChartOptionsPanel.js'),
    ]);
    const panel = new ChartOptionsPanel('duplicate-binding-options', {});
    panel.setColumns([
      { name: 'old_category', type: 'category' },
      { name: 'new_category', type: 'category' },
      { name: 'value', type: 'numeric' },
    ]);
    panel.render();
    const manager = new ChartManager({ workspaceMode: false });
    manager.chartOptionsPanel = panel;
    manager.chartContainer = { async init() {}, render() {}, dispose() {} };
    manager.state.isEChartsLoaded = true;
    manager.state.currentData = {
      columns: ['old_category', 'new_category', 'value'],
      rows: [['A', 'Jan', 10], ['A', 'Feb', 20], ['B', 'Mar', 15]],
    };
    manager.currentEchartsOptions = {
      xAxis: { type: 'category', data: ['A', 'A', 'B'] },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: [10, 20, 15] }],
    };
    manager.originalConfig = structuredClone(manager.currentEchartsOptions);
    manager.currentChartSpec = { chart_type: 'bar', x: 'old_category', y: 'value' };
    manager.originalChartSpec = structuredClone(manager.currentChartSpec);
    panel.syncFromSpec(manager.currentChartSpec);
    panel.setToggles({ sortDesc: true });
    await manager._reapplyQuickToggles({ key: 'sortDesc', value: true });
    const edit = await manager.applyQuickInstruction('show new category in the x axis');
    return {
      edit,
      categories: manager.currentEchartsOptions.xAxis.data,
      values: manager.currentEchartsOptions.series[0].data,
    };
  });

  expect(state.edit.applied).toBe(true);
  expect(state.categories).toEqual(['Feb', 'Mar', 'Jan']);
  expect(state.values).toEqual([20, 15, 10]);
});

test.skip('legacy full-config race is covered by the v2 chart chat reset test', async ({ page }) => {
  const state = await page.evaluate(async () => {
    const { ChartChat } = await import('/src/static/chart-feature/components/ChartChat.js');
    const host = document.createElement('div');
    host.id = 'delayed-chat';
    document.body.appendChild(host);
    const config = {
      xAxis: { type: 'category', data: ['A'] },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: [1] }],
    };
    const changed = structuredClone(config);
    changed.series[0].label = { show: true };
    const originalFetch = window.fetch;
    let release;
    window.fetch = () => new Promise((resolve) => {
      release = () => resolve(new Response(JSON.stringify({
        chart_config: changed,
        derived_series: [],
        out_of_scope: false,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    });
    let applies = 0;
    let resets = 0;
    const chat = new ChartChat('delayed-chat', {
      getCurrentConfig: () => config,
      getCurrentResults: () => ({ columns: ['category', 'value'], rows: [['A', 1]] }),
      getConnection: () => 'test',
      onApply: () => { applies += 1; },
      onReset: () => { resets += 1; },
    });
    chat.mount();
    chat.enable();
    chat._inputEl.value = 'make labels larger';
    const pending = chat._handleSend();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await chat._handleReset();
    release();
    await pending;
    window.fetch = originalFetch;
    return {
      applies,
      resets,
      appliedHidden: chat._appliedEl.hidden,
      input: chat._inputEl.value,
    };
  });

  expect(state).toEqual({
    applies: 0,
    resets: 1,
    appliedHidden: true,
    input: '',
  });
});
