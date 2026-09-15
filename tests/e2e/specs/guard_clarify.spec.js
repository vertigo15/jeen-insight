// @ts-check
// The two "stop and ask" branches: a guard refusal that sends zero rows, and a
// clarification whose choice resumes into a completed analysis.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn } = require('./_helpers');

test.beforeEach(async ({ page }) => { await openHarness(page); });

test('guard refusal shows the failing guards and sends no rows', async ({ page }) => {
  await ask(page, Q.guard);

  // Still an ML routing decision, but the run is blocked before any SQL.
  await expect(lastTurn(page)).toHaveAttribute('data-route-path', 'ml');

  const card = page.locator('#v3-placeholder .v3-ml-card.is-guard');
  await expect(card).toBeVisible();
  await expect(card.locator('.v3-ml-refusal')).toContainText('0 rows');
  await expect(card.locator('.v3-ml-guards li.is-fail')).toHaveCount(2);
  await expect(card.locator('.v3-ml-egress')).toContainText('0 rows sent to the model');

  // The recommended exit is the SQL fallback; an override is offered too.
  await expect(card.locator('.v3-ml-exit.is-recommended')).toContainText('Show the 5 weeks');
  await expect(card.locator('.v3-ml-exit.is-override')).toContainText('Run anyway');

  await expect(page.locator('#v3-meta-row .v3-status.is-blocked')).toContainText('Blocked');
});

test('clarification choice resumes into a completed analysis', async ({ page }) => {
  await ask(page, Q.clarify);

  const card = page.locator('#v3-placeholder .v3-ml-card.is-clarify');
  await expect(card).toBeVisible();
  await expect(card.locator('.v3-ml-message')).toContainText('Which measure');
  await expect(card.locator('[data-exit]')).toHaveCount(2);

  // Pick "Revenue" → POST /api/analysis/run → a completed correlation turn.
  await card.locator('[data-exit]', { hasText: 'Revenue' }).click();

  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
  await expect(lastTurn(page)).toHaveAttribute('data-route-path', 'ml');
  await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('correlation');
});
