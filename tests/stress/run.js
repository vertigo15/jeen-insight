#!/usr/bin/env node
// @ts-check
// Stress test: 1, then 5, then 10 users (STRESS_LEVELS) ask the curated
// knowledge-pair questions in parallel through the UI server's streaming
// endpoint, the way the workspace does. Every answer is timed and graded
// against its knowledge pair's gold SQL. Settings and how to read the report:
// tests/README.md, "Stress".
//
//   node run.js                             every level, then the report
//   node run.js users                       create the stress accounts and log them in
//   node run.js report [<dir>] [--regrade]  grade and render a run (the newest by default)
//   node run.js teardown                    delete the stress accounts and their conversations
//
// Answers are saved as they arrive, so an interrupted run keeps them and
// `node run.js report <dir>` grades and renders what is there.
// Exit code: 0 done, 1 a gate failed, 2 the run could not complete.
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const { RESULTS_DIR, ROOT, loadConfig, assertTargetAllowed } = require('./config');
const { Session, openSession, sleep } = require('./lib/http');
const { stressSessions, assertSeesConnection, teardown } = require('./lib/users');
const { ask } = require('./lib/ask');
const { runLevel } = require('./lib/level');
const { failureOf, gradeAll } = require('./lib/grade');
const { appendAnswer, readAnswers, writeAnswers, summarize, renderMarkdown, renderConsole, percentile } = require('./lib/report');
const { parsePairs, findPair, PROMPT_NAME } = require('../e2e/live/knowledgePairs');
const { sqlglotDialect } = require('../e2e/live/sqlEquivalence');

/** @typedef {import('./config').Config} Config */

/** @param {number | null | undefined} ms */
const sec = (ms) => (ms == null ? '–' : `${(ms / 1000).toFixed(1)}s`);
/** @param {string} target */
const rel = (target) => path.relative(process.cwd(), target) || target;

/** @param {string} text */
function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch (_) {
    return {};
  }
}

/** @param {string} file */
function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (_) {
    return null;
  }
}

/**
 * @param {string} file
 * @param {any} value
 */
function writeJson(file, value) {
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
}

/**
 * Health, then the admin session that creates the accounts and reads the knowledge pairs.
 * @param {Config} config
 */
async function preflight(config) {
  assertTargetAllowed(config);
  let health;
  try {
    health = await new Session(config.base, '').request('GET', '/health', { timeoutMs: 15_000 });
  } catch (error) {
    throw new Error(`${config.base} is not reachable (${/** @type {Error} */ (error).message}); start the stack with 'docker compose up -d'`);
  }
  if (health.status !== 200 || parseJson(health.text).ui_status !== 'healthy') {
    throw new Error(`${config.base}/health → ${health.status} ${health.text.slice(0, 200)}`);
  }
  const admin = await openSession(config.base, config.admin.email, config.admin.password);
  if (admin.user.role !== 'admin') {
    throw new Error(`${config.admin.email} has role "${admin.user.role}"; the runner needs an admin login (LIVE_EMAIL / LIVE_PASSWORD)`);
  }
  return admin;
}

/**
 * @param {Session} session
 * @param {string} name  display name or source_key
 */
async function resolveConnection(session, name) {
  const { connections = [] } = await session.call('GET', '/api/connections');
  const match = connections.find((/** @type {any} */ c) => c.display_name === name || c.source_key === name);
  if (!match) {
    throw new Error(`connection "${name}" not found (LIVE_CONNECTION); available: ${connections.map((/** @type {any} */ c) => c.display_name).join(', ')}`);
  }
  return {
    name: String(match.display_name),
    source_key: String(match.source_key),
    database_type: String(match.database_type || ''),
    is_power_bi: Boolean(match.is_power_bi),
  };
}

/**
 * The gold SQL of each case, read from the resolved system prompt like the live suite does.
 * @param {Session} admin
 * @param {Config} config
 * @param {string} sourceKey
 */
async function goldFor(admin, config, sourceKey) {
  const body = await admin.call('GET', `/api/settings/prompts/${PROMPT_NAME}/resolved?connection=${encodeURIComponent(sourceKey)}`, undefined, 90_000);
  const pairs = parsePairs(body.resolved_content || '');
  return {
    catalogSource: String(body.catalog_source || 'unknown'),
    cases: config.cases.map((c) => ({ id: c.id, question: c.question, rows: c.rows || null, gold_sql: findPair(pairs, c.question)?.sql || null })),
  };
}

/** @param {Session} admin */
async function appInfo(admin) {
  const info = await admin.call('GET', '/api/settings/app-info', undefined, 15_000).catch(() => ({}));
  return { version: info.version ?? null, model: info.llm_model || info.active_model || info.model || null };
}

/**
 * @param {Session} admin
 * @param {string} fallback
 */
async function catalogSource(admin, fallback) {
  const status = await admin.call('GET', '/api/mcp/status', undefined, 15_000).catch(() => ({}));
  return String(status.catalog_source || fallback);
}

function gitState() {
  try {
    const sha = execFileSync('git', ['rev-parse', '--short', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim();
    const dirty = execFileSync('git', ['status', '--porcelain'], { cwd: ROOT, encoding: 'utf8' }).trim() !== '';
    return { sha, dirty };
  } catch (_) {
    return { sha: null, dirty: null };
  }
}

/** @param {Config} config */
function createRunDir(config) {
  const d = new Date();
  const pad = (/** @type {number} */ n) => String(n).padStart(2, '0');
  const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`;
  const host = new URL(config.base).hostname.replace(/[^a-z0-9.-]+/gi, '_');
  for (let n = 1; ; n += 1) {
    const dir = path.join(RESULTS_DIR, `${stamp}-${host}${n > 1 ? `-${n}` : ''}`);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
      return dir;
    }
  }
}

/**
 * @param {any} run
 * @param {Config} config
 */
function gradingContext(run, config) {
  return {
    cases: new Map(run.cases.map((/** @type {any} */ c) => [c.id, c])),
    dialect: run.dialect,
    language: run.language,
    goldDsn: config.goldDsn,
    strict: config.strict,
  };
}

/** @param {any} record */
function progressLine(record) {
  const tag = record.warmup ? '[warm-up]' : `[L${record.level} u${String(record.user).padStart(2, '0')} q${String(record.seq).padStart(2, '0')}]`;
  const failure = failureOf(record);
  if (failure) return `${tag} ${record.case_id}: failed (${failure.kind}) ${failure.detail}`;
  const chart = record.chart ? `  chart ${record.chart.status === 200 ? sec(record.chart.ready_ms) : `HTTP ${record.chart.status}`}` : '';
  return `${tag} ${record.case_id.padEnd(24)} open ${sec(record.t.open)}  table ${sec(record.t.table)}  answer ${sec(record.t.answer)}${chart}`;
}

/**
 * @param {number} level
 * @param {any[]} answers
 * @param {number} wallMs
 */
function levelLine(level, answers, wallMs) {
  /** @type {Record<string, number>} */
  const counts = { correct: 0, wrong: 0, failed: 0, ungraded: 0 };
  for (const answer of answers) counts[answer.verdict] = (counts[answer.verdict] || 0) + 1;
  const times = answers.filter((a) => a.verdict !== 'failed').map((a) => a.t.answer);
  return `[stress] level ${level} done in ${(wallMs / 60_000).toFixed(1)} min: ${counts.correct}/${counts.correct + counts.wrong} correct, `
    + `${counts.failed} failed, ${counts.ungraded} ungraded · answer p50 ${sec(percentile(times, 50))} / p95 ${sec(percentile(times, 95))}`;
}

/**
 * @param {string} dir
 * @param {any} run
 * @param {any[]} answers
 * @param {Config} config
 */
function render(dir, run, answers, config) {
  const summary = summarize(run, answers, config.strict, config.gates);
  writeJson(path.join(dir, 'summary.json'), { run, strict: config.strict, gates_set: config.gates, ...summary });
  fs.writeFileSync(path.join(dir, 'report.md'), renderMarkdown(run, summary, config.strict));
  console.log(`\n${renderConsole(summary)}\n`);
  console.log(`[stress] report: ${rel(path.join(dir, 'report.md'))}`);
  const failed = summary.gates.filter((g) => !g.ok);
  for (const gate of failed) console.log(`[stress] gate failed: ${gate.gate} at ${gate.level} user(s): ${gate.note}`);
  return failed.length ? 1 : 0;
}

/** @param {Config} config */
async function runAll(config) {
  const admin = await preflight(config);
  const connection = await resolveConnection(admin, config.connection);
  const gold = await goldFor(admin, config, connection.source_key);
  const missing = gold.cases.filter((c) => !c.gold_sql).map((c) => c.id);
  if (missing.length) console.warn(`[stress] no knowledge pair matches ${missing.join(', ')}: those answers are timed but not graded`);
  const planned = config.levels.reduce((n, level) => n + level * config.questionsPerUser, 0);
  console.log(`[stress] ${config.base} · ${connection.name} · levels ${config.levels.join(', ')} · ${config.questionsPerUser} question(s) per user`
    + ` → ${planned} answers + 1 warm-up · chart requests ${config.chart ? 'on' : 'off'}`);
  if (config.remote) {
    console.log(`[stress] ${config.host} is a shared stack: starting in 10 s (Ctrl-C to abort)`);
    await sleep(10_000);
  }
  const sessions = await stressSessions(config, admin, Math.max(...config.levels));
  await assertSeesConnection(sessions, connection.source_key);

  const dir = createRunDir(config);
  const runFile = path.join(dir, 'run.json');
  const answersFile = path.join(dir, 'answers.jsonl');
  const run = {
    id: path.basename(dir),
    target: config.base,
    connection,
    dialect: sqlglotDialect(connection.database_type),
    language: connection.is_power_bi ? 'dax' : 'sql',
    catalog_source: await catalogSource(admin, gold.catalogSource),
    app: await appInfo(admin),
    git: gitState(),
    node: process.version,
    settings: {
      levels: config.levels,
      questions_per_user: config.questionsPerUser,
      cases: config.cases.map((c) => c.id),
      think_ms: config.thinkMs,
      ask_timeout_ms: config.askTimeoutMs,
      chart: config.chart,
      cooldown_ms: config.cooldownMs,
    },
    cases: gold.cases,
    started_at: new Date().toISOString(),
    ended_at: /** @type {string | null} */ (null),
    /** @type {{ level: number, started_at: string | null, wall_ms: number, partial?: boolean }[]} */
    levels: [],
  };
  writeJson(runFile, run);
  console.log(`[stress] results: ${rel(dir)} (answers are saved as they arrive)`);
  const context = gradingContext(run, config);
  /** @type {any[]} */
  const all = [];

  const first = config.cases[0];
  const warmup = {
    id: 'warmup', level: 0, user: 1, seq: 0, warmup: true, case_id: first.id, question: first.question,
    ...(await ask(sessions[0], { question: first.question, connection: connection.source_key, chart: config.chart, timeoutMs: config.askTimeoutMs })),
  };
  appendAnswer(answersFile, warmup);
  all.push(warmup);
  console.log(progressLine(warmup));

  for (const [index, level] of config.levels.entries()) {
    console.log(`[stress] level ${level}: ${level} user(s) × ${config.questionsPerUser} question(s), starting together`);
    const outcome = await runLevel({
      level,
      sessions,
      cases: config.cases,
      connection: connection.source_key,
      config,
      onAnswer: (record) => {
        appendAnswer(answersFile, record);
        console.log(progressLine(record));
      },
    });
    all.push(...outcome.answers);
    run.levels.push({ level, started_at: outcome.started_at, wall_ms: outcome.wall_ms });
    gradeAll(all, context);
    writeAnswers(answersFile, all);
    writeJson(runFile, run);
    console.log(levelLine(level, outcome.answers, outcome.wall_ms));
    if (index < config.levels.length - 1 && config.cooldownMs) {
      console.log(`[stress] cooldown ${Math.round(config.cooldownMs / 1000)} s`);
      await sleep(config.cooldownMs);
    }
  }
  run.ended_at = new Date().toISOString();
  writeJson(runFile, run);
  for (const session of [admin, ...sessions]) session.save();
  return render(dir, run, all, config);
}

/** @param {any[]} answers */
function spanOf(answers) {
  const starts = answers.map((a) => Date.parse(a.started_at)).filter(Number.isFinite);
  const ends = answers.map((a) => Date.parse(a.started_at) + (a.t.done ?? a.t.answer ?? 0)).filter(Number.isFinite);
  return starts.length ? Math.max(...ends) - Math.min(...starts) : 0;
}

function newestRun() {
  const dirs = fs.existsSync(RESULTS_DIR) ? fs.readdirSync(RESULTS_DIR).sort().reverse() : [];
  const found = dirs.find((name) => fs.existsSync(path.join(RESULTS_DIR, name, 'run.json')));
  if (!found) throw new Error(`no runs in ${rel(RESULTS_DIR)}`);
  return path.join(RESULTS_DIR, found);
}

/**
 * @param {Config} config
 * @param {string[]} args
 */
async function reportCommand(config, args) {
  const target = args.find((a) => !a.startsWith('--'));
  const dir = target ? path.resolve(target) : newestRun();
  const run = readJson(path.join(dir, 'run.json'));
  if (!run) throw new Error(`${rel(dir)} has no run.json`);
  const answersFile = path.join(dir, 'answers.jsonl');
  const answers = readAnswers(answersFile);
  if (!answers.length) throw new Error(`${rel(answersFile)} has no answers`);
  // An interrupted run has answers for a level it never finished.
  for (const level of new Set(answers.filter((a) => !a.warmup).map((a) => a.level))) {
    if (!run.levels.some((/** @type {any} */ l) => l.level === level)) {
      run.levels.push({ level, started_at: null, wall_ms: spanOf(answers.filter((a) => a.level === level)), partial: true });
    }
  }
  const order = run.settings.levels;
  run.levels.sort((/** @type {any} */ a, /** @type {any} */ b) => order.indexOf(a.level) - order.indexOf(b.level));
  gradeAll(answers, gradingContext(run, config), args.includes('--regrade'));
  writeAnswers(answersFile, answers);
  return render(dir, run, answers, config);
}

/** @param {Config} config */
async function usersCommand(config) {
  const admin = await preflight(config);
  const connection = await resolveConnection(admin, config.connection);
  const sessions = await stressSessions(config, admin, Math.max(...config.levels));
  await assertSeesConnection(sessions, connection.source_key);
  console.log(`[stress] ${sessions.length} stress account(s) ready on ${config.base} and able to use ${connection.name}: ${sessions.map((s) => s.email).join(', ')}`);
  return 0;
}

/** @param {Config} config */
async function teardownCommand(config) {
  const admin = await preflight(config);
  const count = await teardown(config, admin);
  console.log(`[stress] ${count ? `removed ${count} stress account(s)` : 'no stress accounts to remove'} on ${config.base}`);
  return 0;
}

async function main() {
  const [command = 'run', ...args] = process.argv.slice(2);
  const config = loadConfig();
  if (command === 'run') return runAll(config);
  if (command === 'users') return usersCommand(config);
  if (command === 'report') return reportCommand(config, args);
  if (command === 'teardown') return teardownCommand(config);
  throw new Error(`unknown command "${command}" (run | users | report | teardown)`);
}

main().then(
  (code) => { process.exitCode = code; },
  (error) => {
    console.error(`[stress] ${error && error.message ? error.message : error}`);
    process.exitCode = 2;
  },
);
