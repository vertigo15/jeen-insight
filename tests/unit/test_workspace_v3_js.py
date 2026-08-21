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
      if (u.filterResultRows(results, 'us').length !== 1) throw new Error('row filtering');
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


def test_workspace_renders_findings_as_key_insights():
    root = Path(__file__).resolve().parents[2]
    styles = (root / "src/static/workspace/workspace.css").read_text()
    controller = (
        root / "src/static/workspace/workspaceController.js"
    ).read_text()

    assert 'class="v3-insights" aria-label="Key insights"' in controller
    assert "v3-insights-title" in controller
    assert "v3-insight-index" in controller
    assert ".v3-insights {" in styles
    assert "background: var(--rosesoft);" in styles
    assert "color: var(--text);" in styles
