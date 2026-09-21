// @ts-check
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask } = require('./_helpers');

test.describe('conversation and favorite actions', () => {
  test('New conversation stays visible after a long thread is scrolled', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 700 });
    await openHarness(page);
    for (let i = 0; i < 7; i += 1) {
      await ask(page, i % 2 ? Q.sqlCount : Q.sqlAggregate);
    }

    const thread = page.locator('#v3-thread');
    await thread.evaluate((node) => { node.scrollTop = node.scrollHeight; });
    const button = page.locator('#v3-new-conversation');
    await expect(button).toBeVisible();
    await expect(button).toBeEnabled();

    const [panelBox, buttonBox] = await Promise.all([
      page.locator('#v3-conversation').boundingBox(),
      button.boundingBox(),
    ]);
    expect(panelBox).not.toBeNull();
    expect(buttonBox).not.toBeNull();
    expect(buttonBox.y).toBeLessThan(panelBox.y + 60);

    await button.click();
    await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(0);
    await expect(page.locator('#v3-thread .v3-thread-empty')).toBeVisible();
  });

  test('selected answer can be favorited, reopened, and removed from Saved', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlAggregate);

    const favorite = page.locator('#v3-favorite-action');
    await expect(favorite).toBeVisible();
    await expect(favorite).toHaveAttribute('aria-pressed', 'false');
    await favorite.click();
    await expect(favorite).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('#v3-thread .v3-turn-favorite')).toHaveCount(1);

    await page.locator('[data-rail="saved"]').click();
    await expect(page.locator('[data-saved-view="answers"]')).toHaveAttribute('aria-selected', 'true');
    const saved = page.locator('.v3-favorite-item');
    await expect(saved).toHaveCount(1);
    await expect(saved).toContainText(Q.sqlAggregate);

    await saved.click();
    await expect(page.locator('#v3-panel-conversation')).toBeVisible();
    await expect.poll(() => page.evaluate(() => {
      const ctrl = window.ChatController;
      const turn = ctrl.turns.find((item) => item.id === ctrl.selectedTurnId);
      return turn && turn.question;
    })).toBe(Q.sqlAggregate);

    await page.locator('[data-rail="saved"]').click();
    await page.locator('.v3-favorite-item [data-favorite-remove]').click();
    await expect(page.locator('.v3-favorite-item')).toHaveCount(0);
    await expect(page.locator('.v3-saved-empty')).toBeVisible();
  });

  test('mobile RTL drawer keeps localized New conversation and Saved controls visible', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openHarness(page, '?locale=he');
    await page.locator('#v3-conversation-toggle').click();

    const panel = page.locator('#v3-conversation');
    const button = page.locator('#v3-new-conversation');
    await expect(panel).toBeVisible();
    await expect(button).toHaveText(/שיחה חדשה/);
    await expect(page.locator('[data-rail="saved"]')).toHaveAttribute('aria-label', 'תשובות ושאלות שמורות');

    const [box, railBox] = await Promise.all([
      panel.boundingBox(),
      page.locator('.v3-rail').boundingBox(),
    ]);
    expect(box).not.toBeNull();
    expect(railBox).not.toBeNull();
    expect(railBox.x).toBeGreaterThan(box.x);

    await page.locator('[data-rail="saved"]').click();
    await expect(page.locator('[data-saved-view="answers"]')).toHaveText('תשובות');
    await expect(page.locator('[data-saved-view="questions"]')).toHaveText('שאלות');
    await page.locator('[data-saved-view="answers"]').focus();
    await page.keyboard.press('ArrowLeft');
    await expect(page.locator('[data-saved-view="questions"]')).toHaveAttribute('aria-selected', 'true');
    await expect(button).toBeVisible();
  });

  test('a delayed favorite response cannot overwrite another selected answer', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlAggregate);
    await ask(page, Q.sqlCount);
    const turns = page.locator('#v3-thread article.v3-turn');
    await turns.first().click();

    await page.evaluate(() => {
      const baseFetch = window.fetch;
      let release;
      window.__releaseFavorite = () => release?.();
      window.fetch = async (input, init) => {
        const url = typeof input === 'string' ? input : input.url;
        if (/\/favorite$/.test(url) && (init?.method || 'GET') === 'PUT') {
          await new Promise((resolve) => { release = resolve; });
        }
        return baseFetch(input, init);
      };
    });
    await page.locator('#v3-favorite-action').click();
    await turns.last().click();
    await expect(page.locator('#v3-result-title')).toHaveText(Q.sqlCount);
    await page.evaluate(() => window.__releaseFavorite());
    await expect.poll(() => page.evaluate(() => window.ChatController.turns[0].isFavorite)).toBe(true);
    await expect(page.locator('#v3-favorite-action')).toHaveAttribute('aria-pressed', 'false');
  });

  test('an unavailable saved turn never falls back to the newest answer', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlAggregate);
    await page.locator('#v3-favorite-action').click();
    await ask(page, Q.sqlCount);
    await page.locator('[data-rail="saved"]').click();
    await expect(page.locator('.v3-favorite-item')).toHaveCount(1);

    await page.evaluate(() => {
      const baseFetch = window.fetch;
      window.fetch = async (input, init) => {
        const url = typeof input === 'string' ? input : input.url;
        const parsed = new URL(url, location.origin);
        if (/\/api\/conversations\/[^/]+\/turns\/[^/]+$/.test(parsed.pathname)) {
          return new Response(JSON.stringify({ detail: 'gone' }), { status: 404, headers: { 'Content-Type': 'application/json' } });
        }
        if (/\/api\/conversations\/[^/]+$/.test(parsed.pathname)) {
          const response = await baseFetch(input, init);
          const detail = await response.json();
          detail.turns = detail.turns.filter((turn) => turn.question === 'How many orders shipped yesterday?');
          return new Response(JSON.stringify(detail), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        return baseFetch(input, init);
      };
    });
    await page.locator('.v3-favorite-item').click();
    await expect.poll(() => page.evaluate(() => window.ChatController.selectedResultId)).toBeNull();
    await expect.poll(() => page.evaluate(() => (window.__toasts || []).at(-1)?.msg)).toBe('That saved answer is no longer available.');
  });

  test('Saved answers paginate without hiding older favorites', async ({ page }) => {
    await openHarness(page);
    await page.evaluate(() => {
      window.__seedFavoriteItems(Array.from({ length: 51 }, (_, index) => ({
        conversation_id: '11111111-1111-1111-1111-111111111111',
        turn_id: `favorite-${index}`,
        sequence_number: index + 1,
        conversation_title: 'Saved conversation',
        question: `Saved answer ${index + 1}`,
        answer: `Answer ${index + 1}`,
        result_kind: 'text',
        snapshot_status: 'not_applicable',
        source_key: 'sales_db',
        source_label: 'Sales DB',
        connection_available: true,
        favorited_at: new Date(Date.now() - index * 1000).toISOString(),
      })));
    });
    await page.locator('[data-rail="saved"]').click();
    await expect(page.locator('.v3-favorite-item')).toHaveCount(50);
    await expect(page.locator('[data-favorite-more]')).toBeVisible();
    await page.locator('[data-favorite-more]').click();
    await expect(page.locator('.v3-favorite-item')).toHaveCount(51);
    await expect(page.locator('[data-favorite-more]')).toHaveCount(0);
  });
});
