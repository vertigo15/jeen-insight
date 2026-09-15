/**
 * Conversation hydration mapper + generation guard.
 * Run with: node tests/js/test_conversation_hydration.mjs
 */
import assert from 'node:assert/strict';
import {
    ARTIFACT_LOADED,
    ARTIFACT_MISSING,
    applyArtifact,
    hasRenderableResult,
    makeGenerationGuard,
    turnFromServer,
    turnsFromDetail,
} from '../../src/static/workspace/conversationHydration.js';

const CONV = '11111111-1111-1111-1111-111111111111';

function dto(overrides = {}) {
    return {
        turn_id: '22222222-2222-2222-2222-222222222222',
        sequence_number: 2,
        question: 'sales by region',
        sql: 'select region, sum(x) from t group by 1',
        execution_status: 'success',
        result_kind: 'table',
        answer: null,
        error: null,
        metrics: { execution_time_ms: 40, llm_latency_ms: 600 },
        findings: ['North leads'],
        suggestions: null,
        followups: ['and by month?'],
        snapshot_status: 'stored',
        row_count: 3,
        has_chart: true,
        has_rerunnable_query: true,
        created_at: '2026-09-01T10:00:00Z',
        snapshot_at: '2026-09-01T10:00:01Z',
        ...overrides,
    };
}

// ── table turn with a stored snapshot: rows are fetched lazily ──────────────
{
    const turn = turnFromServer(dto(), CONV);
    assert.equal(turn.status, 'success');
    assert.equal(turn.restored, true);
    assert.equal(turn.resultKind, 'table');
    assert.equal(turn.artifactState, ARTIFACT_MISSING);
    assert.equal(turn.canLoadData, false, 'stored snapshots are fetched, not re-run');
    assert.equal(turn.result.results, null, 'hydration payload carries no rows');
    assert.equal(turn.result.session_id, CONV);
    assert.equal(turn.result.query_id, dto().turn_id);
    assert.equal(turn.result.metrics.restored, true);
    assert.deepEqual(turn.result.followups, ['and by month?']);
    assert.deepEqual(turn.result.suggestions, [], 'null analytics become empty arrays for the renderer');
    assert.equal(turn.durationMs, 640);
}

// ── pruned / too_large table turns offer Load data ─────────────────────────
for (const status of ['pruned', 'too_large']) {
    const turn = turnFromServer(dto({ snapshot_status: status, has_chart: false }), CONV);
    assert.equal(turn.artifactState, ARTIFACT_LOADED, `${status}: nothing to fetch`);
    assert.equal(turn.canLoadData, true, `${status}: can re-run`);
    assert.equal(hasRenderableResult(turn), false);
}
{
    const noSql = turnFromServer(dto({ snapshot_status: 'pruned', sql: null, has_rerunnable_query: false }), CONV);
    assert.equal(noSql.canLoadData, false, 'no query text -> no Load data');
}

// ── text turn ───────────────────────────────────────────────────────────────
{
    const turn = turnFromServer(dto({ sql: null, result_kind: 'text', answer: 'Hello!', snapshot_status: 'not_applicable', has_chart: false, has_rerunnable_query: false }), CONV);
    assert.equal(turn.status, 'success');
    assert.equal(turn.resultKind, 'text');
    assert.equal(turn.artifactState, ARTIFACT_LOADED);
    assert.equal(turn.canLoadData, false);
    assert.equal(turn.result.answer, 'Hello!');
}

// ── every non-success execution_status is a failed turn, raw value kept ─────
for (const status of ['error', 'timeout', 'syntax_error', 'pending']) {
    const turn = turnFromServer(dto({ execution_status: status, result_kind: 'error', error: status === 'error' ? 'boom' : null }), CONV);
    assert.equal(turn.status, 'error', status);
    assert.equal(turn.executionStatus, status);
    assert.ok(turn.error && turn.error.length > 0, `${status} has an error message`);
    assert.equal(turn.result.error, status === 'error' ? 'boom' : null);
}

// ── detail -> turns in display order (server sends newest first) ───────────
{
    const detail = {
        conversation: { id: CONV, title: 't' },
        turns: [dto({ sequence_number: 3, turn_id: 'c' }), dto({ sequence_number: 1, turn_id: 'a' }), dto({ sequence_number: 2, turn_id: 'b' })],
        next_cursor: 1,
    };
    const turns = turnsFromDetail(detail);
    assert.deepEqual(turns.map((t) => t.turnId), ['a', 'b', 'c']);
    assert.deepEqual(turnsFromDetail(null), []);
    assert.deepEqual(turnsFromDetail({ conversation: null, turns: [dto()] }), []);
}

// ── applying an artifact attaches rows + chart only together ───────────────
{
    const turn = turnFromServer(dto(), CONV);
    applyArtifact(turn, {
        turn_id: turn.turnId,
        results: { columns: ['region', 'x'], rows: [['N', 1]], row_count: 1 },
        chart_spec: { chart_type: 'bar' },
        chart_config: { series: [{ type: 'bar', data: [1] }] },
        snapshot_status: 'stored',
        snapshot_at: '2026-09-02T00:00:00Z',
    });
    assert.equal(turn.artifactState, ARTIFACT_LOADED);
    assert.equal(hasRenderableResult(turn), true);
    assert.deepEqual(turn.result.results.rows, [['N', 1]]);
    assert.equal(turn.hasChart, true);
    assert.equal(turn.chartState.chart_config.series[0].type, 'bar');
    assert.equal(turn.snapshotAt, '2026-09-02T00:00:00Z');
    assert.equal(turn.canLoadData, false);

    // Rows vanished (pruned between list and fetch): no chart, Load data offered.
    const gone = turnFromServer(dto(), CONV);
    applyArtifact(gone, { turn_id: gone.turnId, results: null, chart_spec: null, chart_config: { series: [] }, snapshot_status: 'pruned', snapshot_at: null });
    assert.equal(gone.artifactState, ARTIFACT_MISSING);
    assert.equal(gone.chartState, null, 'a chart is never kept without its rows');
    assert.equal(gone.hasChart, false);
    assert.equal(gone.canLoadData, true);
    assert.equal(gone.snapshotStatus, 'pruned');
}

// ── ML skills: analysis + low_confidence travel with the turn ──────────────
{
    const analysis = {
        skill: 'forecast', method_used: 'AutoETS', params: { series: { grain: 'week' }, horizon: 8 },
        validation: { metric: 'WAPE', value: 0.064 }, low_confidence: true,
    };
    const turn = turnFromServer(dto({ analysis, low_confidence: true }), CONV);
    assert.equal(turn.result.status, 'completed');
    assert.equal(turn.result.analysis.skill, 'forecast');
    assert.equal(turn.result.low_confidence, true, 'the flag must survive restore');

    // The artifact also carries them (rows + analysis on the same fetch).
    const fresh = turnFromServer(dto(), CONV);
    applyArtifact(fresh, {
        turn_id: fresh.turnId, results: { columns: ['ts', 'actual'], rows: [['2026-01-05', 1]] },
        chart_spec: { chart_type: 'band' }, chart_config: { series: [{ jeenRole: 'actual' }] },
        snapshot_status: 'stored', snapshot_at: null, analysis, low_confidence: false,
    });
    assert.equal(fresh.result.analysis.method_used, 'AutoETS');
    assert.equal(fresh.result.low_confidence, true, 'the envelope flag wins when the column is stale');
    assert.equal(fresh.chartState.chart_spec.chart_type, 'band');

    // A pending proposal (confirm / clarify / guard) restores as its card.
    const proposal = { proposal_id: 'p1', kind: 'guard', skill: 'forecast', message: '62 of 104 periods.', options: [] };
    const card = turnFromServer(dto({ result_kind: 'text', sql: null, snapshot_status: 'not_applicable',
        analysis: { proposal, status: 'blocked' } }), CONV);
    assert.equal(card.result.status, 'blocked');
    assert.equal(card.result.proposal.kind, 'guard');
    assert.equal(card.result.analysis, undefined, 'a proposal is not a completed analysis');
}

// ── generation guard: only the latest generation wins ──────────────────────
{
    const guard = makeGenerationGuard();
    const first = guard.next();
    const second = guard.next();
    assert.equal(guard.isCurrent(first), false, 'older hydration must not apply');
    assert.equal(guard.isCurrent(second), true);
    assert.equal(guard.current(), second);
}

console.log('conversation hydration JS tests passed');
