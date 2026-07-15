/**
 * csrf.js — MUST load before any other script that issues fetch().
 *
 * The Flask layer (Flask-WTF) protects every mutating request
 * (POST/PUT/PATCH/DELETE) with a per-session CSRF token. Rather than touch the
 * ~30 scattered fetch() call sites, we wrap window.fetch so each same-origin
 * mutating request automatically carries the token in the X-CSRFToken header.
 *
 * The token is read from the <meta name="csrf-token"> tag rendered by the
 * server. Cross-origin requests (CDN modules, fonts, etc.) never receive it.
 */
(function () {
  'use strict';

  const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
  const nativeFetch = window.fetch.bind(window);

  function getToken() {
    const el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute('content') : null;
  }

  function isSameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (_e) {
      // Relative URLs that fail to parse are same-origin by definition.
      return true;
    }
  }

  window.fetch = function (input, init) {
    init = init || {};

    // Resolve method + URL whether input is a string, URL, or Request.
    let url;
    let method;
    if (typeof Request !== 'undefined' && input instanceof Request) {
      url = input.url;
      method = (init.method || input.method || 'GET').toUpperCase();
    } else {
      url = String(input);
      method = (init.method || 'GET').toUpperCase();
    }

    if (MUTATING.has(method) && isSameOrigin(url)) {
      const token = getToken();
      if (token) {
        // Merge onto whatever headers the caller already set (Content-Type,
        // Accept, …) so existing requests keep working unchanged.
        const headers = new Headers(
          init.headers ||
            (typeof Request !== 'undefined' && input instanceof Request
              ? input.headers
              : undefined)
        );
        if (!headers.has('X-CSRFToken')) headers.set('X-CSRFToken', token);
        init = Object.assign({}, init, { headers });
      }
    }

    return nativeFetch(input, init);
  };
})();
