// @ts-check
const { expect } = require('@playwright/test');

const HARNESS = '/tests/e2e/harness/index.html';

// Canonical questions — MUST match tests/e2e/harness/fixtures.js (Q) and the
// QUESTIONS list in tests/e2e/generate_fixtures.py.
const Q = {
  sqlAggregate: 'Show me total sales by region last month',
  sqlCount: 'How many orders shipped yesterday?',
  sqlByYear: 'Total sales for the Bikes category by year',
  forecast: 'Forecast profit for the next 8 weeks',
  anomaly: 'Is anything unusual in weekly profit?',
  guard: 'Forecast weekly revenue for a brand-new product line',
  clarify: 'Show the correlation between marketing spend and revenue',
  capability: 'which ML models can I use?',
  greeting: 'hello',
};

/** Load the harness and wait for the real controller to finish booting. */
async function openHarness(page, query = '') {
  await page.goto(HARNESS + query);
  await page.waitForFunction(() => window.ChatController && document.body.classList.contains('v3-ready'));
}

/**
 * Mount the real ChartManager against opt-in deterministic chart API fixtures.
 * The normal harness does not initialize it, which keeps unrelated SQL/ML specs
 * fast and lets chart lifecycle specs control every response and delay.
 */
async function mountChartManager(page, script = {}) {
  await page.evaluate(async (chartScript) => {
    const fixture = window.__FIXTURES__.CHART;
    window.__chartTestManager?.dispose?.();
    window.__CHART_FIXTURES__ = {
      capabilities: {
        map: { enabled: false },
        osm_map: { enabled: false, geocoding_enabled: false },
      },
      generate: chartScript.generate || [{
        response: {
          chart_type: 'bar',
          chart_spec: structuredClone(fixture.barSpec),
          chart_config: structuredClone(fixture.barConfig),
        },
      }],
      edit: chartScript.edit || [],
      rebuild: chartScript.rebuild || [],
    };
    const { ChartManager } = await import('/src/static/chart-feature/chartManager.js');
    const manager = new ChartManager({ workspaceMode: true });
    manager.state.isEChartsLoaded = true;
    manager.setContext({
      queryId: 'chart-e2e-turn',
      question: 'Show sales by region',
      tableEl: document.getElementById('results-display'),
      chartViewEl: document.getElementById('chart-view-container'),
      manageToggle: false,
    });
    window.__chartTestManager = manager;
    await manager.initialize(structuredClone(fixture.results));
    ['v3-chart-types', 'v3-chart-frame', 'v3-chart-edit'].forEach((id) => {
      const element = document.getElementById(id);
      if (element) element.hidden = false;
    });
    const block = document.getElementById('v3-chart-block');
    if (block) block.hidden = false;
    const placeholder = document.getElementById('v3-placeholder');
    if (placeholder) placeholder.hidden = true;
  }, script);
  await page.waitForFunction(() => Boolean(
    window.__chartTestManager
    && window.__chartTestManager.currentEchartsOptions
    && document.querySelector('#chart-display-container canvas')
  ));
}

/** Drive a question through the real controller; resolves after the render. */
async function ask(page, question, opts) {
  await page.evaluate(([q, o]) => window.__ask(q, o), [question, opts || {}]);
}

/** The most recently rendered thread turn. */
function lastTurn(page) {
  return page.locator('#v3-thread article.v3-turn').last();
}

/** The path predicted by the REAL backend rule for a question (from the harness). */
async function routingTruth(page) {
  return page.evaluate(() => (window.__ROUTING_TRUTH__ || {}).enabled || {});
}

module.exports = {
  HARNESS,
  Q,
  openHarness,
  mountChartManager,
  ask,
  lastTurn,
  routingTruth,
  expect,
};
