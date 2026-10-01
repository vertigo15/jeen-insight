// @ts-check
// Settings for the stress runner, read once from the environment. LIVE_* are
// shared with the live suite (tests/e2e/README.md); STRESS_* are this runner's
// own (tests/README.md, "Stress").
const path = require('path');
const { KP_CASES } = require('../e2e/live/kp.cases');

const ROOT = __dirname;
const AUTH_DIR = path.join(ROOT, '.auth');
const RESULTS_DIR = path.join(ROOT, 'results');
const EMAIL_DOMAIN = 'stress.test';
/** Only ever used against localhost; a shared stack needs its own STRESS_USER_PASSWORD. */
const LOCAL_PASSWORD = 'stress-local-Passw0rd';
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);
const STRICTNESS = ['exact', 'equivalent', 'structural'];

/** @param {string} base */
function isRemote(base) {
  return !LOCAL_HOSTS.has(new URL(base).hostname);
}

/**
 * @param {string} name
 * @param {number} fallback
 * @param {number} [min]
 */
function integerEnv(name, fallback, min = 0) {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min) throw new Error(`${name} must be an integer >= ${min}, got "${raw}"`);
  return value;
}

/**
 * A gate: unset means report only. Accuracy and error gates are fractions (0.8 = 80 %).
 * @param {string} name
 * @param {number} [max]
 */
function gateEnv(name, max = Infinity) {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') return null;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0 || value > max) throw new Error(`${name} must be between 0 and ${max}, got "${raw}"`);
  return value;
}

function levelsEnv() {
  const raw = process.env.STRESS_LEVELS || '1,5,10';
  const levels = raw.split(',').map((s) => s.trim()).filter(Boolean).map(Number);
  if (!levels.length || levels.some((n) => !Number.isInteger(n) || n < 1 || n > 99) || new Set(levels).size !== levels.length) {
    throw new Error(`STRESS_LEVELS must list distinct user counts between 1 and 99 (e.g. 1,5,10), got "${raw}"`);
  }
  return levels;
}

function casesEnv() {
  const raw = (process.env.STRESS_CASES || '').trim();
  if (!raw) return KP_CASES;
  const ids = raw.split(',').map((s) => s.trim()).filter(Boolean);
  const unknown = ids.filter((id) => !KP_CASES.some((c) => c.id === id));
  if (unknown.length) {
    throw new Error(`STRESS_CASES: unknown id(s) ${unknown.join(', ')}; kp.cases.js has ${KP_CASES.map((c) => c.id).join(', ')}`);
  }
  return ids.map((id) => /** @type {import('../e2e/live/kp.cases').KpCase} */ (KP_CASES.find((c) => c.id === id)));
}

function loadConfig() {
  const base = (process.env.LIVE_APP_URL || 'http://localhost:8501').replace(/\/+$/, '');
  const remote = isRemote(base);
  const strict = process.env.LIVE_KP_STRICT || 'structural';
  if (!STRICTNESS.includes(strict)) throw new Error(`LIVE_KP_STRICT must be one of ${STRICTNESS.join(' | ')}, got "${strict}"`);
  const prefix = process.env.STRESS_USER_PREFIX || 'stress';
  if (!/^[a-z][a-z0-9-]{0,30}$/.test(prefix)) throw new Error(`STRESS_USER_PREFIX must be lower-case letters, digits and dashes, got "${prefix}"`);
  const cases = casesEnv();
  return {
    base,
    host: new URL(base).host,
    remote,
    allowRemote: process.env.STRESS_ALLOW_REMOTE === '1',
    admin: { email: process.env.LIVE_EMAIL || 'admin', password: process.env.LIVE_PASSWORD || 'admin' },
    connection: process.env.LIVE_CONNECTION || 'AdventureWorksDW',
    strict,
    goldDsn: process.env.LIVE_GOLD_DSN || null,
    levels: levelsEnv(),
    cases,
    questionsPerUser: integerEnv('STRESS_QUESTIONS_PER_USER', cases.length, 1),
    thinkMs: integerEnv('STRESS_THINK_MS', 0),
    askTimeoutMs: integerEnv('STRESS_ASK_TIMEOUT_MS', 300_000, 1_000),
    chart: process.env.STRESS_CHART !== '0',
    cooldownMs: integerEnv('STRESS_COOLDOWN_MS', 30_000),
    users: {
      prefix,
      domain: EMAIL_DOMAIN,
      password: process.env.STRESS_USER_PASSWORD || (remote ? '' : LOCAL_PASSWORD),
    },
    gates: {
      minAccuracy: gateEnv('STRESS_MIN_ACCURACY', 1),
      maxAccuracyDrop: gateEnv('STRESS_MAX_ACCURACY_DROP', 1),
      maxP95Ms: gateEnv('STRESS_MAX_P95_MS'),
      maxErrorRate: gateEnv('STRESS_MAX_ERROR_RATE', 1),
    },
  };
}

/** @typedef {ReturnType<typeof loadConfig>} Config */

/**
 * Refuse to send anything to a shared stack unless it was opted into.
 * @param {Config} config
 */
function assertTargetAllowed(config) {
  if (!config.remote) return;
  if (!config.allowRemote) {
    throw new Error(`${config.base} is not localhost: set STRESS_ALLOW_REMOTE=1 to put stress load on a shared stack`);
  }
  if (!config.users.password) {
    throw new Error('STRESS_USER_PASSWORD is required for a remote target: the stress accounts are created with it');
  }
  if (config.users.password.length < 8) throw new Error('STRESS_USER_PASSWORD must be at least 8 characters (the /api/users rule)');
}

module.exports = { ROOT, AUTH_DIR, RESULTS_DIR, isRemote, loadConfig, assertTargetAllowed };
