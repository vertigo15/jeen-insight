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
  const startedAt = Date.now();
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
  const wallMs = Date.now() - startedAt;
  // The turn renders when the `result` event lands, but send() only clears
  // `sending` once the SSE stream closes (late enrichment events). A follow-up
  // action fired in between — "Answer with SQL instead", another question — is
  // silently ignored by send(), so wait for the controller to be idle.
  await page.waitForFunction(() => window.ChatController && !window.ChatController.sending, null, { timeout: 60_000 }).catch(() => {});
  return { ...(await readTurn(page, question, outcome)), wallMs };
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

// ── Raw payloads (assert on data, scrape the DOM only for "does it render") ──

/**
 * The QueryResponse the UI holds for the selected result turn
 * (WorkspaceController stores the streamed `result` event on the turn).
 * Falls back to the newest turn that has a result.
 */
async function rawResult(page) {
  return page.evaluate(() => {
    const c = window.WorkspaceController;
    if (!c) return null;
    const selected = c.turns.find((t) => t.id === c.selectedResultId);
    if (selected && selected.result) return selected.result;
    const withResult = [...c.turns].reverse().find((t) => t.result);
    return withResult ? withResult.result : null;
  });
}

/** The newest turn's result — including ML stop cards (`proposal`) that the selected-result pointer skips. */
async function lastTurnResult(page) {
  return page.evaluate(() => {
    const c = window.WorkspaceController;
    const last = c && c.turns[c.turns.length - 1];
    return last ? { id: last.id, status: last.status, error: last.error, result: last.result, durationMs: last.durationMs } : null;
  });
}

/** `{chart_spec, chart_config}` of the chart currently on screen (null before a chart exists). */
async function chartState(page) {
  return page.evaluate(() => (window.JeenLegacyBridge && window.JeenLegacyBridge.getChartState ? window.JeenLegacyBridge.getChartState() : null));
}

/** Pick a chart type from the type menu and wait for the repaint. Returns the state after. */
async function selectChartType(page, type, timeout = 30_000) {
  const button = page.locator('#chart-type-selector-container .ctype-btn');
  await expect(button).toBeVisible();
  await button.click();
  // The menu is portaled to <body>.
  const item = page.locator(`.ctype-menu .ctype-item[data-value="${type}"]`);
  await expect(item).toBeVisible();
  await item.click();
  await page.waitForFunction((t) => document.querySelector('#chart-display-container')?.dataset.chartType === t, type, { timeout });
  await waitForChart(page, timeout);
  return chartState(page);
}

/** The rendered chart type (`#chart-display-container[data-chart-type]`). */
async function renderedChartType(page) {
  return page.evaluate(() => document.querySelector('#chart-display-container')?.dataset.chartType || '');
}

/** Public connection record (`/api/connections/{key}`), incl. `database_type`, `is_power_bi`, `metadata_summary`. */
async function connectionInfo(page, key) {
  const response = await page.request.get(`/api/connections/${encodeURIComponent(key)}`, { timeout: API_TIMEOUT });
  if (!response.ok()) throw new Error(`/api/connections/${key} → ${response.status()}`);
  return response.json();
}

/** The session user (`/api/auth/me`): id, name, email, role, flags. */
async function me(page) {
  const response = await page.request.get('/api/auth/me', { timeout: API_TIMEOUT });
  if (!response.ok()) throw new Error(`/api/auth/me → ${response.status()}`);
  return response.json();
}

// ── Answer grounding ─────────────────────────────────────────────────────────

/** Numbers (2+ significant digits) mentioned in a sentence: "1,234.5" → 1234.5, "12%" → 12, "$3.2M" → 3200000. */
function numbersIn(text) {
  const out = [];
  const re = /(?<![\w.])-?\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(%|[kKmMbB](?![a-zA-Z]))?/g;
  let m;
  while ((m = re.exec(String(text || '')))) {
    let n = Number(`${m[1].replace(/,/g, '')}${m[2] || ''}`);
    const unit = (m[3] || '').toLowerCase();
    if (unit === 'k') n *= 1e3;
    if (unit === 'm') n *= 1e6;
    if (unit === 'b') n *= 1e9;
    if (Number.isFinite(n) && String(Math.abs(Math.trunc(n))).length + (m[2] ? m[2].length - 1 : 0) >= 2) out.push(n);
  }
  return out;
}

/**
 * Every number the answer sentence states must be derivable from the rows
 * (a cell value, a rounded cell value, a column total or a row count).
 * Returns { grounded, unmatched, checked }.
 */
function answerGrounded(answer, rows, columns) {
  const stated = numbersIn(answer).filter((n) => !(Number.isInteger(n) && n >= 1900 && n <= 2100)); // years are labels, not facts
  if (!stated.length) return { grounded: true, unmatched: [], checked: 0 };
  const pool = new Set();
  const add = (v) => { if (Number.isFinite(v)) pool.add(v); };
  const list = Array.isArray(rows) ? rows : [];
  const cols = columns && columns.length ? columns : (list[0] ? Object.keys(list[0]) : []);
  add(list.length);
  for (const col of cols) {
    let total = 0; let numeric = 0;
    for (const row of list) {
      const raw = Array.isArray(row) ? row[cols.indexOf(col)] : row[col];
      const v = typeof raw === 'number' ? raw : Number(String(raw ?? '').replace(/[$,]/g, ''));
      if (Number.isFinite(v) && raw !== null && raw !== '') { add(v); total += v; numeric += 1; } else if (typeof raw === 'string') {
        // Digits inside labels ("Road-150 Red, 48") are facts from the data too.
        for (const n of numbersIn(raw)) add(n);
      }
    }
    if (numeric) { add(total); add(total / numeric); }
  }
  const near = (a, b) => {
    if (a === b) return true;
    const scale = Math.max(1, Math.abs(b));
    // Rounded to any sensible precision (2 decimals … thousands / millions).
    return Math.abs(a - b) <= Math.max(0.5 * 10 ** Math.max(0, Math.floor(Math.log10(Math.abs(a) || 1)) - 2), scale * 0.005);
  };
  const unmatched = stated.filter((n) => ![...pool].some((v) => near(n, v)));
  return { grounded: unmatched.length === 0, unmatched, checked: stated.length };
}

// ── Feature-test helpers ─────────────────────────────────────────────────────

/** Trigger a download and return its bytes + suggested filename. */
async function download(page, trigger, timeout = 30_000) {
  const [dl] = await Promise.all([page.waitForEvent('download', { timeout }), trigger()]);
  const file = await dl.path();
  return { name: dl.suggestedFilename(), buffer: fs.readFileSync(file) };
}

/** Clipboard text (the context must have been created with clipboard permissions). */
async function clipboardText(page) {
  return page.evaluate(() => navigator.clipboard.readText());
}

/** Open Settings on a section (`general`, `ai-models`, `prompts`, `metadata-catalog`, `users`, ...). */
async function openSettings(page, sectionId) {
  const overlay = page.locator('.sp-overlay');
  if (!(await overlay.count()) || (await overlay.evaluate((el) => el.hidden))) {
    await page.locator('#v3-settings-button').click();
  }
  await expect(page.locator('.sp-overlay')).toBeVisible();
  const nav = page.locator(`.sp-nav-item[data-id="${sectionId}"]`);
  await expect(nav, `settings section "${sectionId}" is available to this user`).toBeVisible();
  await nav.click();
  await expect(nav).toHaveClass(/is-active/);
  return page.locator('.sp-content');
}

async function closeSettings(page) {
  const close = page.locator('.sp-nav-item.sp-close-btn');
  if (await close.count()) await close.click();
  await expect(page.locator('.sp-overlay')).toBeHidden();
}

/**
 * Run `action` and capture the first request matching `urlPattern`, aborting
 * it so no LLM call is spent. Returns the JSON body (or null on timeout).
 */
async function captureRequest(page, urlPattern, action, timeout = 15_000) {
  let captured = null;
  const handler = async (route) => {
    if (captured === null) {
      captured = route.request().postDataJSON?.() ?? route.request().postData() ?? {};
    }
    await route.abort('aborted');
  };
  await page.route(urlPattern, handler);
  try {
    await action();
    const deadline = Date.now() + timeout;
    while (captured === null && Date.now() < deadline) await page.waitForTimeout(100);
  } finally {
    await page.unroute(urlPattern, handler);
  }
  return captured;
}

/** Snapshot → mutate → restore, even when the test fails in between. */
async function withRestore(getter, setter, body) {
  const original = await getter();
  try {
    return await body(original);
  } finally {
    await setter(original);
  }
}

/** Latency fields from a QueryResponse for the scorecard (`raw.metrics`). */
function latencyOf(raw, wallMs) {
  const m = (raw && raw.metrics) || {};
  return {
    wall_ms: wallMs ?? null,
    llm_latency_ms: m.llm_latency_ms ?? null,
    execution_time_ms: m.execution_time_ms ?? null,
    retry_count: m.retry_count ?? null,
    llm_call_count: m.llm_call_count ?? null,
    total_tokens: m.total_tokens ?? null,
    route: m.route ?? (raw && raw.routing && raw.routing.route) ?? null,
  };
}

/** Attach a typed annotation the scorecard reporter reads (`type`, JSON description). */
function annotate(testInfo, type, value) {
  testInfo.annotations.push({ type, description: typeof value === 'string' ? value : JSON.stringify(value) });
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
  rawResult, lastTurnResult, chartState, selectChartType, renderedChartType, connectionInfo, me,
  numbersIn, answerGrounded,
  download, clipboardText, openSettings, closeSettings, captureRequest, withRestore, latencyOf, annotate,
  unreachable, shot,
};
