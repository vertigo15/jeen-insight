// @ts-check
// Produces labeled, full-page screenshots of each ML state into ./screenshots.
// This is a documentation artifact (not an assertion suite): run
// `npx playwright test specs/gallery.spec.js` to refresh the images.
const path = require('path');
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn } = require('./_helpers');

const SHOTS = path.resolve(__dirname, '..', 'screenshots');
const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, name), fullPage: true });

test('gallery: SQL answer with the SQL path badge', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.sqlAggregate);
  await expect(lastTurn(page)).toHaveAttribute('data-route-path', 'sql');
  await shot(page, '01-sql-answer.png');
});

test('gallery: forecast confirm card (ML path)', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.forecast);
  await expect(page.locator('#v3-placeholder .v3-ml-card.is-confirm')).toBeVisible();
  await shot(page, '02-forecast-confirm.png');
});

test('gallery: forecast confirm card, dark mode', async ({ page }) => {
  await openHarness(page);
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await ask(page, Q.forecast);
  await expect(page.locator('#v3-placeholder .v3-ml-card.is-confirm')).toBeVisible();
  await shot(page, '02b-forecast-confirm-dark.png');
});

test('gallery: forecast confirm card with edits (changed markers, live summary)', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.forecast);
  const card = page.locator('#v3-placeholder .v3-ml-card.is-confirm');
  await card.locator('[data-chip="grain"]').selectOption('month');
  await card.locator('[data-chip="horizon"]').fill('6');
  await card.locator('[data-chip="window"]').fill('5');
  await expect(card.locator('.v3-ml-field.is-invalid')).toHaveCount(1);
  await shot(page, '02c-forecast-confirm-edited.png');
});

test('gallery: anomaly confirm card', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.anomaly);
  await expect(page.locator('#v3-placeholder .v3-ml-card.is-confirm')).toBeVisible();
  await shot(page, '03-anomaly-confirm.png');
});

test('gallery: guard refusal (0 rows sent)', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.guard);
  await expect(page.locator('#v3-placeholder .v3-ml-card.is-guard')).toBeVisible();
  await shot(page, '04-guard-refusal.png');
});

test('gallery: clarification card', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.clarify);
  await expect(page.locator('#v3-placeholder .v3-ml-card.is-clarify')).toBeVisible();
  await shot(page, '05-clarify.png');
});

test('gallery: completed ML result with Model details', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');
  await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('forecast');
  await page.click('[data-dock="model"]');
  await expect(page.locator('#v3-dock-body')).toBeVisible();
  await shot(page, '06-ml-result-model-details.png');
});

test('gallery: Edit setup on a finished result', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');
  await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('forecast');
  await page.click('#v3-meta-row [data-ml-edit]');
  const setup = page.locator('#v3-ml-definition .v3-ml-card.is-definition');
  await expect(setup).toBeVisible();
  await expect(setup.locator('[data-summary]')).toContainText('SUM(Profit)');
  await page.screenshot({ path: path.join(SHOTS, '09-edit-setup.png') });
});

test('gallery: re-run parameter diff', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');
  await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('forecast');
  await page.evaluate(() => window.ChatController.rerunAnalysis('extend the horizon to 12 weeks'));
  await page.click('[data-dock="model"]');
  await expect(page.locator('#v3-dock-body')).toContainText('Changed from the previous run');
  await shot(page, '07-rerun-diff.png');
});

test('gallery: both route badges in one thread', async ({ page }) => {
  await openHarness(page);
  // Two SQL answers and two ML stops so the ML/SQL badges appear side by side.
  await ask(page, Q.sqlAggregate);
  await ask(page, Q.forecast);
  await ask(page, Q.sqlCount);
  await ask(page, Q.anomaly);
  await expect(page.locator('#v3-thread article[data-route-path="ml"]')).toHaveCount(2);
  await expect(page.locator('#v3-thread article[data-route-path="sql"]')).toHaveCount(2);
  await shot(page, '08-route-badges.png');
});
