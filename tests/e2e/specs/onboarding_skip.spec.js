// @ts-check
// Onboarding (FTUE) suppression: "Skip for now" mutes every surface for the
// browser session; "Don't show this again" opts out permanently (server-side).
// The harness only loads onboarding.js when asked (?ftue=1), and its fake
// backend keeps one onboarding row per page (seed it with __ONBOARDING_ROW__).
const { test, expect } = require('@playwright/test');
const { HARNESS } = require('./_helpers');

const FTUE_HARNESS = HARNESS + '?ftue=1';

const welcome = (page) => page.locator('.jo-dialog');
const checklist = (page) => page.locator('.jo-checklist');
const cards = (page) => page.locator('#v3-placeholder .jo-cards');
const coach = (page) => page.locator('.jo-coach');

/** Wait for the workspace AND for onboarding.js to have made its boot decisions. */
async function waitForFtueBoot(page) {
  await page.waitForFunction(() => window.ChatController && document.body.classList.contains('v3-ready'));
  await page.waitForFunction(() => document.body.classList.contains('jo-ready'));
}

async function openFtue(page) {
  await page.goto(FTUE_HARNESS);
  await waitForFtueBoot(page);
}

/** Seed the fake server's onboarding row before the page boots. */
async function seedRow(page, row) {
  await page.addInitScript((r) => { window.__ONBOARDING_ROW__ = r; }, row);
}

/**
 * Make GET /api/user/onboarding fail on the next navigation. The harness
 * replaces window.fetch in-page, so this goes through its hook rather than
 * page.route().
 */
async function failOnboardingGet(page) {
  await page.addInitScript(() => { window.__ONBOARDING_GET_FAILS__ = true; });
}

/** PATCH bodies the FTUE layer sent to /api/user/onboarding. */
async function onboardingPatches(page) {
  return page.evaluate(() => (window.__calls || [])
    .filter((c) => c.url.indexOf('/api/user/onboarding') >= 0 && c.method === 'PATCH')
    .map((c) => c.body));
}

async function expectAllHidden(page) {
  await expect(welcome(page)).toHaveCount(0);
  await expect(page.locator('.jo-backdrop')).toHaveCount(0);
  await expect(checklist(page)).toHaveCount(0);
  await expect(cards(page)).toHaveCount(0);
  await expect(page.locator('.jo-nudge')).toHaveCount(0);
  await expect(coach(page)).toHaveCount(0);
  await expect(page.locator('.jo-tour-scrim')).toHaveCount(0);
  await expect(page.locator('.jo-target-highlight')).toHaveCount(0);
}

/** Fire the usage signals that normally (re)arm surfaces and assert none appear. */
async function expectSignalsStayMuted(page) {
  await page.evaluate(() => {
    document.dispatchEvent(new CustomEvent('jeen:onboarding:pick_connection')); // re-arms cards
    document.dispatchEvent(new CustomEvent('jeen:onboarding:ask_first_question')); // first answer -> nudge
  });
  await expectAllHidden(page);
}

test('a fresh user sees the welcome dialog, checklist and quick-start cards', async ({ page }) => {
  await openFtue(page);
  await expect(welcome(page)).toBeVisible();
  await expect(checklist(page)).toHaveCount(1);
  await expect(cards(page)).toHaveCount(1);
});

test('"Skip for now" mutes every surface for the session and survives a reload', async ({ page, browser }) => {
  await openFtue(page);
  await expect(welcome(page)).toBeVisible();

  await page.click('.jo-dialog [data-skip]');
  await expectAllHidden(page);

  // Plain skip is session-scoped: nothing is written to the server.
  const patches = await onboardingPatches(page);
  expect(patches.some((b) => b.ftue_opted_out || b.welcome_seen)).toBe(false);

  // A connection change / first answer would normally re-arm cards / show the nudge.
  await expectSignalsStayMuted(page);

  // Same tab, reload: still muted (sessionStorage carries the skip).
  await page.reload();
  await waitForFtueBoot(page);
  await expectAllHidden(page);
  await expectSignalsStayMuted(page);

  // A genuinely new session (fresh context => fresh sessionStorage) gets it back.
  const ctx = await browser.newContext();
  const fresh = await ctx.newPage();
  await openFtue(fresh);
  await expect(welcome(fresh)).toBeVisible();
  await ctx.close();
});

test('Escape on the welcome dialog behaves like "Skip for now"', async ({ page }) => {
  await openFtue(page);
  await expect(welcome(page)).toBeVisible();
  await page.keyboard.press('Escape');
  await expectAllHidden(page);
});

test('a session skip survives a reload whose onboarding GET fails soft', async ({ page }) => {
  // The skip is keyed by every identity the page knows. If the onboarding GET
  // is down on reload (user_id unknown), the /api/auth/me identity still matches.
  await openFtue(page);
  await page.click('.jo-dialog [data-skip]');
  await expectAllHidden(page);

  await failOnboardingGet(page);
  await page.reload();
  await waitForFtueBoot(page);
  await expectAllHidden(page);
  await expectSignalsStayMuted(page);
});

test('"Don\'t show this again" persists a dedicated permanent opt-out', async ({ page, browser }) => {
  await openFtue(page);
  await page.check('.jo-dialog [data-dontshow]');
  await page.click('.jo-dialog [data-skip]');
  await expectAllHidden(page);

  await expect.poll(async () => (await onboardingPatches(page)).some((b) => b.ftue_opted_out === true)).toBe(true);
  // The opt-out is its own column, not a bundle of per-surface dismissals.
  const patches = await onboardingPatches(page);
  expect(patches.some((b) => b.checklist_dismissed || b.nudge_dismissed)).toBe(false);

  // Next session, the server row carries ftue_opted_out_at: nothing mounts.
  const ctx = await browser.newContext();
  const next = await ctx.newPage();
  await seedRow(next, { ftue_opted_out_at: '2026-09-20T00:00:00+00:00', welcome_seen_at: '2026-09-20T00:00:00+00:00' });
  await openFtue(next);
  await expectAllHidden(next);
  await expectSignalsStayMuted(next);
  await ctx.close();
});

test('a previously dismissed nudge does not return on the next answer', async ({ page }) => {
  await seedRow(page, {
    welcome_seen_at: '2026-09-15T00:00:00+00:00',
    checklist_dismissed_at: '2026-09-11T00:00:00+00:00',
    nudge_dismissed_at: '2026-09-11T00:00:00+00:00',
  });
  await openFtue(page);
  await page.evaluate(() => {
    document.dispatchEvent(new CustomEvent('jeen:onboarding:ask_first_question'));
  });
  await expect(page.locator('.jo-nudge')).toHaveCount(0);
});

test('a dismissed nudge does not flash if the first answer races the onboarding GET', async ({ page }) => {
  await seedRow(page, {
    welcome_seen_at: '2026-09-15T00:00:00+00:00',
    checklist_dismissed_at: '2026-09-11T00:00:00+00:00',
    nudge_dismissed_at: '2026-09-11T00:00:00+00:00',
  });
  await page.addInitScript(() => { window.__ONBOARDING_GET_DELAY_MS__ = 1200; });
  await page.goto(FTUE_HARNESS);
  await page.waitForFunction(() => window.ChatController && document.body.classList.contains('v3-ready'));
  await page.evaluate(() => {
    document.dispatchEvent(new CustomEvent('jeen:onboarding:ask_first_question'));
  });
  await expect(page.locator('.jo-nudge')).toHaveCount(0);
  await page.waitForFunction(() => document.body.classList.contains('jo-ready'), null, { timeout: 10_000 });
  await expect(page.locator('.jo-nudge')).toHaveCount(0);
  await expect(welcome(page)).toHaveCount(0);
});

test('independent dismissals do not add up to a global opt-out', async ({ page }) => {
  // A user who saw the welcome, closed the checklist and dismissed the nudge
  // has not asked to lose onboarding entirely: quick-start cards still show.
  await seedRow(page, {
    welcome_seen_at: '2026-09-20T00:00:00+00:00',
    checklist_dismissed_at: '2026-09-20T00:00:00+00:00',
    nudge_dismissed_at: '2026-09-20T00:00:00+00:00',
  });
  await openFtue(page);
  await expect(cards(page)).toHaveCount(1);
  await expect(welcome(page)).toHaveCount(0);
  await expect(checklist(page)).toHaveCount(0);
});

test('"Take a tour" still runs when opted out, and nothing lingers afterwards', async ({ page }) => {
  await openFtue(page);
  await page.check('.jo-dialog [data-dontshow]');
  await page.click('.jo-dialog [data-tour]');

  // Everything but the explicitly requested tour is gone.
  await expect(welcome(page)).toHaveCount(0);
  await expect(checklist(page)).toHaveCount(0);
  await expect(cards(page)).toHaveCount(0);
  await expect(coach(page)).toBeVisible();

  // Walk the tour to the end via the primary button.
  for (let i = 0; i < 6 && (await coach(page).count()) > 0; i++) {
    await page.click('.jo-coach [data-next]');
  }
  await expectAllHidden(page);
  await expect(page.locator('.jo-tour-scrim')).toHaveCount(0);

  const patches = await onboardingPatches(page);
  expect(patches.some((b) => b.ftue_opted_out === true)).toBe(true);
  expect(patches.some((b) => b.tour_completed === true)).toBe(true);
});
