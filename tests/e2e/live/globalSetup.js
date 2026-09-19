// Probe the live stack once. When it is not reachable every test skips itself
// (see skipUnlessLive in _live.js) instead of failing on a connection error,
// and an empty storage state is written so the `live` project can still load.
const fs = require('fs');
const path = require('path');

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
  }
};
