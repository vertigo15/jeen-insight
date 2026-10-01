// @ts-check
// Offline checks for the parts of the stress runner a full run would only
// exercise after an hour: stream framing and timings (against a fake UI
// server on localhost), question rotation, verdicts, result fingerprints,
// percentiles, gates and the report. `npm test`; no stack needed, only the
// repo .venv for the SQL grader.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { assertTargetAllowed, isRemote, loadConfig } = require('./config');
const { Session } = require('./lib/http');
const { ask, answerText, createSseParser, statusKind } = require('./lib/ask');
const { questionOrder } = require('./lib/level');
const { failureOf, fingerprint, gradeAnswer, verdictOf } = require('./lib/grade');
const { percentile, readAnswers, renderMarkdown, summarize } = require('./lib/report');

/** @param {number} ms */
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('SSE framing follows the workspace parser across chunk boundaries', () => {
  /** @type {[string, any][]} */
  const events = [];
  const parser = createSseParser((event, data) => events.push([event, data]));
  const stream = [
    'event: open\ndata: {"status":"connected"}\n\n',
    ': heartbeat\n\n',
    'event: partial\r\ndata: {"query_id":"q1","results":{"rows":[{"a":1}]}}\r\n\r\n',
    'event: result\ndata: {"sql":"SELECT 1",\ndata: "answer":"x"}\n\n',
    'event: error\ndata: not json\n\n',
  ].join('');
  for (let i = 0; i < stream.length; i += 7) parser.push(stream.slice(i, i + 7));
  assert.deepEqual(events.map(([event]) => event), ['open', 'partial', 'result', 'error']);
  assert.equal(events[1][1].query_id, 'q1');
  assert.equal(events[2][1].answer, 'x');
  assert.deepEqual(events[3][1], { detail: 'not json' });
});

test('answer fragments flatten like the workspace textOf', () => {
  assert.equal(answerText([{ t: 'Sales were ' }, { t: '12', hl: 'num' }, ' in total.']), 'Sales were 12 in total.');
  assert.equal(answerText(null), '');
  assert.equal(answerText('plain'), 'plain');
});

test('HTTP statuses map to failure kinds', () => {
  assert.equal(statusKind(429), 'rate_limited');
  assert.equal(statusKind(503), 'unavailable');
  assert.equal(statusKind(401), 'auth');
  assert.equal(statusKind(400), 'http_error');
});

test('each user walks the set from its own offset and wraps around', () => {
  assert.deepEqual(questionOrder(0, 3, 12), [0, 1, 2]);
  assert.deepEqual(questionOrder(4, 3, 12), [4, 5, 6]);
  assert.deepEqual(questionOrder(11, 3, 12), [11, 0, 1]);
  assert.deepEqual(questionOrder(0, 5, 3), [0, 1, 2, 0, 1]);
  const firstQuestions = Array.from({ length: 10 }, (_, user) => questionOrder(user, 1, 12)[0]);
  assert.equal(new Set(firstQuestions).size, 10, 'ten users start on ten different questions');
});

test('nearest-rank percentiles, like the live scorecard', () => {
  const values = [5, 1, 4, 2, 3, 10, 9, 8, 7, 6];
  assert.equal(percentile(values, 50), 5);
  assert.equal(percentile(values, 95), 10);
  assert.equal(percentile([], 50), null);
  assert.equal(percentile([null, 3], 50), 3);
});

test('a shared stack needs an explicit opt-in and its own password', () => {
  assert.equal(isRemote('http://localhost:8501'), false);
  assert.equal(isRemote('http://127.0.0.1:8501'), false);
  assert.equal(isRemote('http://jeen-insights.dev161.internal'), true);
  const saved = { ...process.env };
  try {
    process.env.LIVE_APP_URL = 'http://jeen-insights.dev161.internal';
    delete process.env.STRESS_ALLOW_REMOTE;
    delete process.env.STRESS_USER_PASSWORD;
    assert.throws(() => assertTargetAllowed(loadConfig()), /STRESS_ALLOW_REMOTE=1/);
    process.env.STRESS_ALLOW_REMOTE = '1';
    assert.throws(() => assertTargetAllowed(loadConfig()), /STRESS_USER_PASSWORD/);
    process.env.STRESS_USER_PASSWORD = 'a-long-enough-password';
    assert.doesNotThrow(() => assertTargetAllowed(loadConfig()));
    process.env.STRESS_LEVELS = '5,5';
    assert.throws(() => loadConfig(), /distinct/);
  } finally {
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
    Object.assign(process.env, saved);
  }
});

test('the cookie jar keeps, replaces and expires cookies', () => {
  const session = new Session('http://localhost:8501', 'someone@stress.test');
  session.absorb(['session=abc; Path=/; HttpOnly', 'locale=en; Path=/']);
  session.absorb(['session=def; Path=/; HttpOnly', 'locale=; Max-Age=0; Path=/']);
  assert.deepEqual(Object.fromEntries(session.cookies), { session: 'def' });
  session.csrf = 'token';
  const headers = session.headers({}, true);
  assert.equal(headers.Cookie, 'session=def');
  assert.equal(headers['X-CSRFToken'], 'token');
  assert.equal(session.headers({}, false)['X-CSRFToken'], undefined);
});

// ── ask() against a fake UI server ───────────────────────────────────────────

const ROWS = [{ year: 2006, amount: 100.5 }, { year: 2007, amount: 200.25 }];

/** @param {http.ServerResponse} res @param {string} event @param {any} data */
const send = (res, event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

/** Streams like the UI server: open, node, partial, result, enrichment; `busy` → 429, `hang` → never answers. */
function fakeServer() {
  /** @type {any[]} */
  const charts = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', async () => {
      const payload = body ? JSON.parse(body) : {};
      if (req.url === '/api/generate-chart') {
        charts.push({ payload, csrf: req.headers['x-csrftoken'] });
        await sleep(30);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ chart_type: 'bar', chart_spec: { chart_type: 'bar', x: 'year', y: ['amount'] }, chart_config: {} }));
        return;
      }
      if (req.url !== '/api/ask/stream') { res.writeHead(404); res.end(); return; }
      if (payload.question === 'busy') {
        res.writeHead(429, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Too many concurrent queries. Please wait for the current one to finish.' }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Set-Cookie': 'session=refreshed; Path=/' });
      send(res, 'open', { status: 'connected' });
      if (payload.question === 'hang') return;
      await sleep(20);
      send(res, 'node', { node: 'router', status: 'node_started' });
      res.write(': heartbeat\n\n');
      await sleep(20);
      send(res, 'partial', { query_id: 'q-1', session_id: 's-1', sql: 'SELECT year, amount FROM t', results: { columns: ['year', 'amount'], rows: ROWS }, revision: 0, provisional: true });
      await sleep(60);
      send(res, 'result', {
        question: payload.question, query_id: 'q-1', session_id: 's-1', sql: 'SELECT year, amount FROM t',
        results: { columns: ['year', 'amount'], rows: ROWS, row_count: 2 },
        answer: [{ t: 'Amount rose to ' }, { t: '200.25', hl: 'num' }], error: null,
        routing: { path: 'sql', route: 'needs_query' }, metrics: { llm_latency_ms: 1234, total_tokens: 900, llm_call_count: 3, retry_count: 0 },
        trace: [{ node: 'router', elapsed_ms: 5, type: 'llm' }, { node: 'sql_generation', elapsed_ms: 40, type: 'llm' }], prompt: { huge: 'x'.repeat(1000) }, saving: true,
      });
      await sleep(20);
      send(res, 'enrichment', { trace_tail: [{ node: 'save_to_memory', elapsed_ms: 15, type: 'db', after_answer: true }] });
      res.end();
    });
  });
  return { server, charts };
}

test('ask() times the stream, mirrors the chart request and classifies failures', async () => {
  const { server, charts } = fakeServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(null)));
  const port = /** @type {import('net').AddressInfo} */ (server.address()).port;
  try {
    const session = new Session(`http://127.0.0.1:${port}`, 'someone@stress.test');
    session.csrf = 'token';
    const answer = await ask(session, { question: 'Sales by year', connection: 'aw', chart: true, timeoutMs: 5_000 });
    assert.equal(answer.error, null);
    assert.equal(answer.http_status, 200);
    const { open, first_node: firstNode, table, answer: full, done } = answer.t;
    assert.ok(open !== null && firstNode !== null && table !== null && full !== null && done !== null, JSON.stringify(answer.t));
    assert.ok(open <= firstNode && firstNode <= table && table <= full && full <= done, JSON.stringify(answer.t));
    assert.equal(answer.result.answer, 'Amount rose to 200.25');
    assert.equal(answer.result.results.row_count, 2);
    assert.equal(answer.result.prompt, undefined, 'the prompt is not kept');
    assert.deepEqual(answer.result.trace.map((/** @type {any} */ s) => s.node), ['router', 'sql_generation', 'save_to_memory']);
    assert.equal(answer.chart.status, 200);
    assert.deepEqual(answer.chart.encodings, ['year', 'amount']);
    assert.ok(answer.chart.ready_ms >= table);
    assert.deepEqual(charts.map((c) => c.payload), [{ connection: 'aw', query_id: 'q-1', question: 'Sales by year', chart_type: 'auto' }]);
    assert.equal(charts[0].csrf, 'token');
    assert.equal(session.cookies.get('session'), 'refreshed');
    assert.equal(failureOf(answer), null);

    const busy = await ask(session, { question: 'busy', connection: 'aw', chart: true, timeoutMs: 5_000 });
    assert.equal(busy.error?.kind, 'rate_limited');
    assert.match(String(busy.error?.detail), /Too many concurrent queries/);

    const hang = await ask(session, { question: 'hang', connection: 'aw', chart: false, timeoutMs: 300 });
    assert.equal(hang.error?.kind, 'timeout');
    assert.notEqual(hang.t.open, null);
    assert.equal(hang.t.answer, null);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(() => resolve(null)));
  }
});

// ── Grading ──────────────────────────────────────────────────────────────────

/** @param {any} [overrides] */
function answered(overrides = {}) {
  return {
    error: null,
    t: { open: 50, table: 1_000, answer: 2_000, done: 2_500 },
    result: {
      sql: 'SELECT CalendarYear, SUM(SalesAmount) FROM FactInternetSales GROUP BY CalendarYear',
      results: { columns: ['year', 'amount'], rows: [{ year: 2006, amount: 100 }, { year: 2007, amount: 200 }], row_count: 2 },
      answer: 'Sales reached 200 in 2007.',
      routing: { path: 'sql' },
      metrics: {},
      trace: [],
      ...overrides,
    },
  };
}

test('records that hold no answer are failures of a named kind', () => {
  assert.equal(failureOf({ error: { kind: 'timeout', detail: 'x' } })?.kind, 'timeout');
  assert.equal(failureOf({ error: null, result: null })?.kind, 'no_result');
  assert.equal(failureOf(answered({ routing: { path: 'ml' } }))?.kind, 'not_sql');
  assert.equal(failureOf(answered({ proposal: true }))?.kind, 'not_sql');
  assert.equal(failureOf(answered({ clarification: true }))?.kind, 'clarification');
  assert.equal(failureOf(answered({ error: 'Backend unavailable: boom', results: null }))?.kind, 'unavailable');
  assert.equal(failureOf(answered({ error: 'column does not exist', results: null }))?.kind, 'answer_error');
  assert.equal(failureOf(answered({ sql: null }))?.kind, 'no_sql');
  assert.equal(failureOf(answered()), null);
});

test('verdicts: failed, wrong, ungraded and correct at a strictness', () => {
  const base = { failure: null, tier: 'structural', rows_ok: true, row_count: 2, write_sql: false };
  assert.equal(verdictOf({ failure: { kind: 'timeout' } }, 'structural').verdict, 'failed');
  assert.equal(verdictOf(base, 'structural').verdict, 'correct');
  assert.equal(verdictOf(base, 'exact').verdict, 'wrong');
  assert.equal(verdictOf({ ...base, tier: 'execution_match' }, 'exact').verdict, 'correct');
  assert.equal(verdictOf({ ...base, tier: 'mismatch' }, 'structural').verdict, 'wrong');
  assert.equal(verdictOf({ ...base, rows_ok: false }, 'structural').verdict, 'wrong');
  assert.equal(verdictOf({ ...base, write_sql: true }, 'structural').verdict, 'wrong');
  assert.equal(verdictOf({ ...base, tier: 'no_gold' }, 'structural').verdict, 'ungraded');
  assert.equal(verdictOf({ ...base, tier: 'ungraded' }, 'structural').verdict, 'ungraded');
  assert.equal(verdictOf(null, 'structural').verdict, 'ungraded');
});

test('result fingerprints ignore names, column order, row order and formatting', () => {
  const a = { rows: [{ year: 2006, total: 1234.567 }, { year: 2007, total: 10 }] };
  const b = { rows: [[10, '2007'], ['$1,234.57', 2006]] };
  const c = { rows: [{ y: 2006, t: 1234.5 }, { y: 2007, t: 10 }] };
  assert.equal(fingerprint(a), fingerprint(b));
  assert.notEqual(fingerprint(a), fingerprint(c));
  assert.equal(fingerprint({ rows: [{ d: '2008-01-01T00:00:00' }] }), fingerprint({ rows: [{ day: '2008-01-01' }] }));
  assert.equal(fingerprint({ rows: [] }), 'empty');
});

test('grading runs the project SQL grader against the gold (needs the repo .venv)', () => {
  const kase = { rows: [2, 5], gold_sql: 'SELECT CalendarYear, SUM(SalesAmount) FROM FactInternetSales GROUP BY CalendarYear' };
  const context = { kase, dialect: 'postgres', language: /** @type {'sql'} */ ('sql'), goldDsn: null };
  const same = gradeAnswer(answered(), context);
  assert.equal(same.tier, 'exact', JSON.stringify(same));
  assert.equal(same.rows_ok, true);
  assert.equal(same.grounded.grounded, true);
  assert.equal(verdictOf(same, 'structural').verdict, 'correct');
  const other = gradeAnswer(answered({ sql: 'SELECT COUNT(*) FROM DimCustomer' }), context);
  assert.equal(other.tier, 'mismatch', JSON.stringify(other));
  assert.equal(verdictOf(other, 'structural').verdict, 'wrong');
  const tooFew = gradeAnswer(answered(), { ...context, kase: { ...kase, rows: [5, 10] } });
  assert.equal(verdictOf(tooFew, 'structural').verdict, 'wrong');
  assert.equal(gradeAnswer({ error: { kind: 'timeout', detail: 'x' } }, context).failure.kind, 'timeout');
});

// ── Summary, gates, report ───────────────────────────────────────────────────

/**
 * @param {string} id @param {number} level @param {string} caseId
 * @param {'correct' | 'wrong' | 'failed'} outcome @param {number} answerMs @param {string} [print]
 */
function record(id, level, caseId, outcome, answerMs, print = 'fp-a') {
  const failure = outcome === 'failed' ? { kind: 'timeout', detail: 'no answer within 300 s' } : null;
  return {
    id, level, user: 1, seq: 1, case_id: caseId, question: caseId,
    error: failure,
    t: failure ? { open: 10, table: null, answer: null, done: null } : { open: 10 * level, table: answerMs / 2, answer: answerMs, done: answerMs + 100 },
    chart: failure ? null : { status: 200, ready_ms: answerMs, request_ms: 100 },
    result: failure ? null : {
      sql: 'SELECT 1', metrics: { llm_latency_ms: answerMs / 2, total_tokens: 1000, llm_call_count: 3, retry_count: 0 },
      trace: [{ node: 'sql_generation', elapsed_ms: answerMs / 4 }, { node: 'sql_generation', elapsed_ms: 100 }, { node: 'router', elapsed_ms: 50 }],
    },
    grade: failure ? { failure } : {
      failure: null, tier: outcome === 'correct' ? 'structural' : 'mismatch', rows_ok: true, row_count: 1, write_sql: false,
      grounded: { grounded: true, checked: 1, unmatched: [] }, fingerprint: print,
    },
  };
}

test('summary: accuracy per level, change against the first level, stability and gates', () => {
  const run = {
    id: 'selftest', target: 'http://localhost:8501', catalog_source: 'db', node: process.version,
    connection: { name: 'AW', source_key: 'aw', database_type: 'postgres' }, app: { version: '1', model: 'm' }, git: { sha: 'abc', dirty: false },
    started_at: 'start', ended_at: 'end',
    settings: { levels: [1, 5], questions_per_user: 2, cases: ['a', 'b'], think_ms: 0, ask_timeout_ms: 300_000, chart: true },
    cases: [{ id: 'a', question: 'a', gold_sql: 'SELECT 1' }, { id: 'b', question: 'b', gold_sql: null }],
    levels: [{ level: 1, wall_ms: 60_000 }, { level: 5, wall_ms: 60_000 }],
  };
  const answers = [
    { ...record('warmup', 0, 'a', 'correct', 99_999), warmup: true },
    record('L1-1', 1, 'a', 'correct', 2_000),
    record('L1-2', 1, 'b', 'correct', 4_000),
    record('L5-1', 5, 'a', 'correct', 6_000),
    record('L5-2', 5, 'a', 'wrong', 8_000, 'fp-b'),
    record('L5-3', 5, 'b', 'failed', 0),
    record('L5-4', 5, 'b', 'correct', 10_000),
  ];
  const gates = { minAccuracy: 0.9, maxAccuracyDrop: 0.2, maxP95Ms: 9_000, maxErrorRate: 0.5 };
  const summary = summarize(run, answers, 'structural', gates);
  const [one, five] = summary.levels;
  assert.equal(one.asked, 2, 'the warm-up is not measured');
  assert.equal(one.accuracy, 1);
  assert.equal(five.asked, 4);
  assert.equal(five.correct, 2);
  assert.equal(five.failed, 1);
  assert.deepEqual(five.failures, { timeout: 1 });
  assert.ok(Math.abs(five.accuracy - 2 / 3) < 1e-9);
  assert.ok(Math.abs(five.vs_first.accuracy_change - (2 / 3 - 1)) < 1e-9);
  assert.equal(five.timings.answer.p95, 10_000);
  assert.equal(five.vs_first.answer_p95_x, 2.5);
  assert.equal(five.throughput_per_min, 3);
  const step = summary.steps.find((/** @type {any} */ s) => s.node === 'sql_generation');
  assert.equal(step.levels[1].p50, 2_000 / 4 + 100, 'a step that ran twice is summed per answer');
  const a = summary.questions.find((/** @type {any} */ q) => q.id === 'a');
  assert.deepEqual(a.same_rows, { same: 1, answered: 2 });
  assert.equal(a.distinct_results, 2);
  const failedGates = summary.gates.filter((/** @type {any} */ g) => !g.ok).map((/** @type {any} */ g) => `${g.gate}@${g.level}`);
  assert.deepEqual(failedGates.sort(), ['STRESS_MAX_ACCURACY_DROP@5', 'STRESS_MAX_P95_MS@5', 'STRESS_MIN_ACCURACY@5']);
  const markdown = renderMarkdown(run, summary, 'structural');
  for (const heading of ['## Accuracy', '## Speed', '## Where the time goes', '## Per question', '## Wrong answers', '## Failures', '## Gates']) {
    assert.ok(markdown.includes(heading), `report has ${heading}`);
  }
});

test('saved answers: the last line for an id wins and a cut-off line is skipped', () => {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'stress-')), 'answers.jsonl');
  fs.writeFileSync(file, [
    JSON.stringify({ id: 'a', v: 1 }),
    JSON.stringify({ id: 'b', v: 1 }),
    JSON.stringify({ id: 'a', v: 2 }),
    '{"id":"c","v":',
  ].join('\n'));
  assert.deepEqual(readAnswers(file), [{ id: 'a', v: 2 }, { id: 'b', v: 1 }]);
});
