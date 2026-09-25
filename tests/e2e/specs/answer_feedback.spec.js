// @ts-check
// Answer feedback: a compact thumbs up / down pair plus a feedback bubble that
// opens the Jeen-style "Give Feedback" dialog (stars, type chips, message).
// Thumbs post one append-only event each (a second click on the pressed thumb
// withdraws it); the dialog posts rating / type / message. Only SQL and ML
// answers carry the row. The fake backend records every accepted event in
// window.__ANSWER_FEEDBACK_EVENTS__ and can be forced to fail.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn } = require('./_helpers');

const row = (page) => lastTurn(page).locator('.v3-feedback');
const thumb = (page, kind) => row(page).locator(`[data-feedback$=":${kind}"]`);
const dialog = (page) => page.locator('#v3-feedback-dialog [role="dialog"]');
const events = (page) => page.evaluate(() => window.__ANSWER_FEEDBACK_EVENTS__ || []);

test.describe('answer feedback triggers', () => {
  test('a SQL answer shows a small icon row with visible glyphs; a greeting shows none', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    await expect(row(page)).toBeVisible();
    const buttons = row(page).locator('.v3-feedback-btn');
    await expect(buttons).toHaveCount(3);
    // Regression: the global button padding used to collapse the SVG to 0 width.
    for (let i = 0; i < 3; i += 1) {
      const box = await buttons.nth(i).locator('svg').boundingBox();
      expect(box && box.width).toBeGreaterThan(10);
      const size = await buttons.nth(i).boundingBox();
      expect(size && size.height).toBeLessThanOrEqual(28);
    }
    await ask(page, Q.greeting);
    await expect(lastTurn(page).locator('.v3-feedback')).toHaveCount(0);
  });

  test('thumbs up posts an event, is toggled off by a second click, and thumbs down replaces it', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    await thumb(page, 'thumbs_up').click();
    await expect(thumb(page, 'thumbs_up')).toHaveAttribute('aria-pressed', 'true');
    await expect(thumb(page, 'thumbs_down')).toHaveAttribute('aria-pressed', 'false');

    await thumb(page, 'thumbs_up').click();
    await expect(thumb(page, 'thumbs_up')).toHaveAttribute('aria-pressed', 'false');

    await thumb(page, 'thumbs_down').click();
    await expect(thumb(page, 'thumbs_down')).toHaveAttribute('aria-pressed', 'true');
    await expect(thumb(page, 'thumbs_up')).toHaveAttribute('aria-pressed', 'false');

    const sent = await events(page);
    expect(sent.map((e) => e.thumb)).toEqual(['thumbs_up', 'cleared', 'thumbs_down']);
    expect(new Set(sent.map((e) => e.query_id)).size).toBe(1);
    // Thumb events carry no dialog fields.
    for (const e of sent) {
      expect(e.rating).toBeUndefined();
      expect(e.feedback_type).toBeUndefined();
      expect(e.message).toBeUndefined();
    }
  });

  test('a failed thumb save reverts the pressed state', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    await page.evaluate(() => { window.__ANSWER_FEEDBACK_FAILS__ = true; });
    await thumb(page, 'thumbs_up').click();
    await expect(thumb(page, 'thumbs_up')).toHaveAttribute('aria-pressed', 'false');
    expect(await events(page)).toEqual([]);
  });

  test('a restored turn renders its saved thumb pressed', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    // Same shape turnFromServer() produces from ConversationTurn.user_feedback.
    await page.evaluate(() => {
      const wc = window.WorkspaceController;
      wc.turns[0].feedback = 'thumbs_down';
      wc.render();
    });
    await expect(thumb(page, 'thumbs_down')).toHaveAttribute('aria-pressed', 'true');
  });

  test('an ML answer carries the same row', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.anomaly);
    // The confirm card is a proposal, not an answer: no feedback yet.
    await expect(lastTurn(page).locator('.v3-feedback')).toHaveCount(0);
    await page.locator('[data-run]').first().click();
    await expect(lastTurn(page)).toHaveAttribute('data-route-path', 'ml');
    await expect(row(page)).toBeVisible();
    await thumb(page, 'thumbs_up').click();
    await expect(thumb(page, 'thumbs_up')).toHaveAttribute('aria-pressed', 'true');
  });
});

test.describe('give feedback dialog', () => {
  test('opens from the bubble, is labelled, traps focus, and Send waits for a rating or a message', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    await row(page).locator('[data-feedback-open]').click();
    const box = dialog(page);
    await expect(box).toBeVisible();
    await expect(box).toHaveAttribute('aria-modal', 'true');
    await expect(box.locator('#v3-fb-title')).toHaveText('Give Feedback');
    await expect(box.locator('[role="radiogroup"] [role="radio"]')).toHaveCount(5);
    await expect(box.locator('[data-fb-type]')).toHaveCount(4);
    await expect(box.locator('[data-fb-send]')).toBeDisabled();
    // Initial focus lands on the first star and Tab stays inside the dialog.
    await expect(box.locator('[data-fb-star="1"]')).toBeFocused();
    for (let i = 0; i < 12; i += 1) await page.keyboard.press('Tab');
    const inside = await page.evaluate(() => Boolean(document.activeElement?.closest('#v3-feedback-dialog')));
    expect(inside).toBe(true);

    await box.locator('[data-fb-message]').fill('x');
    await expect(box.locator('[data-fb-send]')).toBeEnabled();
    await box.locator('[data-fb-message]').fill('');
    await expect(box.locator('[data-fb-send]')).toBeDisabled();
    await box.locator('[data-fb-star="3"]').click();
    await expect(box.locator('[data-fb-send]')).toBeEnabled();
  });

  test('stars and type chips are single-select and keyboard operable', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    await row(page).locator('[data-feedback-open]').click();
    const box = dialog(page);
    await box.locator('[data-fb-star="4"]').click();
    await expect(box.locator('[data-fb-star="4"]')).toHaveAttribute('aria-checked', 'true');
    // Hover previews the rating under the pointer; move it away so the lit
    // count reflects the committed rating.
    await page.mouse.move(0, 0);
    await expect(box.locator('.v3-fb-star.is-on')).toHaveCount(4);
    await page.keyboard.press('ArrowRight');
    await expect(box.locator('[data-fb-star="5"]')).toHaveAttribute('aria-checked', 'true');
    await expect(box.locator('.v3-fb-star.is-on')).toHaveCount(5);
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowLeft');
    await expect(box.locator('[data-fb-star="3"]')).toHaveAttribute('aria-checked', 'true');
    await expect(box.locator('.v3-fb-star.is-on')).toHaveCount(3);
    await expect(box.locator('[role="radio"][aria-checked="true"]')).toHaveCount(1);

    await box.locator('[data-fb-type="report_bug"]').click();
    await expect(box.locator('[data-fb-type="report_bug"]')).toHaveAttribute('aria-pressed', 'true');
    await box.locator('[data-fb-type="other"]').click();
    await expect(box.locator('[data-fb-type="other"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(box.locator('[data-fb-type="report_bug"]')).toHaveAttribute('aria-pressed', 'false');
    await expect(box.locator('.v3-fb-chip.is-selected')).toHaveCount(1);
  });

  test('Send posts rating, type and message as one event, closes, and marks the trigger', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    await row(page).locator('[data-feedback-open]').click();
    const box = dialog(page);
    await box.locator('[data-fb-star="5"]').click();
    await box.locator('[data-fb-type="ui_bug"]').click();
    await box.locator('[data-fb-message]').fill('  The legend overlaps the axis.  ');
    await box.locator('[data-fb-send]').click();
    await expect(page.locator('#v3-feedback-dialog')).toHaveCount(0);
    await expect(page.locator('body')).not.toHaveClass(/v3-feedback-dialog-open/);

    const sent = await events(page);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ rating: 5, feedback_type: 'ui_bug', message: 'The legend overlaps the axis.' });
    expect(sent[0].thumb).toBeUndefined();
    await expect(row(page).locator('[data-feedback-open]')).toHaveClass(/is-sent/);
    // Focus returns to the (re-rendered) trigger for keyboard users.
    await expect(row(page).locator('[data-feedback-open]')).toBeFocused();
  });

  test('Cancel, Escape and an overlay click close without posting', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    const open = row(page).locator('[data-feedback-open]');

    await open.click();
    await dialog(page).locator('[data-fb-cancel]').click();
    await expect(page.locator('#v3-feedback-dialog')).toHaveCount(0);

    await open.click();
    await page.keyboard.press('Escape');
    await expect(page.locator('#v3-feedback-dialog')).toHaveCount(0);
    await expect(open).toBeFocused();

    await open.click();
    await page.mouse.click(5, 5);
    await expect(page.locator('#v3-feedback-dialog')).toHaveCount(0);

    expect(await events(page)).toEqual([]);
  });

  test('a failed Send keeps the dialog open with an error and no thumb change', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    await page.evaluate(() => { window.__ANSWER_FEEDBACK_FAILS__ = true; });
    await row(page).locator('[data-feedback-open]').click();
    const box = dialog(page);
    await box.locator('[data-fb-star="2"]').click();
    await box.locator('[data-fb-send]').click();
    await expect(box).toBeVisible();
    await expect(box.locator('[role="alert"]')).toBeVisible();
    await expect(box.locator('[data-fb-send]')).toBeEnabled();
    await expect(thumb(page, 'thumbs_down')).toHaveAttribute('aria-pressed', 'false');
  });

  test('a new conversation closes an open dialog', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlByYear);
    await row(page).locator('[data-feedback-open]').click();
    await expect(dialog(page)).toBeVisible();
    await page.evaluate(() => window.WorkspaceController.reset());
    await expect(page.locator('#v3-feedback-dialog')).toHaveCount(0);
  });
});
