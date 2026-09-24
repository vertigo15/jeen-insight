/**
 * Regression checks for chart-chat state transitions.
 * Run with: node tests/js/test_chart_chat.mjs
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// The component reads its copy from the interface-language runtime; install it
// on a global `window` with the English catalog before importing the module so
// the assertions below check real English text rather than raw keys.
const here = path.dirname(fileURLToPath(import.meta.url));
const messages = JSON.parse(fs.readFileSync(path.join(here, '../../src/i18n/messages/en.json'), 'utf8'));
const sandbox = { window: {} };
sandbox.window.__I18N_BOOTSTRAP__ = { locale: 'en', dir: 'ltr', formatLocale: 'en-US', messages };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(here, '../../src/static/vendor/intl-messageformat/intl-messageformat.iife.js'), 'utf8'), sandbox);
sandbox.window.IntlMessageFormat = sandbox.IntlMessageFormat;
vm.runInContext(fs.readFileSync(path.join(here, '../../src/static/i18n/i18n.js'), 'utf8'), sandbox);
globalThis.window = { I18n: sandbox.window.I18n };

const { ChartChat } = await import('../../src/static/chart-feature/components/ChartChat.js');

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
assert.equal(chat._appliedLabelEl.textContent, 'open map layers');

console.log('chart chat JS tests passed');
