// @ts-check
// The confirm-card happy path: an ML question stops for confirmation, shows the
// egress notice and editable chips, and running it appends a completed ML turn.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn } = require('./_helpers');

test.beforeEach(async ({ page }) => { await openHarness(page); });

test('forecast stops on a confirm card with egress notice and chips', async ({ page }) => {
  await ask(page, Q.forecast);

  // Thread turn is on the ML path.
  await expect(lastTurn(page)).toHaveAttribute('data-route-path', 'ml');

  // The answer pane hosts the confirm card.
  const card = page.locator('#v3-placeholder .v3-ml-card.is-confirm');
  await expect(card).toBeVisible();
  await expect(card.locator('.v3-ml-tiermeta')).toContainText('Aggregates only');
  await expect(card.locator('.v3-ml-egress')).toContainText('sent to the analysis service');

  // Editable parameter fields, incl. the horizon, grouped into sections.
  await expect(card.locator('[data-chip="horizon"]')).toHaveValue('8');
  await expect(card.locator('.v3-ml-group legend')).toHaveText(['Data', 'Model', 'Output']);
  await expect(card.locator('[data-run]')).toBeEnabled();
});

test('the confirm card keeps its own type styles inside the answer pane', async ({ page }) => {
  await ask(page, Q.forecast);
  const card = page.locator('#v3-placeholder .v3-ml-card.is-confirm');
  // The pane's caption rule must not reach nested spans (it once turned these faint).
  await expect(card.locator('.v3-skill-chip').first()).toHaveCSS('font-size', '11px');
  await expect(card.locator('.v3-skill-chip').first()).toHaveCSS('color', 'rgb(255, 255, 255)');
  await expect(card.locator('.v3-ml-summary')).toHaveCSS('font-size', '12.5px');
});

test('running the confirm card appends a completed ML result with Model details', async ({ page }) => {
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-run]');

  // A second turn (the child run) is appended and selected.
  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
  await expect(lastTurn(page)).toHaveAttribute('data-route-path', 'ml');

  // The completed result names the skill and validation metric in the strip.
  const meta = page.locator('#v3-meta-row');
  await expect(meta.locator('.v3-skill-chip')).toHaveText('forecast');
  await expect(meta).toContainText('MASE');

  // Model details tab becomes available for ML results.
  await expect(page.locator('[data-dock="model"]')).toBeVisible();
});

test('"Answer with SQL instead" from the card switches to the SQL path', async ({ page }) => {
  await ask(page, Q.forecast);
  await page.click('#v3-placeholder .v3-ml-card [data-sql-instead]');

  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
  const turn = lastTurn(page);
  await expect(turn).toHaveAttribute('data-route-path', 'sql');
  await expect(turn.locator('.v3-route-pill.is-sql')).toHaveText('SQL');
});

test('an expired card explains the state and can ask the question again', async ({ page }) => {
  await page.evaluate(() => {
    const original = window.__FIXTURES__.SCENARIOS.forecast_confirm;
    let calls = 0;
    window.__FIXTURES__.SCENARIOS.forecast_confirm = (body) => {
      const result = original(body);
      if (calls++ === 0) result.proposal.expires_at = '2000-01-01T00:00:00Z';
      return result;
    };
  });
  await ask(page, Q.forecast);

  const card = page.locator('#v3-placeholder .v3-ml-card.is-expired');
  await expect(card).toBeVisible();
  await expect(card.locator('.v3-ml-expired-badge')).toHaveText('Expired');
  await expect(card.locator('#v3-ml-expired-title')).toHaveText('This analysis setup expired');
  await expect(card.locator('[data-recreate]')).toBeEnabled();
  await expect(card.locator('[data-sql-instead]')).toBeEnabled();
  await expect(card.locator('select, input, [data-run], [data-exit]')).toHaveCount(0);
  await expect(card.locator('.v3-ml-static')).not.toHaveCount(0);
  await expect(lastTurn(page).locator('.v3-run-meta')).toContainText('expired');
  await expect(page.locator('#v3-dock-meta')).toContainText('expired');

  // Hold the resend briefly so the button's progress state is observable.
  await page.evaluate(() => {
    const send = window.ChatController.send.bind(window.ChatController);
    window.ChatController.send = (...args) => new Promise((resolve) => {
      setTimeout(() => resolve(send(...args)), 250);
    });
  });
  await card.locator('[data-recreate]').click();
  await expect(card.locator('[data-recreate]')).toHaveAttribute('aria-busy', 'true');
  await expect(card.locator('[data-recreate]')).toHaveText('Asking…');

  await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
  const fresh = page.locator('#v3-placeholder .v3-ml-card.is-confirm');
  await expect(fresh).toBeVisible();
  await expect(fresh).not.toHaveClass(/is-expired/);
  await expect(fresh.locator('[data-run]')).toBeEnabled();
});

test('expired-card recovery is visibly guarded without a connection', async ({ page }) => {
  await ask(page, Q.forecast);
  await page.evaluate(() => {
    const turn = window.ChatController.turns[0];
    turn.result.proposal.expires_at = '2000-01-01T00:00:00Z';
    window.getActiveConnection = () => '';
    window.ChatController.render();
  });

  const card = page.locator('#v3-placeholder .v3-ml-card.is-expired');
  await expect(card.locator('[data-recreate]')).toBeDisabled();
  await expect(card.locator('[data-recreate-hint]')).toBeVisible();
  await expect(card.locator('[data-recreate-hint]')).toHaveText('Select a connection to ask again.');
});

test('a card becomes expired while the page remains open', async ({ page }) => {
  await page.evaluate(() => {
    const original = window.__FIXTURES__.SCENARIOS.forecast_confirm;
    window.__FIXTURES__.SCENARIOS.forecast_confirm = (body) => {
      const result = original(body);
      result.proposal.expires_at = new Date(Date.now() + 800).toISOString();
      return result;
    };
  });
  await ask(page, Q.forecast);

  await expect(page.locator('#v3-placeholder .v3-ml-card.is-confirm')).not.toHaveClass(/is-expired/);
  await expect(page.locator('#v3-placeholder .v3-ml-card.is-expired')).toBeVisible({ timeout: 3000 });
  await expect(page.locator('#v3-dock-meta')).toContainText('expired');
});

test('a server expiry response converts the active card to the expired state', async ({ page }) => {
  await ask(page, Q.forecast);
  await page.evaluate(() => {
    const request = window.fetch;
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url === '/api/analysis/run') {
        return Promise.resolve(new Response(
          JSON.stringify({ detail: 'This proposal has expired; ask the question again.' }),
          { status: 410, headers: { 'Content-Type': 'application/json' } },
        ));
      }
      return request(input, init);
    };
  });

  await page.locator('#v3-placeholder [data-run]').click();
  await expect(page.locator('#v3-placeholder .v3-ml-card.is-expired')).toBeVisible();
  await expect(page.locator('#v3-placeholder [data-recreate]')).toBeEnabled();
  await expect(page.locator('#v3-dock-meta')).toContainText('expired');
});
