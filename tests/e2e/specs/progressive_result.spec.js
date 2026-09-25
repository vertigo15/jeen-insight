// @ts-check
// Progressive answers: a SQL question streams its rows (`partial` SSE event)
// before the summary / insights / follow-ups (`result`). The table is painted
// and selectable while the narrative phases still run; the final result then
// fills the card in place without repainting the grid.
const { test, expect } = require('@playwright/test');
const { Q, openHarness } = require('./_helpers');

const turns = (page) => page.locator('#v3-thread article.v3-turn');

/** Fire a question without awaiting the stream (it is held after `partial`). */
async function askHeld(page, question) {
  await page.evaluate((q) => {
    window.__holdFinalResult = true;
    window.__ask(q);
  }, question);
  await page.waitForFunction(() => {
    const wc = window.WorkspaceController;
    const turn = wc && wc.turns[wc.turns.length - 1];
    return Boolean(turn && turn.status === 'streaming');
  });
}

async function release(page) {
  await page.evaluate(() => {
    window.__holdFinalResult = false;
    window.__releaseFinalResult();
  });
  await page.waitForFunction(() => {
    const wc = window.WorkspaceController;
    const turn = wc && wc.turns[wc.turns.length - 1];
    return Boolean(turn && turn.status === 'success');
  });
}

test('the table is shown while the summary and insights are still being written', async ({ page }) => {
  await openHarness(page);
  await askHeld(page, Q.sqlByYear);

  const turn = turns(page).last();
  await expect(turn).toHaveClass(/is-streaming/);
  await expect(turn.locator('.v3-data-ready')).toContainText('4 rows');
  await expect(turn.locator('.v3-skeleton-group')).toBeVisible();
  await expect(turn.locator('.v3-summary')).toHaveCount(0);
  await expect(turn.locator('.v3-insights')).toHaveCount(0);

  // The answer pane already has the rows and says the analysis is pending.
  await expect(page.locator('#v3-table-block')).toBeVisible();
  await expect(page.locator('#v3-grid .v3-grid-row:not(.v3-grid-head)')).toHaveCount(4);
  await expect(page.locator('#v3-meta-row .v3-status')).toHaveClass(/is-streaming/);
  await expect(page.locator('#v3-meta-row .v3-status')).toHaveText('Analysing');
  // Time to table is already known; insights and chart are not.
  await expect(turn.locator('.v3-timeline [data-timeline="table"]')).toContainText('table');
  await expect(turn.locator('.v3-timeline [data-timeline="insights"]')).toHaveCount(0);

  await release(page);
});

test('the run strip shows when the table, the insights and the chart appeared', async ({ page }) => {
  await openHarness(page);
  await askHeld(page, Q.sqlByYear);
  await release(page);

  const turn = turns(page).last();
  const timeline = turn.locator('.v3-run-strip .v3-timeline');
  await expect(timeline.locator('[data-timeline="table"]')).toContainText(/table \d/);
  await expect(timeline.locator('[data-timeline="insights"]')).toContainText(/insights \d/);
  await expect(timeline.locator('[data-timeline="chart"]')).toHaveCount(0);

  // The chart manager announces its first render; the strip gains "chart".
  await page.evaluate(() => {
    const wc = window.WorkspaceController;
    const turn = wc.turns[wc.turns.length - 1];
    document.dispatchEvent(new CustomEvent('jeen:chart-rendered', { detail: { queryId: turn.result.query_id, kind: 'bar' } }));
  });
  await expect(timeline.locator('[data-timeline="chart"]')).toContainText(/chart \d/);

  const ms = await page.evaluate(() => {
    const wc = window.WorkspaceController;
    return { ...wc.turns[wc.turns.length - 1].timeline };
  });
  expect(ms.tableMs).toBeLessThanOrEqual(ms.insightsMs);
  expect(ms.insightsMs).toBeLessThanOrEqual(ms.chartMs);
});

test('the final result fills the card without repainting the grid', async ({ page }) => {
  await openHarness(page);
  await askHeld(page, Q.sqlByYear);
  await page.evaluate(() => {
    const first = document.querySelector('#v3-grid .v3-grid-row:not(.v3-grid-head)');
    if (first) first.setAttribute('data-e2e-painted', 'before-result');
  });

  await release(page);

  const turn = turns(page).last();
  await expect(turn).not.toHaveClass(/is-streaming/);
  await expect(turn.locator('.v3-skeleton-group')).toHaveCount(0);
  await expect(turn.locator('.v3-summary').first()).toContainText('Bikes sales grew');
  await expect(turn.locator('.v3-followups .v3-chip')).toHaveCount(2);
  await expect(page.locator('#v3-meta-row .v3-status')).toHaveText('Completed');
  await expect(page.locator('#v3-meta-row .v3-status')).not.toHaveClass(/is-streaming/);
  // Same rows, same DOM: the marker set during streaming survived the merge.
  await expect(page.locator('#v3-grid [data-e2e-painted="before-result"]')).toHaveCount(1);
  const state = await page.evaluate(() => {
    const wc = window.WorkspaceController;
    const turn = wc.turns[wc.turns.length - 1];
    return { rev: turn.rev, provisional: turn.provisionalRevision, rows: turn.result.results.rows.length };
  });
  expect(state).toEqual({ rev: 0, provisional: null, rows: 4 });
});

test('a streaming turn can be re-selected and a second question still works', async ({ page }) => {
  await openHarness(page);
  await askHeld(page, Q.sqlByYear);
  const turn = turns(page).last();
  await turn.hover();
  await expect(turn).toHaveAttribute('data-show-label', 'Show answer');
  await release(page);

  await page.evaluate((q) => window.__ask(q), Q.sqlAggregate);
  await expect(turns(page)).toHaveCount(2);
  await expect(turns(page).nth(1)).toHaveClass(/is-selected/);
  await expect(turns(page).nth(1)).not.toHaveClass(/is-streaming/);
});
