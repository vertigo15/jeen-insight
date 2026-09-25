/**
 * What-if scenario engine invariants: rules, series shapes, annotations.
 * Run with: node tests/js/test_chart_scenarios.mjs
 */
import assert from 'node:assert/strict';
import {
    applyAnnotations,
    applyScenarioRule,
    buildScenarioSeries,
    resolveTargetSeries,
    scenarioSpecFromOperation,
    seriesCategories,
    __test__,
} from '../../src/static/chart-feature/utils/chartScenarios.js';

const config = {
    xAxis: { type: 'category', data: ['Jan', 'Feb', 'Mar', 'Apr'] },
    yAxis: { type: 'value' },
    series: [
        { id: 'rev', name: 'Revenue', type: 'line', data: [100, 200, null, 400], smooth: true },
        { id: 'cost', name: 'Cost', type: 'bar', data: [50, 60, 70, 80], stack: 'total' },
        { name: 'MA', type: 'line', data: [1, 2, 3, 4], __derived: { operator: 'moving_avg' } },
        { name: 'Band', type: 'line', data: [1, 2, 3, 4], jeenRole: 'interval_base' },
    ],
};
const categories = seriesCategories(config.series[0], config);
assert.deepEqual(categories, ['Jan', 'Feb', 'Mar', 'Apr']);

// Targets skip derived and helper series.
assert.deepEqual(resolveTargetSeries(config, 'all').map(({ series }) => series.name), ['Revenue', 'Cost']);
assert.deepEqual(resolveTargetSeries(config, 'name:cost').map(({ series }) => series.name), ['Cost']);
assert.deepEqual(resolveTargetSeries(config, 'id:rev').map(({ series }) => series.name), ['Revenue']);
assert.deepEqual(resolveTargetSeries(config, 'name:Band'), []);

// Rules: nulls stay null; "from" applies to the tail; set_point is exact.
assert.deepEqual(applyScenarioRule({ scenario: 'set_point', category: 'mar', value: 300 }, [100, 200, null, 400], categories), [100, 200, 300, 400]);
assert.deepEqual(applyScenarioRule({ scenario: 'scale', percent: 10, from_category: 'Mar' }, [100, 200, null, 400], categories), [100, 200, null, 440]);
assert.deepEqual(applyScenarioRule({ scenario: 'scale', factor: 0.5 }, [100, 200, null, 400], categories), [50, 100, null, 200]);
assert.deepEqual(applyScenarioRule({ scenario: 'shift', delta: -25 }, [100, 200, null, 400], categories), [75, 175, null, 375]);
assert.equal(applyScenarioRule({ scenario: 'set_point', category: 'Dec', value: 1 }, [1], ['Jan']), null);

// Operation → spec normalisation resolves the category label and defaults a label.
const spec = scenarioSpecFromOperation({ op: 'scenario_set_point', target: 'name:revenue', category: 'FEB', value: '250' }, config);
assert.equal(spec.kind, 'scenario');
assert.equal(spec.target, 'name:revenue');
assert.equal(spec.category, 'Feb');
assert.equal(spec.value, 250);
assert.equal(spec.label, 'FEB = 250');
assert.equal(scenarioSpecFromOperation({ op: 'scenario_scale', target: 'all', percent: 10 }, config).label, '+10%');
assert.equal(scenarioSpecFromOperation({ op: 'scenario_shift', target: 'all', delta: 1500, from_category: 'Mar' }, config).label, '+1.5K from Mar');
assert.throws(() => scenarioSpecFromOperation({ op: 'scenario_scale', target: 'all', percent: 5000 }, config), (error) => error.code === 'invalid_scenario');
assert.throws(() => scenarioSpecFromOperation({ op: 'scenario_set_point', target: 'name:Nope', category: 'Jan', value: 1 }, config), (error) => error.code === 'target_not_found');
assert.throws(() => scenarioSpecFromOperation({ op: 'scenario_set_point', target: 'all', category: 'Dec', value: 1 }, config), (error) => error.code === 'invalid_scenario' || error.code === 'unknown_category');

// Series shapes: one dashed copy per matched real series, styled by type, never a stack collision.
const copies = buildScenarioSeries({ kind: 'scenario', scenario: 'shift', target: 'all', delta: 10, label: '+10' }, config);
assert.equal(copies.length, 2);
assert.equal(copies[0].name, 'Scenario: +10 (Revenue)');
assert.equal(copies[0].type, 'line');
assert.equal(copies[0].lineStyle.type, 'dashed');
assert.deepEqual(copies[0].data, [110, 210, null, 410]);
assert.equal(copies[0].smooth, true);
assert.equal(copies[1].type, 'bar');
assert.equal(copies[1].stack, 'total__scenario');
assert.equal(copies[1].__derived.kind, 'scenario');
assert.equal(copies[1].__derived.label, '+10');
const single = buildScenarioSeries({ kind: 'scenario', scenario: 'set_point', target: 'id:rev', category: 'Jan', value: 1, label: 'Jan = 1' }, config);
assert.equal(single[0].name, 'Scenario: Jan = 1');
assert.deepEqual(buildScenarioSeries({ kind: 'scenario', scenario: 'set_point', target: 'id:gone', category: 'Jan', value: 1, label: 'x' }, config), []);

// Tuple points keep their x value.
const tuples = { series: [{ name: 'T', type: 'line', data: [['2026-01', 5], ['2026-02', 7]] }] };
const tupleCopy = buildScenarioSeries({ kind: 'scenario', scenario: 'scale', target: 'all', factor: 2, label: '×2' }, tuples);
assert.deepEqual(tupleCopy[0].data, [['2026-01', 10], ['2026-02', 14]]);

// Pie: outer dashed ring with recomputed values.
const pie = { series: [{ name: 'Share', type: 'pie', radius: '62%', center: ['50%', '50%'], data: [{ name: 'A', value: 3 }, { name: 'B', value: 1 }] }] };
const ring = buildScenarioSeries({ kind: 'scenario', scenario: 'set_point', target: 'all', category: 'B', value: 3, label: 'B = 3' }, pie)[0];
assert.equal(ring.type, 'pie');
assert.deepEqual(ring.radius, ['62%', '76%']);
assert.deepEqual(ring.center, ['50%', '50%']);
assert.deepEqual(ring.data, [{ name: 'A', value: 3 }, { name: 'B', value: 3 }]);

// Annotations: stats, thresholds, and the two highlight renderings.
assert.equal(__test__.stat([1, 2, 3, 4], 'median'), 2.5);
assert.equal(__test__.stat([1, null, 3], 'avg'), 2);
assert.equal(__test__.matchesPredicate(5, { op: 'between', value: 10, value2: 1 }), true);
assert.equal(__test__.matchesPredicate(null, { op: 'gt', value: 0 }), false);
const drawn = applyAnnotations(config, {
    referenceLines: [{ axis: 'y', stat: 'max', target: 'name:Cost', label: 'Max cost' }, { axis: 'y', value: 150, label: 'Target' }],
    highlights: [
        { target: 'name:Cost', predicate: { op: 'gte', value: 70 }, color: '#2563eb' },
        { target: 'id:rev', categories: ['Feb'], label: 'Peak' },
    ],
});
assert.equal(drawn.series[1].markLine.data[0].yAxis, 80);
assert.equal(drawn.series[0].markLine.data[0].yAxis, 150);
assert.equal(drawn.series[1].data[2].itemStyle.color, '#2563eb');
assert.equal(drawn.series[1].data[3].itemStyle.color, '#2563eb');
assert.equal(drawn.series[1].data[0], 50);
assert.deepEqual(drawn.series[0].markPoint.data[0].coord, ['Feb', 200]);
assert.deepEqual(config.series[1].data, [50, 60, 70, 80], 'source config is not mutated');
assert.equal(config.series[0].markLine, undefined);

// Horizontal bars: value axis is X, so the reference line pins xAxis.
const horizontal = {
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: ['A', 'B'] },
    series: [{ name: 'H', type: 'bar', data: [1, 2] }],
};
const hDrawn = applyAnnotations(horizontal, { referenceLines: [{ axis: 'y', value: 1.5, label: 't' }], highlights: [] });
assert.equal(hDrawn.series[0].markLine.data[0].xAxis, 1.5);

console.log('chart scenarios JS tests passed');
