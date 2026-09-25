// @ts-check
// A finished ML answer leads somewhere: "Adjustments to try" chips re-run the
// analysis with the server's validated patch, thumbs up/down reach /api/feedback
// with the values the DB admits (and a thumbs-down opens Model details), and
// "Check against actuals" shows realized accuracy beside the model's claim.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn } = require('./_helpers');

test.beforeEach(async ({ page }) => { await openHarness(page); });

async function runForecast(page) {
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
  await expect(page.locator('[data-dock="model"]')).toBeVisible();
}

async function postedCalls(page, path) {
  return page.evaluate((p) => (window.__calls || []).filter((c) => c.url.indexOf(p) >= 0 && c.method === 'POST'), path);
}

test('Model details lists adjustments to try and a chip re-runs with its patch', async ({ page }) => {
  await runForecast(page);
  await page.click('[data-dock="model"]');

  const section = page.locator('#v3-dock-body .v3-ml-adjustments');
  await expect(section).toBeVisible();
  await expect(section.locator('.v3-ml-section-title')).toHaveText('Adjustments to try');
  const chips = section.locator('[data-ml-adjust]');
  await expect(chips).toHaveCount(2);
  await expect(chips.first()).toHaveClass(/is-recommended/);
  await expect(chips.first()).toContainText('Look back 105 weeks');
  await expect(chips.nth(1)).toContainText('Try Theta');

  await chips.first().click();
  // The patch reaches /api/analysis/rerun exactly as the server offered it, and a child turn is appended.
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(3);
  const reruns = await postedCalls(page, '/api/analysis/rerun');
  expect(reruns.length).toBe(1);
  expect(reruns[0].body.params_patch).toEqual({ window: 105 });
  expect(reruns[0].body.instruction).toBeUndefined();
});

test('thumbs feedback posts an admitted value with notes and a thumbs-down opens Model details', async ({ page }) => {
  await runForecast(page);
  const turn = lastTurn(page);
  const row = turn.locator('.v3-feedback');
  await expect(row).toBeVisible();
  await expect(row.locator('.v3-feedback-label')).toHaveText('Was this answer useful?');

  await row.locator('[data-feedback$=":thumbs_down"]').click();
  await expect(lastTurn(page).locator('[data-feedback$=":thumbs_down"]')).toHaveAttribute('aria-pressed', 'true');
  // An ML thumbs-down leads to the adjustments: the Model details dock opens.
  await expect(page.locator('[data-dock="model"]')).toHaveClass(/is-active/);
  await expect(page.locator('#v3-dock-body .v3-ml-adjustments')).toBeVisible();

  let calls = await postedCalls(page, '/api/feedback');
  expect(calls.length).toBe(1);
  expect(calls[0].body.feedback).toBe('thumbs_down');
  expect(calls[0].body.comment).toBeUndefined();
  expect(typeof calls[0].body.query_id).toBe('string');

  // The note goes in the field the API stores (``notes``), never ``comment``.
  const note = lastTurn(page).locator('[data-feedback-note]');
  await expect(note).toBeVisible();
  await note.fill('The band looked too narrow');
  await lastTurn(page).locator('[data-feedback-note-send]').click();
  await expect(lastTurn(page).locator('.v3-feedback-status')).toHaveText('Note sent ✓');
  calls = await postedCalls(page, '/api/feedback');
  expect(calls.length).toBe(2);
  expect(calls[1].body).toMatchObject({ feedback: 'thumbs_down', notes: 'The band looked too narrow' });
});

test('a rejected feedback save reverts the button and says so', async ({ page }) => {
  await runForecast(page);
  await page.evaluate(() => {
    const request = window.fetch;
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.indexOf('/api/feedback') >= 0) {
        return Promise.resolve(new Response(JSON.stringify({ detail: 'Query not found for this user' }),
          { status: 404, headers: { 'Content-Type': 'application/json' } }));
      }
      return request(input, init);
    };
  });
  await lastTurn(page).locator('[data-feedback$=":thumbs_up"]').click();
  await expect(lastTurn(page).locator('[data-feedback$=":thumbs_up"]')).toHaveAttribute('aria-pressed', 'false');
  await expect(lastTurn(page).locator('.v3-feedback-status')).toHaveCount(0);
});

test('"Check against actuals" shows realized accuracy beside the claim', async ({ page }) => {
  await runForecast(page);
  await page.click('[data-dock="model"]');
  const section = page.locator('#v3-dock-body .v3-ml-accuracy');
  await expect(section).toBeVisible();
  await section.locator('[data-ml-accuracy]').click();

  const calls = await postedCalls(page, '/api/analysis/forecast/accuracy');
  expect(calls.length).toBe(1);
  expect(typeof calls[0].body.query_id).toBe('string');
  expect(calls[0].body.connection).toBeTruthy();

  const refreshed = page.locator('#v3-dock-body .v3-ml-accuracy');
  await expect(refreshed.locator('.v3-stats')).toContainText('Realized MASE');
  await expect(refreshed.locator('.v3-stats')).toContainText('0.930');
  await expect(refreshed.locator('.v3-stats')).toContainText('0.710');
  await expect(refreshed.locator('.v3-ml-accuracy-note')).toContainText('2 elapsed periods scored, 6 still pending');
  await expect(refreshed.locator('.v3-ml-accuracy-note')).toContainText('2026-09-13');
  await expect(refreshed.locator('tbody tr')).toHaveCount(2);
  await expect(refreshed.locator('tbody tr.is-outside')).toHaveCount(1);
  await expect(refreshed.locator('[data-ml-accuracy]')).toHaveText('Re-check');
});
