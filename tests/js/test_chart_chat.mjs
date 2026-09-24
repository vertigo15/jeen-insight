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

class FakeClassList {
    constructor(owner) {
        this.owner = owner;
        this.values = new Set();
    }
    add(...names) { names.forEach((name) => this.values.add(name)); }
    contains(name) { return this.values.has(name); }
    toggle(name, force) {
        const next = force === undefined ? !this.values.has(name) : Boolean(force);
        if (next) this.values.add(name);
        else this.values.delete(name);
        return next;
    }
}

class FakeElement {
    constructor(tagName, ownerDocument) {
        this.tagName = tagName.toUpperCase();
        this.ownerDocument = ownerDocument;
        this.children = [];
        this.attributes = new Map();
        this.listeners = new Map();
        this.classList = new FakeClassList(this);
        this.dataset = {};
        this.hidden = false;
        this.disabled = false;
        this.readOnly = false;
        this.value = '';
        this.textContent = '';
        this._innerHTML = '';
    }
    set className(value) {
        this.classList.values = new Set(String(value).split(/\s+/).filter(Boolean));
    }
    get className() { return [...this.classList.values].join(' '); }
    set innerHTML(value) {
        this._innerHTML = String(value);
        this.children = [];
        if (this._innerHTML.includes('<span>')) {
            const span = new FakeElement('span', this.ownerDocument);
            this.appendChild(span);
        }
    }
    get innerHTML() { return this._innerHTML; }
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    addEventListener(type, handler) { this.listeners.set(type, handler); }
    dispatch(type, event = {}) { this.listeners.get(type)?.({ preventDefault() {}, ...event }); }
    querySelector(selector) {
        if (selector === 'span') return this.children.find((child) => child.tagName === 'SPAN') || null;
        return null;
    }
    cloneNode() {
        const clone = new FakeElement(this.tagName, this.ownerDocument);
        clone.className = this.className;
        clone.textContent = this.textContent;
        for (const [name, value] of this.attributes) clone.setAttribute(name, value);
        return clone;
    }
    focus() { this.ownerDocument.activeElement = this; }
}

class FakeDocument {
    constructor() {
        this.elements = new Map();
        this.activeElement = null;
    }
    createElement(tagName) { return new FakeElement(tagName, this); }
    getElementById(id) { return this.elements.get(id) || null; }
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

function mountChat(hooks = {}) {
    const document = new FakeDocument();
    const container = new FakeElement('div', document);
    document.elements.set('chart-chat-test', container);
    globalThis.document = document;
    const chat = new ChartChat('chart-chat-test', hooks);
    chat.mount();
    chat.enable();
    return { chat, container, document };
}

const applyRender = deferred();
const resetRender = deferred();
let fetchCalls = 0;
let lastPayload = null;
globalThis.fetch = async (_url, options = {}) => {
    fetchCalls += 1;
    lastPayload = options.body ? JSON.parse(options.body) : null;
    return {
        ok: true,
        status: 200,
        async json() {
            return {
                chart_config: { series: [{ type: 'bar', data: [1] }] },
                derived_series: [],
                notes: 'Rendered',
            };
        },
    };
};

const { chat, container, document } = mountChat({
    getCurrentConfig: () => ({ series: [] }),
    getCurrentResults: () => ({ columns: ['value'], rows: [[1]] }),
    getConnection: () => 'analytics',
    getCurrentDerivedSpecs: () => [{
        operator: 'moving_avg',
        source_column: 'value',
        params: { window: 2 },
    }],
    onApply: () => applyRender.promise,
    onReset: () => resetRender.promise,
});

// Blank Apply remains keyboard-focusable but cannot submit.
assert.equal(chat._applyBtnEl.disabled, false);
assert.equal(chat._applyBtnEl.getAttribute('aria-disabled'), 'true');
await chat._handleSend();
assert.equal(fetchCalls, 0);

chat._inputEl.value = 'הצג revenue by region';
chat._inputEl.dispatch('input');
assert.equal(chat._applyBtnEl.getAttribute('aria-disabled'), 'false');

chat._inputEl.focus();
const send = chat._handleSend();
await Promise.resolve();
assert.equal(fetchCalls, 1);
assert.equal(chat._inputEl.disabled, false);
assert.equal(chat._inputEl.readOnly, true);
assert.equal(chat._inputEl.getAttribute('aria-busy'), 'true');
assert.equal(document.activeElement, chat._inputEl);
chat.disable(); // ChartManager invalidates controls while its render is pending.
assert.equal(chat._inputEl.disabled, false);
assert.equal(chat._inputEl.readOnly, true);
assert.equal(document.activeElement, chat._inputEl);
chat.enable(); // Successful render makes the chart interactive again.
assert.equal(chat._statusEl.getAttribute('role'), 'status');
assert.equal(chat._statusEl.getAttribute('aria-live'), 'polite');
assert.equal(chat._statusEl.hidden, false);
assert.equal(chat._statusEl.textContent, 'Working on it…');

applyRender.resolve();
await send;
assert.equal(chat._inputEl.readOnly, false);
assert.equal(chat._inputEl.getAttribute('aria-busy'), 'false');
assert.equal(document.activeElement, chat._inputEl);
assert.equal(chat._statusEl.textContent, 'Chart updated. This edit is session-only and is not saved.');
assert.deepEqual(lastPayload.active_derived_series, [{
    operator: 'moving_avg',
    source_column: 'value',
    params: { window: 2 },
}]);

// Applied copy is a separate compact row, with localized copy outside BDI.
assert.equal(container.children[0], chat._rowEl);
assert.equal(container.children[1], chat._appliedEl);
assert.equal(chat._rowEl.children.includes(chat._appliedEl), false);
assert.equal(chat._appliedLabelEl.textContent, 'Applied:');
assert.equal(chat._appliedInstructionEl.tagName, 'BDI');
assert.equal(chat._appliedInstructionEl.getAttribute('dir'), 'auto');
assert.equal(chat._appliedInstructionEl.textContent, 'Rendered');
assert.equal(chat._resetBtnEl.getAttribute('aria-label'), 'Reset chart');
assert.deepEqual(chat.messages.map((message) => message.role), ['user', 'assistant']);

const reset = chat._handleReset();
await Promise.resolve();
assert.equal(chat._statusEl.textContent, 'Resetting the chart…');
assert.equal(chat._appliedEl.hidden, false);
assert.equal(chat._inputEl.readOnly, true);
resetRender.resolve();
await reset;
assert.equal(chat._appliedEl.hidden, true);
assert.equal(chat._entryEl.hidden, false);
assert.equal(document.activeElement, chat._inputEl);
assert.equal(chat._statusEl.textContent, 'Chart reset to its original state.');

// A rejected async render never announces success and restores input focus.
const renderError = new Error('renderer unavailable');
chat.hooks.onApply = async () => { throw renderError; };
chat._inputEl.value = 'use a line';
chat._inputEl.dispatch('input');
const originalConsoleError = console.error;
console.error = () => {};
await chat._handleSend();
console.error = originalConsoleError;
assert.equal(chat._statusEl.textContent, 'Got a config back but failed to render it. The chart was not changed.');
assert.equal(chat._statusEl.dataset.kind, 'error');
assert.equal(document.activeElement, chat._inputEl);

chat.hooks.getCurrentConfig = () => null;
chat._inputEl.value = 'needs a chart';
chat._inputEl.dispatch('input');
await chat._handleSend();
assert.equal(chat._statusEl.dataset.kind, 'warn');
assert.equal(chat._statusEl.textContent, 'Generate a chart first, then I can refine it.');
assert.equal(document.activeElement, chat._inputEl);

// Analysis mode keeps the same focus/busy contract and does not call chart edit.
let rerunInstruction = '';
const { chat: analysisChat, document: analysisDocument } = mountChat({
    onAnalysisRerun: async (instruction) => { rerunInstruction = instruction; },
});
analysisChat.setAnalysisMode(true);
analysisChat._inputEl.value = 'weekly instead of daily';
analysisChat._inputEl.dispatch('input');
await analysisChat._handleSend();
assert.equal(rerunInstruction, 'weekly instead of daily');
assert.equal(analysisChat._statusEl.textContent, 'The re-run finished and a new result was added to the conversation.');
assert.equal(analysisDocument.activeElement, analysisChat._inputEl);
assert.equal(fetchCalls, 2);

console.log('chart chat JS tests passed');
