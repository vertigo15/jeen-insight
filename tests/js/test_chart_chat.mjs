/**
 * Regression checks for chart-chat state transitions.
 * Run with: node tests/js/test_chart_chat.mjs
 */
import assert from 'node:assert/strict';
import { ChartChat } from '../../src/static/chart-feature/components/ChartChat.js';

const chat = new ChartChat('unused', {});
chat.mounted = true;
chat._inputEl = { disabled: true };
chat._applyBtnEl = {
    disabled: true,
    classList: { toggle() {} },
    querySelector() { return { textContent: '' }; },
};
chat._entryEl = { hidden: true };
chat._appliedEl = { hidden: true };
chat._appliedLabelEl = { textContent: '' };

chat.enable();
assert.equal(chat._inputEl.disabled, false);
assert.equal(chat._applyBtnEl.disabled, false);

chat._showApplied('open map layers');
assert.equal(chat._entryEl.hidden, false);
assert.equal(chat._appliedEl.hidden, false);
assert.equal(chat._appliedLabelEl.textContent, 'Applied: open map layers');

console.log('chart chat JS tests passed');
