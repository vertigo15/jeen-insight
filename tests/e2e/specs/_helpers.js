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

module.exports = { HARNESS, Q, openHarness, ask, lastTurn, routingTruth, expect };
