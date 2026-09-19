// @ts-check
// Helpers for the LIVE suite: drive the real UI on :8501 the way a user does
// and read back what the user sees. Everything is structural — the LLM is
// nondeterministic, so tests assert shapes (route badge, status, row ranges,
// SQL clauses, card sections), never exact numbers.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');

const BASE = process.env.LIVE_APP_URL || 'http://localhost:8501';
const EMAIL = process.env.LIVE_EMAIL || 'admin';
const PASSWORD = process.env.LIVE_PASSWORD || 'admin';
const CONNECTION = process.env.LIVE_CONNECTION || 'AdventureWorksDW';
const SHOTS = path.resolve(__dirname, '..', 'live-screenshots');

/** How long the UI may take, end to end, per kind of answer. */
const WAIT = {
  sql: 180_000,      // router + SQL generation + execution + insights (LLM latency varies a lot under load)
  card: 150_000,     // router + planner + span probe → confirm/guard card
  run: 180_000,      // /api/analysis/run → sandbox → chart
};

const INFRA_ERROR = /Backend unavailable|Invalid internal token|Name or service not known|ECONNREFUSED|Max retries exceeded|502|503|504/i;

const unreachable = () => process.env.LIVE_UNREACHABLE === '1';

/** Skip a whole file when the stack is down (globalSetup sets the flag). */
function skipUnlessLive() {
  test.skip(unreachable(), `live stack not reachable at ${BASE} — start it with 'docker compose up -d'`);
}

// ── Session ──────────────────────────────────────────────────────────────────

async function login(page) {
  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="email"]', EMAIL);
  await page.fill('input[name="password"]', PASSWORD);
  await page.press('input[name="password"]', 'Enter');
  // A successful login redirects away from /login; anything else is a failure
  // whose message is on the login page itself.
  const left = await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 30_000 }).then(() => true).catch(() => false);
  if (!left) {
    const text = await page.locator('.login-error, .error, [role="alert"]').first().textContent().catch(() => '');
    throw new Error(`Login failed for ${EMAIL}: ${(text || '').trim() || 'still on /login'} (set LIVE_EMAIL / LIVE_PASSWORD)`);
  }
  await page.waitForFunction(() => document.body.classList.contains('v3-ready'), null, { timeout: 60_000 });
}

/** Open the workspace with the saved session and wait for the app to boot. */
async function openApp(page) {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  if (page.url().includes('/login')) throw new Error('Session expired or login failed — rerun the setup project');
  await page.waitForFunction(() => window.ChatController && document.body.classList.contains('v3-ready'), null, { timeout: 60_000 });
}

/** Read the anti-CSRF token the authenticated page exposes for mutating calls. */
async function csrfToken(page) {
  return page.evaluate(() => document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '');
}

// ── Connection and conversation ──────────────────────────────────────────────

/** Make `displayName` (or source_key) the active connection; returns its source_key. */
async function selectConnection(page, displayName = CONNECTION) {
  const response = await page.request.get('/api/connections', { timeout: 60_000 });
  if (!response.ok()) throw new Error(`/api/connections → ${response.status()}`);
  const { connections = [] } = await response.json();
  const match = connections.find((c) => c.display_name === displayName || c.source_key === displayName);
  if (!match) {
    throw new Error(`Connection "${displayName}" not found; available: ${connections.map((c) => c.display_name).join(', ')}`);
  }
  const active = await page.evaluate(() => window.getActiveConnection());
  if (active !== match.source_key) {
    await page.evaluate((key) => window.onConnectionChange(key), match.source_key);
  }
  await expect(page.locator('#connection-pill-name')).toHaveText(match.display_name, { timeout: 30_000 });
  await settle(page, 60_000);
  return match.source_key;
}

/**
 * Wait (bounded) for the controller to go idle: no restore in flight, nothing
 * sending. Returns false instead of failing — a restore that never finishes is
 * the app's problem to show, and newConversation() resets it regardless.
 */
async function settle(page, timeout = 30_000) {
  const idle = await page.waitForFunction(() => {
    const c = window.ChatController;
    return c && !c.hydrating && !c.sending && !document.querySelector('.v3-thread-restoring');
  }, null, { timeout }).then(() => true).catch(() => false);
  if (!idle) console.warn(`[live] controller still restoring/sending after ${timeout / 1000}s — resetting via New conversation`);
  await page.waitForTimeout(300);
  return idle;
}

async function newConversation(page) {
  await settle(page);
  await page.evaluate(() => window.ChatController.newConversation());
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(0);
  await page.waitForFunction(() => window.ChatController && !window.ChatController.hydrating && !window.ChatController.sending);
}

// ── Asking ───────────────────────────────────────────────────────────────────

/**
 * Ask through the real composer path and wait for the answer to render.
 * Resolves to what the user sees:
 *   { question, failed, path, status, answer, errorText, card, skill }
 * `card` is 'confirm' | 'clarify' | 'guard' | null (the ML stop cards).
 */
async function ask(page, question, options = {}, timeout = WAIT.sql) {
  const before = await page.locator('#v3-thread article.v3-turn').count();
  // Fire and forget: send() resolves only when the stream ends, and evaluate()
  // would await it — the wait below must own the timeout instead.
  await page.evaluate(([q, o]) => { void window.ChatController.send(q, o); }, [question, options]);
  const handle = await page.waitForFunction((n) => {
    const turns = document.querySelectorAll('#v3-thread article.v3-turn');
    if (turns.length <= n) return null;
    const last = turns[turns.length - 1];
    if (last.classList.contains('is-running')) return null;
    const error = last.querySelector('.v3-error-block');
    if (error) return { failed: true, errorText: error.textContent.trim() };
    if (!last.hasAttribute('data-route-path')) return null;
    return { failed: false, path: last.getAttribute('data-route-path') };
  }, before, { timeout });
  const outcome = await handle.jsonValue();
  // The turn renders when the `result` event lands, but send() only clears
  // `sending` once the SSE stream closes (late enrichment events). A follow-up
  // action fired in between — "Answer with SQL instead", another question — is
  // silently ignored by send(), so wait for the controller to be idle.
  await page.waitForFunction(() => window.ChatController && !window.ChatController.sending, null, { timeout: 60_000 }).catch(() => {});
  return readTurn(page, question, outcome);
}

async function readTurn(page, question, outcome) {
  const state = await page.evaluate(() => {
    const turns = document.querySelectorAll('#v3-thread article.v3-turn');
    const last = turns[turns.length - 1];
    const card = document.querySelector('#v3-placeholder .v3-ml-card');
    const kind = card ? (card.classList.contains('is-confirm') ? 'confirm' : card.classList.contains('is-clarify') ? 'clarify' : card.classList.contains('is-guard') ? 'guard' : null) : null;
    return {
      status: (document.querySelector('#v3-meta-row .v3-status')?.textContent || '').trim(),
      answer: (last?.querySelector('.v3-summary')?.textContent || '').trim(),
      card: kind,
      skill: card?.getAttribute('data-skill') || (document.querySelector('#v3-meta-row .v3-skill-chip')?.textContent || '').trim() || null,
      pill: (last?.querySelector('.v3-route-pill')?.textContent || '').trim(),
      // "SQL · <router reason>" — names why the answer took the path it took.
      pillTitle: last?.querySelector('.v3-route-pill')?.getAttribute('title') || '',
    };
  });
  return { question, ...outcome, ...state };
}

/** Fail fast, and say so, when the failure is the stack rather than the product. */
function assertNoBackendError(turn) {
  if (turn.failed && INFRA_ERROR.test(turn.errorText || '')) {
    throw new Error(`Infrastructure failure (not a product regression) — ${turn.errorText}`);
  }
}

/** A finished, successful turn on `path` ('sql' | 'ml' | 'greeting'). */
function expectPath(turn, path) {
  assertNoBackendError(turn);
  expect(turn.failed, `turn failed: ${turn.errorText}`).toBe(false);
  let hint = turn.pillTitle ? ` (${turn.pillTitle})` : '';
  if (path === 'ml' && turn.path === 'sql') {
    hint += ' — the planner fell back to SQL; the route pill title carries its reason (e.g. no date/numeric columns in the catalog it was given).';
  }
  expect(turn.path, `expected the ${path} path for "${turn.question}", got ${turn.path}${hint}`).toBe(path);
}

// ── Reading the workspace ────────────────────────────────────────────────────

/** Open a dock tab (idempotent) and return its text. */
async function openDock(page, tab) {
  const button = page.locator(`[data-dock="${tab}"]`);
  await expect(button).toBeVisible();
  if (!(await button.evaluate((el) => el.classList.contains('is-active')))) await button.click();
  const body = page.locator('#v3-dock-body');
  await expect(body).toBeVisible();
  return (await body.innerText()).trim();
}

/** The generated SQL shown on the "SQL & run details" tab. */
async function sqlText(page) {
  await openDock(page, 'sql');
  return (await page.locator('#v3-dock-body pre').innerText()).trim();
}

/** SQL answers open with the chart collapsed; expand it and wait for the paint. */
async function expandChart(page, timeout = 45_000) {
  await expect(page.locator('#v3-chart-block')).toBeVisible();
  const toggle = page.locator('#v3-chart-toggle');
  if ((await toggle.textContent())?.trim() === 'Expand') {
    await toggle.click();
    await page.evaluate(() => window.dispatchEvent(new Event('resize')));
  }
  await waitForChart(page, timeout);
}

/** Data rows the user can see (0 when the table block is hidden — its DOM keeps the last grid). */
async function gridRows(page) {
  const block = page.locator('#v3-table-block');
  if (!(await block.count()) || !(await block.isVisible())) return 0;
  return page.locator('#v3-grid .v3-grid-row[data-row]').count();
}

async function gridHeaders(page) {
  const text = await page.locator('#v3-grid .v3-grid-row.v3-grid-head').innerText().catch(() => '');
  return text.split(/\s{2,}|\n/).map((s) => s.trim()).filter(Boolean);
}

async function chartVisible(page) {
  const canvas = page.locator('#v3-chart-frame canvas').first();
  if (!(await canvas.count())) return false;
  const box = await canvas.boundingBox();
  return Boolean(box && box.height > 40);
}

/** Wait for the chart to paint (ECharts renders asynchronously; a busy machine takes a while). */
async function waitForChart(page, timeout = 45_000) {
  await page.waitForFunction(() => {
    const c = document.querySelector('#v3-chart-frame canvas');
    return c && c.getBoundingClientRect().height > 40;
  }, null, { timeout });
}

const metaRow = (page) => page.locator('#v3-meta-row');
const confirmCard = (page) => page.locator('#v3-placeholder .v3-ml-card.is-confirm');
const guardCard = (page) => page.locator('#v3-placeholder .v3-ml-card.is-guard');
const anyCard = (page) => page.locator('#v3-placeholder .v3-ml-card');

/** Click Run on the confirm card and wait for the completed child turn. */
async function runCard(page, timeout = WAIT.run) {
  const before = await page.locator('#v3-thread article.v3-turn').count();
  await confirmCard(page).locator('[data-run]').click();
  await waitForCompleted(page, before, timeout);
}

async function waitForCompleted(page, turnsBefore, timeout = WAIT.run) {
  await page.waitForFunction((n) => {
    const turns = document.querySelectorAll('#v3-thread article.v3-turn');
    const last = turns[turns.length - 1];
    if (turns.length <= n || !last || last.classList.contains('is-running')) return false;
    if (last.querySelector('.v3-error-block')) return true;
    const status = document.querySelector('#v3-meta-row .v3-status')?.textContent || '';
    return /Completed|Blocked/.test(status) || Boolean(document.querySelector('#v3-placeholder .v3-ml-card'));
  }, turnsBefore, { timeout });
  const failed = await page.locator('#v3-thread article.v3-turn').last().locator('.v3-error-block').count();
  if (failed) {
    const text = await page.locator('#v3-thread article.v3-turn').last().locator('.v3-error-block').innerText();
    if (INFRA_ERROR.test(text)) throw new Error(`Infrastructure failure (not a product regression) — ${text}`);
    throw new Error(`run failed: ${text}`);
  }
}

// ── Backend helpers a test may call directly (same session cookie) ───────────

// The BFF can be slow to answer a side call while it streams a long answer;
// give these a full minute rather than the 20 s action timeout.
const API_TIMEOUT = 60_000;

/** The deterministic routing preview for a question (no LLM call). */
async function routingPreview(page, question, analysis) {
  const params = new URLSearchParams({ q: question });
  if (analysis != null) params.set('analysis', String(analysis));
  const response = await page.request.get(`/api/analysis/routing?${params}`, { timeout: API_TIMEOUT });
  if (!response.ok()) throw new Error(`/api/analysis/routing → ${response.status()}`);
  return response.json();
}

/** Forget "don't ask again" so the confirm card shows for `skill`. */
async function forgetSkill(page, connection, skill) {
  const response = await page.request.post('/api/analysis/skills/prefs', {
    headers: { 'X-CSRFToken': await csrfToken(page), 'Content-Type': 'application/json' },
    data: { connection, skill, remember: false },
    timeout: API_TIMEOUT,
  });
  if (!response.ok()) throw new Error(`/api/analysis/skills/prefs(${skill}) → ${response.status()} ${await response.text()}`);
}

/** Every skill the stack has registered, with its remembered flag. */
async function listSkills(page, connection) {
  const response = await page.request.get(`/api/analysis/skills?connection=${encodeURIComponent(connection)}`, { timeout: API_TIMEOUT });
  if (!response.ok()) throw new Error(`/api/analysis/skills → ${response.status()}`);
  return (await response.json()).skills || [];
}

/**
 * The global catalog source ('db' | 'mcp'). Both must serve the same typed
 * catalog; every test records which one it ran under so a regression in one
 * adapter (the MCP one used to drop column types) is attributable.
 */
async function catalogSource(page) {
  const response = await page.request.get('/api/mcp/status', { timeout: API_TIMEOUT }).catch(() => null);
  if (!response || !response.ok()) return 'unknown';
  const body = await response.json().catch(() => ({}));
  return String(body.catalog_source || 'unknown');
}

/** Annotate the test with the stack's catalog source. */
async function noteCatalogSource(page, testInfo) {
  const source = await catalogSource(page);
  testInfo.annotations.push({ type: 'catalog_source', description: source });
  return source;
}

/**
 * Skip an ML test (from inside its body) when the deterministic router would
 * not send the question to ML — so a routing regression reads as its own
 * failure, and a stack with ML disabled skips instead of timing out.
 */
async function requireMlRoute(page, question) {
  const preview = await routingPreview(page, question);
  test.skip(!preview.ml_skills_enabled, 'ML_SKILLS_ENABLED is off on this stack');
  expect(preview.would_route, `routing preview for "${question}": ${preview.reason}`).toBe('needs_analysis');
  return preview;
}

// ── Artifacts ────────────────────────────────────────────────────────────────

let shotIndex = 0;
async function shot(page, name) {
  fs.mkdirSync(SHOTS, { recursive: true });
  shotIndex += 1;
  await page.screenshot({ path: path.join(SHOTS, `${String(shotIndex).padStart(2, '0')}-${name}.png`) });
}

module.exports = {
  BASE, EMAIL, PASSWORD, CONNECTION, WAIT, INFRA_ERROR,
  skipUnlessLive, login, openApp, csrfToken,
  selectConnection, settle, newConversation,
  ask, readTurn, assertNoBackendError, expectPath,
  openDock, sqlText, gridRows, gridHeaders, chartVisible, waitForChart, expandChart,
  metaRow, confirmCard, guardCard, anyCard, runCard, waitForCompleted,
  routingPreview, forgetSkill, listSkills, requireMlRoute, catalogSource, noteCatalogSource,
  unreachable, shot,
};
