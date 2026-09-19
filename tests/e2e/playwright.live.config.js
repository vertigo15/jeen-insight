// @ts-check
// LIVE suite: real SQL and ML questions driven through the running UI on
// :8501 (Docker stack → BFF → API → LLM + data source). Nothing is stubbed.
//
//   cd tests/e2e && npm run test:live          # e2e (default)
//   npm run test:live:smoke | test:live:features | test:live:regression | test:live:security
//   npm run test:live:kp | test:live:conv | test:live:ml | test:live:sql | test:live:admin
//
// Env: LIVE_APP_URL (http://localhost:8501), LIVE_EMAIL (admin),
// LIVE_PASSWORD (admin), LIVE_CONNECTION (AdventureWorksDW display name),
// LIVE_ONLY=<tag[,tag…]> (area tags kp|conv|ml|sql|resilience|features|history|
// extras|admin or type tags smoke|e2e|feature|regression|security), LIVE_RETRIES,
// LIVE_KP_ALL / LIVE_KP_LIMIT / LIVE_KP_STRICT / LIVE_GOLD_DSN (knowledge pairs),
// LIVE_MIN_PASS_RATE / LIVE_MIN_SQL_EQUIV / LIVE_MAX_P95_MS (scorecard gates).
// The whole suite skips itself when the stack is not reachable, so it is safe
// to leave in `npm test`-adjacent tooling; it is deliberately NOT part of CI.
const { defineConfig, devices } = require('@playwright/test');
const path = require('path');

const BASE = process.env.LIVE_APP_URL || 'http://localhost:8501';
const AUTH_FILE = path.join(__dirname, '.auth', 'live.json');
const ONLY = (process.env.LIVE_ONLY || '').split(',').map((t) => t.trim().replace(/^@/, '')).filter(Boolean);

module.exports = defineConfig({
  testDir: './live',
  // One or more tags: a test runs when it carries ANY of them.
  grep: ONLY.length ? new RegExp(`@(${ONLY.join('|')})\\b`) : undefined,
  // Serial: one LLM conversation at a time, the analytics sandbox allows two
  // concurrent runs, and the tests share a login session.
  fullyParallel: false,
  workers: 1,
  retries: process.env.LIVE_RETRIES ? Number(process.env.LIVE_RETRIES) : 1,
  // An ML run is 60–120 s end to end; a forecast test also re-runs.
  timeout: 240_000,
  expect: { timeout: 20_000 },
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report-live', open: 'never' }],
    ['junit', { outputFile: 'test-results-live/junit.xml' }],
    [require.resolve('./live/scorecard.reporter.js'), { outputDir: 'test-results-live' }],
  ],
  outputDir: 'test-results-live',
  globalSetup: require.resolve('./live/globalSetup.js'),
  use: {
    baseURL: BASE,
    viewport: { width: 1440, height: 900 },
    actionTimeout: 20_000,
    navigationTimeout: 60_000,
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    // Feature tests read what Copy put on the clipboard.
    permissions: ['clipboard-read', 'clipboard-write'],
    acceptDownloads: true,
  },
  projects: [
    { name: 'setup', testMatch: /auth\.setup\.js/ },
    {
      name: 'live',
      testMatch: /.*\.live\.spec\.js/,
      dependencies: ['setup'],
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, storageState: AUTH_FILE },
    },
  ],
});
