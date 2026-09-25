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

// A render failure after valid operations restores the exact prior session.
const beforeEdit = manager.chartSession.snapshot();
const revision = manager._chartEditToken();
let renderAttempts = 0;
manager._renderWorking = async (owner) => {
    manager._assertOwner(owner);
    renderAttempts += 1;
    if (renderAttempts === 1) throw new Error('synthetic render failure');
};
const originalConsoleError = console.error;
console.error = () => {};
await assert.rejects(
    manager.applyEditedOperations([
        { op: 'set_color', target: 'all', color: '#22c55e' },
    ], revision),
    /synthetic render failure/,
);
console.error = originalConsoleError;
assert.equal(renderAttempts, 2, 'rollback is rendered after the failed candidate');
assert.deepEqual(manager.chartSession.snapshot(), beforeEdit);

const disposedOwner = manager._beginOperation();
manager.dispose();
assert.equal(disposedOwner.controller.signal.aborted, true);
assert.equal(manager._owns(disposedOwner), false, 'disposed manager can never regain ownership');

console.log('chart_manager_epoch JS tests passed');
