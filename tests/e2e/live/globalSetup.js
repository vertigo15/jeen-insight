// Probe the live stack once. When it is not reachable every test skips itself
// (see skipUnlessLive in _live.js) instead of failing on a connection error,
// and an empty storage state is written so the `live` project can still load.
// When it is reachable, fetch the knowledge pairs (ground truth for @kp) into
// live/.generated/knowledge_pairs.json before the test files are collected.
const fs = require('fs');
const path = require('path');
const { fetchKnowledgePairs, PAIRS_FILE } = require('./knowledgePairs');

const BASE = process.env.LIVE_APP_URL || 'http://localhost:8501';
const AUTH_DIR = path.join(__dirname, '..', '.auth');
const AUTH_FILE = path.join(AUTH_DIR, 'live.json');

module.exports = async () => {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  let reachable = false;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8_000);
    const response = await fetch(`${BASE}/login`, { redirect: 'manual', signal: controller.signal });
    clearTimeout(timer);
    reachable = response.status > 0 && response.status < 500;
  } catch (error) {
    reachable = false;
  }
  process.env.LIVE_UNREACHABLE = reachable ? '0' : '1';
  process.env.LIVE_BASE_RESOLVED = BASE;
  if (!reachable) {
    console.warn(`[live] ${BASE} is not reachable — the live suite will be skipped. Start the stack with 'docker compose up -d'.`);
    if (!fs.existsSync(AUTH_FILE)) fs.writeFileSync(AUTH_FILE, JSON.stringify({ cookies: [], origins: [] }));
    return;
  }

  // Ground truth for the @kp suite. Skipped with LIVE_SKIP_KP_FETCH=1 (reuse
  // the last file, e.g. while iterating on a spec) — the login route allows
  // five attempts a minute and this is one of them.
  if (process.env.LIVE_SKIP_KP_FETCH === '1' && fs.existsSync(PAIRS_FILE)) {
    console.log(`[live] reusing ${path.relative(process.cwd(), PAIRS_FILE)} (LIVE_SKIP_KP_FETCH=1)`);
    return;
  }
  const result = await fetchKnowledgePairs({
    base: BASE,
    email: process.env.LIVE_EMAIL || 'admin',
    password: process.env.LIVE_PASSWORD || 'admin',
    connection: process.env.LIVE_CONNECTION || 'AdventureWorksDW',
  });
  process.env.LIVE_ADMIN = result.role === 'admin' ? '1' : '0';
  if (result.error) {
    console.warn(`[live] knowledge pairs unavailable — ${result.error}. @kp tests run without gold SQL.`);
  } else {
    console.log(`[live] ${result.pairs.length} knowledge pairs for ${result.connection} (${result.source_key}, catalog ${result.catalog_source}) → ${path.relative(process.cwd(), PAIRS_FILE)}`);
  }
};
