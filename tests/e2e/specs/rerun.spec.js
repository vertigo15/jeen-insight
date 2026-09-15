// @ts-check
// Re-running a completed ML result appends a child turn and records the param
// diff (never mutating the parent).
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn } = require('./_helpers');

test.beforeEach(async ({ page }) => { await openHarness(page); });

test('re-run appends a child turn and shows the parameter diff', async ({ page }) => {
  // Get a completed forecast result first (confirm → run).
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
  await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('forecast');

  // Re-run with a natural-language instruction (the composer's re-run path).
  await page.evaluate(() => window.ChatController.rerunAnalysis('extend the horizon to 12 weeks'));
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(3);
  await expect(lastTurn(page)).toHaveAttribute('data-route-path', 'ml');

  // Model details for the child turn shows the horizon change 8 → 12.
  await page.click('[data-dock="model"]');
  const dock = page.locator('#v3-dock-body');
  await expect(dock).toContainText('Changed from the previous run');
  await expect(dock.locator('.v3-ml-diff-chip')).toContainText('horizon');
  await expect(dock.locator('.v3-ml-diff-chip')).toContainText('12');
});
