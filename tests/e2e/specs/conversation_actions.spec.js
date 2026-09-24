// @ts-check
const { test, expect } = require('@playwright/test');
const { Q, openHarness, ask } = require('./_helpers');

test.describe('conversation and favorite actions', () => {
  test('New conversation stays visible after a long thread is scrolled', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 700 });
    await openHarness(page);
    for (let i = 0; i < 7; i += 1) {
      await ask(page, i % 2 ? Q.sqlCount : Q.sqlAggregate);
    }

    const thread = page.locator('#v3-thread');
    await thread.evaluate((node) => { node.scrollTop = node.scrollHeight; });
    const button = page.locator('#v3-new-conversation');
    await expect(button).toBeVisible();
    await expect(button).toBeEnabled();

    const [panelBox, buttonBox] = await Promise.all([
      page.locator('#v3-conversation').boundingBox(),
      button.boundingBox(),
    ]);
    expect(panelBox).not.toBeNull();
    expect(buttonBox).not.toBeNull();
    expect(buttonBox.y).toBeLessThan(panelBox.y + 60);

    await button.click();
    await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(0);
    await expect(page.locator('#v3-thread .v3-thread-empty')).toBeVisible();
  });

  test('selected answer can be favorited, reopened, and removed from Saved', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlAggregate);

    const favorite = page.locator('#v3-favorite-action');
    await expect(favorite).toBeVisible();
    await expect(favorite).toContainText('Save answer');
    await expect(favorite).toHaveAttribute('aria-pressed', 'false');
    await favorite.click();
    await expect(favorite).toHaveAttribute('aria-pressed', 'true');
    await expect(favorite).toContainText('Saved');
    await expect(page.locator('#v3-thread .v3-turn-favorite')).toHaveCount(1);

    await page.locator('[data-rail="saved"]').click();
    await expect(page.locator('[data-saved-view="answers"]')).toHaveAttribute('aria-selected', 'true');
    const saved = page.locator('.v3-favorite-item');
    await expect(saved).toHaveCount(1);
    await expect(saved).toContainText(Q.sqlAggregate);

    await saved.click();
    await expect(page.locator('#v3-panel-conversation')).toBeVisible();
    await expect.poll(() => page.evaluate(() => {
      const ctrl = window.ChatController;
      const turn = ctrl.turns.find((item) => item.id === ctrl.selectedTurnId);
      return turn && turn.question;
    })).toBe(Q.sqlAggregate);

    await page.locator('[data-rail="saved"]').click();
    await page.locator('.v3-favorite-item [data-favorite-remove]').click();
    await expect(page.locator('.v3-favorite-item')).toHaveCount(0);
    await expect(page.locator('.v3-saved-empty')).toBeVisible();
  });

  test('New conversation cancels an active answer and ignores its late response', async ({ page }) => {
    await openHarness(page);
    await page.evaluate(async () => {
      const baseFetch = window.fetch;
      window.__answerRequestAborted = false;
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.includes('/api/ask/stream')) {
          return new Promise((resolve, reject) => {
            init.signal.addEventListener('abort', () => {
              window.__answerRequestAborted = true;
              reject(new DOMException('Aborted', 'AbortError'));
            }, { once: true });
          });
        }
        return baseFetch(input, init);
      };
      void window.ChatController.send('A deliberately delayed answer');
    });
    await expect.poll(() => page.evaluate(() => window.ChatController.sending)).toBe(true);
    const button = page.locator('#v3-new-conversation');
    await expect(button).toBeEnabled();
    await expect(button).toHaveAttribute('title', 'Stop current work and start a new conversation');
    await button.click();
    await expect.poll(() => page.evaluate(() => window.__answerRequestAborted)).toBe(true);
    await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => window.ChatController.sending)).toBe(false);
  });

  test('New conversation aborts a restored-data rerun', async ({ page }) => {
    await openHarness(page);
    await page.evaluate(async () => {
      const dto = {
        turn_id: '22222222-2222-2222-2222-222222222222',
        sequence_number: 1,
        question: 'Restored answer',
        sql: 'SELECT 1',
        execution_status: 'success',
        result_kind: 'table',
        answer: 'one',
        metrics: {},
        findings: [],
        suggestions: [],
        followups: [],
        snapshot_status: 'pruned',
        has_chart: false,
        has_rerunnable_query: true,
      };
      const ctrl = window.ChatController;
      const hydration = await import('/src/static/workspace/conversationHydration.js');
      const turn = hydration.turnFromServer(dto, '11111111-1111-1111-1111-111111111111');
      ctrl.turns = [turn];
      ctrl.conversation = { id: turn.conversationId, title: turn.question, source_key: 'sales_db' };
      ctrl.selectedTurnId = turn.id;
      ctrl.selectedResultId = turn.id;
      ctrl.render();

      const baseFetch = window.fetch;
      window.__rerunRequestAborted = false;
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : input.url;
        if (/\/rerun$/.test(url)) {
          return new Promise((resolve, reject) => {
            init.signal.addEventListener('abort', () => {
              window.__rerunRequestAborted = true;
              reject(new DOMException('Aborted', 'AbortError'));
            }, { once: true });
          });
        }
        return baseFetch(input, init);
      };
      void ctrl.rerunTurn(turn.id);
    });
    await expect.poll(() => page.evaluate(() => window.ChatController.turns[0]?.rerunning)).toBe(true);
    await page.locator('#v3-new-conversation').click();
    await expect.poll(() => page.evaluate(() => window.__rerunRequestAborted)).toBe(true);
    await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(0);
  });

  test('New conversation aborts an ML analysis rerun', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.forecast);
    await page.locator('#v3-placeholder [data-run]').click();
    await expect(page.locator('#v3-meta-row .v3-skill-chip')).toHaveText('forecast');
    await page.evaluate(() => {
      const baseFetch = window.fetch;
      window.__analysisRerunAborted = false;
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.includes('/api/analysis/rerun')) {
          return new Promise((resolve, reject) => {
            init.signal.addEventListener('abort', () => {
              window.__analysisRerunAborted = true;
              reject(new DOMException('Aborted', 'AbortError'));
            }, { once: true });
          });
        }
        return baseFetch(input, init);
      };
      void window.ChatController.rerunAnalysis('extend the horizon');
    });
    await expect.poll(() => page.evaluate(() => Boolean(window.ChatController._analysisRerunInFlight))).toBe(true);
    await page.locator('#v3-new-conversation').click();
    await expect.poll(() => page.evaluate(() => window.__analysisRerunAborted)).toBe(true);
    await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(0);
  });

  test('mobile RTL drawer keeps localized New conversation and Saved controls visible', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openHarness(page, '?locale=he');
    await page.locator('[data-rail="conversation"]').click();

    const panel = page.locator('#v3-conversation');
    const button = page.locator('#v3-new-conversation');
    await expect(panel).toBeVisible();
    await expect(button).toHaveText(/שיחה חדשה/);
    await expect(page.locator('[data-rail="saved"]')).toHaveAttribute('aria-label', 'תשובות ושאלות שמורות');

    const [box, railBox] = await Promise.all([
      panel.boundingBox(),
      page.locator('.v3-rail').boundingBox(),
    ]);
    expect(box).not.toBeNull();
    expect(railBox).not.toBeNull();
    expect(railBox.x).toBeGreaterThan(box.x);

    await page.locator('[data-rail="saved"]').click();
    await expect(page.locator('[data-saved-view="answers"]')).toHaveText('תשובות');
    await expect(page.locator('[data-saved-view="questions"]')).toHaveText('שאלות');
    await page.locator('[data-saved-view="answers"]').focus();
    await page.keyboard.press('ArrowLeft');
    await expect(page.locator('[data-saved-view="questions"]')).toHaveAttribute('aria-selected', 'true');
    await expect(button).toBeVisible();
  });

  test('Hebrew answer actions use Save and Saved terminology', async ({ page }) => {
    await openHarness(page, '?locale=he');
    await ask(page, Q.sqlAggregate);
    const save = page.locator('#v3-favorite-action');
    await expect(save).toContainText('שמירת התשובה');
    await save.click();
    await expect(save).toContainText('נשמר');
    await expect.poll(() => page.evaluate(() => (window.__toasts || []).at(-1)?.msg)).toBe('התשובה נשמרה');
  });

  test('a delayed favorite response cannot overwrite another selected answer', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlAggregate);
    await ask(page, Q.sqlCount);
    const turns = page.locator('#v3-thread article.v3-turn');
    await turns.first().click();

    await page.evaluate(() => {
      const baseFetch = window.fetch;
      let release;
      window.__releaseFavorite = () => release?.();
      window.fetch = async (input, init) => {
        const url = typeof input === 'string' ? input : input.url;
        if (/\/favorite$/.test(url) && (init?.method || 'GET') === 'PUT') {
          await new Promise((resolve) => { release = resolve; });
        }
        return baseFetch(input, init);
      };
    });
    await page.locator('#v3-favorite-action').click();
    await turns.last().click();
    await expect(page.locator('#v3-result-title')).toHaveText(Q.sqlCount);
    await page.evaluate(() => window.__releaseFavorite());
    await expect.poll(() => page.evaluate(() => window.ChatController.turns[0].isFavorite)).toBe(true);
    await expect(page.locator('#v3-favorite-action')).toHaveAttribute('aria-pressed', 'false');
  });

  test('an unavailable saved turn shows persistent recovery and can be removed', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlAggregate);
    await page.locator('#v3-favorite-action').click();
    await ask(page, Q.sqlCount);
    await page.locator('[data-rail="saved"]').click();
    await expect(page.locator('.v3-favorite-item')).toHaveCount(1);

    await page.evaluate(() => {
      const baseFetch = window.fetch;
      window.fetch = async (input, init) => {
        const url = typeof input === 'string' ? input : input.url;
        const parsed = new URL(url, location.origin);
        if (/\/api\/conversations\/[^/]+\/turns\/[^/]+$/.test(parsed.pathname)) {
          return new Response(JSON.stringify({ detail: 'gone' }), { status: 404, headers: { 'Content-Type': 'application/json' } });
        }
        if (/\/api\/conversations\/[^/]+$/.test(parsed.pathname)) {
          const response = await baseFetch(input, init);
          const detail = await response.json();
          detail.turns = detail.turns.filter((turn) => turn.question === 'How many orders shipped yesterday?');
          return new Response(JSON.stringify(detail), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        return baseFetch(input, init);
      };
    });
    await page.locator('.v3-favorite-item').click();
    await expect.poll(() => page.evaluate(() => window.ChatController.selectedResultId)).toBeNull();
    const unavailable = page.locator('.v3-saved-unavailable');
    await expect(unavailable).toBeVisible();
    await expect(unavailable).toContainText('This saved answer is unavailable.');
    await unavailable.locator('[data-saved-remove]').click();
    await expect(page.locator('[data-panel="saved"]')).toBeVisible();
    await expect(page.locator('.v3-favorite-item')).toHaveCount(0);
  });

  test('Saved cards expose unavailable connections and refresh requirements', async ({ page }) => {
    await openHarness(page);
    await page.evaluate(() => {
      window.__seedFavoriteItems([
        {
          conversation_id: '11111111-1111-1111-1111-111111111111',
          turn_id: 'saved-unavailable',
          sequence_number: 1,
          conversation_title: 'Saved conversation',
          question: 'Unavailable answer',
          answer: 'Saved text',
          result_kind: 'text',
          snapshot_status: 'not_applicable',
          source_key: 'gone',
          source_label: 'Removed source',
          connection_available: false,
          favorited_at: new Date().toISOString(),
        },
        {
          conversation_id: '11111111-1111-1111-1111-111111111111',
          turn_id: 'saved-refresh',
          sequence_number: 2,
          conversation_title: 'Saved conversation',
          question: 'Refresh answer',
          answer: 'Saved table',
          result_kind: 'table',
          snapshot_status: 'pruned',
          source_key: 'sales_db',
          source_label: 'Sales DB',
          connection_available: true,
          favorited_at: new Date().toISOString(),
        },
      ]);
    });
    await page.locator('[data-rail="saved"]').click();
    await expect(page.locator('.v3-favorite-badge', { hasText: 'Connection unavailable' })).toBeVisible();
    await expect(page.locator('.v3-favorite-badge', { hasText: 'Data refresh required' })).toBeVisible();
  });

  test('deleting a conversation with stale saved count requires explicit confirmation', async ({ page }) => {
    await openHarness(page);
    await page.evaluate(() => {
      const conversationId = '11111111-1111-1111-1111-111111111111';
      window.__seedConversations([{
        id: conversationId,
        title: 'Conversation with saved answer',
        source_key: 'sales_db',
        source_label: 'Sales DB',
        connection_available: true,
        turn_count: 1,
        saved_answer_count: 0,
      }]);
      window.__seedFavoriteItems([{
        conversation_id: conversationId,
        turn_id: 'saved-turn',
        sequence_number: 1,
        conversation_title: 'Conversation with saved answer',
        question: 'Saved question',
        answer: 'Saved answer',
        result_kind: 'text',
        snapshot_status: 'not_applicable',
        source_key: 'sales_db',
        source_label: 'Sales DB',
        connection_available: true,
        favorited_at: new Date().toISOString(),
      }]);
      window.__confirmMessages = [];
      window.confirm = (message) => {
        window.__confirmMessages.push(message);
        return true;
      };
    });
    await page.locator('[data-rail="history"]').click();
    await page.locator('[data-conv-action="delete"]').click();
    await expect(page.locator('.v3-conv-item')).toHaveCount(0);
    const messages = await page.evaluate(() => window.__confirmMessages);
    expect(messages).toHaveLength(2);
    expect(messages[1]).toContain('saved answer');
    const deletes = await page.evaluate(() => (window.__calls || []).filter((call) =>
      call.method === 'DELETE' && /\/api\/conversations\/[^/]+/.test(call.url)
    ).map((call) => call.url));
    expect(deletes).toHaveLength(2);
    expect(deletes[1]).toContain('delete_saved=true');
  });

  test('Saved answers paginate without hiding older favorites', async ({ page }) => {
    await openHarness(page);
    await page.evaluate(() => {
      window.__seedFavoriteItems(Array.from({ length: 51 }, (_, index) => ({
        conversation_id: '11111111-1111-1111-1111-111111111111',
        turn_id: `favorite-${index}`,
        sequence_number: index + 1,
        conversation_title: 'Saved conversation',
        question: `Saved answer ${index + 1}`,
        answer: `Answer ${index + 1}`,
        result_kind: 'text',
        snapshot_status: 'not_applicable',
        source_key: 'sales_db',
        source_label: 'Sales DB',
        connection_available: true,
        favorited_at: new Date(Date.now() - index * 1000).toISOString(),
      })));
    });
    await page.locator('[data-rail="saved"]').click();
    await expect(page.locator('.v3-favorite-item')).toHaveCount(50);
    await expect(page.locator('[data-favorite-more]')).toBeVisible();
    await page.locator('[data-favorite-more]').click();
    await expect(page.locator('.v3-favorite-item')).toHaveCount(51);
    await expect(page.locator('[data-favorite-more]')).toHaveCount(0);
  });

  test('rail Conversation icon toggles the panel and dots a collapsed thread', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await openHarness(page);
    const rail = page.locator('[data-rail="conversation"]');
    const panel = page.locator('#v3-conversation');
    const dot = rail.locator('.v3-rail-dot');
    await expect(rail).toHaveClass(/is-active/);
    await expect(panel).toBeVisible();

    await ask(page, Q.sqlAggregate);
    // Clicking the active Conversation icon collapses the panel; the rail shows
    // the collapsed state plus an unread-style dot because the thread has a turn.
    await rail.click();
    await expect(panel).toBeHidden();
    await expect(rail).toHaveClass(/is-collapsed/);
    await expect(rail).not.toHaveClass(/is-active/);
    await expect(dot).toBeVisible();

    // Clicking again reopens it and clears the dot.
    await rail.click();
    await expect(panel).toBeVisible();
    await expect(rail).toHaveClass(/is-active/);
    await expect(dot).toBeHidden();
  });

  test('editing a sent question reruns it in place and marks it edited', async ({ page }) => {
    await openHarness(page);
    await ask(page, Q.sqlAggregate);
    await ask(page, Q.sqlCount);
    const turns = page.locator('#v3-thread article.v3-turn');
    await expect(turns).toHaveCount(2);

    const first = turns.first();
    await first.locator('[data-edit]').click();
    const input = first.locator('.v3-edit-input');
    await expect(input).toBeFocused();
    await input.fill('Edited: show revenue by product');
    await first.locator('.v3-edit-save').click();

    // In-place replacement: still two cards, the later turn is untouched, and the
    // first turn now carries the new question with an "edited" meta.
    await expect(page.locator('#v3-thread article.v3-turn')).toHaveCount(2);
    await expect(turns.nth(1)).toContainText(Q.sqlCount);
    await expect.poll(() => page.evaluate(() => window.ChatController.turns[0].question)).toBe('Edited: show revenue by product');
    await expect.poll(() => page.evaluate(() => window.ChatController.turns[0].edited)).toBe(true);
    await expect(turns.first().locator('.v3-turn-meta')).toContainText('edited');
  });
});
