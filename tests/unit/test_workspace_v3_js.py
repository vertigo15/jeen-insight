from __future__ import annotations

import re
import subprocess
from pathlib import Path


def test_workspace_v3_pure_utilities():
    root = Path(__file__).resolve().parents[2]
    controller = root / "src/static/workspace/workspaceController.js"
    script = f"""
      const fs = require('fs');
      const vm = require('vm');
      global.window = {{
        addEventListener() {{}},
        escapeHtml(value) {{ return String(value); }}
      }};
      vm.runInThisContext(fs.readFileSync({str(controller)!r}, 'utf8'));
      const u = window.WorkspaceV3Utils;
      if (u.PHASES.length !== 11) throw new Error('phase count');
      if (u.PHASES[0].id !== 'setup') throw new Error('the pre-load phase runs first');
      if (u.NODE_PHASE.pbi_execute_query !== 'execution') throw new Error('DAX mapping');
      if (u.NODE_PHASE.sql_generator !== 'generation') throw new Error('SQL mapping');
      if (u.NODE_PHASE.pre_graph_setup !== 'setup') throw new Error('pre-graph mapping');
      if (u.NODE_PHASE.filter_planner !== 'filters' || u.NODE_PHASE.filter_grounder !== 'filters') throw new Error('filter mapping');
      if (u.NODE_PHASE.empty_filter_result_check !== 'execution') throw new Error('empty-check mapping');

      const results = {{
        columns: ['amount', 'region'],
        rows: [
          {{amount: 10, region: 'EU'}},
          {{amount: null, region: 'US'}},
          {{amount: 30, region: 'EU'}}
        ],
        truncated: true,
        cap: 3
      }};
      const profile = u.compactProfile(results);
      if (profile[0].type !== 'number') throw new Error('numeric profile');
      if (profile[0].distinct !== 2) throw new Error('distinct profile');
      if (profile[0].nullPct !== 33.3) throw new Error('null profile');
      const cap = u.cappedMeta(results);
      if (!cap.capped || cap.loaded !== 3 || cap.total !== null) throw new Error('cap label');
      if (u.textOf([{{t:'Revenue '}}, {{text:'grew'}}]) !== 'Revenue grew') throw new Error('fragment text');
      const note = u.safeTraceNote({{node:'sql_generator', type:'llm', detail:'SELECT secret FROM payroll'}});
      if (note.includes('SELECT') || note.includes('payroll')) throw new Error('SQL leaked into inline trace');
      const preload = u.safeTraceNote({{node:'pre_graph_setup', type:'db', detail:'question-specific catalog 25ms · reusable catalog 7ms'}});
      if (!preload.includes('question-specific catalog 25ms')) throw new Error('pre-graph timing hidden');
      if (u.filterResultRows(results, 'us').length !== 1) throw new Error('row filtering');
      if (u.directionOf('המכירות עלו ב-37%') !== 'rtl') throw new Error('Hebrew direction');
      if (u.directionOf('Revenue increased 37%') !== 'ltr') throw new Error('English direction');
      if (u.directionOf('צמיחה of 36.9% לעומת 2006') !== 'rtl') throw new Error('mixed Hebrew direction');
      const failed = u.selectionForTurn('good', {{id:'bad', status:'error'}});
      if (failed.selectedTurnId !== 'bad' || failed.selectedResultId !== 'good') throw new Error('error replaced good result');
      const success = u.selectionForTurn('good', {{id:'new', status:'success'}});
      if (success.selectedResultId !== 'new') throw new Error('successful turn not selected');
      const streaming = u.selectionForTurn('good', {{id:'live', status:'streaming'}});
      if (streaming.selectedResultId !== 'live') throw new Error('streaming turn not selected');
      if (!u.turnShowsResult({{status:'error', provisionalRevision: 0, result: {{results: {{rows: [[1]]}}}}}})) throw new Error('failed-after-rows hidden');
      if (u.turnShowsResult({{status:'error', result: {{results: {{rows: [[1]]}}}}}})) throw new Error('plain error shown as result');
    """

    subprocess.run(["node", "-e", script], cwd=root, check=True)


def test_workspace_progressive_result_merges_partial_without_chart_rebuild():
    """`partial` paints the rows and selects the turn; the final `result` for
    the same dataset merges the narrative in place (no revision bump, chart
    kept, grid repaint skipped). A second partial or a different dataset is a
    new revision. A failure after the rows keeps them on screen."""
    root = Path(__file__).resolve().parents[2]
    controller = root / "src/static/workspace/workspaceController.js"
    script = f"""
      const fs = require('fs');
      const vm = require('vm');
      const narrative = [];
      global.window = {{
        addEventListener() {{}},
        escapeHtml(value) {{ return String(value); }},
        JeenLegacyBridge: {{ applyResultNarrative(data) {{ narrative.push(data); }} }},
      }};
      global.document = {{ dispatchEvent() {{}} }};
      vm.runInThisContext(fs.readFileSync({str(controller)!r}, 'utf8'));
      const c = window.WorkspaceController;
      let renders = 0;
      c.render = () => {{ renders += 1; }};
      c.renderConversation = () => {{}};
      c._scrollThread = () => {{}};
      let captured = 0;
      c._captureSelectedChart = () => {{ captured += 1; }};
      const phases = () => Object.fromEntries(window.WorkspaceV3Utils.PHASES.map((p) => [p.id, 'pending']));
      const newTurn = (id) => ({{ id, question: 'q', status: 'running', startedAt: 0, rev: 0, phaseState: phases(), trace: [], result: null, error: null }});
      const rows = {{ columns: ['x'], rows: [[1], [2]] }};

      // 1. partial → streaming, selected, rows in place, rev untouched.
      let turn = newTurn('t1');
      c.turns = [turn];
      c._onPartial(turn, {{ query_id: 'q1', session_id: 's1', sql: 'select 1', results: rows, revision: 0, provisional: true }});
      if (turn.status !== 'streaming') throw new Error('status after partial: ' + turn.status);
      if (c.selectedResultId !== 't1' || c.selectedTurnId !== 't1') throw new Error('partial did not select the turn');
      if (turn.result.results !== rows || turn.rev !== 0 || turn.provisionalRevision !== 0) throw new Error('partial state');
      if (turn.phaseState.execution !== 'done') throw new Error('execution phase not marked done');
      if (renders !== 1) throw new Error('partial should render once');

      // 2. final result, same dataset → merge; chart + grid untouched.
      c.lastAppliedResultId = 't1';   // the chart was applied from the partial
      const provisionalResults = turn.result.results;
      c._onResult(turn, {{ query_id: 'q1', session_id: 's1', sql: 'select 1', results: {{ columns: ['x'], rows: [[1], [2]] }},
        answer: 'Two rows.', findings: ['f1'], followups: ['next?'], metrics: {{ llm_latency_ms: 5 }}, trace: [] }});
      if (turn.status !== 'success') throw new Error('status after result');
      if (turn.rev !== 0) throw new Error('same dataset bumped rev');
      if (turn.provisionalRevision !== null) throw new Error('provisional flag not cleared');
      if (c.lastAppliedResultId !== 't1') throw new Error('chart application reset');
      if (captured !== 0) throw new Error('chart captured on a same-dataset merge');
      if (turn.result.results !== provisionalResults) throw new Error('rows object replaced');
      if (turn.result.answer !== 'Two rows.' || turn.result.findings[0] !== 'f1') throw new Error('narrative not merged');
      if (c._tableRepaintHold !== c._tableKey(turn)) throw new Error('grid repaint not held for the same table');
      if (narrative.length !== 1 || narrative[0].answer !== 'Two rows.') throw new Error('legacy panels not refreshed');
      // Timeline: table stamped at the partial, insights at the result, chart when it renders (once).
      if (!Number.isFinite(turn.timeline.tableMs) || !Number.isFinite(turn.timeline.insightsMs)) throw new Error('timeline stamps: ' + JSON.stringify(turn.timeline));
      if (turn.timeline.insightsMs < turn.timeline.tableMs) throw new Error('insights before table');
      if (turn.timeline.chartMs != null) throw new Error('chart stamped before render');
      c._onChartRendered({{ queryId: 'q1', kind: 'bar' }});
      if (!Number.isFinite(turn.timeline.chartMs)) throw new Error('chart not stamped');
      const firstChart = turn.timeline.chartMs;
      c._onChartRendered({{ queryId: 'q1', kind: 'line' }});
      if (turn.timeline.chartMs !== firstChart) throw new Error('chart re-render overwrote time to chart');
      const html = c._timelineHtml(turn);
      if (!html.includes('data-timeline="table"') || !html.includes('data-timeline="insights"') || !html.includes('data-timeline="chart"')) throw new Error('timeline html: ' + html);

      // 3. two partials with identical SQL → new revision, chart dropped.
      turn = newTurn('t2'); c.turns = [turn]; c.lastAppliedResultId = null;
      c._onPartial(turn, {{ query_id: 'q2', sql: 'select 1', results: rows, revision: 0 }});
      c.lastAppliedResultId = 't2';
      turn.chartState = {{ chart_config: {{}} }};
      c._onPartial(turn, {{ query_id: 'q2', sql: 'select 1', results: {{ columns: ['x'], rows: [[9]] }}, revision: 1 }});
      if (turn.rev !== 1) throw new Error('second partial did not bump rev: ' + turn.rev);
      if (c.lastAppliedResultId !== null || turn.chartState !== null) throw new Error('stale chart kept');
      if (turn.timeline.chartMs != null || turn.timeline.chartSettled) throw new Error('new rows kept the old chart time');
      if (turn.provisionalRevision !== 1) throw new Error('revision not recorded');

      // 4. final result with a different dataset than the partial → replace + bump.
      c.lastAppliedResultId = 't2';
      c._onResult(turn, {{ query_id: 'q2', sql: 'select 2', results: {{ columns: ['x'], rows: [[7], [8], [9]] }}, answer: 'x', trace: [] }});
      if (turn.rev !== 2) throw new Error('changed dataset did not bump rev: ' + turn.rev);
      if (c.lastAppliedResultId !== null) throw new Error('changed dataset kept the chart');
      if (turn.result.results.rows.length !== 3) throw new Error('rows not replaced');
      if (c._tableRepaintHold) throw new Error('changed dataset held the repaint');

      // 5. failure after the rows: keep them, mark the error.
      turn = newTurn('t3'); c.turns = [turn]; c.lastAppliedResultId = null;
      c._onPartial(turn, {{ query_id: 'q3', sql: 'select 1', results: rows, revision: 0 }});
      c._onError(turn, new Error('LLM timed out'));
      if (turn.status !== 'error' || turn.error !== 'LLM timed out') throw new Error('error state');
      if (turn.result.results !== rows) throw new Error('rows dropped on error');
      if (!window.WorkspaceV3Utils.turnShowsResult(turn)) throw new Error('failed-after-rows turn hidden from the answer pane');

      // 6. a plain result without a partial behaves as before.
      turn = newTurn('t4'); c.turns = [turn]; captured = 0;
      c._onResult(turn, {{ query_id: 'q4', sql: 'select 1', results: rows, answer: 'a', trace: [] }});
      if (turn.status !== 'success' || turn.rev !== 0 || captured !== 1) throw new Error('plain result path changed');
    """

    subprocess.run(["node", "-e", script], cwd=root, check=True)


def test_workspace_run_details_tooltips_and_execution_order():
    """Run-details rows explain each step (time split, tokens); token counts say
    which is input and which is output; the final trace puts the pre-load first
    and its breakdown adds up; the shown turn's milestones reach the drawer."""
    root = Path(__file__).resolve().parents[2]
    controller = root / "src/static/workspace/workspaceController.js"
    script = f"""
      const fs = require('fs');
      const vm = require('vm');
      const milestones = [];
      global.window = {{
        addEventListener() {{}},
        escapeHtml(value) {{ return String(value); }},
        I18n: {{
          t(key, args) {{ return args ? key + ' ' + JSON.stringify(args) : key; }},
          has(key) {{ return key === 'conversation.trace.nodes.filter_planner'; }},
          formatNumber(n) {{ return 'N' + n; }},
        }},
        JeenLegacyBridge: {{ applyResultNarrative() {{}}, setRunMilestones(t) {{ milestones.push(t); }} }},
      }};
      global.document = {{ dispatchEvent() {{}} }};
      vm.runInThisContext(fs.readFileSync({str(controller)!r}, 'utf8'));
      const u = window.WorkspaceV3Utils;

      const lines = u.nodeTip({{ node: 'filter_planner', elapsed_ms: 4690, llm_ms: 400, input_tokens: 3012, output_tokens: 58 }}).split('\\n');
      if (lines[0] !== 'conversation.trace.nodes.filter_planner') throw new Error('description: ' + lines[0]);
      if (!lines[1].startsWith('conversation.trace.tips.timeSplit') || !lines[1].includes('"model":"400ms"') || !lines[1].includes('"other":"4.29s"')) throw new Error('time split: ' + lines[1]);
      if (!lines[2].includes('"input":"N3012"') || !lines[2].includes('"output":"N58"')) throw new Error('tokens: ' + lines[2]);
      const plain = u.nodeTip({{ node: 'mystery_step', elapsed_ms: 12 }}).split('\\n');
      if (plain[0] !== 'conversation.trace.nodes.fallback' || plain.length !== 2) throw new Error('fallback tip: ' + plain);

      const tips = u.tokenTips({{ input_tokens: 10712, output_tokens: 494 }}, [
        {{ node: 'fused_router', status: 'node_finished', input_tokens: 2100, output_tokens: 40 }},
        {{ node: 'sql_generator', status: 'node_finished', input_tokens: 4200, output_tokens: 150 }},
        {{ node: 'sql_generator', status: 'node_started' }},
      ]);
      if (!tips.input.startsWith('results.tokens.inTip {{"count":"N10712"}}')) throw new Error('input tip: ' + tips.input);
      if (!tips.input.includes('fused_router N2100 · sql_generator N4200')) throw new Error('input split: ' + tips.input);
      if (!tips.output.startsWith('results.tokens.outTip {{"count":"N494"}}') || !tips.output.includes('sql_generator N150')) throw new Error('output tip: ' + tips.output);
      const detail = '1 filter(s) planned · catalog examples matched 1 column(s): dimproductcategory.englishproductcategoryname · value search skipped';
      if (u.safeTraceNote({{ node: 'filter_planner', type: 'llm', detail }}) !== detail) throw new Error('filter planner detail hidden');

      const c = window.WorkspaceController;
      c.render = () => {{}};
      c.renderConversation = () => {{}};
      c._scrollThread = () => {{}};
      c._captureSelectedChart = () => {{}};
      const turn = {{ id: 't1', question: 'q', status: 'running', startedAt: 0, rev: 0,
        phaseState: Object.fromEntries(u.PHASES.map((p) => [p.id, 'pending'])), trace: [], result: null, error: null }};
      c.turns = [turn];
      for (const node of ['context_composer', 'fused_router']) {{
        c._onNode(turn, {{ node, status: 'node_started' }});
        c._onNode(turn, {{ node, status: 'node_finished', elapsed_ms: 5 }});
      }}
      // An older server sends the pre-load only in the final trace: it still lists first.
      c._onResult(turn, {{ query_id: 'q1', sql: 'select 1', results: {{ columns: ['x'], rows: [[1]] }}, answer: 'a', trace: [
        {{ node: 'pre_graph_setup', elapsed_ms: 10500, mcp_timing: {{ filtered_tool_ms: 4850, full_restore_ms: 4600, connection_ms: 0, parse_ms: 1, full_restore_cache: 'invalidated' }} }},
        {{ node: 'context_composer', elapsed_ms: 1 }},
        {{ node: 'fused_router', elapsed_ms: 1810, llm_ms: 1700, input_tokens: 2100, output_tokens: 40 }},
      ] }});
      const order = turn.trace.map((e) => e.node).join(',');
      if (order !== 'pre_graph_setup,context_composer,fused_router') throw new Error('order: ' + order);
      if (turn.trace.some((e) => e.status !== 'node_finished')) throw new Error('started events kept after the result');
      if (turn.trace[2].llm_ms !== 1700) throw new Error('server fields not merged');
      if (turn.phaseState.setup !== 'done') throw new Error('setup phase not marked done');
      if (turn.persisting || turn.phaseState.save !== 'done') throw new Error('a result that does not say saving is final');

      const html = c._traceHtml(turn, turn.trace);
      if (!html.includes('conversation.trace.mcpOther') || !html.includes('1.05s')) throw new Error('other-setup row: ' + html);
      if (!html.includes('conversation.trace.cache.invalidated')) throw new Error('cache state missing');
      if (!html.includes('data-tip="conversation.trace.tips.mcpFiltered"')) throw new Error('sub-row tooltip missing');
      if (!html.includes('data-tip="conversation.trace.tips.reconcile"')) throw new Error('reconcile tooltip missing');

      turn.timeline = {{ tableMs: 22900, insightsMs: 27300 }};
      c._syncRunMilestones(turn);
      if (milestones.at(-1).tableMs !== 22900 || milestones.at(-1).insightsMs !== 27300) throw new Error('milestones not sent');
      c._syncRunMilestones({{ ...turn, restored: true }});
      if (milestones.at(-1) !== null) throw new Error('a restored turn has no milestones');
    """

    subprocess.run(["node", "-e", script], cwd=root, check=True)


def test_workspace_steps_after_the_answer_complete_its_trace():
    """The answer arrives before the history writes; their steps come in the
    enrichment event, join the run list in order, and do not count against
    the time to the answer."""
    root = Path(__file__).resolve().parents[2]
    controller = root / "src/static/workspace/workspaceController.js"
    script = f"""
      const fs = require('fs');
      const vm = require('vm');
      let narrated = 0;
      global.window = {{
        addEventListener() {{}},
        escapeHtml(value) {{ return String(value); }},
        I18n: {{ t(key, args) {{ return args ? key + ' ' + JSON.stringify(args) : key; }}, has() {{ return false; }} }},
        JeenLegacyBridge: {{ applyResultNarrative() {{ narrated += 1; }}, setRunMilestones() {{}} }},
      }};
      global.document = {{ dispatchEvent() {{}} }};
      vm.runInThisContext(fs.readFileSync({str(controller)!r}, 'utf8'));
      const u = window.WorkspaceV3Utils;
      const c = window.WorkspaceController;
      for (const name of ['render', 'renderConversation', '_scrollThread', '_captureSelectedChart', '_setActionsEnabled', '_renderFavoriteAction']) c[name] = () => {{}};
      const turn = {{ id: 't1', question: 'q', status: 'running', startedAt: 0, rev: 0,
        phaseState: Object.fromEntries(u.PHASES.map((p) => [p.id, 'pending'])), trace: [], result: null, error: null, timeline: {{}} }};
      c.turns = [turn];
      c._onResult(turn, {{ query_id: 'q1', sql: 'select 1', results: {{ columns: ['x'], rows: [[1]] }}, answer: 'a', saving: true, trace: [
        {{ node: 'execute_query', elapsed_ms: 800 }},
        {{ node: 'response_formatter', elapsed_ms: 0 }},
      ] }});
      c.lastAppliedResultId = 't1';
      turn.durationMs = 1000;
      // Until the history row is written the turn cannot be favorited.
      if (!turn.persisting || turn.phaseState.save !== 'running') throw new Error('early answer treated as saved');
      // The history write streams live after the answer, then the tail lands.
      c._onNode(turn, {{ node: 'save_to_memory', status: 'node_started' }});
      c._onNode(turn, {{ node: 'save_to_memory', status: 'node_finished', elapsed_ms: 1200 }});
      c._onEnrichment(turn, {{ result_handle: 'h1', trace_tail: [
        {{ node: 'save_to_memory', elapsed_ms: 1200, after_answer: true, detail: 'query_id=q1' }},
        {{ node: 'observability_log', elapsed_ms: 0, after_answer: true }},
      ] }});

      const order = turn.trace.map((e) => e.node).join(',');
      if (order !== 'execute_query,response_formatter,save_to_memory,observability_log') throw new Error('order: ' + order);
      if (turn.trace.some((e) => e.status !== 'node_finished')) throw new Error('live started event kept');
      if (turn.persisting || turn.phaseState.save !== 'done') throw new Error('not settled after the tail');
      if (turn.result.trace.length !== 4 || 'trace_tail' in turn.result) throw new Error('result trace not completed');
      if (turn.result.result_handle !== 'h1') throw new Error('other enrichment fields dropped');
      if (narrated !== 1) throw new Error('drawer not refreshed');
      const html = c._traceHtml(turn, turn.trace);
      if (!html.includes('&quot;graph&quot;:&quot;800ms&quot;') || !html.includes('&quot;overhead&quot;:&quot;200ms&quot;')) throw new Error('reconcile: ' + html);
      if (!u.nodeTip(turn.trace[2]).includes('conversation.trace.tips.afterAnswer')) throw new Error('after-answer tip');
      if (u.nodeTip(turn.trace[0]).includes('afterAnswer')) throw new Error('tip on a step before the answer');
    """

    subprocess.run(["node", "-e", script], cwd=root, check=True)


def test_workspace_bootstrap_hides_legacy_layout():
    root = Path(__file__).resolve().parents[2]
    template = (root / "src/templates/index.html").read_text()
    styles = (root / "src/static/workspace/workspace.css").read_text()
    controller = (
        root / "src/static/workspace/workspaceController.js"
    ).read_text()

    assert '<body class="v3-booting">' in template
    assert "body.v3-booting > .app-layout { visibility: hidden; }" in styles
    assert "document.body.classList.remove('v3-booting')" in controller
    assert "WorkspaceController.init();" in controller
    assert 'id="save-analysis-btn"' not in template
    assert "'#save-analysis-btn'" not in controller


def test_workspace_chart_starts_collapsed_with_aligned_controls():
    root = Path(__file__).resolve().parents[2]
    styles = (root / "src/static/workspace/workspace.css").read_text()
    controller = (
        root / "src/static/workspace/workspaceController.js"
    ).read_text()

    assert "chartCollapsed: true" in controller
    # The toggle label comes from the locale catalog (common.expand / common.collapse).
    assert 'id="v3-chart-toggle" type="button" class="v3-text-btn"' in controller
    assert 'aria-expanded="false" aria-controls="v3-chart-types v3-chart-frame v3-chart-edit"' in controller
    assert "controls.hidden = this.chartCollapsed" in controller
    assert "frame.hidden = this.chartCollapsed" in controller
    assert "edit.hidden = this.chartCollapsed" in controller
    assert "setChartCollapsed?.(this.chartCollapsed)" in controller
    assert 'id="v3-analysis-adjust"' in controller
    assert "if (analysisAdjust) analysisAdjust.hidden = true" in controller
    assert ".v3-chart-primary > .chart-type-selector-container {" in styles
    assert ".v3-chart-types[hidden] { display: none !important; }" in styles
    assert ".v3-chart-edit[hidden] { display: none !important; }" in styles
    assert 'id="v3-chart-more"' in controller
    assert "this._renderChartOptionsOverflow()" in controller
    assert ".v3-chart-secondary.is-open { display: flex; }" in styles
    assert "height: 30px;" in styles
    assert "#v3-chart-toggle {" in styles


def test_workspace_ml_small_chat_stays_chart_only():
    root = Path(__file__).resolve().parents[2]
    controller = (root / "src/static/workspace/workspaceController.js").read_text()
    manager = (root / "src/static/chart-feature/chartManager.js").read_text()
    chat = (
        root / "src/static/chart-feature/components/ChartChat.js"
    ).read_text()
    options = (
        root / "src/static/chart-feature/components/ChartOptionsPanel.js"
    ).read_text()

    assert "fetch('/api/edit-chart'" in chat
    assert "setAnalysisMode(on)" in manager
    assert "this.analysisMode = Boolean(on)" in manager
    assert "this.chartChat?.setAnalysisMode(false)" in manager
    assert "onAnalysisRerun:" not in manager
    assert "this.chartTypeSelector?.setDisabled(" in manager
    assert "this.chartOptionsPanel?.setAnalysisMode(this.analysisMode)" in manager
    assert "this.analysisMode && key === 'sortDesc'" in options
    assert "if (this.analysisMode) return;" in options
    assert "if (chat && chartEdit && chat.parentNode !== chartEdit) chartEdit.appendChild(chat)" in controller
    assert "if (analysisAdjust) analysisAdjust.hidden = true" in controller
    assert "if (this._analysisRerunInFlight)" in controller
    assert "this._selectionVersion === run.selectionVersion" in controller
    assert "if (id !== this.selectedTurnId) this._selectionVersion += 1" in controller
    assert "generation !== this._generation || abort.signal.aborted" in controller
    assert "timeoutMs: 185_000" in controller
    assert "this.lastAppliedResultId !== current.id" in controller
    assert "current.chartLoading || current.chartUnavailable" in controller
    assert 'id="v3-chart-status"' in controller


def test_workspace_table_places_describe_below_grid_with_toolbar_separator():
    root = Path(__file__).resolve().parents[2]
    controller = (root / "src/static/workspace/workspaceController.js").read_text()
    styles = (root / "src/static/workspace/workspace.css").read_text()

    assert controller.index('id="v3-grid-wrap"') < controller.index('id="v3-describe-slot"')
    assert 'class="v3-table-actions"' in controller
    assert "#v3-table-block > .v3-toolbar" in styles
    assert "border-top: 1px solid var(--border)" in styles


def test_workspace_renders_findings_as_key_insights():
    root = Path(__file__).resolve().parents[2]
    styles = (root / "src/static/workspace/workspace.css").read_text()
    controller = (
        root / "src/static/workspace/workspaceController.js"
    ).read_text()

    assert "class=\"v3-insights\" aria-label=\"${h('conversation.turn.keyInsights')}\"" in controller
    assert "v3-insights-title" in controller
    assert "v3-insight-index" in controller
    assert 'dir="${insightsDirection}"' in controller
    assert 'dir="${directionOf(summary)}"' in controller
    assert 'dir="${directionOf(question)}"' in controller
    assert ".v3-insights {" in styles
    assert "background: var(--insight-bg);" in styles
    assert "color: var(--text);" in styles
    # Previous answers collapse: insights / follow-ups / feedback are only
    # painted on the selected ("Show answer") turn.
    assert ".v3-turn:not(.is-selected) .v3-insights," in styles
    assert ".v3-turn:not(.is-selected) .v3-followups," in styles
    assert ".v3-turn:not(.is-selected) .v3-feedback { display: none; }" in styles


def test_workspace_answer_feedback_triggers_and_dialog():
    root = Path(__file__).resolve().parents[2]
    styles = (root / "src/static/workspace/workspace.css").read_text()
    controller = (root / "src/static/workspace/workspaceController.js").read_text()

    # Triggers: thumbs + feedback bubble, and the padding reset that keeps the
    # SVGs from collapsing under the global `button { padding: 0 16px }`.
    assert 'data-feedback="${turn.id}:${kind}"' in controller
    assert 'data-feedback-open="${turn.id}"' in controller
    assert 'aria-haspopup="dialog"' in controller
    assert re.search(r"\.v3-feedback-btn \{[^}]*padding: 0;", styles)
    # Thumb events go to the append-only log; a second click withdraws.
    assert "'/api/answer-feedback'" in controller
    assert "thumb: next || 'cleared'" in controller
    # Only SQL / ML answers carry the row.
    assert "_feedbackEligible(turn)" in controller
    # No dock side-effect on thumbs-down any more.
    assert "dockTab = 'model'" not in controller.split("async sendThumb")[1].split("_feedbackHtml(turn)")[0]

    # Dialog: labelled, modal, radiogroup stars, single-select chips, Send gate.
    for needle in (
        'role="dialog" aria-modal="true" aria-labelledby="v3-fb-title"',
        'role="radiogroup"',
        'role="radio" aria-checked=',
        "FEEDBACK_TYPES: ['general', 'report_bug', 'ui_bug', 'other']",
        "_feedbackDialogCanSend(state)",
        "closeFeedbackDialog()",
    ):
        assert needle in controller, needle
    # Entrance animation is scoped to the mount, not replayed on re-render.
    assert ".v3-fb-overlay.is-entering .v3-fb-modal { animation:" in styles
    # Jeen UI Modal dimensions: 448px card, radius 16, 24px padding.
    assert re.search(r"\.v3-fb-modal \{[^}]*width: min\(448px, 100%\)", styles, re.S)
    assert re.search(r"\.v3-fb-modal \{[^}]*border-radius: 16px", styles, re.S)
    assert re.search(r"\.v3-fb-modal \{[^}]*padding: 24px", styles, re.S)
    # Dark mode via tokens, no black send button (panel review).
    assert re.search(r"\.v3-fb-send \{[^}]*background: var\(--rose\)", styles, re.S)
