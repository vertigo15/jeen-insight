// @ts-check
// Result-header actions: Send appears only for users who can actually send
// (Microsoft Entra + a delivery connector), and the top bar carries no
// notifications control until notifications exist.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask } = require('./_helpers');

test.beforeEach(async ({ page }) => { await openHarness(page); });

test('Send stays hidden for a user without Entra and a delivery connector', async ({ page }) => {
  await ask(page, Q.sqlAggregate);
  await expect(page.locator('#export-btn')).toBeEnabled();
  await expect(page.locator('#send-result-btn')).toBeHidden();
});

test('Send shows for an eligible user and enables once the result has a handle', async ({ page }) => {
  // auth.js announces the signed-in user after the shell is built.
  await page.evaluate(() => {
    window._currentUser = { ...window._currentUser, connectors_enabled: true, is_entra: true };
    document.dispatchEvent(new CustomEvent('jeen:current-user', { detail: window._currentUser }));
  });
  const send = page.locator('#send-result-btn');
  await expect(send).toBeVisible();
  await expect(send).toBeDisabled();
  await expect(send).toHaveAttribute('title', 'Send or share this result');

  // A result without a server snapshot cannot be sent; the tooltip says why.
  await ask(page, Q.sqlCount);
  await expect(send).toBeDisabled();
  await expect(send).toHaveAttribute('title', 'This result cannot be sent (no server snapshot).');

  await page.evaluate(() => { window._resultHandle = 'handle-1'; });
  await ask(page, Q.sqlAggregate);
  await expect(send).toBeEnabled();
  await expect(send).toHaveAttribute('title', 'Send result');
});

test('the top bar has no notifications control', async ({ page }) => {
  await expect(page.locator('.v3-topbar [aria-label="Notifications"]')).toHaveCount(0);
  await expect(page.locator('.v3-topbar .v3-topbar-icon')).toHaveCount(0);
});
