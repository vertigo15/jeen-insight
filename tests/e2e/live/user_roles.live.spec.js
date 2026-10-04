// @ts-check
// User roles (@admin @security): Insights and Metadata roles live on one shared
// account and are managed separately. Run as an Insights admin who is not a
// Metadata admin (LIVE_EMAIL / LIVE_PASSWORD). It creates one account, signs in
// as it with its own password, and changes its Insights role. A Metadata admin
// must delete the account afterwards: this session is not allowed to. No LLM calls.
const { test, expect } = require('@playwright/test');
const L = require('./_live');
const KP = require('./knowledgePairs');

const PREFIX = process.env.LIVE_ROLES_PREFIX || 'live-roles';
const PASSWORD = 'LiveRoles-Passw0rd!';

/** CSRF token of an API request context that has signed in. */
async function contextCsrf(ctx) {
  const html = await (await ctx.get('/')).text();
  return (html.match(/name="csrf-token"\s+content="([^"]+)"/) || [])[1] || '';
}

async function send(ctx, method, url, csrf, data) {
  const response = await ctx.fetch(url, {
    method,
    headers: { 'X-CSRFToken': csrf, 'Content-Type': 'application/json' },
    data,
    timeout: 30_000,
  });
  return { status: response.status(), body: await response.json().catch(() => ({})) };
}

test.describe('User roles', { tag: ['@admin', '@security'] }, () => {
  test.beforeEach(() => L.skipUnlessLive());

  test('an Insights-only admin manages Insights roles but cannot change Metadata roles or delete accounts', async ({ page }, testInfo) => {
    await L.openApp(page);
    const me = await L.me(page);
    test.skip(me.role !== 'admin', 'needs an Insights admin session');
    const before = await (await page.request.get('/api/users')).json();
    test.skip(before.find((u) => u.id === me.id)?.metadata_role === 'admin', 'run as an Insights admin who is not a Metadata admin');

    const email = `${PREFIX}-${Date.now()}@test.local`;
    L.annotate(testInfo, 'note', `created ${email}; a Metadata admin must delete it`);

    // 1. Create the account in Settings > Users. The Metadata role is locked to viewer.
    await L.openSettings(page, 'users');
    await expect(page.locator('#sp-add-metadata-role')).toBeDisabled();
    await expect(page.locator('#sp-add-metadata-role')).toHaveValue('viewer');
    await page.locator('#sp-add-name').fill('Live Roles User');
    await page.locator('#sp-add-email').fill(email);
    await page.locator('#sp-add-password').fill(PASSWORD);
    await page.locator('#sp-add-role').selectOption('viewer');
    const [created] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/api/users') && r.request().method() === 'POST'),
      page.locator('#sp-add-submit').click(),
    ]);
    expect(created.status(), await created.text()).toBe(201);
    const userId = (await created.json()).id;
    const row = page.locator('#sp-users-rows tr', { hasText: email });
    await expect(row.locator('.sp-role-select')).toHaveValue('viewer');
    await expect(row.locator('.sp-metadata-role-select')).toHaveValue('viewer');
    await expect(row.locator('.sp-metadata-role-select')).toBeDisabled();
    await expect(row.locator('.sp-user-del-btn')).toBeDisabled();

    // 2. The new account signs in with its own password as an Insights viewer.
    let login = await KP.loginRequestContext({ base: L.BASE, email, password: PASSWORD });
    expect(login.user.role).toBe('viewer');
    expect((await login.ctx.get('/api/users')).status()).toBe(403);
    await login.ctx.dispose();

    // 3. Promote it to Insights admin in the UI; its Metadata role does not move.
    const [promoted] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith(`/api/users/${userId}/role`) && r.request().method() === 'PATCH'),
      row.locator('.sp-role-select').selectOption('admin'),
    ]);
    expect(promoted.status(), await promoted.text()).toBe(200);
    const afterPromote = (await (await page.request.get('/api/users')).json()).find((u) => u.id === userId);
    expect(afterPromote).toMatchObject({ role: 'admin', metadata_role: 'viewer' });

    // 4. This session cannot change Metadata roles, grant Metadata admin, or delete the shared account.
    const adminCsrf = await L.csrfToken(page);
    expect((await send(page.request, 'PATCH', `/api/users/${userId}/role`, adminCsrf, { app: 'metadata', role: 'admin' })).status).toBe(403);
    expect((await send(page.request, 'DELETE', `/api/users/${userId}`, adminCsrf)).status).toBe(403);
    expect((await send(page.request, 'POST', '/api/users', adminCsrf, {
      name: 'Never Created', email: `${PREFIX}-never-${Date.now()}@test.local`, password: PASSWORD, role: 'viewer', metadata_role: 'admin',
    })).status).toBe(403);
    expect((await send(page.request, 'PATCH', `/api/users/${me.id}/role`, adminCsrf, { app: 'insights', role: 'viewer' })).status).toBe(400);

    // 5. Signed in again, the promoted account is an Insights admin and is just as limited on Metadata.
    login = await KP.loginRequestContext({ base: L.BASE, email, password: PASSWORD });
    try {
      expect(login.user.role).toBe('admin');
      const csrf = await contextCsrf(login.ctx);
      expect(csrf, 'signed-in page exposes a CSRF token').not.toBe('');
      expect((await login.ctx.get('/api/users')).status()).toBe(200);
      expect((await send(login.ctx, 'PATCH', `/api/users/${me.id}/role`, csrf, { app: 'metadata', role: 'admin' })).status).toBe(403);
      expect((await send(login.ctx, 'DELETE', `/api/users/${me.id}`, csrf)).status).toBe(403);
      expect((await send(login.ctx, 'PATCH', `/api/users/${userId}/role`, csrf, { app: 'insights', role: 'viewer' })).status).toBe(400);
    } finally {
      await login.ctx.dispose();
    }

    // 6. Nothing above changed anyone's Metadata role.
    const after = await (await page.request.get('/api/users')).json();
    for (const user of before) {
      expect(after.find((u) => u.id === user.id)?.metadata_role, `Metadata role of ${user.email}`).toBe(user.metadata_role);
    }
    expect(after.find((u) => u.id === userId)?.metadata_role).toBe('viewer');

    // Leave the account as an Insights viewer until a Metadata admin deletes it.
    expect((await send(page.request, 'PATCH', `/api/users/${userId}/role`, adminCsrf, { app: 'insights', role: 'viewer' })).status).toBe(200);
  });
});
