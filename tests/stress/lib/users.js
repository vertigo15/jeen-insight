// @ts-check
// The stress accounts: one viewer per simulated user, because the API caps
// parallel questions per account (MAX_CONCURRENT_QUERIES_PER_USER). They are
// created and removed through the admin routes Settings > Users uses, and only
// emails of the form <prefix>-NN@stress.test are ever touched.
const fs = require('fs');
const { openSession, sessionFile } = require('./http');

/**
 * @typedef {import('../config').Config} Config
 * @typedef {import('./http').Session} Session
 */

/**
 * @param {Config['users']} users
 * @param {number} n
 */
function stressEmail(users, n) {
  return `${users.prefix}-${String(n).padStart(2, '0')}@${users.domain}`;
}

/** @param {Config['users']} users */
function stressPattern(users) {
  const escape = (/** @type {string} */ s) => s.replace(/[.*+?^${}()|[\]\\-]/g, '\\$&');
  return new RegExp(`^${escape(users.prefix)}-\\d{2}@${escape(users.domain)}$`);
}

/** @param {Session} admin */
async function listUsers(admin) {
  const users = await admin.call('GET', '/api/users');
  return Array.isArray(users) ? users : [];
}

/**
 * @param {Session} admin
 * @param {string} email
 * @param {string} password
 * @param {number} n
 */
async function createUser(admin, email, password, n) {
  const reply = await admin.request('POST', '/api/users', {
    json: { name: `Stress user ${String(n).padStart(2, '0')}`, email, password, role: 'viewer' },
  });
  if (reply.status !== 201 && reply.status !== 409) throw new Error(`POST /api/users (${email}) → ${reply.status} ${reply.text.slice(0, 200)}`);
  return reply.status === 201;
}

/**
 * @param {Session} admin
 * @param {{ id: number, email: string }} user
 * @param {Config['users']} users
 */
async function deleteUser(admin, user, users) {
  if (!stressPattern(users).test(String(user.email).toLowerCase())) throw new Error(`refusing to delete ${user.email}: not a stress account`);
  await admin.call('DELETE', `/api/users/${user.id}`);
}

/**
 * Sessions for stress users 1..count, creating missing accounts. An account
 * whose password no longer matches STRESS_USER_PASSWORD is recreated.
 * @param {Config} config
 * @param {Session} admin
 * @param {number} count
 */
async function stressSessions(config, admin, count) {
  const existing = new Set((await listUsers(admin)).map((u) => String(u.email).toLowerCase()));
  /** @type {Session[]} */
  const sessions = [];
  for (let n = 1; n <= count; n += 1) {
    const email = stressEmail(config.users, n);
    if (!existing.has(email)) {
      await createUser(admin, email, config.users.password, n);
      console.log(`[stress] created ${email} (viewer)`);
    }
    let session;
    try {
      session = await openSession(config.base, email, config.users.password);
    } catch (error) {
      if (/** @type {any} */ (error).status !== 401) throw error;
      const stale = (await listUsers(admin)).find((u) => String(u.email).toLowerCase() === email);
      if (stale) await deleteUser(admin, stale, config.users);
      await createUser(admin, email, config.users.password, n);
      console.log(`[stress] ${email} had a different password; recreated it`);
      session = await openSession(config.base, email, config.users.password);
    }
    sessions.push(session);
  }
  return sessions;
}

/**
 * Every stress user must be able to pick the connection under test.
 * @param {Session[]} sessions
 * @param {string} sourceKey
 */
async function assertSeesConnection(sessions, sourceKey) {
  for (const session of sessions) {
    const { connections = [] } = await session.call('GET', '/api/connections');
    if (!connections.some((/** @type {any} */ c) => c.source_key === sourceKey)) {
      throw new Error(`${session.email} cannot see connection ${sourceKey}`);
    }
  }
}

/**
 * Delete every conversation of the session's user; returns how many went.
 * @param {Session} session
 */
async function deleteConversations(session) {
  let removed = 0;
  for (;;) {
    const { items = [] } = await session.call('GET', '/api/conversations?all=true&limit=200');
    if (!items.length) return removed;
    let removedNow = 0;
    for (const item of items) {
      const reply = await session.request('DELETE', `/api/conversations/${encodeURIComponent(item.id)}?delete_saved=true`);
      if (reply.status === 200) removedNow += 1;
      else if (reply.status !== 404) throw new Error(`DELETE /api/conversations/${item.id} → ${reply.status} ${reply.text.slice(0, 200)}`);
    }
    if (!removedNow) return removed;
    removed += removedNow;
  }
}

/**
 * Remove the stress accounts and their conversations. Usage-ledger rows stay:
 * there is no route that deletes them.
 * @param {Config} config
 * @param {Session} admin
 */
async function teardown(config, admin) {
  const pattern = stressPattern(config.users);
  const accounts = (await listUsers(admin)).filter((u) => pattern.test(String(u.email).toLowerCase()));
  for (const account of accounts) {
    let removed = 0;
    try {
      removed = await deleteConversations(await openSession(config.base, account.email, config.users.password));
    } catch (error) {
      console.warn(`[stress] ${account.email}: conversations kept (${/** @type {Error} */ (error).message})`);
    }
    await deleteUser(admin, account, config.users);
    fs.rmSync(sessionFile(config.base, account.email), { force: true });
    console.log(`[stress] removed ${account.email} and ${removed} conversation(s)`);
  }
  return accounts.length;
}

module.exports = { stressEmail, stressPattern, stressSessions, assertSeesConnection, teardown };
