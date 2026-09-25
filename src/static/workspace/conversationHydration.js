/**
 * Conversation hydration: pure mapping helpers shared by the Workspace v3
 * controller and the node test-suite (tests/js/test_conversation_hydration.mjs).
 *
 * Nothing in here touches the DOM or fetch. The controller owns I/O; these
 * functions turn API DTOs (ConversationDetail / ConversationTurn / TurnArtifact)
 * into the `turns[]` shape the workspace renders, and provide the generation
 * guard that drops stale responses.
 */

export const SNAPSHOT_STORED = 'stored';
export const SNAPSHOT_TOO_LARGE = 'too_large';
export const SNAPSHOT_PRUNED = 'pruned';
export const SNAPSHOT_NOT_APPLICABLE = 'not_applicable';

/** Artifact loading states for a restored turn. */
export const ARTIFACT_MISSING = 'missing';
export const ARTIFACT_LOADING = 'loading';
export const ARTIFACT_LOADED = 'loaded';
export const ARTIFACT_FAILED = 'failed';

/** Preserve both versioned in-page snapshots and legacy server chart pairs. */
export function chartStateFromArtifact(artifact) {
    if (!artifact || !artifact.chart_config) return null;
    return {
        chart_config: artifact.chart_config,
        chart_spec: artifact.chart_spec || null,
        ...(artifact.chart_toggles ? { chart_toggles: artifact.chart_toggles } : {}),
        ...(artifact.derived_specs ? { derived_specs: artifact.derived_specs } : {}),
        ...(artifact.chart_session ? { chart_session: artifact.chart_session } : {}),
    };
}

/**
 * Map one ConversationTurn DTO to a workspace turn.
 *
 * Restored turns carry `restored: true`, a `result` compatible with the live
 * QueryResponse (so `renderWorkspace` and the legacy bridge need no special
 * casing) and `artifactState` describing whether rows still have to be fetched.
 */
export function turnFromServer(dto, conversationId) {
    const status = dto.execution_status === 'success' ? 'success' : 'error';
    const kind = dto.result_kind || (dto.sql ? 'table' : 'text');
    const needsArtifact = status === 'success' && kind === 'table' && dto.snapshot_status === SNAPSHOT_STORED;
    const canLoadData = status === 'success' && kind === 'table'
        && dto.snapshot_status !== SNAPSHOT_STORED
        && dto.snapshot_status !== SNAPSHOT_NOT_APPLICABLE
        && Boolean(dto.has_rerunnable_query);
    // ML skills: a completed analysis stores its method/validation/guard view;
    // a pending confirm / clarify / guard card stores its proposal. Both travel
    // with the turn so a restored ML answer renders like a live one.
    const analysis = dto.analysis && typeof dto.analysis === 'object' ? dto.analysis : null;
    const proposal = analysis && analysis.proposal ? analysis.proposal : null;
    const ml = proposal
        ? { status: analysis.status || proposal.kind, proposal }
        : analysis && analysis.skill
            ? { status: 'completed', analysis, low_confidence: Boolean(dto.low_confidence || analysis.low_confidence) }
            : {};
    return {
        id: `turn-${dto.turn_id}`,
        turnId: dto.turn_id,
        conversationId,
        sequence: Number(dto.sequence_number) || 0,
        question: dto.question || '',
        status,
        restored: true,
        resultKind: kind,
        executionStatus: dto.execution_status || 'pending',
        snapshotStatus: dto.snapshot_status || SNAPSHOT_NOT_APPLICABLE,
        snapshotAt: dto.snapshot_at || null,
        askedAt: dto.created_at || null,
        hasChart: Boolean(dto.has_chart),
        isFavorite: Boolean(dto.is_favorite),
        // Result-quality feedback already recorded for this turn (thumbs_up /
        // thumbs_down), so the buttons render pressed after a reload.
        feedback: dto.user_feedback || null,
        canLoadData,
        artifactState: needsArtifact ? ARTIFACT_MISSING : ARTIFACT_LOADED,
        startedAt: 0,
        durationMs: dto.metrics && Number.isFinite(Number(dto.metrics.execution_time_ms))
            ? Number(dto.metrics.execution_time_ms) + Number(dto.metrics.llm_latency_ms || 0)
            : null,
        phaseState: {},
        trace: [],
        traceOpen: false,
        error: status === 'error' ? (dto.error || `Query ${dto.execution_status || 'failed'}`) : null,
        chartState: null,
        result: {
            question: dto.question || '',
            query_id: dto.turn_id,
            session_id: conversationId,
            sql: dto.sql || null,
            // Rows arrive with the artifact; text turns never have any.
            results: null,
            answer: dto.answer == null ? null : dto.answer,
            error: status === 'error' ? (dto.error || null) : null,
            metrics: { ...(dto.metrics || {}), restored: true },
            findings: dto.findings || [],
            suggestions: dto.suggestions || [],
            followups: dto.followups || [],
            trace: [],
            ...ml,
        },
    };
}

/** Map a ConversationDetail (newest page first) to display order (oldest first). */
export function turnsFromDetail(detail) {
    if (!detail || !detail.conversation) return [];
    const conversationId = detail.conversation.id;
    const turns = (detail.turns || []).map((dto) => turnFromServer(dto, conversationId));
    turns.sort((a, b) => a.sequence - b.sequence);
    return turns;
}

/**
 * Attach a TurnArtifact (rows + optional chart baseline) to a restored turn.
 * Chart state is only kept alongside rows; the chart machinery cannot render
 * a config without its dataset.
 */
export function applyArtifact(turn, artifact) {
    if (!turn || !artifact) return turn;
    const results = artifact.results || null;
    turn.result = { ...(turn.result || {}), results };
    if (artifact.analysis && typeof artifact.analysis === 'object' && artifact.analysis.skill) {
        turn.result.analysis = artifact.analysis;
        turn.result.status = 'completed';
        turn.result.low_confidence = Boolean(artifact.low_confidence || artifact.analysis.low_confidence);
    }
    turn.snapshotStatus = artifact.snapshot_status || turn.snapshotStatus;
    turn.snapshotAt = artifact.snapshot_at || turn.snapshotAt;
    if (results && artifact.chart_config) {
        turn.chartState = chartStateFromArtifact(artifact);
        turn.hasChart = true;
    } else {
        turn.chartState = null;
        turn.hasChart = false;
    }
    turn.artifactState = results ? ARTIFACT_LOADED : ARTIFACT_MISSING;
    turn.canLoadData = !results && turn.resultKind === 'table' && Boolean(turn.result?.sql);
    return turn;
}

/** Whether a turn can be shown in the result pane right now. */
export function hasRenderableResult(turn) {
    return Boolean(turn && turn.status === 'success' && turn.result && turn.result.results);
}

/**
 * Generation guard: every hydration / connection switch / live send bumps the
 * generation; a response is applied only if its generation is still current.
 */
export function makeGenerationGuard() {
    let current = 0;
    return {
        next() { current += 1; return current; },
        isCurrent(generation) { return generation === current; },
        current() { return current; },
    };
}

if (typeof globalThis !== 'undefined') {
    globalThis.ConversationHydration = {
        turnFromServer,
        turnsFromDetail,
        applyArtifact,
        chartStateFromArtifact,
        hasRenderableResult,
        makeGenerationGuard,
        SNAPSHOT_STORED,
        SNAPSHOT_TOO_LARGE,
        SNAPSHOT_PRUNED,
        SNAPSHOT_NOT_APPLICABLE,
        ARTIFACT_MISSING,
        ARTIFACT_LOADING,
        ARTIFACT_LOADED,
        ARTIFACT_FAILED,
    };
}
