// @ts-check
// Numbers that name something (years, keys) render as-is in the result grid
// and the profiling dock, while measures keep their thousands separators.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask } = require('./_helpers');

test.beforeEach(async ({ page }) => {
  // The harness does not load script.js; stand in for its locale formatter so
  // the grid's grouping path runs exactly as in production.
  await page.addInitScript(() => {
    window.JeenLegacyBridge = {
      formatTableValue: (value, index, numeric) => (numeric
        ? Number(value).toLocaleString('en-US', { maximumFractionDigits: 0 })
        : String(value)),
      applyResult() {},
    };
  });
  await openHarness(page);
});

const cell = (page, row, col) => page.locator(`#v3-grid [data-row="${row}"] .v3-grid-cell`).nth(col);

test('year and key columns are not thousands-grouped; measures are', async ({ page }) => {
  await ask(page, Q.sqlByYear);
  await expect(cell(page, 0, 0)).toHaveText('2005');
  await expect(cell(page, 0, 1)).toHaveText('20050701');
  await expect(cell(page, 0, 2)).toHaveText('3,266,374');
  await expect(cell(page, 3, 0)).toHaveText('2008');
});

test('the profiling dock shows the real year range, not compact notation', async ({ page }) => {
  await ask(page, Q.sqlByYear);
  await page.click('[data-dock="profiling"]');
  const yearRow = page.locator('.v3-profile-row', { has: page.locator('.v3-profile-name strong', { hasText: 'CalendarYear' }) });
  await expect(yearRow.locator('.v3-profile-range span')).toHaveText('2005 – 2008');
});
