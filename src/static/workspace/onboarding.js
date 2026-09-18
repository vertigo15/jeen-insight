/* ==========================================================================
   Jeen Insights — first-time user experience (FTUE) controller.

   Loaded AFTER workspaceController.js. Reads the DOM the v3 workspace already
   builds and layers on: a getting-started checklist, an actionable empty state
   (quick-start cards), a post-first-answer nudge, a welcome dialog (re-shown
   each session until the user opts out via "Don't show this again"), and a
   four-step guided tour.

   No new dependencies, no build step. Persistence is server-side (per user) via
   GET/PATCH /api/user/onboarding; browser mutations ride csrf.js's X-CSRFToken.
   Checklist completion is driven by the four `jeen:onboarding:*` CustomEvents the
   app emits (see workspaceController.js / script.js) — not by new instrumentation.
   ========================================================================== */

(function () {
  'use strict';

  var API = '/api/user/onboarding';
  var CHECK = 'M2 6l3 3 5-6';

  var CHECK_ITEMS = [
    { key: 'pick_connection',    label: 'Pick a connection' },
    { key: 'ask_first_question', label: 'Ask your first question',       action: 'Start' },
    { key: 'open_sql',           label: 'Open the SQL behind an answer' },
    { key: 'pin_question',       label: 'Pin a question you will reuse',  action: 'Show me' }
  ];

  var TOUR_STEPS = [
    {
      id: 'connection', selector: '#v3-connection-slot', placement: 'below-left', pill: true, inPanel: false,
      title: 'Select your active database or project.',
      body: 'Every question, suggestion and saved answer belongs to the connection shown here.'
    },
    {
      id: 'suggestions', selector: '.v3-suggestions', placement: 'right', inPanel: true, tab: 'conversation',
      title: 'Click any suggested question to auto-fill a query.',
      body: "Suggestions are generated from this connection's curated metadata, so they always run."
    },
    {
      id: 'composer', selector: '.v3-composer', placement: 'above', inPanel: true,
      title: 'Or type your own question in plain English or Hebrew.',
      body: 'Type @ for tables, # for columns, / for saved templates.'
    },
    {
      // The tab bar is hidden by default (Settings > General), so fall back to
      // the always-visible rail icon for the same section.
      id: 'tables', selector: '#v3-tab-tables', placement: 'below-left', inPanel: true, finish: true,
      fallbackSelector: '[data-rail="tables"]', fallbackPlacement: 'right',
      title: 'Not sure what is in there? Browse tables and columns.',
      body: 'Curated business terms sit next to the physical column names, so you can see what the agent sees.'
    }
  ];

  var state = { data: null, ready: false };
  var checklistEl = null;
  var successCount = 0;
  var nudgeShown = false;

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
      checklist: {}, checklist_dismissed_at: null, nudge_dismissed_at: null
    };
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
    ['welcome_seen_at', 'tour_completed_at', 'checklist_dismissed_at', 'nudge_dismissed_at']
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
        title: 'Ask here',
        body: 'Type a question in plain English or Hebrew — or click a suggestion to auto-fill one.'
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
    var star = panel && panel.querySelector('.pin-icon[aria-label="Pin question"]');
    if (star) {
      showHint(star, {
        placement: 'right',
        title: 'Pin a question',
        body: 'Tap the star on any question to pin it. Pinned questions stay here for this connection.'
      });
      return;
    }
    if (attempt < 8) { setTimeout(function () { spotlightPin(attempt + 1); }, 150); return; }
    var tab = document.getElementById('v3-tab-pinned');
    var placement = 'below-left';
    if (!isVisible(tab)) { tab = document.querySelector('[data-rail="pinned"]'); placement = 'right'; }
    if (tab) showHint(tab, {
      placement: placement,
      title: 'Pinned lives here',
      body: 'Ask a question, then tap its star to pin it — it will show up in this list to reuse.'
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
    card.setAttribute('aria-label', 'Getting started');

    var header = el('button', 'jo-checklist-header');
    header.type = 'button';
    header.setAttribute('aria-expanded', 'true');
    header.innerHTML =
      '<span class="jo-ring" aria-hidden="true">' +
      '<svg class="jo-ring-check" viewBox="0 0 12 12" fill="none" aria-hidden="true">' +
      '<path d="' + CHECK + '" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
      '</span>' +
      '<span class="jo-checklist-title">Getting started</span>' +
      '<span class="jo-checklist-spacer"></span>' +
      '<span class="jo-checklist-count"></span>' +
      '<svg class="jo-chevron" width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">' +
      '<path d="M3.5 5.25L7 8.75l3.5-3.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    var dismiss = el('button', 'jo-checklist-dismiss', '&times;');
    dismiss.type = 'button';
    dismiss.setAttribute('aria-label', 'Dismiss getting started');
    header.appendChild(dismiss);

    var list = el('ul', 'jo-checklist-items');
    CHECK_ITEMS.forEach(function (item) {
      var li = el('li', 'jo-item');
      li.dataset.key = item.key;
      li.innerHTML =
        '<span class="jo-item-circle"><svg viewBox="0 0 12 12" fill="none" aria-hidden="true">' +
        '<path d="' + CHECK + '" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></span>' +
        '<span class="jo-item-label"></span>';
      li.querySelector('.jo-item-label').textContent = item.label;
      if (item.action) {
        var act = el('button', 'jo-item-action');
        act.type = 'button';
        act.textContent = item.action;
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
    if (title) title.textContent = complete ? 'You\u2019re all set' : 'Getting started';
    checklistEl.setAttribute('aria-label', complete ? 'Getting started — complete' : 'Getting started');

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
  var CARDS = [
    { primary: true, eyebrow: 'TRY A QUESTION', title: 'Show total sales for 2006',
      body: 'One measure, one year. Returns a single figure and the SQL behind it.',
      cta: 'Run it \u2192', run: 'Show total sales for 2006' },
    { eyebrow: 'SEE A CHART', title: 'Compare sales by month, 2006 vs 2007',
      body: 'A grouped bar chart you can refine by asking for a different view.',
      cta: 'Run it \u2192', run: 'Compare sales by month, 2006 vs 2007' },
    { eyebrow: 'LOOK AROUND FIRST', title: 'Explore tables & schema',
      body: 'Browse what this connection exposes before you ask anything.',
      cta: 'Open Tables \u2192', tables: true }
  ];
  var PLACEHOLDER_HTML =
    '<strong>No result yet</strong>' +
    '<span>Ask a question on the left. The chart, rows, SQL and profiling for that answer appear here.</span>';

  function mountCards() {
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
      btn.querySelector('.jo-card-eyebrow').textContent = card.eyebrow;
      btn.querySelector('.jo-card-title').textContent = card.title;
      btn.querySelector('.jo-card-body').textContent = card.body;
      btn.querySelector('.jo-card-cta').textContent = card.cta;
      btn.addEventListener('click', function () {
        if (card.tables) {
          if (window.ChatController) {
            window.ChatController.setTab('tables');
            window.ChatController.setConversation(true);
          }
          if (typeof window.loadTables === 'function') window.loadTables();
        } else if (window.ChatController && typeof window.ChatController.send === 'function') {
          window.ChatController.send(card.run);
        }
      });
      grid.appendChild(btn);
    });
    var note = el('div', 'jo-cards-note');
    note.textContent = 'Answers land here: the chart, the rows behind it, the SQL, and per-column profiling.';

    ph.classList.add('v3-placeholder--cards');
    ph.innerHTML = '';
    ph.appendChild(grid);
    ph.appendChild(note);
  }

  function retireCards() {
    var ph = document.getElementById('v3-placeholder');
    if (!ph) return;
    ph.classList.remove('v3-placeholder--cards');
    ph.innerHTML = PLACEHOLDER_HTML;
  }

  // ---------------------------------------------------------------- nudge (State 06)
  function currentQuestion() {
    var title = document.getElementById('v3-result-title');
    return title ? (title.textContent || '').trim() : '';
  }

  function maybeShowNudge() {
    if (nudgeShown || successCount !== 1) return;
    if (state.data && state.data.nudge_dismissed_at) return;
    var scroll = document.querySelector('.v3-workspace .v3-scroll');
    if (!scroll) return;
    nudgeShown = true;

    var nudge = el('div', 'jo-nudge');
    nudge.setAttribute('role', 'status');
    nudge.innerHTML =
      '<div class="jo-nudge-title">Your answer is saved to this conversation.</div>' +
      '<div class="jo-nudge-body">Pin it to reuse the question on this connection later, or open SQL &amp; run details to see how it was produced.</div>' +
      '<div class="jo-nudge-actions">' +
      '<button type="button" class="jo-btn-primary" data-pin>Pin it</button>' +
      '<button type="button" class="jo-btn-ghost" data-dismiss>Dismiss</button>' +
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
      '<span>Jeen Insights</span></div>' +
      '<h2 class="jo-dialog-title" id="jo-welcome-title">Welcome to Jeen Insights</h2>' +
      '<p class="jo-dialog-body">Ask questions about your data in plain language. Every answer comes back with the chart, the rows, and the SQL that produced them.</p>' +
      '<div class="jo-steps">' +
      '<div class="jo-step"><div class="jo-step-num">01</div><div class="jo-step-title">Pick a connection</div><div class="jo-step-body">Your active database sits in the top bar.</div></div>' +
      '<div class="jo-step"><div class="jo-step-num">02</div><div class="jo-step-title">Ask in plain language</div><div class="jo-step-body">Start from a suggestion or type your own.</div></div>' +
      '<div class="jo-step"><div class="jo-step-num">03</div><div class="jo-step-title">Check the work</div><div class="jo-step-body">Chart, rows and SQL open side by side.</div></div>' +
      '</div>' +
      '<div class="jo-dialog-footer">' +
      '<button type="button" class="jo-pill-primary" data-tour>Take a 30-second tour</button>' +
      '<button type="button" class="jo-pill-ghost" data-skip>Skip for now</button>' +
      '<label class="jo-dialog-dontshow"><input type="checkbox" data-dontshow>' +
      '<span>Don\u2019t show this again</span></label>' +
      '<span class="jo-dialog-meta">4 steps &middot; 30s</span>' +
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
    // Product decision: the welcome dialog + tour re-appear every session UNTIL
    // the user explicitly opts out. Skipping / taking the tour is temporary; only
    // ticking "Don't show this again" persists welcome_seen and suppresses it.
    function close() { teardown(); if (optedOut()) patch({ welcome_seen: true }); }
    function skip() { close(); }
    function take() { close(); startTour(); }
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

  function startTour() {
    if (tour) return;
    dismissHint();
    tour = {
      index: 0,
      scrim: el('div', 'jo-tour-scrim'),
      coach: null,
      target: null,
      ro: null,
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
      '<button type="button" class="jo-btn-ghost" data-skip>Skip tour</button>' +
      '<span class="jo-dots" data-dots></span>' +
      '</div>';
    coach.querySelector('[data-next]').addEventListener('click', advance);
    coach.querySelector('[data-skip]').addEventListener('click', finishTour);
    document.body.appendChild(coach);
    return coach;
  }

  function fillCoach(step) {
    var c = tour.coach;
    c.querySelector('[data-step]').textContent = 'Step ' + (tour.index + 1) + ' / ' + TOUR_STEPS.length;
    c.querySelector('[data-title]').textContent = step.title;
    c.querySelector('[data-body]').textContent = step.body;
    c.querySelector('[data-next]').textContent = step.finish ? 'Finish' : 'Next';
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
    switch (placement) {
      case 'right':      left = r.right + gap;      top = r.top; break;
      case 'above':      left = r.left;             top = r.top - ch - gap; break;
      case 'below-left':
      default:           left = r.left;             top = r.bottom + gap; break;
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
    if (tour || !target) return;
    dismissHint();
    opts = opts || {};
    hint = { target: target, coach: null, placement: opts.placement || 'above', timer: null };
    target.classList.add('jo-target-highlight');
    if (opts.pill) target.classList.add('jo-target-highlight--pill');

    var coach = el('div', 'jo-coach jo-coach--hint');
    coach.setAttribute('role', 'dialog');
    coach.setAttribute('aria-live', 'polite');
    coach.innerHTML =
      '<div class="jo-coach-title" data-title></div>' +
      '<div class="jo-coach-body" data-body></div>' +
      '<div class="jo-coach-actions"><button type="button" class="jo-btn-primary" data-got>Got it</button></div>';
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
    if (hint.target) hint.target.classList.remove('jo-target-highlight', 'jo-target-highlight--pill');
    if (hint.coach) hint.coach.remove();
    if (hint.timer) clearTimeout(hint.timer);
    window.removeEventListener('resize', hintReposition);
    window.removeEventListener('scroll', hintReposition, true);
    document.removeEventListener('keydown', hintKey, true);
    hint = null;
  }

  function advance() {
    if (!tour) return;
    if (tour.index < TOUR_STEPS.length - 1) { tour.index++; renderStep(); }
    else finishTour();
  }

  function finishTour() {
    if (!tour) return;
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
    tour = null;
    patch({ tour_completed: true });
  }

  // ---------------------------------------------------------------- signals
  function onAnswer() {
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

      mountChecklist();
      mountCards();
      if (!state.data.welcome_seen_at) showWelcome();
    });
  });
})();
