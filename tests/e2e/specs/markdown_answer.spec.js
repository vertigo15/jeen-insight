// @ts-check
// Trusted text answers (the capability route) render as safe, formatted HTML;
// everything else stays escaped plain text. Also exercises the MarkdownLite
// parser directly for XSS / malformed input.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn } = require('./_helpers');

test.beforeEach(async ({ page }) => { await openHarness(page); });

test('capability answer renders Markdown in the conversation card', async ({ page }) => {
  await ask(page, Q.capability);
  const md = lastTurn(page).locator('.v3-markdown');
  await expect(md).toHaveCount(1);
  await expect(md.locator('h3')).toHaveText('Time Series (aggregates only)');
  await expect(md.locator('ul li')).toHaveCount(2);
  await expect(md.locator('li strong').first()).toHaveText('Anomaly detection');
  await expect(md.locator('table')).toHaveCount(1);
  // A mixed Hebrew/English table cell is preserved.
  await expect(md.locator('th', { hasText: 'אלגוריתם' })).toHaveCount(1);
  await expect(md.locator('code')).toHaveText('sales_amount');
});

test('capability answer renders Markdown in the result pane', async ({ page }) => {
  await ask(page, Q.capability);
  const paneMd = page.locator('#v3-placeholder .v3-markdown');
  await expect(paneMd).toHaveCount(1);
  await expect(paneMd.locator('ul li')).toHaveCount(2);
  await expect(paneMd.locator('table tbody tr')).toHaveCount(1);
  // Bold labels stay inline with their description (the pane title style must not leak in).
  const label = paneMd.locator('li strong').first();
  await expect(label).toHaveCSS('display', 'inline');
  await expect(label).toHaveCSS('font-weight', '600');
});

test('a plain-text answer in the result pane reads as body text', async ({ page }) => {
  await ask(page, Q.greeting);
  const answer = page.locator('#v3-placeholder .v3-text-answer');
  await expect(answer).toHaveText('Hello! Ask me anything about your data.');
  await expect(answer).toHaveCSS('font-size', '14.5px');
  await expect(answer).toHaveCSS('text-align', 'start');
});

test('an embedded HTML/script payload is escaped, never executed', async ({ page }) => {
  let dialogFired = false;
  page.on('dialog', (d) => { dialogFired = true; d.dismiss(); });
  await ask(page, Q.capability);
  const md = lastTurn(page).locator('.v3-markdown');
  // No real <script>/<img> element is created from the answer text.
  await expect(md.locator('script')).toHaveCount(0);
  await expect(md.locator('img')).toHaveCount(0);
  // The payload survives as visible, escaped text.
  await expect(md).toContainText('<script>alert(1)</script>');
  expect(dialogFired).toBe(false);
});

test('a non-Markdown route (greeting) stays escaped plain text', async ({ page }) => {
  await ask(page, Q.greeting);
  const turn = lastTurn(page);
  await expect(turn.locator('.v3-markdown')).toHaveCount(0);
  await expect(turn.locator('.v3-summary')).toHaveCount(1);
});

test('MarkdownLite parser: escaping, structure and malformed input', async ({ page }) => {
  const out = await page.evaluate(() => {
    const R = window.MarkdownLite.render;
    return {
      heading: R('# One\n## Two\n### Three'),          // -> h2/h3/h4, never h1
      script: R('<script>alert(1)</script>'),
      attr: R('**b" onerror="x**'),
      underscore: R('customer_key_id and sales_amount'),
      fence: R('```\n<b>x</b>\n```'),
      badTable: R('| A | B |\n|---|\n| 1 |'),           // ragged, must not throw
      emphasisRun: R('****'.repeat(50)),                 // pathological, must not hang
    };
  });
  expect(out.heading).toContain('<h2>One</h2>');
  expect(out.heading).toContain('<h4>Three</h4>');
  expect(out.heading).not.toContain('<h1');
  expect(out.script).not.toContain('<script>');
  expect(out.script).toContain('&lt;script&gt;');
  expect(out.attr).toContain('<strong>');
  expect(out.attr).toContain('&quot;');
  expect(out.underscore).not.toContain('<em>');        // underscores are not emphasis
  expect(out.fence).toContain('<pre><code>&lt;b&gt;x&lt;/b&gt;');
  expect(typeof out.badTable).toBe('string');
  expect(typeof out.emphasisRun).toBe('string');
});
