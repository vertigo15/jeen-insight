from __future__ import annotations

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
      if (u.PHASES.length !== 9) throw new Error('phase count');
      if (u.NODE_PHASE.pbi_execute_query !== 'execution') throw new Error('DAX mapping');
      if (u.NODE_PHASE.sql_generator !== 'generation') throw new Error('SQL mapping');
      if (u.NODE_PHASE.pre_graph_setup !== 'catalog') throw new Error('pre-graph mapping');

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
    assert "edit.hidden = this.chartCollapsed || isAnalysis" in controller
    assert "setChartCollapsed?.(this.chartCollapsed)" in controller
    assert 'id="v3-analysis-adjust"' in controller
    assert "target = isAnalysis ? analysisContent : chartEdit" in controller
    assert ".v3-chart-primary > .chart-type-selector-container {" in styles
    assert ".v3-chart-types[hidden] { display: none !important; }" in styles
    assert ".v3-chart-edit[hidden] { display: none !important; }" in styles
    assert 'id="v3-chart-more"' in controller
    assert "this._renderChartOptionsOverflow()" in controller
    assert ".v3-chart-secondary.is-open { display: flex; }" in styles
    assert "height: 30px;" in styles
    assert "#v3-chart-toggle {" in styles


def test_workspace_chart_modes_keep_ml_reruns_separate_from_graph_edits():
    root = Path(__file__).resolve().parents[2]
    controller = (root / "src/static/workspace/workspaceController.js").read_text()
    manager = (root / "src/static/chart-feature/chartManager.js").read_text()
    chat = (
        root / "src/static/chart-feature/components/ChartChat.js"
    ).read_text()
    options = (
        root / "src/static/chart-feature/components/ChartOptionsPanel.js"
    ).read_text()

    assert "this._inputEl.placeholder = this._analysisMode ? ANALYSIS_PLACEHOLDER() : CHART_PLACEHOLDER()" in chat
    assert "rerunAnalysis" in chat and "rerunTitle" in chat
    assert "setAnalysisMode(on)" in manager
    assert "this.analysisMode = Boolean(on)" in manager
    assert "this.chartTypeSelector?.setDisabled(" in manager
    assert "this.chartOptionsPanel?.setAnalysisMode(this.analysisMode)" in manager
    assert "this.analysisMode && key === 'sortDesc'" in options
    assert "if (this.analysisMode) return;" in options
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
