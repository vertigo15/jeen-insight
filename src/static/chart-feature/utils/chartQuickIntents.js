/**
 * Deterministic parser for the small chart-edit vocabulary already exposed by
 * the toolbar. A fully matched instruction can be applied in the browser;
 * anything ambiguous falls through to the LLM chart editor.
 */

const TYPO_REPLACEMENTS = new Map([
    ['lable', 'label'],
    ['lables', 'labels'],
    ['lebels', 'labels'],
    ['colore', 'color'],
    ['colour', 'color'],
    ['colours', 'colors'],
]);

const CUSTOM_COLOURS = Object.freeze({
    red: ['#d4574a', '#ef8b7f', '#a83e34', '#f4b8b1', '#c94b41', '#7d2d27', '#f9d7d3', '#e36b60'],
    black: ['#222222', '#444444', '#111111', '#666666', '#333333', '#000000', '#888888', '#555555'],
    gray: ['#6b7280', '#9ca3af', '#4b5563', '#d1d5db', '#7c8594', '#374151', '#e5e7eb', '#5f6978'],
    grey: ['#6b7280', '#9ca3af', '#4b5563', '#d1d5db', '#7c8594', '#374151', '#e5e7eb', '#5f6978'],
    orange: ['#e8703a', '#f2a35e', '#c9512a', '#f7c98a', '#d9863f', '#8f3a1e', '#fbe0bd', '#e0894a'],
});

const NAMED_PALETTES = new Set(['jeen', 'purple', 'blue', 'green', 'teal', 'warm', 'classic']);

function normalize(value) {
    return String(value || '')
        .toLowerCase()
        .replace(/\b(?:don['’]?t|dont|do\s+not)\b/g, 'not')
        .replace(/[“”"'()]/g, ' ')
        .replace(/[_-]+/g, ' ')
        .replace(/[^\p{L}\p{N}#%+\s,;&]/gu, ' ')
        .split(/\s+/)
        .filter(Boolean)
        .map((word) => TYPO_REPLACEMENTS.get(word) || word)
        .join(' ')
        .trim();
}

function splitClauses(text) {
    return text
        .split(/\s+(?:and|then)\s+|[,;&+]+/g)
        .map((part) => part.trim())
        .filter(Boolean);
}

function singular(word) {
    return word.length > 3 && word.endsWith('s') ? word.slice(0, -1) : word;
}

function columnBindingIntent(clause, columns) {
    if (!/\b(?:x axis|xaxis|category axis|horizontal axis)\b/.test(clause)) return null;
    const clauseTokens = new Set(clause.split(/\s+/).map(singular));
    const candidates = (columns || []).map((column) => {
        const name = typeof column === 'string' ? column : column?.name;
        const tokens = normalize(name).split(/\s+/).filter(Boolean).map(singular);
        return { name, tokens };
    }).filter((candidate) => candidate.name && candidate.tokens.length);
    const matches = candidates.filter((candidate) =>
        candidate.tokens.every((token) => clauseTokens.has(token))
    ).sort((left, right) => right.tokens.length - left.tokens.length);
    if (!matches.length) return null;
    if (matches.length > 1 && matches[0].tokens.length === matches[1].tokens.length) return null;
    return { type: 'binding', field: 'xColumn', value: matches[0].name };
}

function booleanIntent(clause, noun) {
    if (!new RegExp(`\\b${noun}s?\\b`).test(clause)) return null;
    if (/\bnot\b/.test(clause) && /\b(?:hide|remove|disable|off|without)\b/.test(clause)) return null;
    const off = /\b(?:no|not|hide|remove|disable|off|without)\b/.test(clause);
    return !off;
}

function colorIntent(clause) {
    if (/\b(?:not|series|negative|positive|above|below|only|except|when)\b/.test(clause)) return null;
    const names = [...NAMED_PALETTES, ...Object.keys(CUSTOM_COLOURS)];
    const name = names.find((candidate) => new RegExp(`\\b${candidate}\\b`).test(clause));
    if (!name) return null;
    const hasColourWord = /\bcolors?\b/.test(clause);
    const hasColourVerb = /\b(?:make|set|change|use|paint|switch)\b/.test(clause);
    if (!hasColourWord && !hasColourVerb) return null;
    if (CUSTOM_COLOURS[name]) {
        return { type: 'customPalette', name: name === 'grey' ? 'gray' : name, colors: CUSTOM_COLOURS[name].slice() };
    }
    return { type: 'palette', id: name };
}

function parseClause(clause, columns) {
    if (/\bnot\b/.test(clause) && /\b(?:x axis|xaxis|category axis|horizontal axis)\b/.test(clause)) return null;
    if (/\blabels?\b/.test(clause) && /\b(?:red|black|gray|grey|orange|purple|blue|green|teal|warm|classic|jeen)\b/.test(clause)) return null;
    const binding = columnBindingIntent(clause, columns);
    if (binding) return binding;
    if (/\blabels?\b/.test(clause) && /\b(?:only|top|except|every|specific|table|axis|tick)\b/.test(clause)) return null;
    const labels = booleanIntent(clause, 'label');
    if (labels !== null) return { type: 'toggle', key: 'dataLabels', value: labels };
    const legend = booleanIntent(clause, 'legend');
    if (legend !== null) return { type: 'toggle', key: 'legend', value: legend };
    const zoom = booleanIntent(clause, 'zoom');
    if (zoom !== null) return { type: 'toggle', key: 'dataZoom', value: zoom };
    if (/\bsort\b/.test(clause)) {
        const descending = !/\b(?:not|original|reset|ascending|lowest to highest|off)\b/.test(clause);
        return { type: 'toggle', key: 'sortDesc', value: descending };
    }
    return colorIntent(clause);
}

export function parseChartQuickIntent(instruction, { columns = [] } = {}) {
    const text = normalize(instruction);
    if (!text) return null;
    const clauses = splitClauses(text);
    const commands = clauses.map((clause) => parseClause(clause, columns));
    if (!commands.length || commands.some((command) => !command)) return null;
    return { commands };
}

export const __test = { normalize, splitClauses, columnBindingIntent };
