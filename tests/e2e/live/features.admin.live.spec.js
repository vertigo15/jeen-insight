// @ts-check
// Admin settings (@admin): every test snapshots the state it touches and
// restores it in a finally block, so a failure never leaves the shared stack
// changed. Skips unless the login is an admin. No LLM calls: preference
// effects on the query payload are captured by intercepting (and aborting)
// the request the UI would have sent.
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');
const KP = require('./knowledgePairs');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');

test.describe('Admin settings', { tag: ['@admin', '@feature'] }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  let connection = '';
  /** @type {any} */
  let user = null;

  /** Mutating call through the BFF with the page's CSRF token. */
  async function mutate(method, url, data) {
    const response = await page.request.fetch(url, {
      method,
      headers: { 'X-CSRFToken': await L.csrfToken(page), 'Content-Type': 'application/json' },
      data: data === undefined ? undefined : JSON.stringify(data),
      timeout: 60_000,
    });
    return response;
  }
  async function getJson(url) {
    const response = await page.request.get(url, { timeout: 60_000 });
    if (!response.ok()) throw new Error(`${url} → ${response.status()} ${(await response.text()).slice(0, 200)}`);
    return response.json();
  }
  const toast = (text) => page.locator('.toast, .v3-toast, [role="status"], [role="alert"]', { hasText: text }).first();

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;
    const context = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
    page = await context.newPage();
    await L.openApp(page);
    connection = await L.selectConnection(page);
    user = await L.me(page);
  });

  test.beforeEach(async ({}, testInfo) => {
    L.skipUnlessLive();
    test.skip(!user || user.role !== 'admin', `login "${user && user.email}" is not an admin`);
    await L.noteCatalogSource(page, testInfo);
  });

  test.afterEach(async ({}, testInfo) => {
    if (page && testInfo.status !== testInfo.expectedStatus) {
      await testInfo.attach('workspace', { body: await page.screenshot(), contentType: 'image/png' });
    }
    if (page) await L.closeSettings(page).catch(() => {});
  });

  test.afterAll(async () => { await page?.context().close(); });

  // ── General preferences (per browser; restored via "Reset all") ───────────

  test('general preferences persist and shape the next query payload', async ({}, testInfo) => {
    const prefsBefore = await page.evaluate(() => JSON.stringify(window.JeenPreferences ? window.JeenPreferences.getAll() : {}));
    try {
      await L.openSettings(page, 'general');
      await page.locator('#sp-theme').selectOption('dark');
      await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
      await page.locator('#sp-rowlimit').selectOption('25');
      await page.locator('#sp-temp').selectOption('0.0');
      await page.locator('#sp-insights').selectOption('off');
      await L.closeSettings(page);

      await page.reload({ waitUntil: 'domcontentloaded' });
      await L.openApp(page);
      await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');

      // The payload the UI would send carries the preferences; abort it so no LLM call is spent.
      await L.settle(page, 60_000);
      const payload = await L.captureRequest(page, '**/api/ask/stream', async () => {
        await page.evaluate(() => { void window.ChatController.send('Total sales amount by year (payload capture)'); });
      }, 15_000);
      expect(payload, 'the UI issued /api/ask/stream').toBeTruthy();
      L.annotate(testInfo, 'ask_payload', { limit: payload.limit, temperature: payload.temperature, eval_analytics: payload.eval_analytics });
      // Each preference is checked softly so one mismatch does not hide another.
      // Settings writes `rowLimit` (preferences.js) but the v3 composer reads
      // `prefs.resultLimit` (workspaceController.js send()); Settings writes
      // `autoInsights` but the composer reads `prefs.aiAnalytics`. Either
      // mismatch means the preference is silently ignored.
      if (payload.limit !== 25) L.annotate(testInfo, 'finding', `Row limit = 25 in Settings, but the query payload carries limit=${payload.limit} (Settings writes prefs.rowLimit, workspaceController.send reads prefs.resultLimit)`);
      expect.soft(payload.limit, 'Row limit preference reaches the query payload').toBe(25);
      expect.soft(payload.temperature, 'temperature preference reaches the query payload').toBe(0);
      if (payload.eval_analytics !== false) L.annotate(testInfo, 'finding', `Auto-insights = Off in Settings, but the query payload carries eval_analytics=${payload.eval_analytics} (Settings writes prefs.autoInsights, workspaceController.send reads prefs.aiAnalytics)`);
      expect.soft(payload.eval_analytics, 'Auto-insights = Off should send eval_analytics=false').toBe(false);
    } finally {
      // The aborted send leaves a failed turn behind; reset the thread and the preferences.
      await page.evaluate((json) => {
        const prefs = JSON.parse(json);
        if (window.JeenPreferences && window.JeenPreferences.resetAll) window.JeenPreferences.resetAll();
        if (window.JeenPreferences && prefs.theme && window.JeenPreferences.setTheme) window.JeenPreferences.setTheme(prefs.theme);
        if (window.ChatController) window.ChatController.newConversation();
      }, prefsBefore).catch(() => {});
      await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
      await L.openApp(page).catch(() => {});
    }
    await L.openSettings(page, 'general');
    page.once('dialog', (dialog) => dialog.accept());
    await page.locator('#sp-reset-prefs').click();
    await expect(page.locator('#sp-rowlimit')).not.toHaveValue('25');
  });

  // ── Query & Safety runtime guardrails ─────────────────────────────────────

  test('runtime guardrails save, clamp, persist and refuse blanks', async () => {
    const original = await getJson('/api/settings/runtime');
    try {
      await L.openSettings(page, 'query-safety');
      const turns = page.locator('#sp-rt-turns');
      await expect(turns).toBeVisible({ timeout: 30_000 });
      await expect(turns).toHaveValue(String(original.conversation_context_turns));
      const next = original.conversation_context_turns > 1 ? original.conversation_context_turns - 1 : original.conversation_context_turns + 1;

      await turns.fill('');
      await page.locator('#sp-rt-save').click();
      await expect(toast(/Enter a number/)).toBeVisible({ timeout: 10_000 });

      await turns.fill(String(next));
      const [saved] = await Promise.all([
        page.waitForResponse((r) => r.url().includes('/api/settings/runtime') && r.request().method() === 'PUT'),
        page.locator('#sp-rt-save').click(),
      ]);
      expect(saved.ok(), `PUT runtime → ${saved.status()}`).toBe(true);
      await expect(toast(/settings saved/)).toBeVisible({ timeout: 10_000 });
      expect((await getJson('/api/settings/runtime')).conversation_context_turns).toBe(next);
      await L.closeSettings(page);
      await L.openSettings(page, 'query-safety');
      await expect(page.locator('#sp-rt-turns')).toHaveValue(String(next), { timeout: 30_000 });

      // Out-of-range values are clamped, not rejected.
      const clamped = await mutate('PUT', '/api/settings/runtime', { max_result_rows: 10_000_000 });
      expect(clamped.ok()).toBe(true);
      expect((await clamped.json()).max_result_rows).toBeLessThan(10_000_000);
    } finally {
      const restore = await mutate('PUT', '/api/settings/runtime', original);
      expect(restore.ok(), 'runtime settings restored').toBe(true);
    }
  });

  // ── Prompt registry ───────────────────────────────────────────────────────

  test('a prompt can be edited, saved as a new version, restored from history and reset', async ({}, testInfo) => {
    const list = await getJson('/api/settings/prompts');
    // Prefer a prompt off the hot path of a question so an interrupted test hurts least.
    const preferred = ['chart_edit', 'chart_generation', 'insights', 'profile'];
    const meta = list.find((p) => preferred.some((n) => String(p.name).includes(n))) || list[list.length - 1];
    const name = meta.name;
    L.annotate(testInfo, 'prompt', name);
    const before = await getJson(`/api/settings/prompts/${name}`);
    const marker = `\n-- live-test marker ${Date.now()}`;
    try {
      await L.openSettings(page, 'prompts');
      await page.locator(`.sp-prompt-row[data-name="${name}"]`).click();
      await page.locator(`#sp-edit-${name}`).click();
      const textarea = page.locator(`#sp-prompt-ta-${name}`);
      await expect(textarea).toBeVisible();
      const original = await textarea.inputValue();
      await textarea.fill(original + marker);
      const save = page.locator(`#sp-save-${name}`);
      await expect(save).toBeEnabled();
      await save.click();
      await expect(toast(/Prompt saved/)).toBeVisible({ timeout: 30_000 });
      await expect.poll(async () => (await getJson(`/api/settings/prompts/${name}`)).content, { timeout: 30_000 }).toContain(marker.trim());
      const after = await getJson(`/api/settings/prompts/${name}`);
      expect(after.version).toBeGreaterThan(before.version || 0);

      // Version history lists the new version; restoring the previous one (confirm dialog) brings the old content back.
      await expect(page.locator(`#sp-vh-${name}`)).toBeVisible();
      await page.locator(`#sp-vh-${name} summary`).click();
      const rows = page.locator(`#sp-vh-body-${name} .sp-vh-row`);
      await expect(rows.first()).toBeVisible({ timeout: 30_000 });
      expect(await rows.count()).toBeGreaterThanOrEqual(2);
      await expect(page.locator(`#sp-vh-body-${name} .sp-vh-row`, { hasText: 'Active' })).toHaveCount(1);
      const restores = page.locator(`#sp-vh-body-${name} .sp-vh-restore`);
      await expect(restores.first()).toBeVisible();
      page.once('dialog', (dialog) => dialog.accept());
      await restores.first().click();
      await expect.poll(async () => (await getJson(`/api/settings/prompts/${name}`)).content, { timeout: 30_000 }).not.toContain(marker.trim());
      const back = await getJson(`/api/settings/prompts/${name}`);
      expect(back.content.trim()).toBe(String(before.content).trim());
    } finally {
      // Leave the prompt exactly as found: default → reset; custom → re-save the original text.
      if (before.is_custom) await mutate('PUT', `/api/settings/prompts/${name}`, { content: before.content });
      else await mutate('DELETE', `/api/settings/prompts/${name}`);
    }
    const final = await getJson(`/api/settings/prompts/${name}`);
    expect(final.content.trim()).toBe(String(before.content).trim());
  });

  test('the resolved system prompt for the connection lists the knowledge pairs', async () => {
    const resolved = await getJson(`/api/settings/prompts/${KP.PROMPT_NAME}/resolved?connection=${encodeURIComponent(connection)}`);
    expect(resolved.connection.source_key).toBe(connection);
    const pairs = KP.parsePairs(resolved.resolved_content || '');
    expect(pairs.length, `pairs parsed from the ${resolved.catalog_source} catalog`).toBeGreaterThan(0);
    expect(resolved.unresolved_placeholders).not.toContain('knowledge_pairs');
  });

  // ── AI models ─────────────────────────────────────────────────────────────

  test('model health re-check updates the cards and the active model can be switched and back', async ({}, testInfo) => {
    const models = await getJson('/api/settings/models');
    const active = models.find((m) => m.is_active);
    expect(active, 'one model is active').toBeTruthy();
    L.annotate(testInfo, 'active_model', active.name);
    await L.openSettings(page, 'ai-models');
    await expect(page.locator('.sp-model-card.is-active')).toHaveCount(1, { timeout: 30_000 });
    const [health] = await Promise.all([
      page.waitForResponse((r) => r.url().includes('/api/settings/models/health'), { timeout: 120_000 }),
      page.locator('#sp-models-recheck').click(),
    ]);
    expect(health.ok(), `health → ${health.status()}`).toBe(true);
    const report = await health.json();
    L.annotate(testInfo, 'models_health', JSON.stringify(report).slice(0, 300));
    await expect(page.locator('.sp-model-card.is-active')).toHaveCount(1);

    const other = models.find((m) => !m.is_active && m.available);
    test.skip(!other, 'only one available model on this stack');
    try {
      const switched = await mutate('PUT', '/api/settings/models/active', { name: other.name });
      expect(switched.ok(), `switch → ${switched.status()}`).toBe(true);
      expect((await getJson('/api/settings/models/active')).name || (await getJson('/api/settings/models')).find((m) => m.is_active).name).toBe(other.name);
    } finally {
      const back = await mutate('PUT', '/api/settings/models/active', { name: active.name });
      expect(back.ok(), 'active model restored').toBe(true);
    }
  });

  // ── Metadata catalog source and MCP ───────────────────────────────────────

  test('catalog source switches between DB and MCP, both adapters serve the knowledge pairs, then restores', async ({}, testInfo) => {
    const status = await getJson(`/api/mcp/status?connection=${encodeURIComponent(connection)}`);
    const original = String(status.catalog_source);
    test.skip(!status.active_server_id, 'no active MCP server: only the DB source is available');
    const other = original === 'mcp' ? 'db' : 'mcp';
    L.annotate(testInfo, 'catalog_switch', `${original} → ${other} → ${original}`);
    try {
      await L.openSettings(page, 'metadata-catalog');
      const seg = page.locator('#mc-seg');
      await expect(seg).toBeVisible({ timeout: 30_000 });
      await expect(seg.locator(`[data-src="${original}"]`)).toHaveClass(/is-active/);
      const [put] = await Promise.all([
        page.waitForResponse((r) => r.url().includes('/api/mcp/catalog-source') && r.request().method() === 'PUT', { timeout: 60_000 }),
        seg.locator(`[data-src="${other}"]`).click(),
      ]);
      expect(put.ok(), `PUT catalog-source → ${put.status()}`).toBe(true);
      await expect(seg.locator(`[data-src="${other}"]`)).toHaveClass(/is-active/, { timeout: 30_000 });
      expect(String((await getJson(`/api/mcp/status?connection=${encodeURIComponent(connection)}`)).catalog_source)).toBe(other);

      // The other adapter must give the planner the same pairs.
      const resolved = await getJson(`/api/settings/prompts/${KP.PROMPT_NAME}/resolved?connection=${encodeURIComponent(connection)}`);
      expect(String(resolved.catalog_source)).toBe(other);
      const pairs = KP.parsePairs(resolved.resolved_content || '');
      expect(pairs.length, `knowledge pairs under the ${other} catalog`).toBeGreaterThan(0);

      // Cache refresh works from the same panel.
      const refresh = page.locator('#mc-refresh-btn');
      if (await refresh.isVisible() && await refresh.isEnabled()) {
        const [refreshed] = await Promise.all([
          page.waitForResponse((r) => r.url().includes('/api/mcp/refresh'), { timeout: 120_000 }),
          refresh.click(),
        ]);
        expect(refreshed.ok(), `refresh → ${refreshed.status()}`).toBe(true);
      }
    } finally {
      const restore = await mutate('PUT', '/api/mcp/catalog-source', { catalog_source: original });
      expect(restore.ok(), 'catalog source restored').toBe(true);
    }
    expect(String((await getJson(`/api/mcp/status?connection=${encodeURIComponent(connection)}`)).catalog_source)).toBe(original);
  });

  test('MCP server health-check passes, tools list the catalog prompt, and a read-only tool call returns content', async ({}, testInfo) => {
    const status = await getJson(`/api/mcp/status?connection=${encodeURIComponent(connection)}`);
    test.skip(!status.active_server_id, 'no active MCP server configured');
    const serverId = status.active_server_id;
    const health = await mutate('POST', `/api/mcp/servers/${serverId}/health-check`, {});
    expect(health.ok(), `health-check → ${health.status()}`).toBe(true);
    const healthBody = await health.json();
    L.annotate(testInfo, 'mcp_health', JSON.stringify(healthBody).slice(0, 200));
    expect(JSON.stringify(healthBody)).toMatch(/ok|healthy|reachable|"status"/i);

    const tools = await getJson(`/api/mcp/servers/${serverId}/tools`);
    const names = JSON.stringify(tools);
    expect(names).toMatch(/get_catalog_prompt/);
    expect(names).toMatch(/list_connections/);

    // Tools not flagged read-only need explicit confirmation (409 otherwise): assert the gate, then confirm.
    const unconfirmed = await mutate('POST', `/api/mcp/servers/${serverId}/tools/call`, { tool_name: 'list_connections', arguments: {}, confirmed: false });
    expect([200, 409]).toContain(unconfirmed.status());
    if (unconfirmed.status() === 409) L.annotate(testInfo, 'note', 'list_connections is not marked read-only by the server: confirmation gate engaged');
    const call = unconfirmed.status() === 200
      ? unconfirmed
      : await mutate('POST', `/api/mcp/servers/${serverId}/tools/call`, { tool_name: 'list_connections', arguments: {}, confirmed: true });
    expect(call.ok(), `tools/call → ${call.status()} ${(await call.text()).slice(0, 200)}`).toBe(true);
    const result = await call.json();
    expect(result.ok, `tool call ok: ${JSON.stringify(result).slice(0, 200)}`).toBe(true);
    expect(JSON.stringify(result.result)).toMatch(new RegExp(connection, 'i'));

    await L.openSettings(page, 'metadata-catalog');
    const testBtn = page.locator('#mc-test-btn');
    if (await testBtn.count()) {
      await testBtn.first().click();
      await expect(page.locator('.sp-content')).toContainText(/healthy|ok|reachable|tools/i, { timeout: 60_000 });
    }
  });

  // ── Users ─────────────────────────────────────────────────────────────────

  test('users can be created, re-roled and removed; self controls are protected', async () => {
    const email = `live-test-${Date.now()}@test.local`;
    let createdId = null;
    try {
      await L.openSettings(page, 'users');
      await expect(page.locator('#sp-add-submit')).toBeVisible({ timeout: 30_000 });
      await page.locator('#sp-add-name').fill('Live Test User');
      await page.locator('#sp-add-email').fill(email);
      await page.locator('#sp-add-password').fill('LiveTest-Passw0rd!');
      await page.locator('#sp-add-role').selectOption('viewer');
      const [created] = await Promise.all([
        page.waitForResponse((r) => r.url().endsWith('/api/users') && r.request().method() === 'POST'),
        page.locator('#sp-add-submit').click(),
      ]);
      expect(created.status(), await created.text()).toBe(201);
      createdId = (await created.json()).id;
      const row = page.locator('#sp-users-rows tr', { hasText: email });
      await expect(row).toBeVisible({ timeout: 30_000 });
      await expect(row.locator('.sp-role-select')).toHaveValue('viewer');

      const [reroled] = await Promise.all([
        page.waitForResponse((r) => r.url().includes(`/api/users/${createdId}/role`) && r.request().method() === 'PATCH'),
        row.locator('.sp-role-select').selectOption('editor'),
      ]);
      expect(reroled.ok()).toBe(true);
      const users = await getJson('/api/users');
      expect(users.find((u) => u.id === createdId).role).toBe('editor');

      // Own row ("(you)" badge): no delete button and the role select is disabled.
      const mine = page.locator('#sp-users-rows tr', { has: page.locator('.sp-user-you') });
      await expect(mine).toHaveCount(1);
      await expect(mine.locator('.sp-role-select')).toBeDisabled();
      await expect(mine.locator('.sp-user-del-btn')).toHaveCount(0);
      const selfDelete = await mutate('DELETE', `/api/users/${user.id}`);
      expect(selfDelete.status()).toBe(400);

      page.once('dialog', (dialog) => dialog.accept());
      const [deleted] = await Promise.all([
        page.waitForResponse((r) => r.url().endsWith(`/api/users/${createdId}`) && r.request().method() === 'DELETE'),
        row.locator('.sp-user-del-btn').click(),
      ]);
      expect(deleted.ok()).toBe(true);
      await expect(page.locator('#sp-users-rows tr', { hasText: email })).toHaveCount(0, { timeout: 30_000 });
      createdId = null;
    } finally {
      if (createdId) await mutate('DELETE', `/api/users/${createdId}`).catch(() => {});
    }
  });

  test('About shows the running version, model and prompt count', async () => {
    await L.openSettings(page, 'about');
    const card = page.locator('#sp-about-card');
    await expect(card).toBeVisible({ timeout: 30_000 });
    const text = await card.innerText();
    const info = await getJson('/api/settings/app-info');
    for (const value of [info.version, info.model, info.active_model].filter(Boolean)) expect(text).toContain(String(value));
    expect(text).toMatch(/prompt/i);
  });
});
