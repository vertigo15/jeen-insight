/**
 * Deterministic accessibility checks for chart option controls.
 * Run with: node tests/js/test_chart_controls_accessibility.mjs
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

globalThis.window = {};

const { ChartOptionsPanel, syncToggleButton } = await import(
    '../../src/static/chart-feature/components/ChartOptionsPanel.js'
);
const { MapOptionsPanel } = await import(
    '../../src/static/chart-feature/components/MapOptionsPanel.js'
);

class FakeClassList {
    constructor() { this.values = new Set(); }
    contains(name) { return this.values.has(name); }
    toggle(name, force) {
        if (force) this.values.add(name);
        else this.values.delete(name);
    }
}

class FakeButton {
    constructor() {
        this.classList = new FakeClassList();
        this.attributes = new Map();
        this.dataset = {};
        this.disabled = false;
        this.title = '';
    }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
}

const button = new FakeButton();
syncToggleButton(button, false);
assert.equal(button.getAttribute('aria-pressed'), 'false');
assert.equal(button.getAttribute('aria-disabled'), 'false');
assert.equal(button.classList.contains('is-on'), false);

syncToggleButton(button, true);
assert.equal(button.getAttribute('aria-pressed'), 'true');
assert.equal(button.classList.contains('is-on'), true);

syncToggleButton(button, 'mixed');
assert.equal(button.getAttribute('aria-pressed'), 'mixed');
assert.equal(button.classList.contains('is-on'), false);
assert.equal(button.classList.contains('is-mixed'), true);

syncToggleButton(button, false, {
    disabled: true,
    describedBy: 'analysis-note',
    title: 'Unavailable for this analysis',
});
assert.equal(button.disabled, true);
assert.equal(button.getAttribute('aria-disabled'), 'true');
assert.equal(button.getAttribute('aria-describedby'), 'analysis-note');

// Every panel state change goes back through the same painter.
const panelButton = new FakeButton();
panelButton.dataset.key = 'legend';
const container = {
    querySelectorAll(selector) {
        return selector === '.chart-opt-toggle[data-key]' ? [panelButton] : [];
    },
};
globalThis.document = {
    getElementById(id) { return id === 'chart-options-test' ? container : null; },
};
let toggleEvents = 0;
const panel = new ChartOptionsPanel('chart-options-test', {
    onQuickToggle() { toggleEvents += 1; },
});
panel._onToggle('legend');
assert.equal(panelButton.getAttribute('aria-pressed'), 'false');
assert.equal(panelButton.classList.contains('is-on'), false);
assert.equal(toggleEvents, 1);

const mapButton = new FakeButton();
const mapPanel = new MapOptionsPanel('map-options-test');
mapPanel._syncToggleButton(mapButton, true);
assert.equal(mapButton.getAttribute('aria-pressed'), 'true');
assert.equal(mapButton.classList.contains('is-on'), true);
mapPanel._syncToggleButton(mapButton, 'mixed');
assert.equal(mapButton.getAttribute('aria-pressed'), 'mixed');
assert.equal(mapButton.classList.contains('is-mixed'), true);

// Native <button> controls provide Enter/Space activation; focus-visible CSS
// supplies the keyboard focus indicator without changing pointer styling.
const here = path.dirname(fileURLToPath(import.meta.url));
const optionsSource = fs.readFileSync(
    path.join(here, '../../src/static/chart-feature/components/ChartOptionsPanel.js'),
    'utf8'
);
const mapSource = fs.readFileSync(
    path.join(here, '../../src/static/chart-feature/components/MapOptionsPanel.js'),
    'utf8'
);
const chartCss = fs.readFileSync(path.join(here, '../../src/static/style.css'), 'utf8');
const workspaceCss = fs.readFileSync(
    path.join(here, '../../src/static/workspace/workspace.css'),
    'utf8'
);
assert.match(optionsSource, /<button type="button" class="chart-opt-toggle"/);
assert.match(optionsSource, /class="chart-opts-toggles"[^>]*role="group"/);
assert.match(mapSource, /class="map-options-section" role="group"/);
assert.match(mapSource, /data-map-toggle="labels" aria-pressed="false"/);
assert.match(chartCss, /\.chart-opt-toggle:focus-visible/);
assert.match(chartCss, /\[dir="rtl"\] \.chart-refine-apply svg/);
assert.doesNotMatch(workspaceCss, /\.v3-chart-edit button\s*\{/);
assert.match(workspaceCss, /\.v3-chart-edit \.chart-refine-reset\s*\{[\s\S]*?background:\s*transparent/);

console.log('chart control accessibility JS tests passed');
