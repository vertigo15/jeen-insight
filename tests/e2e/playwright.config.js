// @ts-check
const { defineConfig, devices } = require('@playwright/test');
const path = require('path');

// The harness is plain static files served from the repo root, so URLs like
// /src/static/... and /tests/e2e/harness/... resolve without a build step.
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const PORT = process.env.E2E_PORT ? Number(process.env.E2E_PORT) : 8099;
const HARNESS_URL = `http://127.0.0.1:${PORT}/tests/e2e/harness/index.html`;

module.exports = defineConfig({
  testDir: './specs',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['line']] : [['list']],
  // Regenerate the routing truth table from the real backend before the run.
  globalSetup: require.resolve('./globalSetup.js'),
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    // Record a video for every test and keep a screenshot on failure. The
    // gallery spec also writes labeled full-page screenshots to ./screenshots.
    video: 'on',
    screenshot: 'only-on-failure',
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: `python3 -m http.server ${PORT} --bind 127.0.0.1`,
    cwd: REPO_ROOT,
    url: HARNESS_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
