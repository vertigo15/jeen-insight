// @ts-check
// Settings > Users manages two roles per shared account: the Insights role and
// the Metadata (Schema Modeler) role. Only a Metadata admin may change Metadata
// roles or delete the shared account.
const { test, expect } = require('@playwright/test');
const { openHarness } = require('./_helpers');

const ME = { id: 1, name: 'Ada Admin', email: 'ada@jeen.ai', role: 'admin' };

function users(myMetadataRole) {
  return [
    { id: 1, name: 'Ada Admin', email: 'ada@jeen.ai', role: 'admin', metadata_role: myMetadataRole, status: 'active', avatar_hue: 10 },
    { id: 2, name: 'Ben Viewer', email: 'ben@jeen.ai', role: 'viewer', metadata_role: 'editor', status: 'active', avatar_hue: 200 },
  ];
}

async function openUsers(page, myMetadataRole) {
  await openHarness(page);
  await page.evaluate(async ({ me, list }) => {
    window._currentUser = me;
    window.__userCalls = [];
    const fallbackFetch = window.fetch;
    const json = (body, status = 200) => new Response(JSON.stringify(body), {
      status, headers: { 'Content-Type': 'application/json' },
    });
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input.url;
      const method = (init.method || 'GET').toUpperCase();
      if (url === '/api/users' && method === 'GET') return json(list);
      if (url.startsWith('/api/users')) {
        const body = init.body ? JSON.parse(init.body) : null;
        window.__userCalls.push({ url, method, body });
        if (method === 'POST') return json({ id: 3, ...body }, 201);
        if (method === 'PATCH' && body.role === 'admin') return json({ error: 'refused' }, 403);
        if (method === 'PATCH') return json({ id: 2, ...body });
        return json({ deleted: true });
      }
      return fallbackFetch(input, init);
    };
    const { SettingsPage } = await import('/src/static/settings/settingsPage.js');
    const settings = new SettingsPage();
    settings.mount();
    settings.open();
    document.querySelector('.sp-nav-item[data-id="users"]').click();
  }, { me: ME, list: users(myMetadataRole) });
  await expect(page.locator('#sp-users-rows tr')).toHaveCount(2);
}

const ben = (page) => page.locator('#sp-users-rows tr', { hasText: 'ben@jeen.ai' });
const mine = (page) => page.locator('#sp-users-rows tr', { hasText: 'ada@jeen.ai' });
const calls = (page) => page.evaluate(() => window.__userCalls);

test('an Insights-only admin changes Insights roles but not Metadata roles or accounts', async ({ page }) => {
  await openUsers(page, 'viewer');

  await expect(ben(page).locator('.sp-role-select')).toBeEnabled();
  await expect(ben(page).locator('.sp-metadata-role-select')).toBeDisabled();
  await expect(ben(page).locator('.sp-metadata-role-select')).toHaveAttribute('title', /Only a Metadata admin/);
  await expect(ben(page).locator('.sp-user-del-btn')).toBeDisabled();
  await expect(page.locator('#sp-add-metadata-role')).toBeDisabled();
  await expect(page.locator('#sp-add-metadata-role')).toHaveValue('viewer');

  await ben(page).locator('.sp-role-select').selectOption('editor');
  await expect.poll(() => calls(page)).toEqual([
    { url: '/api/users/2/role', method: 'PATCH', body: { app: 'insights', role: 'editor' } },
  ]);
});

test('a Metadata admin can change both roles and delete accounts', async ({ page }) => {
  await openUsers(page, 'admin');

  await expect(ben(page).locator('.sp-metadata-role-select')).toBeEnabled();
  await expect(ben(page).locator('.sp-user-del-btn')).toBeEnabled();
  await expect(page.locator('#sp-add-metadata-role')).toBeEnabled();

  await ben(page).locator('.sp-metadata-role-select').selectOption('viewer');
  await expect.poll(() => calls(page)).toEqual([
    { url: '/api/users/2/role', method: 'PATCH', body: { app: 'metadata', role: 'viewer' } },
  ]);
});

test('your own row is read-only in both apps', async ({ page }) => {
  await openUsers(page, 'admin');

  await expect(mine(page).locator('.sp-role-select')).toBeDisabled();
  await expect(mine(page).locator('.sp-metadata-role-select')).toBeDisabled();
  await expect(mine(page).locator('.sp-user-del-btn')).toHaveCount(0);
});

test('one click on Add user sends one request after the list reloads', async ({ page }) => {
  await openUsers(page, 'admin');
  // A refused change reloads the list, which used to wire Add user a second time.
  await ben(page).locator('.sp-role-select').selectOption('admin');
  await expect.poll(async () => (await calls(page)).length).toBe(1);
  await expect(ben(page).locator('.sp-role-select')).toHaveValue('viewer');

  await page.locator('#sp-add-name').fill('Cleo New');
  await page.locator('#sp-add-email').fill('cleo@jeen.ai');
  await page.locator('#sp-add-password').fill('long-enough-1');
  await page.locator('#sp-add-metadata-role').selectOption('editor');
  await page.locator('#sp-add-submit').click();

  await expect.poll(async () => (await calls(page)).filter((c) => c.method === 'POST')).toEqual([
    {
      url: '/api/users',
      method: 'POST',
      body: { name: 'Cleo New', email: 'cleo@jeen.ai', password: 'long-enough-1', role: 'editor', metadata_role: 'editor' },
    },
  ]);
});
