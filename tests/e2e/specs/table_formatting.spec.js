// @ts-check
// Numbers that name something (years, keys) render as-is in the result grid
// and the profiling dock, while measures keep their thousands separators.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask } = require('./_helpers');

test.beforeEach(async ({ page }) => {
  // The harness does not load script.js; stand in for its locale formatter so
  // the grid's grouping path runs exactly as in production.
  await page.addInitScript(() => {
    const nativeDateParse = Date.parse.bind(Date);
    window.__dateParseCount = 0;
    Date.parse = (...args) => {
      window.__dateParseCount += 1;
      return nativeDateParse(...args);
    };
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

async function openGeneralSettings(page) {
  await page.evaluate(async () => {
    const { SettingsPage } = await import('/src/static/settings/settingsPage.js');
    window.__dateSettingsPage = new SettingsPage();
    window.__dateSettingsPage.mount();
    window.__dateSettingsPage.open();
  });
}

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

test('date preference defaults to ISO for existing accounts', async ({ page }) => {
  await ask(page, Q.sqlByYear);
  await expect(cell(page, 0, 3)).toHaveText('2005-07-01');
});

for (const [preference, expectedDate, expectedTimestamp] of [
  ['auto', '07/01/2005', '07/01/2005 00:00:00'],
  ['dmy', '01/07/2005', '01/07/2005 00:00:00'],
  ['mdy', '07/01/2005', '07/01/2005 00:00:00'],
  ['iso', '2005-07-01', '2005-07-01 00:00:00'],
]) {
  test(`${preference} date preference formats dates and hides redundant midnight columns`, async ({ page }) => {
    await openHarness(page, `?dateFormat=${preference}`);
    await ask(page, Q.sqlByYear);

    // MonthStart contains only midnight values, so its time component is noise.
    await expect(cell(page, 0, 3)).toHaveText(expectedDate);
    // UpdatedAt contains a meaningful time in another row, so midnight remains
    // explicit throughout this mixed timestamp column.
    await expect(cell(page, 0, 4)).toHaveText(expectedTimestamp);
    await expect(cell(page, 1, 4)).toContainText('13:45:00');
  });
}

test('displayed dates and raw ISO dates both filter the result', async ({ page }) => {
  await openHarness(page, '?dateFormat=dmy');
  await ask(page, Q.sqlByYear);
  const filter = page.locator('#v3-result-filter');

  await filter.fill('01/07/2005');
  await expect(page.locator('#v3-grid [data-row]')).toHaveCount(1);
  await expect(cell(page, 0, 3)).toHaveText('01/07/2005');

  await filter.fill('2005-07');
  await expect(page.locator('#v3-grid [data-row]')).toHaveCount(1);
});

test('profiling uses the configured date format', async ({ page }) => {
  await openHarness(page, '?dateFormat=dmy');
  await ask(page, Q.sqlByYear);
  await page.click('[data-dock="profiling"]');
  const dateRow = page.locator('.v3-profile-row', { has: page.locator('.v3-profile-name strong', { hasText: 'MonthStart' }) });
  await expect(dateRow.locator('.v3-profile-range span')).toHaveText('01/07/2005 – 01/01/2008');
});

test('date format changes apply live without navigation or metadata rescans', async ({ page }) => {
  await ask(page, Q.sqlByYear);
  const initialParseCount = await page.evaluate(() => window.__dateParseCount);
  await page.evaluate(() => {
    window.__noReloadMarker = 'kept';
    window.I18n.setDateFormat('dmy');
  });

  await expect(cell(page, 0, 3)).toHaveText('01/07/2005');
  expect(await page.evaluate(() => window.__noReloadMarker)).toBe('kept');
  expect(await page.evaluate(() => window.__dateParseCount)).toBe(initialParseCount);
});

test('settings save applies live without reloading the workspace', async ({ page }) => {
  await page.evaluate(() => {
    const harnessFetch = window.fetch;
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (url.includes('/api/auth/me/date-format')) {
        return Promise.resolve(new Response('{"date_format":"dmy"}', {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }));
      }
      return harnessFetch(input, init);
    };
  });
  await ask(page, Q.sqlByYear);
  await page.evaluate(() => { window.__noReloadMarker = 'kept'; });
  await openGeneralSettings(page);
  await page.locator('#sp-dateformat').selectOption('dmy');

  await expect.poll(() => page.evaluate(() => window.I18n.dateFormat)).toBe('dmy');
  expect(await page.evaluate(() => window.__noReloadMarker)).toBe('kept');
  await expect(cell(page, 0, 3)).toHaveText('01/07/2005');
});

test('settings save failure restores the prior preference', async ({ page }) => {
  await page.evaluate(() => {
    const harnessFetch = window.fetch;
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (url.includes('/api/auth/me/date-format')) {
        return Promise.resolve(new Response('{"code":"DB_ERROR"}', {
          status: 500, headers: { 'Content-Type': 'application/json' },
        }));
      }
      return harnessFetch(input, init);
    };
  });
  await page.evaluate(() => {
    window.__lastToast = null;
    window.showToast = (message, type) => { window.__lastToast = { message, type }; };
  });
  await openGeneralSettings(page);
  await page.locator('#sp-dateformat').selectOption('dmy');

  await expect(page.locator('#sp-dateformat')).toHaveValue('iso');
  expect(await page.evaluate(() => window.I18n.dateFormat)).toBe('iso');
  await expect.poll(() => page.evaluate(() => window.__lastToast?.type)).toBe('error');
});

test('canonical timezone variants trim midnight without shifting the date', async ({ page }) => {
  await page.evaluate(() => {
    const original = window.__FIXTURES__.SCENARIOS.sql_by_year;
    window.__FIXTURES__.SCENARIOS.sql_by_year = () => {
      const result = original();
      result.results.rows[0][3] = '2005-07-01T00:00:00Z';
      result.results.rows[1][3] = '2006-01-01 00:00:00+03';
      result.results.rows[2][3] = '2007-01-01 00:00:00.000+0300';
      return result;
    };
  });
  await ask(page, Q.sqlByYear);

  await expect(cell(page, 0, 3)).toHaveText('2005-07-01');
  await expect(cell(page, 1, 3)).toHaveText('2006-01-01');
  await expect(cell(page, 2, 3)).toHaveText('2007-01-01');
});

test('large result filtering reuses cached column metadata', async ({ page }) => {
  await page.evaluate(() => {
    const original = window.__FIXTURES__.SCENARIOS.sql_by_year;
    window.__FIXTURES__.SCENARIOS.sql_by_year = () => {
      const result = original();
      const seed = result.results.rows;
      result.results.rows = Array.from({ length: 10000 }, (_, index) => {
        const row = seed[index % seed.length].slice();
        row[1] += index;
        return row;
      });
      result.results.row_count = result.results.rows.length;
      return result;
    };
  });
  await ask(page, Q.sqlByYear);
  const before = await page.evaluate(() => window.__dateParseCount);
  const elapsed = await page.evaluate(() => {
    const started = performance.now();
    window.WorkspaceController.filter = '2005-07';
    window.WorkspaceController.renderTable();
    return performance.now() - started;
  });

  expect(await page.evaluate(() => window.__dateParseCount)).toBe(before);
  expect(elapsed).toBeLessThan(500);
});
