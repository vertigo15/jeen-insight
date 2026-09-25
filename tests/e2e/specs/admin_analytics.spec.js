// @ts-check
/**
 * Settings › Analytics (admin only).
 *
 * Deterministic: the fake backend answers /api/admin/analytics/* from
 * fixtures.ANALYTICS. Covers the admin gate, the KPI / chart / table / feed
 * rendering, range and filter refetches, the CSV export and the
 * "migration not applied" state — in English and in Hebrew (RTL).
 */
const { test, expect } = require('@playwright/test');
const { openHarness } = require('./_helpers');

async function openSettingsAs(page, role, { locale = 'en', unavailable = false } = {}) {
  await openHarness(page, locale === 'he' ? '?locale=he' : '');
  await page.evaluate(async ({ role, unavailable }) => {
    window.__calls = [];
    window.__ANALYTICS_UNAVAILABLE__ = unavailable;
    window._currentUser = { id: 1, name: 'Tester', email: 'tester@jeen.ai', role };
    const { SettingsPage } = await import('/src/static/settings/settingsPage.js');
    const settings = new SettingsPage();
    settings.mount();
    settings.open();
    window.__settings = settings;
  }, { role, unavailable });
}

async function openAnalyticsTab(page, role = 'admin', opts = {}) {
  await openSettingsAs(page, role, opts);
  await page.locator('.sp-nav-item[data-id="analytics"]').click();
}

const analyticsCalls = (page) => page.evaluate(() =>
  (window.__calls || []).filter((c) => c.url.startsWith('/api/admin/analytics/')).map((c) => c.url));

test.describe('Settings › Analytics (admin)', () => {
  test('the tab is hidden for non-admins and never fetches', async ({ page }) => {
    await openSettingsAs(page, 'editor');
    await expect(page.locator('.sp-nav-item[data-id="analytics"]')).toBeHidden();
    await expect(page.locator('.sp-nav-item[data-id="users"]')).toBeHidden();
    expect(await analyticsCalls(page)).toEqual([]);
  });

  test('renders KPIs, chart, tables and feed for an admin', async ({ page }) => {
    await openAnalyticsTab(page);

    await expect(page.locator('.sp-an-kpi[data-kpi="dau"] .sp-an-kpi-value')).toContainText('2');
    await expect(page.locator('.sp-an-kpi[data-kpi="questions"] .sp-an-kpi-value')).toContainText('40');
    await expect(page.locator('.sp-an-kpi[data-kpi="successRate"] .sp-an-kpi-value')).toContainText('90%');
    // Delta against the previous window (40 vs 20 → +100%), coloured "good".
    await expect(page.locator('.sp-an-kpi[data-kpi="questions"] .sp-an-kpi-delta.is-good')).toContainText('+100%');
    // Thumbs-down going up is "bad".
    await expect(page.locator('.sp-an-kpi[data-kpi="thumbsDown"] .sp-an-kpi-delta.is-bad')).toBeVisible();
    // Metric definitions travel as tooltips.
    await expect(page.locator('.sp-an-kpi[data-kpi="dau"]')).toHaveAttribute('title', /Sign-ins alone do not count/);

    // ECharts drew into the activity card.
    await expect(page.locator('#sp-an-chart canvas')).toHaveCount(1);

    const users = page.locator('#sp-an-users tbody tr');
    await expect(users).toHaveCount(2);
    await expect(users.nth(0)).toContainText('Dana Levi');
    await expect(users.nth(0)).toContainText('dana@jeen.ai');
    await expect(users.nth(0)).toContainText('94%');
    // An SSO/deleted principal without an auth_users row shows its raw id.
    await expect(users.nth(1)).toContainText('sso-abc');

    const conns = page.locator('#sp-an-connections tbody tr');
    await expect(conns).toHaveCount(2);
    await expect(conns.nth(0)).toContainText('sales_db');
    await expect(conns.nth(0)).toContainText('29%');   // thumbs-down rate 0.2857

    await expect(page.locator('#sp-an-skills tbody tr')).toHaveCount(2);
    await expect(page.locator('#sp-an-skills')).toContainText('forecast');
    await expect(page.locator('#sp-an-errors')).toContainText('timeout');
    await expect(page.locator('#sp-an-errors')).toContainText('Forecast weekly revenue for a brand-new product line');

    const feed = page.locator('.sp-an-feed-item');
    await expect(feed).toHaveCount(3);
    await expect(feed.nth(0)).toContainText('Dana Levi');
    await expect(feed.nth(0)).toContainText('the total looks wrong');
    await expect(feed.nth(0)).toContainText('Show me total sales by region last month');
    await expect(feed.nth(0).locator('.sp-an-badge-down')).toBeVisible();
    await expect(feed.nth(0).locator('.sp-an-badge-rating')).toContainText('★★');
    await expect(feed.nth(2)).toContainText('Great chart');
    // "Load more" is hidden when there is no further page.
    await expect(page.locator('#sp-an-more')).toBeHidden();

    const calls = await analyticsCalls(page);
    expect(calls.some((u) => u.startsWith('/api/admin/analytics/overview?days=30'))).toBe(true);
    expect(calls.some((u) => u.startsWith('/api/admin/analytics/feedback?days=30'))).toBe(true);
  });

  test('range picker and feedback filters refetch with the right parameters', async ({ page }) => {
    await openAnalyticsTab(page);
    await expect(page.locator('.sp-an-feed-item')).toHaveCount(3);

    await page.evaluate(() => { window.__calls = []; });
    await page.locator('.sp-an-range-btn[data-days="7"]').click();
    await expect(page.locator('.sp-an-range-btn[data-days="7"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.sp-an-kpi[data-kpi="dau"] .sp-an-kpi-value')).toContainText('2');
    await expect.poll(() => analyticsCalls(page)).toContain('/api/admin/analytics/overview?days=7');
    await expect.poll(async () => (await analyticsCalls(page)).filter((u) => u.includes('days=7')).length).toBeGreaterThanOrEqual(6);

    await page.evaluate(() => { window.__calls = []; });
    await page.locator('[data-filter="thumb"]').selectOption('thumbs_down');
    await expect(page.locator('.sp-an-feed-item')).toHaveCount(1);
    await expect(page.locator('.sp-an-feed-item')).toContainText('Dana Levi');
    const afterThumb = await analyticsCalls(page);
    expect(afterThumb).toEqual(['/api/admin/analytics/feedback?days=7&thumb=thumbs_down&limit=50']);

    // Connection options come from top-connections.
    await page.locator('[data-filter="connection"]').selectOption('hr_db');
    await expect(page.locator('#sp-an-feedback')).toContainText('No feedback matches these filters.');
    await page.locator('[data-filter="thumb"]').selectOption('');
    await expect(page.locator('.sp-an-feed-item')).toHaveCount(1);
    await expect(page.locator('.sp-an-feed-item')).toContainText('Great chart');
  });

  test('exports the visible feedback rows as a formula-safe CSV', async ({ page }) => {
    await openAnalyticsTab(page);
    await expect(page.locator('.sp-an-feed-item')).toHaveCount(3);

    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.locator('[data-export="feedback"]').click(),
    ]);
    expect(download.suggestedFilename()).toMatch(/^jeen-analytics-feedback-30d-\d{4}-\d{2}-\d{2}\.csv$/);
    const text = await (await download.createReadStream()).toArray().then((chunks) => Buffer.concat(chunks).toString('utf8'));
    const lines = text.replace(/^\uFEFF/, '').split('\r\n');
    expect(lines[0]).toBe('occurred_at,user,email,connection,thumb,rating,type,message,question,query_id');
    expect(lines).toHaveLength(4);
    // A message starting with "=" is neutralised so a spreadsheet won't evaluate it.
    expect(lines[1]).toContain(",'=SUM(A1) the total looks wrong,");
    expect(lines[1]).toContain('Dana Levi');
  });

  test('shows the migration notice when the API answers 503', async ({ page }) => {
    await openAnalyticsTab(page, 'admin', { unavailable: true });
    await expect(page.locator('.sp-an-notice')).toContainText('Analytics is not available on this database yet');
    await expect(page.locator('.sp-an-notice')).toContainText('036_usage_events');
    await expect(page.locator('#sp-an-body')).toBeHidden();
  });

  test('leaving the tab releases the chart and coming back re-renders it', async ({ page }) => {
    await openAnalyticsTab(page);
    await expect(page.locator('#sp-an-chart canvas')).toHaveCount(1);
    await page.locator('.sp-nav-item[data-id="general"]').click();
    await expect(page.locator('#sp-an-chart')).toHaveCount(0);
    await page.locator('.sp-nav-item[data-id="analytics"]').click();
    await expect(page.locator('#sp-an-chart canvas')).toHaveCount(1);
    await expect(page.locator('.sp-an-feed-item')).toHaveCount(3);
  });

  test('renders in Hebrew with RTL layout', async ({ page }) => {
    await openAnalyticsTab(page, 'admin', { locale: 'he' });
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    await expect(page.locator('.sp-nav-item[data-id="analytics"]')).toContainText('אנליטיקה');
    await expect(page.locator('.sp-section-title').first()).toContainText('אנליטיקה');
    await expect(page.locator('.sp-an-kpi[data-kpi="questions"] .sp-an-kpi-label')).toContainText('שאלות');
    await expect(page.locator('#sp-an-chart canvas')).toHaveCount(1);
    await expect(page.locator('.sp-an-feed-item')).toHaveCount(3);
    // Free text keeps its own direction inside <bdi>.
    await expect(page.locator('.sp-an-feed-item').nth(0).locator('.sp-an-feed-message bdi')).toContainText('the total looks wrong');
  });
});
