// @ts-check
// Knowledge pairs are the ground truth for the @kp suite: the metadata's
// curated "question -> SQL" examples the LLM is shown in the system prompt
// (src/agent/prompts/jeen_insights_system.md, "# Knowledge Pairs"). No route
// returns them as records, so globalSetup reads the resolved system prompt
// (admin-only) and parses the rendered lines back into {question, sql} pairs.
// The result is written to live/.generated/knowledge_pairs.json (git-ignored)
// and read at test-collection time by kp.live.spec.js.
const fs = require('fs');
const path = require('path');
const { request } = require('@playwright/test');

const GENERATED_DIR = path.join(__dirname, '.generated');
const PAIRS_FILE = path.join(GENERATED_DIR, 'knowledge_pairs.json');
const PROMPT_NAME = 'jeen_insights_system';

/** @typedef {{ question: string, sql: string, category: string, tags: string }} KnowledgePair */
/**
 * @typedef {Object} PairsFile
 * @property {string} connection            display name (or key) requested
 * @property {string} source_key
 * @property {string} database_type
 * @property {boolean} is_power_bi
 * @property {string} catalog_source        'db' | 'mcp' | 'unknown'
 * @property {string} role                  role of the login used to fetch
 * @property {string} fetched_at
 * @property {string} [error]               why pairs are missing, when they are
 * @property {KnowledgePair[]} pairs
 */

/** Lower-case, letters/digits only, single spaces — how two phrasings of a question are matched. */
function normalizeQuestion(text) {
  return String(text || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
}

/**
 * Parse the rendered "Knowledge Pairs" section into records. Two renderings
 * exist and the parser accepts both:
 *   A. metadata DB (metadata_loader._format_lines) and the MCP client's own
 *      formatter — one record per line:
 *        - Category: <c> | Question: <q> | SQL: <sql> | Tags: <t>
 *      SQL may contain "|" (e.g. "||" concat) or newlines, so records are
 *      split on the leading "Category:" and tags taken from the LAST "| Tags:".
 *   B. the schema-modeler MCP server's catalog markdown, passed through:
 *        Q: <question>
 *        SQL:
 *        <sql, possibly several lines>
 * @param {string} text
 * @returns {KnowledgePair[]}
 */
function parsePairs(text) {
  const section = knowledgeSection(String(text || ''));
  const lineRecords = parseLineRecords(section);
  return lineRecords.length ? lineRecords : parseQSqlBlocks(section);
}

/** Format A (see parsePairs). */
function parseLineRecords(section) {
  const records = section.split(/(?:^|\n)\s*-?\s*Category:\s*/).slice(1);
  /** @type {KnowledgePair[]} */
  const pairs = [];
  for (const record of records) {
    const qAt = record.indexOf('| Question:');
    const sAt = record.indexOf('| SQL:', qAt + 1);
    const tAt = record.lastIndexOf('| Tags:');
    if (qAt < 0 || sAt < 0) continue;
    const category = record.slice(0, qAt).trim();
    const question = record.slice(qAt + '| Question:'.length, sAt).trim();
    const sqlEnd = tAt > sAt ? tAt : record.length;
    const sql = record.slice(sAt + '| SQL:'.length, sqlEnd).trim();
    const tags = tAt > sAt ? record.slice(tAt + '| Tags:'.length).trim() : '';
    if (!question || !sql || /^No statement$/i.test(sql) || /^No question$/i.test(question)) continue;
    pairs.push({ question, sql, category: category || 'General', tags });
  }
  return pairs;
}

/** Format B (see parsePairs): "Q: …" then a "SQL:" line, SQL until the next "Q:" or the end. */
function parseQSqlBlocks(section) {
  const blocks = section.split(/^\s*Q:\s*/m).slice(1);
  /** @type {KnowledgePair[]} */
  const pairs = [];
  for (const block of blocks) {
    const sqlAt = block.search(/^\s*SQL:\s*/m);
    if (sqlAt < 0) continue;
    const question = block.slice(0, sqlAt).trim();
    const sql = block.slice(sqlAt).replace(/^\s*SQL:\s*/m, '').trim();
    if (!question || !sql) continue;
    pairs.push({ question, sql, category: 'General', tags: '' });
  }
  return pairs;
}

/**
 * Best effort: attach category/tags from /api/knowledge-questions (which lists
 * questions without SQL) to pairs parsed from the prompt, matched by question.
 * @param {KnowledgePair[]} pairs
 * @param {Array<{question: string, category?: string, tags?: string}>} questions
 */
function enrichPairs(pairs, questions) {
  const byQuestion = new Map(questions.map((q) => [normalizeQuestion(q.question), q]));
  for (const pair of pairs) {
    const meta = byQuestion.get(normalizeQuestion(pair.question));
    if (!meta) continue;
    if (meta.category) pair.category = String(meta.category);
    if (meta.tags) pair.tags = String(meta.tags);
  }
  return pairs;
}

/** The text between "# Knowledge Pairs" and the next "# " heading; the whole text when the heading is absent. */
function knowledgeSection(text) {
  const start = text.search(/^#+\s*Knowledge Pairs\s*$/im);
  if (start < 0) return text;
  const rest = text.slice(start).replace(/^#+\s*Knowledge Pairs\s*$/im, '');
  const next = rest.search(/^#+\s+\S/m);
  return next < 0 ? rest : rest.slice(0, next);
}

/**
 * Log in through the Flask form with an API request context (no browser) and
 * return the context plus the session user. Throws on bad credentials.
 * @param {{ base: string, email: string, password: string }} opts
 */
async function loginRequestContext({ base, email, password }) {
  const ctx = await request.newContext({ baseURL: base, timeout: 60_000 });
  const page = await ctx.get('/login');
  const html = await page.text();
  const csrf = (html.match(/name="csrf_token"\s+value="([^"]+)"/) || [])[1] || '';
  const response = await ctx.post('/login', { form: { csrf_token: csrf, email, password }, maxRedirects: 0 });
  if (response.status() !== 302 && response.status() !== 303) {
    const body = await response.text();
    const message = (body.match(/class="[^"]*(?:login-error|error)[^"]*"[^>]*>([^<]+)</) || [])[1] || `HTTP ${response.status()}`;
    await ctx.dispose();
    throw new Error(`login as ${email} failed: ${message.trim()}`);
  }
  const me = await ctx.get('/api/auth/me');
  if (!me.ok()) {
    await ctx.dispose();
    throw new Error(`/api/auth/me → ${me.status()} after login`);
  }
  return { ctx, user: await me.json() };
}

/**
 * Resolve the connection display name (or key) to its public record.
 * @param {import('@playwright/test').APIRequestContext} ctx
 * @param {string} name
 */
async function resolveConnection(ctx, name) {
  const response = await ctx.get('/api/connections');
  if (!response.ok()) throw new Error(`/api/connections → ${response.status()}`);
  const { connections = [] } = await response.json();
  const match = connections.find((c) => c.display_name === name || c.source_key === name);
  if (!match) {
    throw new Error(`connection "${name}" not found; available: ${connections.map((c) => c.display_name).join(', ')}`);
  }
  return match;
}

/**
 * Fetch and persist the pairs. Never throws: a missing admin role or a failed
 * call is recorded in the file so the @kp suite can explain "no gold".
 * @param {{ base: string, email: string, password: string, connection: string }} opts
 * @returns {Promise<PairsFile>}
 */
async function fetchKnowledgePairs({ base, email, password, connection }) {
  /** @type {PairsFile} */
  const out = {
    connection, source_key: '', database_type: '', is_power_bi: false,
    catalog_source: 'unknown', role: '', fetched_at: new Date().toISOString(), pairs: [],
  };
  let ctx = null;
  try {
    const login = await loginRequestContext({ base, email, password });
    ctx = login.ctx;
    out.role = String(login.user.role || '');
    const conn = await resolveConnection(ctx, connection);
    out.source_key = conn.source_key;
    out.database_type = String(conn.database_type || '');
    out.is_power_bi = Boolean(conn.is_power_bi);
    if (out.role !== 'admin') {
      out.error = `login "${email}" has role "${out.role}"; reading knowledge pairs needs an admin (set LIVE_EMAIL/LIVE_PASSWORD)`;
      return out;
    }
    // Resolving renders the live catalog (possibly via MCP); the BFF allows 45 s.
    const resolved = await ctx.get(`/api/settings/prompts/${PROMPT_NAME}/resolved`, {
      params: { connection: conn.source_key }, timeout: 90_000,
    });
    if (!resolved.ok()) {
      out.error = `/api/settings/prompts/${PROMPT_NAME}/resolved → ${resolved.status()} ${(await resolved.text()).slice(0, 200)}`;
      return out;
    }
    const body = await resolved.json();
    out.catalog_source = String(body.catalog_source || 'unknown');
    out.pairs = parsePairs(body.resolved_content || '');
    if (!out.pairs.length) {
      out.error = 'the resolved prompt lists no knowledge pairs for this connection';
      return out;
    }
    const questions = await ctx.get('/api/knowledge-questions', { params: { connection: conn.source_key } }).catch(() => null);
    if (questions && questions.ok()) {
      const payload = await questions.json().catch(() => ({}));
      enrichPairs(out.pairs, Array.isArray(payload.questions) ? payload.questions : []);
    }
    return out;
  } catch (error) {
    out.error = error && error.message ? error.message : String(error);
    return out;
  } finally {
    if (ctx) await ctx.dispose().catch(() => {});
    fs.mkdirSync(GENERATED_DIR, { recursive: true });
    fs.writeFileSync(PAIRS_FILE, JSON.stringify(out, null, 2));
  }
}

/**
 * Read the generated file (at test-collection time). Missing file → empty set
 * with an explanatory error, never a throw.
 * @returns {PairsFile}
 */
function loadPairs() {
  try {
    return JSON.parse(fs.readFileSync(PAIRS_FILE, 'utf8'));
  } catch (error) {
    return {
      connection: process.env.LIVE_CONNECTION || 'AdventureWorksDW', source_key: '', database_type: '',
      is_power_bi: false, catalog_source: 'unknown', role: '', fetched_at: '',
      error: `no ${path.relative(process.cwd(), PAIRS_FILE)} — globalSetup did not fetch knowledge pairs (${error.message})`,
      pairs: [],
    };
  }
}

/**
 * Find the pair whose question matches `question` (normalized). Exact match
 * first, then a unique pair that contains / is contained by the question.
 * @param {KnowledgePair[]} pairs
 * @param {string} question
 * @returns {KnowledgePair | null}
 */
function findPair(pairs, question) {
  const want = normalizeQuestion(question);
  if (!want) return null;
  const exact = pairs.find((p) => normalizeQuestion(p.question) === want);
  if (exact) return exact;
  const loose = pairs.filter((p) => {
    const have = normalizeQuestion(p.question);
    return have.includes(want) || want.includes(have);
  });
  return loose.length === 1 ? loose[0] : null;
}

module.exports = {
  PAIRS_FILE, GENERATED_DIR, PROMPT_NAME,
  normalizeQuestion, parsePairs, knowledgeSection, enrichPairs,
  loginRequestContext, resolveConnection, fetchKnowledgePairs, loadPairs, findPair,
};
