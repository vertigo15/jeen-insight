/**
 * auth.js — runs on every page load.
 *
 * Fetches /api/auth/me (served by Flask session) and:
 *  1. Exposes window._currentUser for the settings page and other scripts.
 *  2. Renders the avatar circle in the topbar.
 *  3. Wires the avatar → dropdown (account info + sign-out).
 *
 * If the request returns 401 the page redirects to /login (Flask already
 * handles this at the server level, but this covers XHR-style edge cases).
 */

(function () {
  'use strict';

  // ── helpers ────────────────────────────────────────────────────────────────

  /** Return the one- or two-letter initials for a display name. */
  function _initials(name) {
    if (!name) return '?';
    const parts = name.trim().split(/\s+/);
    if (parts.length === 1) return parts[0][0].toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  /** Convert an HSL hue (0-359) to a CSS background colour pair. */
  function _hueStyle(hue) {
    return `background: hsl(${hue}, 55%, 52%); color: #fff;`;
  }

  const t = (key, args) => (window.I18n && typeof window.I18n.t === 'function' ? window.I18n.t(key, args) : String(key));
  const ROLE_CLASS  = { admin: 'role-admin', editor: 'role-editor', viewer: 'role-viewer' };
  const roleLabel = (role) => (window.I18n && window.I18n.has(`settings.users.roles.${role}`) ? t(`settings.users.roles.${role}`) : role);

  // ── bootstrap ──────────────────────────────────────────────────────────────

  async function init() {
    let user;
    try {
      const res = await fetch('/api/auth/me', { credentials: 'same-origin' });
      if (res.status === 401) {
        // Session expired while page was open — reload to trigger Flask redirect.
        window.location.replace('/login');
        return;
      }
      if (!res.ok) return;
      user = await res.json();
    } catch {
      return; // network error — don't crash the rest of the app
    }

    window._currentUser = user;

    // ── Topbar avatar ──────────────────────────────────────────────────────
    const btn    = document.getElementById('user-avatar-btn');
    const avatar = document.getElementById('user-avatar');
    if (btn && avatar) {
      avatar.textContent  = _initials(user.name);
      avatar.style.cssText = _hueStyle(user.avatar_hue ?? 220);
      btn.style.display   = 'flex';
    }

    // ── Dropdown info ──────────────────────────────────────────────────────
    const nameEl  = document.getElementById('user-dropdown-name');
    const emailEl = document.getElementById('user-dropdown-email');
    const roleEl  = document.getElementById('user-role-badge');
    if (nameEl)  nameEl.textContent  = user.name  || '';
    if (emailEl) emailEl.textContent = user.email || '';
    if (roleEl) {
      roleEl.textContent  = roleLabel(user.role);
      roleEl.className    = 'user-role-badge ' + (ROLE_CLASS[user.role] || '');
    }

    // ── Toggle dropdown on click ───────────────────────────────────────────
    const drop = document.getElementById('user-dropdown');
    if (btn && drop) {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const open = !drop.hidden;
        drop.hidden = open;
        btn.setAttribute('aria-expanded', String(!open));
      });

      // Close on outside click
      document.addEventListener('click', () => {
        drop.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
      });
    }

    // ── Interface language ─────────────────────────────────────────────────
    // An account property (PATCH /api/auth/me/locale). The save is not
    // optimistic: the page reloads only after the server persisted the value
    // and set the cookie, so a failed save never leaves a half-flipped shell.
    _wireLanguagePicker(drop);

    // ── Sign out ────────────────────────────────────────────────────────────
    // /logout is POST-only + CSRF-protected, so a bare <a href> no longer
    // works. Issue a token-bearing POST (csrf.js adds the header) then redirect.
    const logoutLink = document.querySelector('.user-dropdown-logout');
    if (logoutLink) {
      logoutLink.addEventListener('click', async (e) => {
        e.preventDefault();
        try {
          await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
        } catch {
          /* ignore network errors — redirect regardless */
        }
        window.location.replace('/login');
      });
    }

    _surfaceConnectorResult();
  }

  function _wireLanguagePicker(drop) {
    const group = document.getElementById('user-lang-picker');
    if (!group) return;
    const options = [...group.querySelectorAll('.user-lang-option')];
    const current = (window.I18n && window.I18n.locale) || document.documentElement.lang || 'en';
    const setBusy = (busy) => {
      group.setAttribute('aria-busy', String(busy));
      options.forEach((b) => { b.disabled = busy; });
    };
    const choose = async (tag) => {
      if (!tag || tag === current) return;
      setBusy(true);
      try {
        const res = await fetch('/api/auth/me/locale', {
          method: 'PATCH',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ locale: tag }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        // Server has set the cookie + session; the shell is built once at boot,
        // so a reload is what renders everything in the new language.
        window.location.reload();
      } catch (err) {
        console.warn('[auth] language save failed:', err);
        setBusy(false);
        if (typeof window.showToast === 'function') {
          window.showToast(t('settings.general.language.saveFailed'), 'error');
        }
      }
    };
    // Keep the dropdown open while saving (the document click handler closes it).
    group.addEventListener('click', (e) => e.stopPropagation());
    options.forEach((button) => button.addEventListener('click', () => choose(button.dataset.locale)));
    group.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const rtl = Boolean(window.I18n && window.I18n.isRtl);
      const forward = rtl ? 'ArrowLeft' : 'ArrowRight';
      const idx = Math.max(0, options.indexOf(document.activeElement));
      let next = idx;
      if (event.key === forward) next = (idx + 1) % options.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = options.length - 1;
      else next = (idx - 1 + options.length) % options.length;
      event.preventDefault();
      options[next].focus();
      choose(options[next].dataset.locale);
    });
    if (drop) drop.addEventListener('keydown', (e) => { if (e.key === 'Escape') drop.hidden = true; });
  }

  // Surface OAuth connect results bounced back from /integrations/callback.
  function _surfaceConnectorResult() {
    const params = new URLSearchParams(window.location.search);
    const result = params.get('connector_result');
    if (!result) return;
    const msg = params.get('connector_msg') || '';
    const toast = (m, t) => {
      if (typeof window.showToast === 'function') window.showToast(m, t);
    };
    if (result === 'connected') {
      toast(t('connection.established'), 'success');
    } else {
      toast(msg ? t('connection.connectFailedDetail', { detail: window.I18n ? window.I18n.isolate(msg) : msg }) : t('connection.connectFailed'), 'error');
    }
    // Strip the params so a refresh doesn't re-fire the toast.
    params.delete('connector_result');
    params.delete('connector_msg');
    const qs = params.toString();
    const clean = window.location.pathname + (qs ? '?' + qs : '');
    window.history.replaceState({}, '', clean);
  }

  // Run after DOM is ready.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
