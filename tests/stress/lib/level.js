// @ts-check
// One concurrency level: N stress users start at the same moment and each asks
// its questions back-to-back (STRESS_THINK_MS apart), each in a new
// conversation. User u starts at case u, so different questions are in flight
// together instead of N copies of the same one.
const { ask } = require('./ask');
const { sleep } = require('./http');

/**
 * The case indexes user `user` (0-based) asks, in order: a rotation of the
 * set that wraps around when there are more questions than cases.
 * @param {number} user
 * @param {number} questionsPerUser
 * @param {number} caseCount
 */
function questionOrder(user, questionsPerUser, caseCount) {
  return Array.from({ length: questionsPerUser }, (_, i) => (user + i) % caseCount);
}

/**
 * @param {{
 *   level: number,
 *   sessions: import('./http').Session[],
 *   cases: { id: string, question: string }[],
 *   connection: string,
 *   config: import('../config').Config,
 *   onAnswer: (record: any) => void,
 * }} options
 */
async function runLevel({ level, sessions, cases, connection, config, onAnswer }) {
  const startedAt = new Date().toISOString();
  const t0 = performance.now();
  /** @type {any[]} */
  const answers = [];
  await Promise.all(sessions.slice(0, level).map(async (session, user) => {
    const order = questionOrder(user, config.questionsPerUser, cases.length);
    for (let i = 0; i < order.length; i += 1) {
      const kase = cases[order[i]];
      const answer = await ask(session, { question: kase.question, connection, chart: config.chart, timeoutMs: config.askTimeoutMs });
      const record = { id: `L${level}-u${user + 1}-q${i + 1}`, level, user: user + 1, seq: i + 1, case_id: kase.id, question: kase.question, ...answer };
      answers.push(record);
      onAnswer(record);
      if (config.thinkMs && i < order.length - 1) await sleep(config.thinkMs);
    }
  }));
  return { level, answers, started_at: startedAt, wall_ms: Math.round(performance.now() - t0) };
}

module.exports = { questionOrder, runLevel };
