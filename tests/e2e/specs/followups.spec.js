// @ts-check
// Follow-up questions are long, so they stack as full-width rows instead of a
// wrapping row of uneven pills.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn } = require('./_helpers');

test('follow-up questions stack as full-width rows', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.sqlByYear);
  const list = lastTurn(page).locator('.v3-followups');
  const chips = list.locator('.v3-chip');
  await expect(chips).toHaveCount(2);

  const listBox = await list.boundingBox();
  const boxes = await Promise.all([chips.nth(0).boundingBox(), chips.nth(1).boundingBox()]);
  for (const box of boxes) {
    expect(Math.abs(box.width - listBox.width)).toBeLessThan(1);
  }
  expect(boxes[1].y).toBeGreaterThan(boxes[0].y + boxes[0].height - 1);
  await expect(chips.first()).toHaveCSS('border-radius', '10px');
});
