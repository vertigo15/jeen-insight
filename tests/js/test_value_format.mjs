/**
 * Tests for the client-side chart value formatter.
 * Run with:  node tests/js/test_value_format.mjs
 */
import assert from 'node:assert/strict';
import { makeValueFormatter } from '../../src/static/chart-feature/utils/valueFormat.js';

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

// Non-compact shows full numbers.
const full = makeValueFormatter({ kind: 'number', compact: false });
assert.equal(full(1500000), '1500000');
assert.equal(full(1234.5), '1234.5');

console.log('value_format JS tests passed (28 assertions)');
