"""Stage grouping for the Analytics execution-path timeline.

The grouping lives in the settings page script. These cases lock the marker
rules: a delivered table, an estimated marker for older rows, a retry when the
stage order goes backwards, and no marker when nothing was shown.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
import { buildLineage } from './src/static/settings/analyticsSection.js';

const cases = [
  {
    name: 'delivered table',
    run: { outcome: 'success', row_count: 3, route: 'needs_query' },
    trace: [
      { node: 'fused_router', elapsed_ms: 10 },
      { node: 'sql_generator', elapsed_ms: 20 },
      { node: 'trivial_result_check', elapsed_ms: 5, shown: true },
    ],
    markers: ['shown'],
    attempts: 1,
    note: '',
  },
  {
    name: 'estimated older run',
    run: { outcome: 'success', row_count: 2 },
    trace: [
      { node: 'execute_query', elapsed_ms: 8 },
      { node: 'trivial_result_check', elapsed_ms: 1 },
      { node: 'trivial_result_check', elapsed_ms: 1 },
    ],
    markers: [null, 'estimated'],
    attempts: 1,
    note: '',
  },
  {
    name: 'retry starts a new attempt',
    run: { outcome: 'success', row_count: 1 },
    trace: [
      { node: 'sql_generator', elapsed_ms: 10, shown: true },
      { node: 'feedback_classifier', elapsed_ms: 4 },
      { node: 'sql_generator', elapsed_ms: 12 },
    ],
    markers: ['shown', null, null],
    attempts: 2,
    note: '',
  },
  {
    name: 'ml table arrives with the final answer',
    run: { outcome: 'success', row_count: 4 },
    trace: [
      { node: 'analysis_run', elapsed_ms: 30 },
      { node: 'response_formatter', elapsed_ms: 6 },
    ],
    markers: ['final'],
    attempts: 1,
    note: '',
  },
  {
    name: 'zero rows',
    run: { outcome: 'error', row_count: 0 },
    trace: [{ node: 'execute_dax', elapsed_ms: 9 }],
    markers: [],
    attempts: 1,
    note: 'zero',
  },
  {
    name: 'nothing shown',
    run: { outcome: 'success', row_count: null, route: 'greeting' },
    trace: [{ node: 'fused_router', elapsed_ms: 3 }],
    markers: [],
    attempts: 1,
    note: 'none',
  },
];

const failures = [];
for (const item of cases) {
  const lineage = buildLineage(item.run, item.trace);
  const markers = lineage.attempts.flatMap((attempt) => attempt.stages.flatMap((stage) => stage.steps.map((step) => step.marker || null)));
  const expected = item.markers.filter((marker) => marker);
  const actual = markers.filter(Boolean);
  if (lineage.attempts.length !== item.attempts || lineage.note !== item.note || JSON.stringify(actual) !== JSON.stringify(expected)) {
    failures.push({ name: item.name, attempts: lineage.attempts.length, note: lineage.note, markers: actual });
  }
}
if (failures.length) {
  console.error(JSON.stringify(failures, null, 2));
  process.exit(1);
}
"""


def test_lineage_markers_follow_the_path_that_ran():
    node = shutil.which("node")
    assert node, "node is required to check the execution-path grouping"
    completed = subprocess.run(
        [node, "--input-type=module", "-e", SCRIPT],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
