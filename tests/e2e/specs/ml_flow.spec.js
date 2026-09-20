// @ts-check
// The confirm-card happy path: an ML question stops for confirmation, shows the
// egress notice and editable chips, and running it appends a completed ML turn.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn } = require('./_helpers');

test.beforeEach(async ({ page }) => { await openHarness(page); });

test('forecast stops on a confirm card with egress notice and chips', async ({ page }) => {
  await ask(page, Q.forecast);

  // Thread turn is on the ML path.
  await expect(lastTurn(page)).toHaveAttribute('data-route-path', 'ml');

  // The answer pane hosts the confirm card.
  const card = page.locator('#v3-placeholder .v3-ml-card.is-confirm');
  await expect(card).toBeVisible();
  await expect(card.locator('.v3-ml-tiermeta')).toContainText('Aggregates only');
  await expect(card.locator('.v3-ml-egress')).toContainText('sent to the analysis service');

  // Editable parameter fields, incl. the horizon, grouped into sections.
  await expect(card.locator('[data-chip="horizon"]')).toHaveValue('8');
  await expect(card.locator('.v3-ml-group legend')).toHaveText(['Data', 'Model', 'Output']);
  await expect(card.locator('[data-run]')).toBeEnabled();
});

test('running the confirm card appends a completed ML result with Model details', async ({ page }) => {
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');

  // A second turn (the child run) is appended and selected.
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
  await expect(lastTurn(page)).toHaveAttribute('data-route-path', 'ml');

  // The completed result names the skill and validation metric in the strip.
  const meta = page.locator('#v3-meta-row');
  await expect(meta.locator('.v3-skill-chip')).toHaveText('forecast');
  await expect(meta).toContainText('MASE');

  // Model details tab becomes available for ML results.
  await expect(page.locator('[data-dock="model"]')).toBeVisible();
});

test('"Answer with SQL instead" from the card switches to the SQL path', async ({ page }) => {
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-sql-instead]');

  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
  const turn = lastTurn(page);
  await expect(turn).toHaveAttribute('data-route-path', 'sql');
  await expect(turn.locator('.v3-route-pill.is-sql')).toHaveText('SQL');
});
