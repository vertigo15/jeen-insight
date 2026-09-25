// @ts-check
// Previous answers in the conversation thread stay collapsed (question, run
// strip, summary); Key insights, follow-up chips and the feedback row are only
// painted on the selected turn. Clicking "Show answer" on an older card selects
// it and expands it, collapsing the one that was open. SQL and ML answers share
// the rule.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask } = require('./_helpers');

const turns = (page) => page.locator('#v3-thread article.v3-turn');

/**
 * Give every successful turn saved findings + follow-ups (what the eval step
 * produces live) so the expanded/collapsed assertions do not depend on which
 * fixture happens to carry them. `restored` mimics a hydrated conversation
 * whose rows were not kept.
 */
async function enrich(page, { restored = false } = {}) {
  await page.evaluate((asRestored) => {
    const wc = window.WorkspaceController;
    wc.turns.forEach((turn) => {
      if (turn.status !== 'success' || !turn.result || turn.result.proposal) return;
      turn.result.findings = ['A saved finding.', 'Another saved finding.'];
      turn.result.followups = ['A saved follow-up?'];
      if (asRestored) {
        turn.restored = true;
        turn.artifactState = 'missing';
        turn.snapshotStatus = 'pruned';
        turn.result.results = null;
      }
    });
    wc.render();
  }, restored);
}

async function expectCollapsed(turn) {
  await expect(turn).not.toHaveClass(/is-selected/);
  await expect(turn.locator('.v3-summary').first()).toBeVisible();
  await expect(turn.locator('.v3-insights')).toBeHidden();
  await expect(turn.locator('.v3-followups')).toBeHidden();
  await expect(turn.locator('.v3-feedback')).toBeHidden();
}

async function expectExpanded(turn) {
  await expect(turn).toHaveClass(/is-selected/);
  await expect(turn.locator('.v3-insights .v3-finding')).toHaveCount(2);
  await expect(turn.locator('.v3-followups .v3-chip')).toHaveCount(1);
  await expect(turn.locator('.v3-followups .v3-chip').first()).toBeVisible();
}

test('the newest answer is expanded and every previous one is collapsed', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.sqlByYear);
  await enrich(page);
  await expectExpanded(turns(page).nth(0));

  await ask(page, Q.sqlAggregate);
  await enrich(page);
  await expect(turns(page)).toHaveCount(2);
  await expectCollapsed(turns(page).nth(0));
  await expectExpanded(turns(page).nth(1));
});

test('"Show answer" on a previous turn expands it and collapses the open one', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.sqlByYear);
  await ask(page, Q.sqlAggregate);
  await enrich(page);
  const first = turns(page).nth(0);
  const second = turns(page).nth(1);

  await first.hover();
  await expect(first).toHaveAttribute('data-show-label', 'Show answer');
  await first.click();
  await expectExpanded(first);
  await expectCollapsed(second);

  // Keyboard users get the same behaviour from the focusable card.
  await second.focus();
  await page.keyboard.press('Enter');
  await expectExpanded(second);
  await expectCollapsed(first);
});

test('an ML answer collapses and expands like a SQL answer', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.sqlByYear);
  await ask(page, Q.anomaly);
  await enrich(page);
  const sql = turns(page).nth(0);
  const ml = turns(page).nth(1);
  await expect(ml).toHaveAttribute('data-route-path', 'ml');

  // The ML card is the selected turn; the SQL answer behind it is collapsed.
  await expect(ml).toHaveClass(/is-selected/);
  await expectCollapsed(sql);

  await sql.click();
  await expectExpanded(sql);
  await expect(ml).not.toHaveClass(/is-selected/);
});

test('a hydrated conversation opens with only the newest turn expanded', async ({ page }) => {
  await openHarness(page);
  await ask(page, Q.sqlByYear);
  await ask(page, Q.sqlAggregate);
  // Saved findings / follow-ups render on restored turns even without rows
  // (PR #66) — but still only on the selected one.
  await enrich(page, { restored: true });
  const first = turns(page).nth(0);
  const second = turns(page).nth(1);
  await expect(second).toHaveClass(/is-restored/);
  await expectCollapsed(first);
  await expectExpanded(second);

  await first.click();
  await expectExpanded(first);
  await expectCollapsed(second);
});
