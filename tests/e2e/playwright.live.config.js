// @ts-check
// LIVE suite: real SQL and ML questions driven through the running UI on
// :8501 (Docker stack → BFF → API → LLM + data source). Nothing is stubbed.
//
//   cd tests/e2e && npm run test:live          # everything
//   npm run test:live:sql | npm run test:live:ml
//
// Env: LIVE_APP_URL (http://localhost:8501), LIVE_EMAIL (admin),
// LIVE_PASSWORD (admin), LIVE_CONNECTION (AdventureWorksDW display name),
// LIVE_ONLY=sql|ml|resilience (tag filter), LIVE_RETRIES.
// The whole suite skips itself when the stack is not reachable, so it is safe
// to leave in `npm test`-adjacent tooling; it is deliberately NOT part of CI.
const { defineConfig, devices } = require('@playwright/test');
const path = require('path');

const BASE = process.env.LIVE_APP_URL || 'http://localhost:8501';
const AUTH_FILE = path.join(__dirname, '.auth', 'live.json');
const ONLY = (process.env.LIVE_ONLY || '').trim().replace(/^@/, '');

module.exports = defineConfig({
  testDir: './live',
  grep: ONLY ? new RegExp(`@${ONLY}\\b`) : undefined,
  // Serial: one LLM conversation at a time, the analytics sandbox allows two
  // concurrent runs, and the tests share a login session.
  fullyParallel: false,
  workers: 1,
  retries: process.env.LIVE_RETRIES ? Number(process.env.LIVE_RETRIES) : 1,
  // An ML run is 60–120 s end to end; a forecast test also re-runs.
  timeout: 240_000,
  expect: { timeout: 20_000 },
  reporter: [['list'], ['html', { outputFolder: 'playwright-report-live', open: 'never' }]],
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
