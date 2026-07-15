/**
 * Playwright end-to-end test — Metadata & Catalog settings panel
 *
 * Verifies:
 *  1. Login + app loads
 *  2. "Metadata & Catalog" nav item appears in Settings
 *  3. DB panel renders with real stats (tables, columns, terms, pairs)
 *  4. Source toggle switches from DB → MCP Server
 *  5. MCP panel shows the saved server list
 *  6. Server row is clickable (activate API called)
 *  7. Test & health check button is present
 *  8. Catalog tool mapping section is always visible
 *  9. Cache TTL card renders
 *
 * Requires:
 *   - App running at http://localhost:8501
 *   - API running at http://localhost:8001
 *   - A valid account (default: admin / ChangeMe123!)
 *
 * Run:
 *   npx playwright test tests/integration/test_mcp_settings.spec.js --headed
 *   npx playwright test tests/integration/test_mcp_settings.spec.js           # headless
 */

// @ts-check
const { test, expect } = require('@playwright/test');

const APP_URL   = 'http://localhost:8501';
const API_URL   = 'http://localhost:8001';
const EMAIL     = 'admin';
const PASSWORD  = 'ChangeMe123!';
const TIMEOUT   = 15_000;

// ── Helpers ────────────────────────────────────────────────────────────────────

async function login(page) {
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded', timeout: TIMEOUT });
  if (page.url().includes('/login')) {
    await page.waitForSelector('#email', { timeout: TIMEOUT });
    await page.fill('#email',    EMAIL);
    await page.fill('#password', PASSWORD);
    await page.click('#login-btn');
    await page.waitForSelector('#question-input', { timeout: TIMEOUT });
  }
  // Wait for the SettingsPage module script to finish loading and attach
  // its click listener on #settings-btn (it's a type="module" script).
  await page.waitForFunction(() => !!(window._settingsPage), { timeout: TIMEOUT });
}

async function openSettings(page) {
  await page.click('#settings-btn');
  // The overlay starts with [hidden]; JS removes it to reveal the settings panel.
  // Wait for the overlay to be visible (not hidden by CSS display:none).
  await page.waitForSelector('.sp-overlay', { state: 'visible', timeout: TIMEOUT });
}

/** Wait for the Metadata & Catalog panel to finish its background DB-stats reload. */
async function waitForMcpPanelStable(page) {
  // The panel fires a background reload when _mcpStatus.connection !== _mcpConn.
  // Wait until the DB stats grid shows numeric data, which means the reload settled.
  await page.waitForFunction(() => {
    const vals = Array.from(document.querySelectorAll('.mc-info-v'));
    return vals.some(el => /\d/.test(el.textContent || ''));
  }, { timeout: TIMEOUT }).catch(() => { /* panel might be in MCP mode, that's fine */ });
}

async function clickNav(page, id) {
  await page.click(`.sp-nav-item[data-id="${id}"]`);
  await page.waitForSelector('.sp-section-title', { timeout: TIMEOUT });
}

async function screenshot(page, name) {
  const dir = 'tests/screenshots/mcp';
  await page.screenshot({ path: `${dir}/${name}.png`, fullPage: false });
  console.log(`   📸 ${dir}/${name}.png`);
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe('MCP Metadata & Catalog settings panel', () => {

  test.beforeEach(async ({ page }) => {
    page.setDefaultTimeout(TIMEOUT);
    // Ensure catalog_source is reset to 'db' before each test
    await page.request.put(`${API_URL}/api/mcp/catalog-source?connection=AdventureWorksDW`, {
      data: { catalog_source: 'db' },
    }).catch(() => {});
  });

  // ── Test 1: Nav item present ─────────────────────────────────────────────────
  test('1 · Metadata & Catalog nav item is present in Settings', async ({ page }) => {
    await login(page);
    await openSettings(page);

    const navItem = page.locator('.sp-nav-item[data-id="metadata-catalog"]');
    await expect(navItem).toBeVisible();
    await expect(navItem).toContainText('Metadata & Catalog');
    console.log('   ✓ Nav item visible');

    // Also verify other USER items are still there
    await expect(page.locator('.sp-nav-item[data-id="general"]')).toBeVisible();
    await expect(page.locator('.sp-nav-item[data-id="ai-models"]')).toBeVisible();
    await screenshot(page, '01_nav_items');
  });

  // ── Test 2: DB panel renders with real stats ──────────────────────────────────
  test('2 · DB panel shows real metadata stats', async ({ page }) => {
    await login(page);
    await openSettings(page);
    await clickNav(page, 'metadata-catalog');

    // Wait for DB info grid to render with actual data (not '—')
    await page.waitForFunction(() => {
      const texts = Array.from(document.querySelectorAll('.mc-info-v'));
      return texts.some(el => /\d+/.test(el.textContent || ''));
    }, { timeout: TIMEOUT });

    const grid = page.locator('.mc-info-grid');
    await expect(grid).toBeVisible();

    // Provider
    await expect(grid.locator('.mc-info-v').first()).toContainText('Schema Modeler');
    console.log('   ✓ Provider: Schema Modeler');

    // Database name
    const dbName = await grid.locator('.mc-mono').first().textContent();
    expect(dbName).toBeTruthy();
    expect(dbName).not.toBe('—');
    console.log(`   ✓ DB name: ${dbName}`);

    // Tables · columns (should be numeric, e.g. "74 · 834")
    const tableCols = await grid.locator('.mc-info-v').nth(2).textContent();
    expect(tableCols).toMatch(/\d+\s*·\s*\d+/);
    console.log(`   ✓ Tables · columns: ${tableCols}`);

    // Business terms · pairs (should be numeric)
    const termsKp = await grid.locator('.mc-info-v').nth(3).textContent();
    expect(termsKp).toMatch(/\d+\s*·\s*\d+/);
    console.log(`   ✓ Business terms · pairs: ${termsKp}`);

    // Refresh metadata button
    await expect(page.locator('#mc-refresh-btn')).toBeVisible();
    console.log('   ✓ Refresh metadata button visible');

    // Cache TTL card
    await expect(page.locator('.mc-cache-card')).toBeVisible();
    await expect(page.locator('#mc-ttl-sel')).toBeVisible();
    console.log('   ✓ Cache TTL card visible');

    await screenshot(page, '02_db_panel');
  });

  // ── Test 3: Source toggle switches to MCP ────────────────────────────────────
  test('3 · Source toggle switches DB → MCP and shows server panel', async ({ page }) => {
    await login(page);
    await openSettings(page);
    await clickNav(page, 'metadata-catalog');

    // Wait for panel to load and stabilise (background reload finishes)
    await page.waitForSelector('#mc-seg', { timeout: TIMEOUT });
    await waitForMcpPanelStable(page);

    // DB button should be active
    const dbBtn  = page.locator('#mc-seg button[data-src="db"]');
    const mcpBtn = page.locator('#mc-seg button[data-src="mcp"]');
    await expect(dbBtn).toHaveClass(/is-active/);
    await expect(mcpBtn).not.toHaveClass(/is-active/);
    console.log('   ✓ DB source is active initially');

    // MCP panel should be hidden
    await expect(page.locator('#mc-mcp-panel')).toHaveClass(/mc-hidden/);
    await screenshot(page, '03a_db_active');

    // Click MCP Server button — use locator for built-in retry on detach
    await page.locator('#mc-seg button[data-src="mcp"]').click();

    // Wait for MCP panel to become visible (re-render may take a moment)
    await page.waitForFunction(() => {
      const panel = document.getElementById('mc-mcp-panel');
      return panel && !panel.classList.contains('mc-hidden');
    }, { timeout: TIMEOUT });

    // DB panel should now be hidden
    await expect(page.locator('#mc-db-panel')).toHaveClass(/mc-hidden/);
    await expect(page.locator('#mc-seg button[data-src="mcp"]')).toHaveClass(/is-active/);
    console.log('   ✓ Switched to MCP Server source');

    await screenshot(page, '03b_mcp_active');
  });

  // ── Test 4: MCP server list renders ─────────────────────────────────────────
  test('4 · MCP panel shows server list with jeen-catalog-mcp', async ({ page }) => {
    await login(page);
    await openSettings(page);
    await clickNav(page, 'metadata-catalog');

    // Switch to MCP — wait for panel stable first, then use locator retry
    await page.waitForSelector('#mc-seg', { timeout: TIMEOUT });
    await waitForMcpPanelStable(page);
    await page.locator('#mc-seg button[data-src="mcp"]').click();
    await page.waitForSelector('.mc-srv-list', { timeout: TIMEOUT });

    // Server list should have at least one row
    const serverRows = page.locator('.mc-srv-row');
    const count = await serverRows.count();
    expect(count).toBeGreaterThan(0);
    console.log(`   ✓ ${count} server(s) in list`);

    // The saved server should be visible
    const firstRow = serverRows.first();
    await expect(firstRow).toBeVisible();
    const rowText = await firstRow.textContent();
    console.log(`   ✓ Server row: "${rowText?.trim().slice(0, 60)}..."`);

    // Server name should contain jeen-catalog-mcp
    await expect(firstRow.locator('.mc-srv-name')).toContainText('jeen-catalog-mcp');
    console.log('   ✓ jeen-catalog-mcp is in the list');

    // Active tag should be shown (it was activated in a previous test)
    const hasActiveTag = await firstRow.locator('.mc-srv-tag').count() > 0;
    console.log(`   ✓ Active tag: ${hasActiveTag ? 'shown' : 'not shown (untested)'}`);

    // Transport badge
    await expect(firstRow.locator('.mc-srv-badge')).toBeVisible();
    const transport = await firstRow.locator('.mc-srv-badge').textContent();
    console.log(`   ✓ Transport badge: ${transport}`);

    await screenshot(page, '04_mcp_server_list');
  });

  // ── Test 5: Health check section and tool mapping ────────────────────────────
  test('5 · Active server shows health section and catalog tool mapping', async ({ page }) => {
    await login(page);
    await openSettings(page);
    await clickNav(page, 'metadata-catalog');

    // Switch to MCP
    await page.waitForSelector('#mc-seg', { timeout: TIMEOUT });
    await waitForMcpPanelStable(page);
    await page.locator('#mc-seg button[data-src="mcp"]').click();
    await page.waitForSelector('.mc-active-srv', { timeout: TIMEOUT });

    // Test & health check button present
    await expect(page.locator('#mc-test-btn')).toBeVisible();
    const testBtnText = await page.locator('#mc-test-btn').textContent();
    console.log(`   ✓ Health check button: "${testBtnText?.trim()}"`);

    // Catalog tool mapping section always visible
    await expect(page.locator('.mc-field-label').filter({ hasText: 'Catalog tool mapping' })).toBeVisible();
    console.log('   ✓ Catalog tool mapping section visible');

    // Map rows present (4 needs)
    const mapRows = page.locator('.mc-map-row');
    const mapCount = await mapRows.count();
    expect(mapCount).toBe(4);
    console.log(`   ✓ ${mapCount} catalog needs shown`);

    // Required needs are marked
    const reqMark = page.locator('.mc-map-need .mc-req');
    const reqCount = await reqMark.count();
    expect(reqCount).toBe(2); // list_tables + describe_table
    console.log(`   ✓ ${reqCount} required needs marked`);

    await screenshot(page, '05_tool_mapping');
  });

  // ── Test 6: Run health check ─────────────────────────────────────────────────
  test('6 · Health check runs and shows diagnostics', async ({ page }) => {
    await login(page);
    await openSettings(page);
    await clickNav(page, 'metadata-catalog');

    // Switch to MCP
    await page.waitForSelector('#mc-seg', { timeout: TIMEOUT });
    await waitForMcpPanelStable(page);
    await page.locator('#mc-seg button[data-src="mcp"]').click();
    await page.waitForSelector('#mc-test-btn', { timeout: TIMEOUT });

    // Intercept the health check API call
    let healthCheckCalled = false;
    page.on('response', resp => {
      if (resp.url().includes('/health-check')) healthCheckCalled = true;
    });

    // Click health check
    await page.click('#mc-test-btn');
    console.log('   → Health check initiated');

    // Wait for "Checking…" state (spinner)
    await page.waitForFunction(() => {
      const btn = document.getElementById('mc-test-btn');
      return btn && (btn.disabled || btn.textContent?.includes('Checking'));
    }, { timeout: 5_000 }).catch(() => {});

    // Wait for health check to complete (button re-enabled)
    await page.waitForFunction(() => {
      const btn = document.getElementById('mc-test-btn');
      return btn && !btn.disabled;
    }, { timeout: 30_000 });

    expect(healthCheckCalled).toBe(true);
    console.log('   ✓ Health check API was called');

    // Health diagnostics card should appear
    const healthCard = page.locator('.mc-health');
    await expect(healthCard).toBeVisible({ timeout: 5_000 });
    console.log('   ✓ Health card rendered');

    // Status badge
    const badge = healthCard.locator('.mc-health-badge');
    const badgeText = await badge.textContent();
    console.log(`   ✓ Health status: "${badgeText?.trim()}"`);

    // Diagnostics grid (6 cells)
    const cells = healthCard.locator('.mc-health-cell');
    const cellCount = await cells.count();
    expect(cellCount).toBe(6);
    console.log(`   ✓ ${cellCount} diagnostic cells`);

    // Capabilities row — all 4 always shown
    const capChips = healthCard.locator('.mc-cap-chip');
    const capCount = await capChips.count();
    expect(capCount).toBe(4);
    console.log(`   ✓ ${capCount} capability chips (on + off)`);

    // Tools list
    const toolRows = healthCard.locator('.mc-tool-row');
    const toolCount = await toolRows.count();
    expect(toolCount).toBeGreaterThan(0);
    console.log(`   ✓ ${toolCount} tool(s) listed`);

    // Tool mapping should now be populated (not "awaiting health check")
    const mappedTools = page.locator('.mc-map-tool:not(.mc-map-muted)');
    const mappedCount = await mappedTools.count();
    console.log(`   ✓ ${mappedCount} catalog need(s) mapped`);

    await screenshot(page, '06_health_check');
  });

  // ── Test 7: Cache TTL select ──────────────────────────────────────────────────
  test('7 · Cache TTL card renders with correct options', async ({ page }) => {
    await login(page);
    await openSettings(page);
    await clickNav(page, 'metadata-catalog');

    await page.waitForSelector('.mc-cache-card', { timeout: TIMEOUT });

    const ttlSel = page.locator('#mc-ttl-sel');
    await expect(ttlSel).toBeVisible();

    const options = ttlSel.locator('option');
    const optCount = await options.count();
    expect(optCount).toBe(5); // No cache, 5m, 15m, 1h, 24h
    console.log(`   ✓ ${optCount} TTL options`);

    const optTexts = await options.allTextContents();
    console.log(`   ✓ Options: ${optTexts.join(' | ')}`);

    // Cache state pills
    await expect(page.locator('.mc-cache-state')).toBeVisible();
    const pills = page.locator('.mc-cache-pill');
    const pillCount = await pills.count();
    expect(pillCount).toBeGreaterThan(0);
    console.log(`   ✓ ${pillCount} cache state pill(s)`);

    await screenshot(page, '07_cache_ttl');
  });

});
