// @ts-check
// The setup form's behaviour in a real DOM: the live summary, "changed" markers
// and Reset all, the grain→window rule, client-side bounds (nothing is sent),
// a structured 422 landing on its field, and "Edit setup" on a finished result.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask } = require('./_helpers');

const CARD = '#v3-placeholder .v3-ml-card.is-confirm';

test.beforeEach(async ({ page }) => { await openHarness(page); });

test('the planning line reads the setup back and follows edits', async ({ page }) => {
  await ask(page, Q.forecast);
  const card = page.locator(CARD);
  const summary = card.locator('[data-summary]');
  await expect(summary).toHaveText('SUM(Profit) · per week · last 26 weeks · 8 weeks ahead · 90% interval · Auto model');

  await card.locator('[data-chip="horizon"]').fill('12');
  await card.locator('[data-chip="method"]').selectOption('drift');
  await expect(summary).toHaveText('SUM(Profit) · per week · last 26 weeks · 12 weeks ahead · 90% interval · Drift model');

  // Changed fields are marked in text, not only colour; one Reset all restores them.
  const horizon = card.locator('.v3-ml-field[data-field="horizon"]');
  await expect(horizon).toHaveClass(/is-changed/);
  await expect(horizon.locator('[data-badge]')).toBeVisible();
  await expect(card.locator('.v3-ml-field.is-changed')).toHaveCount(2);
  // While anything differs from the plan the egress line carries no stale numbers.
  await expect(card.locator('[data-egress]')).toContainText('recomputed when you run');
  await card.locator('[data-reset-all]').click();
  await expect(card.locator('.v3-ml-field.is-changed')).toHaveCount(0);
  await expect(card.locator('[data-chip="horizon"]')).toHaveValue('8');
  await expect(card.locator('[data-egress]')).toContainText('about 26 weekly totals');
  await expect(card.locator('[data-reset-all]')).toBeHidden();
});

test('changing the grain moves a default window to the new default and relabels units', async ({ page }) => {
  await ask(page, Q.forecast);
  const card = page.locator(CARD);
  const window = card.locator('[data-chip="window"]');
  const unit = card.locator('.v3-ml-field[data-field="window"] [data-unit]');
  await expect(window).toHaveValue('26');
  await expect(unit).toHaveText('weeks');

  await card.locator('[data-chip="grain"]').selectOption('month');
  await expect(window).toHaveValue('24');
  await expect(unit).toHaveText('months');
  await expect(card.locator('.v3-ml-field[data-field="horizon"] [data-unit]')).toHaveText('months');
  await expect(card.locator('[data-summary]')).toContainText('per month · last 24 months · 8 months ahead');

  // A number the user typed is kept; only its unit changes.
  await window.fill('40');
  await card.locator('[data-chip="grain"]').selectOption('day');
  await expect(window).toHaveValue('40');
  await expect(unit).toHaveText('days');
});

test('out-of-bounds values are caught on the card and nothing is sent', async ({ page }) => {
  await ask(page, Q.forecast);
  const card = page.locator(CARD);
  await card.locator('[data-chip="window"]').fill('5');
  const field = card.locator('.v3-ml-field[data-field="window"]');
  await expect(field).toHaveClass(/is-invalid/);
  await expect(field.locator('.v3-ml-fielderr')).toHaveText('At least 12 weeks.');

  const before = await page.evaluate(() => (window.__calls || []).filter((c) => c.url.includes('/api/analysis/run')).length);
  await card.locator('[data-run]').click();
  await expect(card.locator('[data-errsum]')).toHaveText('1 field needs a look.');
  await expect(card.locator('[data-chip="window"]')).toBeFocused();
  await expect(card.locator('[data-chip="window"]')).toHaveAttribute('aria-invalid', 'true');
  const after = await page.evaluate(() => (window.__calls || []).filter((c) => c.url.includes('/api/analysis/run')).length);
  expect(after).toBe(before);
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(1);
});

test('a structured 422 from the server lands on the field it names', async ({ page }) => {
  await ask(page, Q.forecast);
  const card = page.locator(CARD);
  // 60 is inside the card's bounds; the fake backend refuses it for this history.
  await card.locator('[data-chip="horizon"]').fill('60');
  await card.locator('[data-run]').click();
  const field = card.locator('.v3-ml-field[data-field="horizon"]');
  await expect(field).toHaveClass(/is-invalid/);
  await expect(field.locator('.v3-ml-fielderr')).toContainText('at most 13');
  await expect(card.locator('[data-chip="horizon"]')).toBeFocused();
  await expect(card.locator('.v3-ml-error')).toHaveCount(0);
  await expect(card.locator('[data-run]')).toBeEnabled();
});

test('the patch carries only changed fields, typed', async ({ page }) => {
  await ask(page, Q.forecast);
  const card = page.locator(CARD);
  await card.locator('[data-chip="horizon"]').fill('12');
  await card.locator('[data-chip="interval"]').selectOption('0.8');
  await card.locator('[data-chip="method"]').selectOption('theta');
  await card.locator('[data-run]').click();
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
  const call = await page.evaluate(() => (window.__calls || []).find((c) => c.url.includes('/api/analysis/run')));
  expect(call.body.params_patch).toEqual({ horizon: 12, interval: 0.8, method: 'theta' });
});

test('chips without metadata still render as a runnable form', async ({ page }) => {
  await ask(page, Q.anomaly);
  const card = page.locator(CARD);
  await expect(card.locator('.v3-ml-group legend')).toHaveText(['Setup']);
  await expect(card.locator('[data-chip="window"]')).toHaveValue('26');
  await expect(card.locator('[data-chip="window"]')).not.toHaveAttribute('min', /.+/);
  await card.locator('[data-run]').click();
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
});

test('Edit setup opens the finished run\'s form; Re-run waits for a change', async ({ page }) => {
  await ask(page, Q.forecast);
  await page.click(`${CARD} [data-run]`);
  await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('forecast');

  await page.click('#v3-meta-row [data-ml-edit]');
  const setup = page.locator('#v3-ml-definition .v3-ml-card.is-definition');
  await expect(setup).toBeVisible();
  // The pane may have been scrolled to the table; opening the setup reveals it.
  await expect(setup.locator('[data-summary]')).toBeInViewport();
  await expect(setup.locator('.v3-ml-group legend')).toHaveText(['Data', 'Model', 'Output']);
  await expect(setup.locator('[data-summary]')).toContainText('SUM(Profit) · per week · last 26 weeks');
  await expect(setup.locator('[data-run]')).toBeDisabled();
  await expect(setup.locator('[data-note]')).toBeVisible();
  await expect(setup.locator('.v3-ml-error')).toHaveCount(0);

  await setup.locator('[data-chip="horizon"]').fill('12');
  await expect(setup.locator('[data-run]')).toBeEnabled();
  await expect(setup.locator('[data-note]')).toBeHidden();
  await setup.locator('[data-run]').click();
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(3);
  const call = await page.evaluate(() => (window.__calls || []).find((c) => c.url.includes('/api/analysis/rerun')));
  expect(call.body.params_patch).toEqual({ horizon: 12 });
});

test('fields are labelled and reachable by keyboard', async ({ page }) => {
  await ask(page, Q.forecast);
  const card = page.locator(CARD);
  const measure = card.locator('[data-chip="measure_column"]');
  const id = await measure.getAttribute('id');
  await expect(card.locator(`label[for="${id}"]`)).toHaveText(/Measure/);
  await expect(card.locator('[data-chip="window"]')).toHaveAttribute('aria-describedby', /window-help .*window-err/);
  await measure.focus();
  await page.keyboard.press('Tab');
  await expect(card.locator('[data-chip="agg"]')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(card.locator('[data-chip="date_column"]')).toBeFocused();
});
