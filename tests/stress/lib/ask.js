// @ts-check
// One question, asked the way the workspace does it
// (src/static/workspace/workspaceController.js): POST /api/ask/stream in a new
// conversation, and POST /api/generate-chart as soon as the first rows arrive.
// Every time is in ms since the question was sent:
//   headers     the UI server answered
//   open        first SSE event: a UI-server thread and the API have taken it
//   first_node  the graph started
//   table       first `partial` event: the rows are on screen
//   answer      `result` event: the sentence and insights
//   done        the stream closed: history is saved
const CHART_TIMEOUT_MS = 120_000; // the browser's own limit (chartManager.js _postChart)

/**
 * Incremental SSE framing with the workspace's rules (`_stream`): an event
 * ends at a blank line, `:` lines are comments (heartbeats), data lines join.
 * @param {(event: string, data: any) => void} onEvent
 */
function createSseParser(onEvent) {
  let buffer = '';
  return {
    /** @param {string} text */
    push(text) {
      buffer = (buffer + text).replace(/\r\n/g, '\n');
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (!block || block.startsWith(':')) continue;
        let event = 'message';
        /** @type {string[]} */
        const dataLines = [];
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
        }
        let data = {};
        if (dataLines.length) {
          try {
            data = JSON.parse(dataLines.join('\n'));
          } catch (_) {
            data = { detail: dataLines.join('\n') };
          }
        }
        onEvent(event, data);
      }
    },
  };
}

/**
 * The answer sentence as text: a string or the fragment array insights use (workspace `textOf`).
 * @param {any} value
 * @returns {string}
 */
function answerText(value) {
  if (value == null) return '';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(answerText).join('');
  if (typeof value === 'object') return answerText(value.t || value.text || value.content || '');
  return '';
}

/** @param {number} status */
function statusKind(status) {
  if (status === 429) return 'rate_limited';
  if (status === 401 || status === 403) return 'auth';
  if (status === 502 || status === 503 || status === 504) return 'unavailable';
  return 'http_error';
}

/** @param {any} value */
function clip(value) {
  return (typeof value === 'string' ? value : JSON.stringify(value)).slice(0, 300);
}

/** @param {string} body */
function errorText(body) {
  try {
    const parsed = JSON.parse(body);
    return String(parsed.error || parsed.detail || body);
  } catch (_) {
    return body;
  }
}

/** @param {any} results */
function rowCount(results) {
  return results && Array.isArray(results.rows) ? results.rows.length : 0;
}

/**
 * What the report needs from a QueryResponse (no prompt, slim trace).
 * @param {any} result
 * @param {any[]} traceTail  steps that ran after the answer was sent (`enrichment.trace_tail`)
 */
function slimResult(result, traceTail) {
  if (!result) return null;
  const results = result.results && typeof result.results === 'object' ? result.results : null;
  const rows = results && Array.isArray(results.rows) ? results.rows : [];
  const steps = [...(Array.isArray(result.trace) ? result.trace : []), ...traceTail];
  return {
    query_id: result.query_id ?? null,
    session_id: result.session_id ?? null,
    status: result.status ?? null,
    sql: result.sql ?? null,
    answer: answerText(result.answer),
    error: result.error ?? null,
    routing: result.routing ?? null,
    metrics: result.metrics ?? null,
    results: results
      ? { columns: results.columns ?? [], rows, row_count: Number(results.row_count ?? rows.length), truncated: Boolean(results.truncated) }
      : null,
    findings: Array.isArray(result.findings) ? result.findings.length : 0,
    empty_result: Boolean(result.empty_result),
    clarification: Boolean(result.filter_clarification || result.route_clarification),
    proposal: Boolean(result.proposal),
    trace: steps
      .filter((step) => step && step.node)
      .map((step) => ({
        node: String(step.node),
        elapsed_ms: Number(step.elapsed_ms) || 0,
        type: step.type || null,
        // pre_graph_setup: which catalog path served the question (mcp_timing only on the question-specific MCP path).
        ...(step.catalog_source ? { catalog_source: String(step.catalog_source) } : {}),
        ...(step.mcp_timing && typeof step.mcp_timing === 'object' ? { mcp_timing: step.mcp_timing } : {}),
      })),
  };
}

/**
 * The chart request the browser sends when rows arrive. Never throws.
 * @param {import('./http').Session} session
 * @param {{ connection: string, question: string, queryId: string }} request
 * @param {number} t0  performance.now() when the question was sent
 */
async function requestChart(session, { connection, question, queryId }, t0) {
  const started = performance.now();
  const done = () => ({ request_ms: Math.round(performance.now() - started), ready_ms: Math.round(performance.now() - t0) });
  try {
    const reply = await session.request('POST', '/api/generate-chart', {
      json: { connection, query_id: queryId, question, chart_type: 'auto' },
      timeoutMs: CHART_TIMEOUT_MS,
    });
    const outcome = { status: reply.status, ...done() };
    if (reply.status !== 200) return { ...outcome, detail: clip(errorText(reply.text)) };
    const body = JSON.parse(reply.text);
    const spec = body.chart_spec || {};
    const encodings = [spec.x, ...(Array.isArray(spec.y) ? spec.y : [spec.y]), spec.series].filter(Boolean).map(String);
    return { ...outcome, chart_type: body.chart_type || spec.chart_type || null, encodings };
  } catch (error) {
    return { status: 0, ...done(), detail: clip(/** @type {Error} */ (error).message) };
  }
}

/**
 * Ask one question and time it. Resolves (never rejects) with the timings, the
 * slimmed QueryResponse, the chart outcome, and `error` when there was no
 * usable answer at the transport or stream level.
 * @param {import('./http').Session} session
 * @param {{ question: string, connection: string, chart: boolean, timeoutMs: number }} options
 */
function ask(session, { question, connection, chart, timeoutMs }) {
  return new Promise((resolve) => {
    const startedAt = new Date().toISOString();
    const t0 = performance.now();
    const since = () => Math.round(performance.now() - t0);
    /** @type {Record<'headers' | 'open' | 'first_node' | 'table' | 'answer' | 'done', number | null>} */
    const t = { headers: null, open: null, first_node: null, table: null, answer: null, done: null };
    const events = { node: 0, partial: 0 };
    let status = 0;
    /** @type {any} */
    let result = null;
    /** @type {any[]} */
    let traceTail = [];
    /** @type {{ kind: string, detail: string } | null} */
    let error = null;
    /** @type {Promise<any> | null} */
    let chartTask = null;
    /** @type {NodeJS.Timeout | null} */
    let deadline = null;
    let settled = false;

    /** @param {any} data */
    const startChart = (data) => {
      if (!chart || chartTask || !data || !data.query_id || !rowCount(data.results)) return;
      chartTask = requestChart(session, { connection, question, queryId: String(data.query_id) }, t0);
    };

    const finish = () => {
      if (settled) return;
      settled = true;
      if (deadline) clearTimeout(deadline);
      Promise.resolve(chartTask).then((chartOutcome) => resolve({
        started_at: startedAt,
        http_status: status,
        t,
        events,
        error,
        result: slimResult(result, traceTail),
        chart: chartOutcome || null,
      }));
    };

    const parser = createSseParser((event, data) => {
      const now = since();
      if (event === 'open') {
        if (t.open === null) t.open = now;
      } else if (event === 'node') {
        events.node += 1;
        if (t.first_node === null) t.first_node = now;
      } else if (event === 'partial') {
        events.partial += 1;
        if (t.table === null) t.table = now;
        startChart(data);
      } else if (event === 'result') {
        t.answer = now;
        result = data;
        // No partial before it: the table and the narrative land together (workspace `_onResult`).
        if (t.table === null && rowCount(data.results)) t.table = now;
        startChart(data);
      } else if (event === 'enrichment') {
        traceTail = Array.isArray(data.trace_tail) ? data.trace_tail : [];
      } else if (event === 'error' && !error) {
        error = { kind: 'stream_error', detail: clip(data.detail ?? data) };
      }
    });

    const request = session.stream('/api/ask/stream', { question, connection, eval_analytics: true }, {
      onResponse(res) {
        status = res.statusCode || 0;
        t.headers = since();
        res.setEncoding('utf8');
        if (status !== 200) {
          let body = '';
          res.on('data', (chunk) => { body += chunk; });
          res.on('end', () => {
            error = { kind: statusKind(status), detail: clip(errorText(body)) };
            finish();
          });
          return;
        }
        res.on('data', (chunk) => parser.push(chunk));
        res.on('end', () => {
          t.done = since();
          if (!result && !error) error = { kind: 'no_result', detail: 'the stream closed without a result' };
          finish();
        });
        res.on('error', (e) => {
          if (!result && !error) error = { kind: 'network', detail: clip(e.message) };
          finish();
        });
      },
      onError(e) {
        if (!result && !error) error = { kind: 'network', detail: clip(e.message) };
        finish();
      },
    });

    // An answer that arrived keeps counting even if the history writes outlast the deadline.
    deadline = setTimeout(() => {
      if (!result && !error) error = { kind: 'timeout', detail: `no answer within ${Math.round(timeoutMs / 1000)} s` };
      request.destroy();
      finish();
    }, timeoutMs);
  });
}

module.exports = { ask, createSseParser, answerText, statusKind, slimResult };
