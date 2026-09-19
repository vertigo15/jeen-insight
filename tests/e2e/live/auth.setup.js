// @ts-check
// Logs in once and saves the session for the `live` project (storageState).
const path = require('path');
const { test } = require('@playwright/test');
const { skipUnlessLive, login } = require('./_live');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');

test('log in to the live stack', async ({ page }) => {
  skipUnlessLive();
  await login(page);
  await page.context().storageState({ path: AUTH_FILE });
});
