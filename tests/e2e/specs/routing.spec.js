// @ts-check
// The core question the user asked: "how do I know when the app runs ML vs SQL?"
// Every canonical question is driven through the real controller and the visible
// path badge is asserted against the REAL backend routing prediction.
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask, lastTurn, routingTruth } = require('./_helpers');

function expectedPath(wouldRoute) {
  if (wouldRoute === 'needs_analysis') return 'ml';
  if (wouldRoute === 'greeting') return 'greeting';
  if (wouldRoute === 'capability') return 'capability';
  return 'sql'; // needs_query and router_decides both surface as SQL at run time
}

test.beforeEach(async ({ page }) => { await openHarness(page); });

const CASES = [
  ['SQL: aggregate lookup', Q.sqlAggregate],
  ['SQL: count lookup', Q.sqlCount],
  ['ML: forecast cue', Q.forecast],
  ['ML: anomaly cue', Q.anomaly],
  ['ML: forecast cue (short history)', Q.guard],
  ['ML: correlation cue', Q.clarify],
  ['capability: which ML models', Q.capability],
  ['greeting', Q.greeting],
];

for (const [name, question] of CASES) {
  test(`routes "${name}" to the path the backend predicts`, async ({ page }) => {
    const truth = await routingTruth(page);
    const predicted = expectedPath(truth[question].would_route);

    await ask(page, question);
    const turn = lastTurn(page);
    await expect(turn).toHaveAttribute('data-route-path', predicted);

    if (predicted === 'ml') {
      await expect(turn.locator('.v3-route-pill.is-ml')).toHaveText('ML skill');
    } else if (predicted === 'sql') {
      await expect(turn.locator('.v3-route-pill.is-sql')).toHaveText('SQL');
    } else {
      // Greetings and capability answers never SQL and never an ML card: no path pill.
      await expect(turn.locator('.v3-route-pill')).toHaveCount(0);
    }
  });
}

test('the routing dry-run endpoint agrees with the badge (no LLM call)', async ({ page }) => {
  const check = async (q) => page.evaluate(async (question) => {
    const r = await fetch(`/api/analysis/routing?q=${encodeURIComponent(question)}`);
    return r.json();
  }, q);

  expect((await check(Q.forecast)).would_route).toBe('needs_analysis');
  expect((await check(Q.forecast)).skill_hint).toBe('forecast');
  expect((await check(Q.sqlAggregate)).would_route).toBe('router_decides');
  expect((await check(Q.greeting)).would_route).toBe('greeting');
});
