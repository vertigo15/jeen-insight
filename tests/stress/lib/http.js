// @ts-check
// A per-user session against the UI server on plain node:http / node:https: a
// cookie jar, the CSRF token the workspace page carries, and the Flask login
// form. fetch is avoided on purpose: its built-in header and body timeouts
// would cut off a question that is only waiting for a free UI-server thread.
const fs = require('fs');
const http = require('http');
const https = require('https');
const path = require('path');
const { AUTH_DIR } = require('../config');

/** `/login` allows 5 POSTs a minute per client IP (src/ui_app.py); stay one under it. */
const LOGINS_PER_MINUTE = 4;
/** @type {number[]} */
const loginTimes = [];

/** @param {number} ms */
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function loginSlot() {
  for (;;) {
    const now = Date.now();
    while (loginTimes.length && now - loginTimes[0] >= 60_000) loginTimes.shift();
    if (loginTimes.length < LOGINS_PER_MINUTE) {
      loginTimes.push(now);
      return;
    }
    const wait = 60_000 - (now - loginTimes[0]) + 500;
    console.log(`[stress] login pacing: next login in ${Math.ceil(wait / 1000)} s (the login route allows 5 a minute per IP)`);
    await sleep(wait);
  }
}

/**
 * @param {string} base
 * @param {string} email
 */
function sessionFile(base, email) {
  const host = new URL(base).host.replace(/[^a-z0-9.-]+/gi, '_');
  return path.join(AUTH_DIR, host, `${email.toLowerCase()}.json`);
}

/** @param {string} file */
function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (_) {
    return null;
  }
}

/** @typedef {{ status: number, headers: import('http').IncomingHttpHeaders, text: string }} Reply */

class Session {
  /**
   * @param {string} base
   * @param {string} email
   */
  constructor(base, email) {
    this.base = new URL(base);
    this.email = email;
    /** @type {Map<string, string>} */
    this.cookies = new Map();
    this.csrf = '';
    /** @type {any} */
    this.user = null;
  }

  get transport() {
    return this.base.protocol === 'https:' ? https : http;
  }

  /**
   * @param {Record<string, string>} extra
   * @param {boolean} mutating
   */
  headers(extra, mutating) {
    /** @type {Record<string, string>} */
    const headers = { Accept: 'application/json', ...extra };
    if (this.cookies.size) headers.Cookie = [...this.cookies].map(([name, value]) => `${name}=${value}`).join('; ');
    if (mutating) {
      // Flask-WTF also checks the Referer when the site runs on HTTPS (WTF_CSRF_SSL_STRICT).
      headers.Referer = `${this.base.origin}/`;
      if (this.csrf) headers['X-CSRFToken'] = this.csrf;
    }
    return headers;
  }

  /** @param {string[] | undefined} setCookie */
  absorb(setCookie) {
    for (const line of setCookie || []) {
      const [pair, ...attributes] = line.split(';');
      const eq = pair.indexOf('=');
      if (eq <= 0) continue;
      const name = pair.slice(0, eq).trim();
      const value = pair.slice(eq + 1).trim();
      const expired = attributes.some((a) => /^\s*max-age\s*=\s*0\s*$/i.test(a))
        || attributes.some((a) => /^\s*expires\s*=/i.test(a) && Date.parse(a.slice(a.indexOf('=') + 1)) < Date.now());
      if (!value || expired) this.cookies.delete(name);
      else this.cookies.set(name, value);
    }
  }

  /**
   * One buffered request. Rejects only on transport errors and the timeout.
   * @param {string} method
   * @param {string} pathname
   * @param {{ json?: any, form?: Record<string, string>, timeoutMs?: number }} [options]
   * @returns {Promise<Reply>}
   */
  request(method, pathname, { json, form, timeoutMs = 60_000 } = {}) {
    /** @type {Record<string, string>} */
    const extra = {};
    let body = null;
    if (json !== undefined) {
      body = Buffer.from(JSON.stringify(json));
      extra['Content-Type'] = 'application/json';
    } else if (form !== undefined) {
      body = Buffer.from(new URLSearchParams(form).toString());
      extra['Content-Type'] = 'application/x-www-form-urlencoded';
    }
    if (body) extra['Content-Length'] = String(body.length);
    const headers = this.headers(extra, method !== 'GET');
    return new Promise((resolve, reject) => {
      const req = this.transport.request(new URL(pathname, this.base), { method, headers }, (res) => {
        this.absorb(res.headers['set-cookie']);
        res.setEncoding('utf8');
        let text = '';
        res.on('data', (chunk) => { text += chunk; });
        res.on('end', () => resolve({ status: res.statusCode || 0, headers: res.headers, text }));
        res.on('error', reject);
      });
      const timer = setTimeout(() => req.destroy(new Error(`${method} ${pathname}: no response within ${timeoutMs / 1000} s`)), timeoutMs);
      req.on('close', () => clearTimeout(timer));
      req.on('error', reject);
      req.end(body || undefined);
    });
  }

  /**
   * JSON in, JSON out; throws with the status and the start of the body unless 2xx.
   * @param {string} method
   * @param {string} pathname
   * @param {any} [json]
   * @param {number} [timeoutMs]
   */
  async call(method, pathname, json, timeoutMs) {
    const reply = await this.request(method, pathname, { json, timeoutMs });
    if (reply.status < 200 || reply.status >= 300) {
      const error = /** @type {Error & { status?: number }} */ (new Error(`${method} ${pathname} → ${reply.status} ${reply.text.slice(0, 300)}`));
      error.status = reply.status;
      throw error;
    }
    return reply.text ? JSON.parse(reply.text) : null;
  }

  /**
   * POST JSON and hand the live response to `onResponse`. Returns the request
   * so the caller can destroy it at its own deadline.
   * @param {string} pathname
   * @param {any} json
   * @param {{ onResponse: (res: import('http').IncomingMessage) => void, onError: (error: Error) => void }} handlers
   */
  stream(pathname, json, { onResponse, onError }) {
    const body = Buffer.from(JSON.stringify(json));
    const headers = this.headers({
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
      'Content-Length': String(body.length),
    }, true);
    const req = this.transport.request(new URL(pathname, this.base), { method: 'POST', headers }, (res) => {
      this.absorb(res.headers['set-cookie']);
      onResponse(res);
    });
    req.on('error', onError);
    req.end(body);
    return req;
  }

  /** The session user, or null when the cookies no longer authenticate. */
  async me() {
    const reply = await this.request('GET', '/api/auth/me');
    this.user = reply.status === 200 ? JSON.parse(reply.text) : null;
    return this.user;
  }

  /** The anti-CSRF token the workspace page exposes (`<meta name="csrf-token">`). */
  async refreshCsrf() {
    const page = await this.request('GET', '/');
    const token = (page.text.match(/<meta name="csrf-token" content="([^"]+)"/) || [])[1];
    if (page.status !== 200 || !token) throw new Error(`GET / → ${page.status}: no csrf-token on the workspace page`);
    this.csrf = token;
  }

  /**
   * Log in through the Flask form, paced under the login rate limit.
   * @param {string} password
   */
  async login(password) {
    this.cookies.clear();
    this.csrf = '';
    const page = await this.request('GET', '/login');
    const token = (page.text.match(/name="csrf_token"\s+value="([^"]+)"/) || [])[1];
    if (!token) throw new Error(`GET /login → ${page.status}: no login form at ${this.base.origin}`);
    for (let attempt = 1; ; attempt += 1) {
      await loginSlot();
      const reply = await this.request('POST', '/login', { form: { csrf_token: token, email: this.email, password, next: '/' } });
      const location = String(reply.headers.location || '');
      if ((reply.status === 302 || reply.status === 303) && !new URL(location, this.base).pathname.startsWith('/login')) break;
      if (reply.status === 429 && attempt < 3) {
        console.log(`[stress] login for ${this.email} was rate limited; retrying in 61 s`);
        await sleep(61_000);
        continue;
      }
      const message = (reply.text.match(/class="[^"]*(?:login-error|error)[^"]*"[^>]*>([^<]+)</) || [])[1] || `HTTP ${reply.status}`;
      const error = /** @type {Error & { status?: number }} */ (new Error(`login as ${this.email} failed: ${message.trim()}`));
      error.status = reply.status;
      throw error;
    }
    await this.refreshCsrf();
    if (!(await this.me())) throw new Error(`login as ${this.email} did not produce a session`);
  }

  save() {
    const file = sessionFile(this.base.href, this.email);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const saved = { email: this.email, cookies: Object.fromEntries(this.cookies), csrf: this.csrf, user: this.user, saved_at: new Date().toISOString() };
    fs.writeFileSync(file, JSON.stringify(saved, null, 2), { mode: 0o600 });
  }
}

/**
 * A session for `email`: the saved one while it still authenticates as that
 * user (sessions last 24 h and CSRF tokens do not expire), else a paced login.
 * @param {string} base
 * @param {string} email
 * @param {string} password
 */
async function openSession(base, email, password) {
  const session = new Session(base, email);
  const saved = readJson(sessionFile(base, email));
  if (saved && saved.cookies && saved.csrf) {
    for (const [name, value] of Object.entries(saved.cookies)) session.cookies.set(name, String(value));
    session.csrf = saved.csrf;
    const user = await session.me();
    if (user && String(user.email).toLowerCase() === email.toLowerCase()) {
      session.save();
      return session;
    }
  }
  await session.login(password);
  session.save();
  return session;
}

module.exports = { Session, openSession, sessionFile, sleep };
