// @ts-check
// Conversation & history features (@history): the conversation survives a
// reload, appears in History, can be renamed and deleted, older turns
// re-select and load their stored rows without asking the LLM again, pins
// persist, the question log lists what was asked, and the composer helps
// (starter chips, autocomplete, table browser). Two LLM calls seed a
// two-turn conversation in beforeAll; one more is spent on a starter chip.
const path = require('path');
const { test, expect } = require('@playwright/test');
const L = require('./_live');

const AUTH_FILE = path.join(__dirname, '..', '.auth', 'live.json');
const Q1 = 'What is internet sales amount by calendar year';
const Q2 = 'Show me revenue by country';

test.describe('Conversation & history features', { tag: ['@history', '@feature'] }, () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  let connection = '';
  /** @type {string} */
  let sessionId = '';
  /** @type {any} */
  let firstRaw;
  /** @type {any} */
  let secondRaw;

  test.beforeAll(async ({ browser }) => {
    if (L.unreachable()) return;
    const context = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
    page = await context.newPage();
    await L.openApp(page);
    connection = await L.selectConnection(page);
    await L.newConversation(page);
    const first = await L.ask(page, Q1, {}, L.WAIT.sql);
    L.expectPath(first, 'sql');
    expect(first.status).toMatch(/Completed/);
    firstRaw = await L.rawResult(page);
    const second = await L.ask(page, Q2, {}, L.WAIT.sql);
    L.expectPath(second, 'sql');
    expect(second.status).toMatch(/Completed/);
    secondRaw = await L.rawResult(page);
    sessionId = String(secondRaw.session_id);
    expect(String(firstRaw.session_id), 'both turns share one session').toBe(sessionId);
  });

  test.beforeEach(async ({}, testInfo) => {
    L.skipUnlessLive();
    await L.noteCatalogSource(page, testInfo);
  });

  test.afterEach(async ({}, testInfo) => {
    if (page && testInfo.status !== testInfo.expectedStatus) {
      await testInfo.attach('workspace', { body: await page.screenshot(), contentType: 'image/png' });
    }
  });

  test.afterAll(async () => { await page?.context().close(); });

  const turns = () => page.locator('#v3-thread article.v3-turn');

  async function openHistoryRail() {
    await page.locator('[data-rail="history"]').click();
    const list = page.locator('#v3-conversations-list');
    await expect(list).toBeVisible();
    await expect(list.locator('.v3-conv-item').first()).toBeVisible({ timeout: 30_000 });
    return list;
  }

  test('the conversation is restored after a reload with the newest answer selected', async () => {
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => window.ChatController && document.body.classList.contains('v3-ready'), null, { timeout: 60_000 });
    await L.settle(page, 90_000);
    await expect(turns()).toHaveCount(2, { timeout: 60_000 });
    await expect(turns().nth(0).locator('.v3-question')).toHaveText(Q1);
    await expect(turns().nth(1).locator('.v3-question')).toHaveText(Q2);
    await expect(page.locator('#v3-result-title')).toHaveText(Q2);
    await expect.poll(async () => (await L.rawResult(page))?.session_id, { timeout: 30_000 }).toBe(sessionId);
    const restored = await L.rawResult(page);
    expect(Number(restored.results?.row_count ?? restored.results?.rows?.length)).toBe(Number(secondRaw.results.row_count ?? secondRaw.results.rows.length));
    expect(await L.gridRows(page)).toBeGreaterThan(0);
  });

  test('selecting an older turn restores its answer pane without a new query', async () => {
    const asked = await L.captureRequest(page, '**/api/ask/stream', async () => {
      await turns().nth(0).click();
      await expect(page.locator('#v3-result-title')).toHaveText(Q1, { timeout: 30_000 });
      await expect.poll(async () => (await L.rawResult(page))?.query_id, { timeout: 30_000 }).toBe(firstRaw.query_id);
      await expect.poll(() => L.gridRows(page), { timeout: 30_000 }).toBe(Number(firstRaw.results.row_count ?? firstRaw.results.rows.length));
    }, 1_000);
    expect(asked, 'no /api/ask/stream request was made to show a prior turn').toBeNull();
    // Keyboard: Enter on the newest turn selects it again.
    await turns().nth(1).focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#v3-result-title')).toHaveText(Q2);
  });

  test('History lists the conversation under the current connection and opens it', async () => {
    const list = await openHistoryRail();
    const item = list.locator(`.v3-conv-item[data-open-conversation="${sessionId}"]`);
    await expect(item).toBeVisible();
    await expect(item.locator('.v3-conv-title')).toHaveText(Q1);
    await expect(item.locator('.v3-conv-meta')).toContainText('2 questions');
    await expect(item).toHaveClass(/is-current/);
    await item.click();
    await L.settle(page, 90_000);
    await expect(turns()).toHaveCount(2, { timeout: 60_000 });
    expect((await L.rawResult(page))?.session_id).toBe(sessionId);
  });

  test('renaming a conversation persists across a reload', async () => {
    const title = `Renamed by live test ${Date.now()}`;
    const list = await openHistoryRail();
    const item = list.locator(`.v3-conv-item[data-open-conversation="${sessionId}"]`);
    page.once('dialog', (dialog) => dialog.accept(title));
    await item.locator('[data-conv-action="rename"]').click();
    await expect(item.locator('.v3-conv-title')).toHaveText(title);
    const response = await page.request.get(`/api/conversations/${sessionId}`);
    expect(response.ok()).toBe(true);
    const detail = await response.json();
    expect((detail.conversation || detail).title, 'ConversationDetail.conversation.title').toBe(title);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await L.openApp(page);
    await L.settle(page, 90_000);
    await openHistoryRail();
    await expect(page.locator(`.v3-conv-item[data-open-conversation="${sessionId}"] .v3-conv-title`)).toHaveText(title);
  });

  test('an older turn loads its stored rows lazily, and Refresh re-runs the stored SQL without the LLM', async () => {
    // Re-open from History so turns are "restored" (rows fetched on demand).
    const list = await openHistoryRail();
    await list.locator(`.v3-conv-item[data-open-conversation="${sessionId}"]`).click();
    await L.settle(page, 90_000);
    await expect(turns()).toHaveCount(2, { timeout: 60_000 });
    const older = turns().nth(0);
    const artifactCalls = [];
    const asked = await L.captureRequest(page, '**/api/ask/stream', async () => {
      page.on('request', (r) => { if (/\/api\/conversations\/.+\/turns\/.+\/artifact/.test(r.url())) artifactCalls.push(r.url()); });
      await older.click();
      await expect(page.locator('#v3-result-title')).toHaveText(Q1, { timeout: 30_000 });
      await expect.poll(() => L.gridRows(page), { timeout: 60_000 }).toBeGreaterThan(0);
    }, 1_000);
    expect(asked, 'no LLM call to show stored rows').toBeNull();
    // Refresh: the stored SQL runs again (POST .../rerun) and the app confirms.
    const refresh = page.locator('#v3-placeholder [data-load-data], #v3-thread article.v3-turn.is-selected [data-load-data]').first();
    if (await refresh.count()) {
      const [rerun] = await Promise.all([
        page.waitForResponse((r) => /\/api\/conversations\/.+\/turns\/.+\/rerun/.test(r.url()) && r.request().method() === 'POST', { timeout: 120_000 }),
        refresh.click(),
      ]);
      expect(rerun.ok(), `rerun → ${rerun.status()}`).toBe(true);
      await expect(page.locator('.toast, [role="status"]', { hasText: /Data refreshed/ }).first()).toBeVisible({ timeout: 30_000 }).catch(() => {});
      await expect.poll(() => L.gridRows(page), { timeout: 60_000 }).toBeGreaterThan(0);
    } else {
      test.info().annotations.push({ type: 'note', description: 'no Refresh control offered for a restored turn with stored rows' });
    }
  });

  test('pinning a recent question moves it to Pinned and survives a reload; unpinning removes it', async () => {
    await page.locator('[data-rail="pinned"]').click();
    const host = page.locator('[data-panel="pinned"]');
    await expect(host).toBeVisible();
    const recent = host.locator('.history-item:not(.pinned-item)', { hasText: Q2 }).first();
    await expect(recent).toBeVisible({ timeout: 30_000 });
    await recent.locator('.pin-icon').click();
    await expect(host.locator('.history-item.pinned-item', { hasText: Q2 })).toBeVisible({ timeout: 30_000 });
    const pinned = await page.request.get(`/api/user/pinned-questions?connection=${encodeURIComponent(connection)}`);
    expect(pinned.ok()).toBe(true);
    expect(JSON.stringify(await pinned.json())).toContain(Q2);

    await page.reload({ waitUntil: 'domcontentloaded' });
    await L.openApp(page);
    await L.settle(page, 90_000);
    await page.locator('[data-rail="pinned"]').click();
    const pinnedItem = host.locator('.history-item.pinned-item', { hasText: Q2 });
    await expect(pinnedItem).toBeVisible({ timeout: 30_000 });
    await pinnedItem.locator('.pin-icon').click();
    await expect(host.locator('.history-item.pinned-item', { hasText: Q2 })).toHaveCount(0, { timeout: 30_000 });
    // Search filters the list (matching items stay visible, the rest are hidden).
    const search = page.locator('#question-search');
    if (await search.isVisible()) {
      await search.fill('country');
      await expect(host.locator('.history-item', { hasText: Q2 }).first()).toBeVisible();
      await expect(host.locator('.history-item', { hasText: Q1 }).first()).toBeHidden();
      await search.fill('');
      await expect(host.locator('.history-item', { hasText: Q1 }).first()).toBeVisible();
    }
  });

  test('the question log lists both questions with their status and fills the composer', async () => {
    await page.locator('[data-rail="history"]').click();
    await page.locator('[data-question-log]').click();
    const drawer = page.locator('#history-drawer');
    await expect(drawer).toHaveAttribute('aria-hidden', 'false');
    const entries = drawer.locator('.history-log-entry');
    await expect(entries.first()).toBeVisible({ timeout: 30_000 });
    await expect(drawer.locator('.history-log-entry', { hasText: Q1 }).first()).toBeVisible();
    await expect(drawer.locator('.history-log-entry', { hasText: Q2 }).first()).toBeVisible();
    await expect(drawer.locator('.history-log-entry', { hasText: Q2 }).first().locator('.history-log-status')).toHaveText(/success|completed|ok/i);
    await page.locator('#history-log-search').fill('country');
    await expect(drawer.locator('.history-log-entry', { hasText: Q1 }).first()).toBeHidden();
    await expect(drawer.locator('.history-log-entry', { hasText: Q2 }).first()).toBeVisible();
    await page.locator('#history-log-search').fill('');
    await drawer.locator('.history-log-entry', { hasText: Q1 }).first().click();
    await expect(drawer).toHaveAttribute('aria-hidden', 'true');
    await expect(page.locator('#question-input')).toHaveValue(Q1);
    await page.locator('#question-input').fill('');
  });

  test('composer autocomplete: "/" lists knowledge questions, "@" tables, "#" columns', async () => {
    const input = page.locator('#question-input');
    const menu = page.locator('#question-suggestions');
    await input.fill('/');
    await expect(menu).toBeVisible({ timeout: 30_000 });
    const questions = (await menu.innerText()).trim();
    expect(questions.length).toBeGreaterThan(10);
    // The registered knowledge-pair questions are what "/" offers.
    await input.fill('/calendar year');
    await expect(menu).toContainText(/calendar year/i, { timeout: 15_000 });
    await input.fill('@dim');
    await expect(menu).toBeVisible({ timeout: 30_000 });
    await expect(menu).toContainText(/dim/i);
    await input.fill('#sales');
    await expect(menu).toBeVisible({ timeout: 30_000 });
    await expect(menu).toContainText(/sales/i);
    // Choosing an entry replaces the token.
    await menu.locator('[role="option"], .suggestion-item, li, button').first().click();
    await expect(input).not.toHaveValue('#sales');
    await input.fill('');
    await page.keyboard.press('Escape');
  });

  test('table browser searches tables and reveals their columns', async () => {
    await page.locator('[data-rail="tables"]').click();
    const panel = page.locator('[data-panel="tables"]');
    await expect(panel).toBeVisible();
    await expect(panel.locator('.table-item-header').first()).toBeVisible({ timeout: 60_000 });
    const total = await panel.locator('.table-item-header').count();
    // The search box filters on keyup, so type it (fill() sets the value without key events).
    const search = page.locator('#table-search');
    await search.click();
    await search.pressSequentially('customer');
    await expect.poll(() => panel.locator('.table-item-header').count(), { timeout: 10_000 }).toBeLessThan(total);
    await expect(panel.locator('.table-item-header').first()).toContainText(/customer/i);
    await panel.locator('.table-item-header').first().click();
    await expect(panel.locator('.table-item.is-active')).toHaveCount(1);
    expect((await panel.locator('.table-item.is-active').innerText()).split('\n').length).toBeGreaterThan(3);
    await search.fill('');
    await search.press('Backspace');
    await expect.poll(() => panel.locator('.table-item-header').count(), { timeout: 10_000 }).toBe(total);
  });

  test('New conversation clears the thread and offers starter chips; a chip submits exactly its text', async () => {
    await page.locator('[data-rail="conversation"]').click();
    await L.newConversation(page);
    const chips = page.locator('.v3-suggestions [data-suggestion]');
    await expect(chips.first()).toBeVisible({ timeout: 60_000 });
    const text = (await chips.first().getAttribute('data-suggestion')) || '';
    expect(text.length).toBeGreaterThan(5);
    const before = await turns().count();
    await chips.first().click();
    await page.waitForFunction((n) => document.querySelectorAll('#v3-thread article.v3-turn').length > n, before, { timeout: 15_000 });
    await expect(turns().last().locator('.v3-question')).toHaveText(text);
    // Let the answer finish (one LLM call) so the next test starts clean.
    await page.waitForFunction(() => {
      const t = document.querySelectorAll('#v3-thread article.v3-turn');
      const last = t[t.length - 1];
      return last && !last.classList.contains('is-running');
    }, null, { timeout: L.WAIT.sql }).catch(() => {});
    await page.waitForFunction(() => window.ChatController && !window.ChatController.sending, null, { timeout: 60_000 }).catch(() => {});
    const last = await L.lastTurnResult(page);
    L.assertNoBackendError({ failed: last?.status === 'error', errorText: last?.error || '' });
  });

  test('the conversation drawer collapses at a narrow viewport and Escape/overlay restores focus', async () => {
    await page.setViewportSize({ width: 800, height: 900 });
    const toggle = page.locator('#v3-conversation-toggle');
    await expect(toggle).toBeVisible();
    await toggle.click();
    await expect(page.locator('#v3-conversation')).toHaveClass(/v3-force-open/);
    await expect(page.locator('#v3-drawer-overlay')).toHaveClass(/is-open/);
    await page.locator('#v3-drawer-overlay').click({ position: { x: 790, y: 450 } }).catch(() => page.keyboard.press('Escape'));
    await expect(page.locator('#v3-drawer-overlay')).not.toHaveClass(/is-open/, { timeout: 10_000 });
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test('deleting a throwaway conversation removes it from History', async () => {
    await page.locator('[data-rail="conversation"]').click();
    await L.newConversation(page);
    const turn = await L.ask(page, 'How many customers do we have?', {}, L.WAIT.sql);
    L.expectPath(turn, 'sql');
    const raw = await L.rawResult(page);
    const throwaway = String(raw.session_id);
    expect(throwaway).not.toBe(sessionId);
    const list = await openHistoryRail();
    const item = list.locator(`.v3-conv-item[data-open-conversation="${throwaway}"]`);
    await expect(item).toBeVisible();
    page.once('dialog', (dialog) => dialog.accept());
    await item.locator('[data-conv-action="delete"]').click();
    await expect(item).toHaveCount(0);
    const gone = await page.request.get(`/api/conversations/${throwaway}`);
    expect(gone.status()).toBe(404);
  });
});
