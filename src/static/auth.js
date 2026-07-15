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

  const ROLE_LABELS = { admin: 'Admin', editor: 'Editor', viewer: 'Viewer' };
  const ROLE_CLASS  = { admin: 'role-admin', editor: 'role-editor', viewer: 'role-viewer' };

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
      roleEl.textContent  = ROLE_LABELS[user.role] || user.role;
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
  }

  // Run after DOM is ready.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
