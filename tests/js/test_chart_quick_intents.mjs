import assert from 'node:assert/strict';
import { parseChartQuickIntent } from '../../src/static/chart-feature/utils/chartQuickIntents.js';

{
    const parsed = parseChartQuickIntent('all lables and change colore to red');
    assert.deepEqual(parsed.commands[0], { type: 'toggle', key: 'dataLabels', value: true });
    assert.equal(parsed.commands[1].type, 'customPalette');
    assert.equal(parsed.commands[1].name, 'red');
    assert.equal(parsed.commands[1].colors.length, 8);
}

{
    const parsed = parseChartQuickIntent('hide labels and turn off legend');
    assert.deepEqual(parsed.commands, [
        { type: 'toggle', key: 'dataLabels', value: false },
        { type: 'toggle', key: 'legend', value: false },
    ]);
}

{
    const parsed = parseChartQuickIntent('use the blue color palette');
    assert.deepEqual(parsed.commands, [{ type: 'palette', id: 'blue' }]);
}

{
    const parsed = parseChartQuickIntent('show month names in the x axis', {
        columns: ['month_number', 'month_name', 'revenue_2006'],
    });
    assert.deepEqual(parsed.commands, [{ type: 'binding', field: 'xColumn', value: 'month_name' }]);
}

assert.equal(parseChartQuickIntent('label only the top three bars'), null,
    'advanced/ambiguous instructions must fall through to the LLM');
assert.equal(parseChartQuickIntent('make the revenue series red'), null,
    'targeted per-series colour remains an LLM edit');
assert.deepEqual(
    parseChartQuickIntent("don't show labels").commands,
    [{ type: 'toggle', key: 'dataLabels', value: false }],
);
assert.equal(
    parseChartQuickIntent('do not change the x axis to month name', {
        columns: ['month_number', 'month_name'],
    }),
    null,
    'negated bindings must never be applied locally',
);
assert.equal(parseChartQuickIntent('make labels red'), null,
    'ambiguous label-colour instructions must fall through to the LLM');
assert.equal(parseChartQuickIntent("don't hide the legend"), null,
    'double-negated controls must fall through rather than invert intent');
assert.equal(parseChartQuickIntent('relabel the axes'), null,
    'label must match as a word, not as a substring');
assert.equal(parseChartQuickIntent('show the table labels'), null,
    'qualified labels outside the chart vocabulary must fall through');
assert.equal(parseChartQuickIntent('use name on the x axis', {
    columns: ['customer_name', 'product_name'],
}), null, 'ambiguous column matches must fall through');

console.log('chart quick-intent JS tests passed');
