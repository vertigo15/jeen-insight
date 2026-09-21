/* ==========================================================================
   Jeen Insights — first-time user experience (FTUE) controller.

   Loaded AFTER workspaceController.js. Reads the DOM the v3 workspace already
   builds and layers on: a getting-started checklist, an actionable empty state
   (quick-start cards), a post-first-answer nudge, a welcome dialog (re-shown
   each session until the user opts out via "Don't show this again"), and a
   four-step guided tour.

   Suppression model:
     * "Skip for now"  -> mutes EVERY surface for the current browser session.
       Client-only (there is no server notion of a session): an in-memory flag
       mirrored into a user-scoped sessionStorage key so it survives a reload.
     * "Don't show this again" -> permanent opt-out of every surface, persisted
       server-side as `ftue_opted_out_at`.

   No new dependencies, no build step. Progress/opt-out persistence is
   server-side (per user) via GET/PATCH /api/user/onboarding; browser mutations
   ride csrf.js's X-CSRFToken. Checklist completion is driven by the four
   `jeen:onboarding:*` CustomEvents the app emits (see workspaceController.js /
   script.js) — not by new instrumentation.
   ========================================================================== */

(function () {
  'use strict';

  var API = '/api/user/onboarding';
  var CHECK = 'M2 6l3 3 5-6';
  var SKIP_KEY_PREFIX = 'jeen:onboarding:skip:';

  // Interface strings come from the locale catalog (static/i18n/i18n.js).
  function t(key, args) {
    return window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key);
  }
  function h(key, args) {
    return window.I18n && typeof window.I18n.h === 'function' ? window.I18n.h(key, args) : String(key);
  }
  function isRtl() { return !!(window.I18n && window.I18n.isRtl); }

  var CHECK_ITEMS = [
    { key: 'pick_connection',    label: 'onboarding.checklist.pickConnection' },
    { key: 'ask_first_question', label: 'onboarding.checklist.askFirst',   action: 'onboarding.checklist.start' },
    { key: 'open_sql',           label: 'onboarding.checklist.openSql' },
    { key: 'pin_question',       label: 'onboarding.checklist.pinQuestion', action: 'onboarding.checklist.showMe' }
  ];

  var TOUR_STEPS = [
    {
      id: 'connection', selector: '#v3-connection-slot', placement: 'below-left', pill: true, inPanel: false,
      title: 'onboarding.tour.connection.title',
      body: 'onboarding.tour.connection.body'
    },
    {
      id: 'suggestions', selector: '.v3-suggestions', placement: 'right', inPanel: true, tab: 'conversation',
      title: 'onboarding.tour.suggestions.title',
      body: 'onboarding.tour.suggestions.body'
    },
    {
      id: 'composer', selector: '.v3-composer', placement: 'above', inPanel: true,
      title: 'onboarding.tour.composer.title',
      body: 'onboarding.tour.composer.body'
    },
    {
      // The tab bar is hidden by default (Settings > General), so fall back to
      // the always-visible rail icon for the same section.
      id: 'tables', selector: '#v3-tab-tables', placement: 'below-left', inPanel: true, finish: true,
      fallbackSelector: '[data-rail="tables"]', fallbackPlacement: 'right',
      title: 'onboarding.tour.tables.title',
      body: 'onboarding.tour.tables.body'
    }
  ];

  var state = { data: null, ready: false };
  var checklistEl = null;
  var successCount = 0;
  var nudgeShown = false;
  // Answers that arrive before GET /api/user/onboarding resolves. Flushed after
  // boot so a returning user's dismissed nudge cannot flash on a fast first hit.
  var pendingAnswers = 0;
  // In-memory "Skip for now" flag. Authoritative within this page even if
  // sessionStorage is unavailable (private mode, storage disabled, quota).
  var sessionMuted = false;

  // ---------------------------------------------------------------- helpers
  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  }

  function defaults() {
    return {
      user_id: null, welcome_seen_at: null, tour_completed_at: null,
      checklist: {}, checklist_dismissed_at: null, nudge_dismissed_at: null,
      ftue_opted_out_at: null
    };
  }

  // ---------------------------------------------------------------- suppression
  // The session-skip key is scoped by user so a logout/login in the same tab
  // cannot inherit another account's skip. Every identity we know about is
  // used, because they come from different requests: `user_id` from the
  // onboarding GET (absent on its fail-soft path) and `_currentUser` from
  // /api/auth/me. Writing under all of them and reading any of them keeps the
  // skip intact across a reload where only one source is available. When no
  // identity is known at all, nothing is written — the in-memory flag covers
  // the page and we never create a shared key another account could inherit.
  function skipKeys() {
    var keys = [];
    var uid = state.data && state.data.user_id;
    if (uid) keys.push(SKIP_KEY_PREFIX + 'id:' + uid);
    var cu = window._currentUser;
    if (cu && cu.id != null) keys.push(SKIP_KEY_PREFIX + 'auth:' + cu.id);
    if (cu && cu.email) keys.push(SKIP_KEY_PREFIX + 'email:' + cu.email);
    return keys;
  }

  function setSessionSkipped() {
    sessionMuted = true;
    try {
      skipKeys().forEach(function (k) { window.sessionStorage.setItem(k, '1'); });
    } catch (e) { /* storage unavailable: in-memory flag suffices */ }
  }

  function sessionSkipped() {
    if (sessionMuted) return true;
    try {
      return skipKeys().some(function (k) { return window.sessionStorage.getItem(k) === '1'; });
    } catch (e) { return false; }
  }

  function permanentlyOptedOut() {
    return !!(state.data && state.data.ftue_opted_out_at);
  }

  // Single gate every FTUE surface checks before mounting.
  function ftueMuted() {
    return sessionSkipped() || permanentlyOptedOut();
  }

  function fetchState() {
    return fetch(API, { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  // Reconcile a server row into local state WITHOUT ever downgrading a known
  // value to null. Onboarding only ever *sets* flags/timestamps and *adds*
  // checklist keys, so a merge-only reconciliation is correct — and it protects
  // optimistic local progress from being wiped by a fail-soft (DB-error) empty
  // row that the API still returns with HTTP 200.
  function reconcile(row) {
    if (!row) return;
    if (!state.data) { state.data = row; return; }
    ['welcome_seen_at', 'tour_completed_at', 'checklist_dismissed_at', 'nudge_dismissed_at', 'ftue_opted_out_at']
      .forEach(function (k) { if (row[k]) state.data[k] = row[k]; });
    var incoming = row.checklist || {};
    state.data.checklist = state.data.checklist || {};
    Object.keys(incoming).forEach(function (k) { if (incoming[k]) state.data.checklist[k] = true; });
  }

  // Best-effort partial update; reconciles the server's response into `state`.
  function patch(body) {
    return fetch(API, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (row) { reconcile(row); return row; })
      .catch(function () { return null; });
  }

  // Poll until the workspace has mounted, then run `fn` once.
  function whenReady(fn) {
    var tries = 0;
    (function poll() {
      if (document.getElementById('v3-conversation') && window.ChatController) { fn(); return; }
      if (tries++ > 200) return; // ~20s ceiling, then give up quietly
      setTimeout(poll, 100);
    })();
  }

  // ---------------------------------------------------------------- checklist
  // The active item's action word ("Start" / "Show me") points the user at the
  // real control that completes that step.
  function runItemAction(key) {
    var ctrl = window.ChatController;
    if (key === 'ask_first_question') {
      var railNew = document.querySelector('[data-rail="conversation"], [data-rail="new"]');
      if (railNew) railNew.click();
      else if (ctrl) { ctrl.setTab('conversation'); ctrl.setConversation(true); }
      var input = document.querySelector('.v3-composer textarea, .v3-composer input');
      if (input) input.focus();
      var composer = document.querySelector('.v3-composer');
      if (composer) showHint(composer, {
        placement: 'above',
        title: t('onboarding.hints.askHere.title'),
        body: t('onboarding.hints.askHere.body')
      });
    } else if (key === 'pin_question') {
      // Reveal the Pinned / recent-questions panel, where each question has a
      // pin control, then spotlight a real star.
      if (ctrl) { ctrl.setConversation(true); ctrl.setTab('pinned'); }
      else { var tab = document.getElementById('v3-tab-pinned'); if (tab) tab.click(); }
      spotlightPin(0);
    }
  }

  // displayHistory() renders the pinned/recent list asynchronously; poll briefly
  // for a real "pin" star, then spotlight it. Fall back to the Pinned tab if the
  // list is still empty (no questions asked yet).
  function spotlightPin(attempt) {
    var panel = document.getElementById('v3-panel-pinned');
    var star = panel && panel.querySelector('.pin-icon[data-pin-action="pin"]');
    if (star) {
      showHint(star, {
        placement: 'right',
        title: t('onboarding.hints.pin.title'),
        body: t('onboarding.hints.pin.body')
      });
      return;
    }
    if (attempt < 8) { setTimeout(function () { spotlightPin(attempt + 1); }, 150); return; }
    var tab = document.getElementById('v3-tab-pinned');
    var placement = 'below-left';
    if (!isVisible(tab)) { tab = document.querySelector('[data-rail="pinned"]'); placement = 'right'; }
    if (tab) showHint(tab, {
      placement: placement,
      title: t('onboarding.hints.pinnedHere.title'),
      body: t('onboarding.hints.pinnedHere.body')
    });
  }

  function isVisible(node) {
    return !!node && !(node.offsetWidth === 0 && node.offsetHeight === 0);
  }

  // A step's primary target may be hidden by a user preference (e.g. the chat
  // tab bar); use its fallback when one is declared and visible.
  function resolveStepTarget(step) {
    var target = document.querySelector(step.selector);
    if (isVisible(target)) return { target: target, placement: step.placement };
    if (step.fallbackSelector) {
      var alt = document.querySelector(step.fallbackSelector);
      if (isVisible(alt)) return { target: alt, placement: step.fallbackPlacement || step.placement };
    }
    return null;
  }

  function checklist() { return (state.data && state.data.checklist) || {}; }
  function doneCount() { return CHECK_ITEMS.filter(function (i) { return checklist()[i.key]; }).length; }
  function isComplete() { return doneCount() >= CHECK_ITEMS.length; }

  function buildChecklist() {
    var card = el('section', 'jo-checklist');
    card.setAttribute('aria-label', t('onboarding.checklist.title'));

    var header = el('button', 'jo-checklist-header');
    header.type = 'button';
    header.setAttribute('aria-expanded', 'true');
    header.innerHTML =
      '<span class="jo-ring" aria-hidden="true">' +
      '<svg class="jo-ring-check" viewBox="0 0 12 12" fill="none" aria-hidden="true">' +
      '<path d="' + CHECK + '" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
      '</span>' +
      '<span class="jo-checklist-title">' + h('onboarding.checklist.title') + '</span>' +
      '<span class="jo-checklist-spacer"></span>' +
      '<span class="jo-checklist-count"></span>' +
      '<svg class="jo-chevron" width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">' +
      '<path d="M3.5 5.25L7 8.75l3.5-3.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    var dismiss = el('button', 'jo-checklist-dismiss', '&times;');
    dismiss.type = 'button';
    dismiss.setAttribute('aria-label', t('onboarding.checklist.dismiss'));
    header.appendChild(dismiss);

    var list = el('ul', 'jo-checklist-items');
    CHECK_ITEMS.forEach(function (item) {
      var li = el('li', 'jo-item');
      li.dataset.key = item.key;
      li.innerHTML =
        '<span class="jo-item-circle"><svg viewBox="0 0 12 12" fill="none" aria-hidden="true">' +
        '<path d="' + CHECK + '" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></span>' +
        '<span class="jo-item-label"></span>';
      li.querySelector('.jo-item-label').textContent = t(item.label);
      if (item.action) {
        var act = el('button', 'jo-item-action');
        act.type = 'button';
        act.textContent = t(item.action);
        act.addEventListener('click', function (e) {
          e.stopPropagation();
          runItemAction(item.key);
        });
        li.appendChild(act);
      }
      list.appendChild(li);
    });

    card.appendChild(header);
    card.appendChild(list);

    header.addEventListener('click', function (e) {
      if (e.target.closest('.jo-checklist-dismiss')) return;
      var collapsed = card.classList.toggle('is-collapsed');
      header.setAttribute('aria-expanded', String(!collapsed));
    });
    dismiss.addEventListener('click', function (e) {
      e.stopPropagation();
      card.remove();
      checklistEl = null;
      patch({ checklist_dismissed: true });
    });

    return card;
  }

  function paintChecklist() {
    if (!checklistEl) return;
    var done = doneCount();
    var total = CHECK_ITEMS.length;
    var pct = Math.round((done / total) * 100);
    var ring = checklistEl.querySelector('.jo-ring');
    if (ring) ring.style.setProperty('--jo-pct', pct + '%');
    var count = checklistEl.querySelector('.jo-checklist-count');
    if (count) count.textContent = done + '/' + total;

    var complete = isComplete();
    var title = checklistEl.querySelector('.jo-checklist-title');
    if (title) title.textContent = complete ? t('onboarding.checklist.allSet') : t('onboarding.checklist.title');
    checklistEl.setAttribute('aria-label', complete ? t('onboarding.checklist.titleComplete') : t('onboarding.checklist.title'));

    var firstIncomplete = CHECK_ITEMS.find(function (i) { return !checklist()[i.key]; });
    checklistEl.querySelectorAll('.jo-item').forEach(function (li) {
      var key = li.dataset.key;
      var isDone = !!checklist()[key];
      var isActive = !isDone && firstIncomplete && firstIncomplete.key === key;
      li.classList.toggle('is-done', isDone);
      li.classList.toggle('is-active', !!isActive);
    });
    checklistEl.classList.toggle('is-complete', complete);
    if (complete) checklistEl.classList.add('is-collapsed');
  }

  function mountChecklist() {
    if (ftueMuted()) return;
    if (checklistEl || (state.data && state.data.checklist_dismissed_at)) return;
    var conversation = document.getElementById('v3-conversation');
    var composerWrap = conversation && conversation.querySelector('.v3-composer-wrap');
    if (!conversation || !composerWrap) return;
    checklistEl = buildChecklist();
    conversation.insertBefore(checklistEl, composerWrap);
    paintChecklist();
  }

  // Mark a checklist item done (idempotent) and persist the jsonb merge.
  function markItem(key) {
    if (!state.ready) return;
    if (checklist()[key]) { paintChecklist(); return; }
    if (!state.data) state.data = defaults();
    state.data.checklist = state.data.checklist || {};
    state.data.checklist[key] = true;
    paintChecklist();
    var merge = {}; merge[key] = true;
    patch({ checklist: merge });
  }

  // ---------------------------------------------------------------- quick-start cards
  // Keys into the `onboarding.cards.*` catalog namespace; `run` is the question
  // sent to the assistant (translated too, so the answer comes back in kind).
  var CARDS = [
    { primary: true, id: 'trySales', run: true },
    { id: 'seeChart', run: true },
    { id: 'explore', tables: true }
  ];
  function placeholderHtml() {
    return '<strong>' + h('shell.result.noResultYet') + '</strong>' +
      '<span>' + h('shell.result.placeholderCopy') + '</span>';
  }

  function mountCards() {
    if (ftueMuted()) return;
    var ph = document.getElementById('v3-placeholder');
    if (!ph || successCount >= 3) return;
    var grid = el('div', 'jo-cards');
    CARDS.forEach(function (card) {
      var btn = el('button', 'jo-card' + (card.primary ? ' jo-card--primary' : ''));
      btn.type = 'button';
      btn.innerHTML =
        '<span class="jo-card-eyebrow"></span>' +
        '<span class="jo-card-title"></span>' +
        '<span class="jo-card-body"></span>' +
        '<span class="jo-card-cta"></span>';
      var base = 'onboarding.cards.' + card.id + '.';
      btn.querySelector('.jo-card-eyebrow').textContent = t(base + 'eyebrow');
      btn.querySelector('.jo-card-title').textContent = t(base + 'title');
      btn.querySelector('.jo-card-body').textContent = t(base + 'body');
      btn.querySelector('.jo-card-cta').textContent = t(base + 'cta');
      btn.addEventListener('click', function () {
        if (card.tables) {
          if (window.ChatController) {
            window.ChatController.setTab('tables');
            window.ChatController.setConversation(true);
          }
          if (typeof window.loadTables === 'function') window.loadTables();
        } else if (window.ChatController && typeof window.ChatController.send === 'function') {
          window.ChatController.send(t(base + 'run'));
        }
      });
      grid.appendChild(btn);
    });
    var note = el('div', 'jo-cards-note');
    note.textContent = t('onboarding.cards.note');

    ph.classList.add('v3-placeholder--cards');
    ph.innerHTML = '';
    ph.appendChild(grid);
    ph.appendChild(note);
  }

  function retireCards() {
    var ph = document.getElementById('v3-placeholder');
    if (!ph) return;
    ph.classList.remove('v3-placeholder--cards');
    ph.innerHTML = placeholderHtml();
  }

  // ---------------------------------------------------------------- nudge (State 06)
  function currentQuestion() {
    var title = document.getElementById('v3-result-title');
    return title ? (title.textContent || '').trim() : '';
  }

  function maybeShowNudge() {
    if (!state.ready || ftueMuted()) return;
    if (nudgeShown || successCount !== 1) return;
    if (state.data && state.data.nudge_dismissed_at) return;
    var scroll = document.querySelector('.v3-workspace .v3-scroll');
    if (!scroll) return;
    nudgeShown = true;

    var nudge = el('div', 'jo-nudge');
    nudge.setAttribute('role', 'status');
    nudge.innerHTML =
      '<div class="jo-nudge-title">' + h('onboarding.nudge.title') + '</div>' +
      '<div class="jo-nudge-body">' + h('onboarding.nudge.body') + '</div>' +
      '<div class="jo-nudge-actions">' +
      '<button type="button" class="jo-btn-primary" data-pin>' + h('onboarding.nudge.pin') + '</button>' +
      '<button type="button" class="jo-btn-ghost" data-dismiss>' + h('onboarding.nudge.dismiss') + '</button>' +
      '</div>';

    function close() { nudge.remove(); patch({ nudge_dismissed: true }); }
    nudge.querySelector('[data-pin]').addEventListener('click', function () {
      var q = currentQuestion();
      if (q && typeof window.pinQuestion === 'function') {
        window.pinQuestion({ stopPropagation: function () {} }, q);
      }
      close();
    });
    nudge.querySelector('[data-dismiss]').addEventListener('click', close);

    scroll.insertBefore(nudge, scroll.firstChild);
  }

  // ---------------------------------------------------------------- welcome dialog (State 01)
  function showWelcome() {
    var shell = document.getElementById('v3-shell');
    var backdrop = el('div', 'jo-backdrop');
    var prevFocus = document.activeElement;

    var mark = '/static/images/jeen-mark.png';
    var dialog = el('div', 'jo-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'jo-welcome-title');
    dialog.innerHTML =
      '<div class="jo-dialog-eyebrow"><img class="jo-dialog-mark" src="' + mark + '" alt="">' +
      '<span>' + h('app.name') + '</span></div>' +
      '<h2 class="jo-dialog-title" id="jo-welcome-title">' + h('onboarding.welcome.title') + '</h2>' +
      '<p class="jo-dialog-body">' + h('onboarding.welcome.body') + '</p>' +
      '<div class="jo-steps">' +
      '<div class="jo-step"><div class="jo-step-num">01</div><div class="jo-step-title">' + h('onboarding.welcome.step1.title') + '</div><div class="jo-step-body">' + h('onboarding.welcome.step1.body') + '</div></div>' +
      '<div class="jo-step"><div class="jo-step-num">02</div><div class="jo-step-title">' + h('onboarding.welcome.step2.title') + '</div><div class="jo-step-body">' + h('onboarding.welcome.step2.body') + '</div></div>' +
      '<div class="jo-step"><div class="jo-step-num">03</div><div class="jo-step-title">' + h('onboarding.welcome.step3.title') + '</div><div class="jo-step-body">' + h('onboarding.welcome.step3.body') + '</div></div>' +
      '</div>' +
      '<div class="jo-dialog-footer">' +
      '<button type="button" class="jo-pill-primary" data-tour>' + h('onboarding.welcome.takeTour') + '</button>' +
      '<button type="button" class="jo-pill-ghost" data-skip>' + h('onboarding.welcome.skip') + '</button>' +
      '<label class="jo-dialog-dontshow"><input type="checkbox" data-dontshow>' +
      '<span>' + h('onboarding.welcome.dontShow') + '</span></label>' +
      '<span class="jo-dialog-meta">' + h('onboarding.welcome.meta', { steps: TOUR_STEPS.length }) + '</span>' +
      '</div>';
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    if (shell) shell.classList.add('jo-blurred');

    function teardown() {
      backdrop.remove();
      if (shell) shell.classList.remove('jo-blurred');
      document.removeEventListener('keydown', onKey, true);
      if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch (e) {} }
    }
    function optedOut() {
      var cb = dialog.querySelector('[data-dontshow]');
      return !!(cb && cb.checked);
    }
    // "Skip for now" mutes the whole FTUE for this browser session only.
    // "Don't show this again" persists a permanent opt-out (ftue_opted_out_at).
    // The session flag is ALSO set on the permanent path so a reload before the
    // PATCH lands cannot flash the FTUE back; the server row takes over after.
    function applyChoice() {
      setSessionSkipped();
      if (optedOut()) {
        if (!state.data) state.data = defaults();
        state.data.ftue_opted_out_at = state.data.ftue_opted_out_at || 'pending';
        patch({ ftue_opted_out: true, welcome_seen: true });
      }
    }
    function skip() {
      teardown();
      applyChoice();
      hideAllSurfaces();
    }
    // The tour was explicitly requested, so it runs once even though everything
    // else is muted from now on (for this session, or permanently if opted out).
    function take() {
      teardown();
      applyChoice();
      hideAllSurfaces();
      startTour({ force: true });
    }
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); skip(); return; }
      if (e.key === 'Tab') {
        var f = dialog.querySelectorAll('button, input');
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }

    dialog.querySelector('[data-skip]').addEventListener('click', skip);
    dialog.querySelector('[data-tour]').addEventListener('click', take);
    document.addEventListener('keydown', onKey, true);
    dialog.querySelector('[data-tour]').focus();
  }

  // ---------------------------------------------------------------- guided tour (Task 5)
  var tour = null;

  // `opts.force` lets the one tour the user explicitly asked for (welcome dialog
  // "Take a tour") run even though the rest of the FTUE is muted.
  function startTour(opts) {
    if (tour) return;
    if (ftueMuted() && !(opts && opts.force)) return;
    dismissHint();
    tour = {
      index: 0,
      scrim: el('div', 'jo-tour-scrim'),
      coach: null,
      target: null,
      ro: null,
      prevFocus: document.activeElement,
      prevTab: window.ChatController ? window.ChatController.activeTab : 'conversation',
      prevConversationOpen: !document.getElementById('v3-conversation').hidden
    };
    document.body.appendChild(tour.scrim);
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
    document.addEventListener('keydown', onTourKey, true);
    renderStep();
  }

  function onTourKey(e) {
    if (!tour) return;
    if (e.key === 'Escape') {
      // Handle before the workspace's own Escape (which closes the drawer at <900).
      e.preventDefault();
      e.stopPropagation();
      finishTour();
    }
  }

  function clearHighlight() {
    if (tour && tour.target) {
      tour.target.classList.remove('jo-target-highlight', 'jo-target-highlight--pill');
    }
    if (tour && tour.ro) { tour.ro.disconnect(); tour.ro = null; }
  }

  function renderStep() {
    if (!tour) return;
    var step = TOUR_STEPS[tour.index];

    // Steps 2-4 live in the conversation panel; force it open on narrow widths.
    if (step.inPanel && window.ChatController && window.innerWidth <= 1100) {
      window.ChatController.setConversation(true);
    }
    if (step.tab && window.ChatController) window.ChatController.setTab(step.tab);

    clearHighlight();
    var resolved = resolveStepTarget(step);

    // Step 2 guard: no starter chips (empty suggestions) -> skip to next step.
    if (!resolved) {
      if (tour.index < TOUR_STEPS.length - 1) { tour.index++; renderStep(); return; }
      finishTour(); return;
    }
    var target = resolved.target;
    tour.target = target;
    tour.placement = resolved.placement;
    target.classList.add('jo-target-highlight');
    if (step.pill) target.classList.add('jo-target-highlight--pill');

    if (!tour.coach) tour.coach = buildCoach();
    fillCoach(step);
    // Position after layout settles (panel open triggers a reflow).
    requestAnimationFrame(function () { positionCoach(step); });

    if ('ResizeObserver' in window) {
      tour.ro = new ResizeObserver(function () { positionCoach(step); });
      tour.ro.observe(target);
    }
    var nextBtn = tour.coach.querySelector('[data-next]');
    if (nextBtn) nextBtn.focus();
  }

  function buildCoach() {
    var coach = el('div', 'jo-coach');
    coach.setAttribute('role', 'dialog');
    coach.setAttribute('aria-live', 'polite');
    coach.innerHTML =
      '<div class="jo-coach-step" data-step></div>' +
      '<div class="jo-coach-title" data-title></div>' +
      '<div class="jo-coach-body" data-body></div>' +
      '<div class="jo-coach-actions">' +
      '<button type="button" class="jo-btn-primary" data-next></button>' +
      '<button type="button" class="jo-btn-ghost" data-skip>' + h('onboarding.tour.skip') + '</button>' +
      '<span class="jo-dots" data-dots></span>' +
      '</div>';
    coach.querySelector('[data-next]').addEventListener('click', advance);
    coach.querySelector('[data-skip]').addEventListener('click', finishTour);
    document.body.appendChild(coach);
    return coach;
  }

  function fillCoach(step) {
    var c = tour.coach;
    c.querySelector('[data-step]').textContent = t('onboarding.tour.step', { current: tour.index + 1, total: TOUR_STEPS.length });
    c.querySelector('[data-title]').textContent = t(step.title);
    c.querySelector('[data-body]').textContent = t(step.body);
    c.querySelector('[data-next]').textContent = step.finish ? t('onboarding.tour.finish') : t('common.next');
    var dots = TOUR_STEPS.map(function (_, i) {
      return '<span class="jo-dot' + (i === tour.index ? ' is-active' : '') + '"></span>';
    }).join('');
    c.querySelector('[data-dots]').innerHTML = dots;
  }

  function placeCoach(target, coach, placement) {
    if (!target || !coach) return;
    var r = target.getBoundingClientRect();
    var cw = coach.offsetWidth;
    var ch = coach.offsetHeight;
    var gap = 12;
    var top, left;
    // Placements are logical: "right" means the trailing side, "below-left"
    // aligns to the leading edge — both mirror when the document is RTL.
    var rtl = isRtl();
    switch (placement) {
      case 'right':      left = rtl ? r.left - cw - gap : r.right + gap; top = r.top; break;
      case 'above':      left = rtl ? r.right - cw : r.left;            top = r.top - ch - gap; break;
      case 'below-left':
      default:           left = rtl ? r.right - cw : r.left;            top = r.bottom + gap; break;
    }
    var vw = window.innerWidth, vh = window.innerHeight;
    left = Math.max(12, Math.min(left, vw - cw - 12));
    top = Math.max(12, Math.min(top, vh - ch - 12));
    coach.style.left = left + 'px';
    coach.style.top = top + 'px';
  }

  function positionCoach(step) {
    if (!tour) return;
    placeCoach(tour.target, tour.coach, tour.placement || step.placement);
  }

  function reposition() { if (tour) positionCoach(TOUR_STEPS[tour.index]); }

  // ---------------------------------------------------------------- coach-mark hint
  // A single-target spotlight that reuses the tour's coach-mark styling, fired
  // when the user clicks a checklist action word. Non-blocking, auto-clears, and
  // yields to the full guided tour if that is running.
  var hint = null;

  function showHint(target, opts) {
    // The mute check also neutralises spotlightPin()'s delayed retries, which
    // could otherwise recreate a hint after the FTUE was torn down.
    if (tour || !target || ftueMuted()) return;
    dismissHint();
    opts = opts || {};
    hint = { target: target, coach: null, placement: opts.placement || 'above', timer: null, prevFocus: document.activeElement };
    target.classList.add('jo-target-highlight');
    if (opts.pill) target.classList.add('jo-target-highlight--pill');

    var coach = el('div', 'jo-coach jo-coach--hint');
    coach.setAttribute('role', 'dialog');
    coach.setAttribute('aria-live', 'polite');
    coach.innerHTML =
      '<div class="jo-coach-title" data-title></div>' +
      '<div class="jo-coach-body" data-body></div>' +
      '<div class="jo-coach-actions"><button type="button" class="jo-btn-primary" data-got>' + h('onboarding.hints.gotIt') + '</button></div>';
    coach.querySelector('[data-title]').textContent = opts.title || '';
    coach.querySelector('[data-body]').textContent = opts.body || '';
    coach.querySelector('[data-got]').addEventListener('click', dismissHint);
    document.body.appendChild(coach);
    hint.coach = coach;

    requestAnimationFrame(function () { placeCoach(target, coach, hint.placement); });
    window.addEventListener('resize', hintReposition);
    window.addEventListener('scroll', hintReposition, true);
    document.addEventListener('keydown', hintKey, true);
    coach.querySelector('[data-got]').focus();
    hint.timer = setTimeout(dismissHint, 7000);
  }

  function hintReposition() { if (hint) placeCoach(hint.target, hint.coach, hint.placement); }
  function hintKey(e) { if (hint && e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); dismissHint(); } }

  function dismissHint() {
    if (!hint) return;
    var hadFocus = !!(hint.coach && hint.coach.contains(document.activeElement));
    if (hint.target) hint.target.classList.remove('jo-target-highlight', 'jo-target-highlight--pill');
    if (hint.coach) hint.coach.remove();
    if (hint.timer) clearTimeout(hint.timer);
    window.removeEventListener('resize', hintReposition);
    window.removeEventListener('scroll', hintReposition, true);
    document.removeEventListener('keydown', hintKey, true);
    var prev = hint.prevFocus;
    hint = null;
    if (hadFocus) restoreFocus(prev);
  }

  function advance() {
    if (!tour) return;
    if (tour.index < TOUR_STEPS.length - 1) { tour.index++; renderStep(); }
    else finishTour();
  }

  // Shared teardown. `persistCompletion` is true when the user finished or
  // exited the tour themselves; false when it is being cancelled because the
  // FTUE was muted underneath it (that is not a "completed" signal).
  function teardownTour(persistCompletion) {
    if (!tour) return;
    var hadFocus = !!(tour.coach && tour.coach.contains(document.activeElement));
    clearHighlight();
    if (tour.coach) tour.coach.remove();
    if (tour.scrim) tour.scrim.remove();
    window.removeEventListener('resize', reposition);
    window.removeEventListener('scroll', reposition, true);
    document.removeEventListener('keydown', onTourKey, true);
    // Restore the user's prior tab / drawer state.
    if (window.ChatController) {
      if (tour.prevTab) window.ChatController.setTab(tour.prevTab);
      if (window.innerWidth <= 1100) window.ChatController.setConversation(!!tour.prevConversationOpen);
    }
    if (hadFocus) restoreFocus(tour.prevFocus);
    tour = null;
    if (persistCompletion) patch({ tour_completed: true });
  }

  // Coach-marks take focus for keyboard users; hand it back when they go away
  // so focus does not fall to <body>. Callers only do this when focus was still
  // inside the coach-mark (an auto-dismiss must not yank focus the user moved).
  // Skip if the element is gone or hidden.
  function restoreFocus(node) {
    if (!node || node === document.body || !node.isConnected || !node.focus) return;
    if (node.offsetWidth === 0 && node.offsetHeight === 0) return;
    try { node.focus(); } catch (e) {}
  }

  function finishTour() { teardownTour(true); }
  function cancelTour() { teardownTour(false); }

  // ---------------------------------------------------------------- mute everything
  // Immediately remove every mounted FTUE surface. Mounting is separately gated
  // by ftueMuted(), so nothing here comes back until the mute lifts.
  function hideAllSurfaces() {
    if (checklistEl) { checklistEl.remove(); checklistEl = null; }
    retireCards();
    var nudge = document.querySelector('.v3-workspace .jo-nudge');
    if (nudge) nudge.remove();
    nudgeShown = true;
    dismissHint();
    cancelTour();
  }

  // ---------------------------------------------------------------- signals
  function onAnswer() {
    // Queue until the server row is in: otherwise the first answer of a session
    // mounts the nudge for a user who already dismissed it (GET still in flight).
    if (!state.ready) { pendingAnswers++; return; }
    markItem('ask_first_question');
    successCount++;
    if (successCount >= 3) retireCards();
    maybeShowNudge();
  }

  document.addEventListener('jeen:onboarding:pick_connection', function () {
    if (!state.ready) return;
    markItem('pick_connection');
    // Switching connection resets the workspace turns, so clear the per-answer
    // quick-start cards AND the stale post-answer nudge, and re-arm both for the
    // fresh connection (the checklist is NOT reset).
    successCount = 0;
    var stale = document.querySelector('.v3-workspace .jo-nudge');
    if (stale) stale.remove();
    if (ftueMuted()) return;
    nudgeShown = false;
    mountCards();
  });
  document.addEventListener('jeen:onboarding:ask_first_question', onAnswer);
  document.addEventListener('jeen:onboarding:open_sql', function () { markItem('open_sql'); });
  document.addEventListener('jeen:onboarding:pin_question', function () { markItem('pin_question'); });

  // ---------------------------------------------------------------- boot
  whenReady(function () {
    fetchState().then(function (row) {
      // Fail-soft: if the GET failed, treat welcome/tour as seen (don't nag when
      // we can't persist), but still show cards + checklist.
      state.data = row || (function () { var d = defaults(); d.welcome_seen_at = 'unknown'; d.tour_completed_at = 'unknown'; return d; })();
      state.ready = true;

      // Reconcile a connection that resolved before this controller booted.
      try {
        if (typeof window.getActiveConnection === 'function' && window.getActiveConnection()) {
          markItem('pick_connection');
        }
      } catch (e) {}

      // Skipped this session or permanently opted out: mount nothing. Progress
      // signals keep persisting server-side so a returning user resumes cleanly.
      if (ftueMuted()) {
        hideAllSurfaces();
      } else {
        mountChecklist();
        mountCards();
        if (!state.data.welcome_seen_at) showWelcome();
      }
      // Replay answers that landed while the GET was in flight, now that we
      // know whether the nudge / cards are still in play.
      if (pendingAnswers) {
        var queued = pendingAnswers;
        pendingAnswers = 0;
        while (queued--) onAnswer();
      }
      // Boot decisions are final: a readiness marker for tests / integrations.
      document.body.classList.add('jo-ready');
    });
  });
})();
