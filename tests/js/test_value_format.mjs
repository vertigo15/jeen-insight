/**
 * Tests for the client-side chart value formatter.
 * Run with:  node tests/js/test_value_format.mjs
 */
import assert from 'node:assert/strict';
import {
    collectNumericValues,
    makeLabelFormatter,
    makeValueFormatter,
} from '../../src/static/chart-feature/utils/valueFormat.js';

// Compact numbers: abbreviate large magnitudes, sensible decimals.
const num = makeValueFormatter({ kind: 'number', compact: true });
assert.equal(num(1234), '1.2K');
assert.equal(num(12345), '12K');
assert.equal(num(1500000), '1.5M');
assert.equal(num(2000000000), '2B');
assert.equal(num(1500000000000), '1.5T');
assert.equal(num(999), '999');
assert.equal(num(0), '0');
assert.equal(num(3.14159), '3.14');
assert.equal(num(0.0123), '0.012');
assert.equal(num(null), '');
assert.equal(num(undefined), '');
assert.equal(num(NaN), '');

// Currency uses the GIVEN symbol only; never assumes "$".
const usd = makeValueFormatter({ kind: 'currency', compact: true, symbol: '$' });
assert.equal(usd(1500000), '$1.5M');
assert.equal(usd(42), '$42');

const eur = makeValueFormatter({ kind: 'currency', compact: true, symbol: '€' });
assert.equal(eur(1500000), '€1.5M');

// Currency with no known symbol → plain number, NO "$".
const curUnknown = makeValueFormatter({ kind: 'currency', compact: true });
assert.equal(curUnknown(1500000), '1.5M');
assert.equal(curUnknown(42), '42');
const curEmpty = makeValueFormatter({ kind: 'currency', compact: true, symbol: '' });
assert.equal(curEmpty(1500000), '1.5M');

// Percent suffixes %.
const pct = makeValueFormatter({ kind: 'percent', compact: false });
assert.equal(pct(12.5), '12.5%');
assert.equal(pct(100), '100%');

// Compact abbreviates huge numbers and drops decimals once scaled ≥ 10.
assert.equal(num(10000000.34), '10M');
assert.equal(num(1309863.4), '1.3M');

// Non-compact: group thousands and drop decimals for big numbers (no
// 10000000.34); keep precision only for small (<1000) values.
const full = makeValueFormatter({ kind: 'number', compact: false });
assert.equal(full(1500000), '1,500,000');
assert.equal(full(10000000.34), '10,000,000');
assert.equal(full(1000), '1,000');
assert.equal(full(1234.5), '1,235');
assert.equal(full(999), '999');
assert.equal(full(999.5), '999.5');

// Big currency/percent reuse the same grouping/abbreviation rules.
const usdFull = makeValueFormatter({ kind: 'currency', compact: false, symbol: '$' });
assert.equal(usdFull(10000000.34), '$10,000,000');
assert.equal(usd(10000000.34), '$10M');

// Percent scale: 0–1 fractions are multiplied ×100 before formatting so a stored
// 0.34 reads as 34%, while an unscaled value formats as-is.
const pctScaled = makeValueFormatter({ kind: 'percent', compact: false, scale: 100 });
assert.equal(pctScaled(0.34), '34%');
assert.equal(pctScaled(0.125), '12.5%');
assert.equal(pctScaled(1), '100%');
// scale defaults to 1 (no multiplication) when absent or invalid.
assert.equal(pct(34), '34%');
const pctScale1 = makeValueFormatter({ kind: 'percent', compact: false, scale: 1 });
assert.equal(pctScale1(34), '34%');
// scale also applies to plain numbers if ever provided.
const numScaled = makeValueFormatter({ kind: 'number', compact: true, scale: 100 });
assert.equal(numScaled(0.015), '1.5');

// ── Data labels: one shared unit per series ────────────────────────────────

// collectNumericValues unwraps every point shape ECharts accepts.
assert.deepEqual(
    collectNumericValues([1, '2', { value: 3 }, [10, 4], { value: [20, 5] }, null, '', 'x', { value: null }]),
    [1, 2, 3, 4, 5],
);

// All values in the thousands -> everything in K, one decimal below 100.
{
    const values = [90220, 73598, 61931, 49827];
    const f = makeLabelFormatter({ kind: 'number' }, values);
    assert.deepEqual(values.map(f), ['90.2K', '73.6K', '61.9K', '49.8K']);
}

// The shared unit is the SMALLEST magnitude, so labels stay comparable.
{
    const values = [1250000, 980000];
    const f = makeLabelFormatter({ kind: 'number' }, values);
    assert.deepEqual(values.map(f), ['1,250K', '980K']);
}
{
    const values = [2500000, 1200000, 4000000];
    const f = makeLabelFormatter({ kind: 'number' }, values);
    assert.deepEqual(values.map(f), ['2.5M', '1.2M', '4M']);
}

// Mixed magnitudes -> full numbers with thousands separators, never "4.3K" next to "950".
{
    const values = [4300, 950, 12.5, 1234567];
    const f = makeLabelFormatter({ kind: 'number' }, values);
    assert.deepEqual(values.map(f), ['4,300', '950', '12.5', '1,234,567']);
}

// Small fractions keep precision; integers never grow decimals.
{
    const f = makeLabelFormatter({ kind: 'number' }, [0.256, 7, 12.349]);
    assert.equal(f(0.256), '0.256');
    assert.equal(f(7), '7');
    assert.equal(f(12.349), '12.35');
}

// Server says "don't abbreviate" -> full numbers even when all are large.
{
    const values = [90220, 73598];
    const f = makeLabelFormatter({ kind: 'number', compact: false }, values);
    assert.deepEqual(values.map(f), ['90,220', '73,598']);
}

// Currency keeps its prefix, percent its suffix (and is never abbreviated).
assert.equal(makeLabelFormatter({ kind: 'currency', symbol: '$' }, [43900000, 36400000])(43900000), '$43.9M');
assert.equal(makeLabelFormatter({ kind: 'currency', symbol: '₪' }, [1200, 950])(1200), '₪1,200');
assert.equal(makeLabelFormatter({ kind: 'percent', scale: 100 }, [0.464, 0.385])(0.464), '46.4%');
assert.equal(makeLabelFormatter({ kind: 'percent' }, [46400, 38500])(46400), '46,400%');

// Negative values follow the same unit as their positive neighbours.
{
    const values = [-12000, 45000];
    const f = makeLabelFormatter({ kind: 'number' }, values);
    assert.deepEqual(values.map(f), ['-12K', '45K']);
}

// Empty / non-numeric input is harmless.
assert.equal(makeLabelFormatter({}, [])(1500), '1,500');
assert.equal(makeLabelFormatter({}, [1500])(null), '');
assert.equal(makeLabelFormatter({}, [1500])('n/a'), 'n/a');

console.log('value_format JS tests passed');
