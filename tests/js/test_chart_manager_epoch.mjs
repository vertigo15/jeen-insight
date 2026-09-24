/**
 * Deterministic lifecycle ownership checks without a browser.
 * Run with: node tests/js/test_chart_manager_epoch.mjs
 */
import assert from 'node:assert/strict';

globalThis.window = {
    JeenPreferences: { getAll: () => ({ chartPalette: 'jeen' }) },
};
globalThis.document = {
    addEventListener() {},
    removeEventListener() {},
    getElementById() { return null; },
    documentElement: { dataset: {} },
};

const { ChartManager } = await import('../../src/static/chart-feature/chartManager.js');
const manager = new ChartManager();

const first = manager._beginOperation();
assert.equal(manager._owns(first), true);
const second = manager._beginOperation();
assert.equal(first.controller.signal.aborted, true, 'new operation aborts the prior controller');
assert.equal(manager._owns(first), false, 'prior epoch loses ownership');
assert.equal(manager._owns(second), true);

manager._finishOperation(first);
assert.equal(manager._chartAbort, second.controller, 'stale finally cannot clear the current owner');
manager._finishOperation(second);
assert.equal(manager._chartAbort, null, 'owner clears its own controller');

manager._setChartSession({
    chart_config: {
        xAxis: { type: 'category', data: ['A'] },
        yAxis: { type: 'value' },
        series: [{ type: 'bar', data: [1] }],
    },
    chart_spec: { chart_type: 'bar', x: 'region', y: ['sales'] },
});
manager._renderReady = false;
assert.equal(
    manager.getSaveState().chart_spec.chart_type,
    'bar',
    'semantic state remains capturable while presentation rendering is in flight',
);

const disposedOwner = manager._beginOperation();
manager.dispose();
assert.equal(disposedOwner.controller.signal.aborted, true);
assert.equal(manager._owns(disposedOwner), false, 'disposed manager can never regain ownership');

console.log('chart_manager_epoch JS tests passed');
