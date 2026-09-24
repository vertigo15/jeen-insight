// @ts-check
const { test, expect } = require('@playwright/test');
const { openHarness } = require('./_helpers');

const MODELS = [
  {
    id: 1,
    name: 'gpt-5.4-nano',
    display_name: 'GPT 5.4 Nano',
    description: 'Ultra-low latency Q&A',
    available: true,
    is_active: false,
    is_default: false,
  },
  {
    id: 2,
    name: 'gpt-5.3-azure',
    display_name: 'GPT 5.3 - Azure OpenAI',
    description: 'Advanced reasoning and comprehensive task handling',
    available: true,
    is_active: true,
    is_default: true,
  },
  {
    id: 3,
    name: 'o1-mini-azure',
    display_name: 'O1 Mini - Azure OpenAI',
    description: 'Scientific research with a smaller footprint',
    available: false,
    is_active: false,
    is_default: false,
  },
];

test.beforeEach(async ({ page }) => {
  await openHarness(page);
  await page.evaluate(async (models) => {
    window._currentUser = { name: 'Admin', email: 'admin@jeen.ai', role: 'admin' };
    const fallbackFetch = window.fetch;
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url === '/api/settings/models') {
        return new Response(JSON.stringify(models), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.startsWith('/api/settings/models/health')) {
        return new Response(JSON.stringify({
          checked_age_seconds: 0,
          healthy_count: 2,
          failing_count: 0,
          skipped_count: 1,
          models: [],
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return fallbackFetch(input, init);
    };

    const { SettingsPage } = await import('/src/static/settings/settingsPage.js');
    const settings = new SettingsPage();
    settings.mount();
    settings.open();
    document.querySelector('.sp-nav-item[data-id="ai-models"]').click();
  }, MODELS);

  await expect(page.locator('.sp-model-card')).toHaveCount(3);
});

test('model search matches plain text and name, description, or display name', async ({ page }) => {
  const search = page.locator('#sp-models-search');

  await search.fill('scientific');
  await expect(page.locator('.sp-model-card')).toHaveCount(1);
  await expect(page.locator('.sp-model-card')).toContainText('O1 Mini');

  await search.fill('gpt-5.4-nano');
  await expect(page.locator('.sp-model-card')).toHaveCount(1);
  await expect(page.locator('.sp-model-card')).toContainText('GPT 5.4 Nano');
});

test('model search supports star and question-mark wildcards', async ({ page }) => {
  const search = page.locator('#sp-models-search');

  await search.fill('GPT 5.? - Azure*');
  await expect(page.locator('.sp-model-card')).toHaveCount(1);
  await expect(page.locator('.sp-model-card')).toContainText('GPT 5.3 - Azure OpenAI');

  await search.fill('*Azure OpenAI');
  await expect(page.locator('.sp-model-card')).toHaveCount(2);

  await search.fill('does-not-exist*');
  await expect(page.locator('.sp-model-card')).toHaveCount(0);
  await expect(page.locator('.sp-models-empty')).toHaveText('No models match this search.');
});
