/**
 * Code-only intent matcher for the chart mini chat.
 *
 * Resolves the most common refinement requests ("pie chart", "hide legend",
 * "sort high to low", "pastel colors", Hebrew equivalents) into the same
 * validated operation list the LLM contract produces, without a model call.
 * The matcher is deliberately strict: every word of the instruction must be
 * consumed by a known phrase or a filler word, otherwise it returns null and
 * the caller falls back to the LLM. It never guesses.
 *
 * No DOM access: shared by the browser and the node tests.
 * @module chartIntentMatcher
 */

import { CHART_PALETTES } from './chartPalettes.js?v=1';

const STRUCTURAL_OPS = new Set([
    'set_chart_type', 'set_sort', 'set_stack', 'set_palette', 'scenario_clear',
]);

const COLOR_WORDS = {
    red: '#dc2626', green: '#16a34a', blue: '#2563eb', orange: '#ea580c',
    purple: '#7c3aed', violet: '#7c3aed', yellow: '#eab308', pink: '#db2777',
    black: '#111111', white: '#ffffff', gray: '#6b7280', grey: '#6b7280',
    teal: '#0d9488', navy: '#1e3a8a', brown: '#92400e', cyan: '#0891b2',
    gold: '#ca8a04', lime: '#65a30d', magenta: '#c026d3', turquoise: '#14b8a6',
    'אדום': '#dc2626', 'ירוק': '#16a34a', 'כחול': '#2563eb', 'כתום': '#ea580c',
    'סגול': '#7c3aed', 'צהוב': '#eab308', 'ורוד': '#db2777', 'שחור': '#111111',
    'לבן': '#ffffff', 'אפור': '#6b7280', 'טורקיז': '#14b8a6', 'חום': '#92400e',
};

const GENERIC_PALETTES = {
    pastel: ['#a5d8ff', '#b2f2bb', '#ffd8a8', '#ffc9c9', '#d0bfff', '#fff3bf', '#c3fae8', '#eebefa'],
    monochrome: ['#111827', '#374151', '#6b7280', '#9ca3af', '#d1d5db', '#e5e7eb'],
    grayscale: ['#111827', '#374151', '#6b7280', '#9ca3af', '#d1d5db', '#e5e7eb'],
    greyscale: ['#111827', '#374151', '#6b7280', '#9ca3af', '#d1d5db', '#e5e7eb'],
    vivid: ['#ef4444', '#f97316', '#eab308', '#22c55e', '#06b6d4', '#3b82f6', '#8b5cf6', '#ec4899'],
    rainbow: ['#ef4444', '#f97316', '#eab308', '#22c55e', '#06b6d4', '#3b82f6', '#8b5cf6', '#ec4899'],
    earth: ['#78350f', '#a16207', '#4d7c0f', '#166534', '#0f766e', '#7c2d12', '#b45309', '#3f6212'],
    ocean: ['#0c4a6e', '#0369a1', '#0284c7', '#0ea5e9', '#38bdf8', '#7dd3fc', '#bae6fd', '#164e63'],
    'פסטל': ['#a5d8ff', '#b2f2bb', '#ffd8a8', '#ffc9c9', '#d0bfff', '#fff3bf', '#c3fae8', '#eebefa'],
};
// The app's named palettes share names with plain colours ("green", "blue"),
// so they only match with a palette qualifier ("green palette", "blue theme").
const NAMED_PALETTES = {};
for (const palette of CHART_PALETTES) {
    if (palette.colors) NAMED_PALETTES[palette.id] = palette.colors;
}
const PALETTE_QUALIFIER = '(?:colors|colours|palette|theme|tones|shades|scheme|ערכה|פלטה|גוונים)';

const CURRENCY_WORDS = [
    ['$', /(?:usd|dollars?|bucks|דולר(?:ים)?|\$)/],
    ['€', /(?:eur|euros?|יורו|€)/],
    ['£', /(?:gbp|pounds?|sterling|לירות?|פאונד|£)/],
    ['₪', /(?:ils|nis|shekels?|sheqels?|שקל(?:ים)?|ש"ח|₪)/],
    ['¥', /(?:jpy|yen|ין|¥)/],
    ['₹', /(?:inr|rupees?|רופי|₹)/],
];

// Words that may remain after all phrases are removed.
const FILLERS = new Set([
    'please', 'make', 'it', 'this', 'that', 'the', 'a', 'an', 'to', 'into', 'as', 'and',
    'with', 'in', 'of', 'chart', 'graph', 'plot', 'show', 'display', 'use', 'change',
    'switch', 'turn', 'convert', 'render', 'set', 'me', 'can', 'you', 'could', 'i',
    'want', 'would', 'like', 'instead', 'now', 'colors', 'colours', 'color', 'colour',
    'series', 'all', 'everything', 'them', 'style', 'theme', 'palette', 'scheme',
    'בבקשה', 'את', 'ה', 'ל', 'תראה', 'הראה', 'הצג', 'הפוך', 'שנה', 'תשנה', 'גרף', 'ו',
    'ב', 'עם', 'של', 'זה', 'זאת', 'אני', 'רוצה', 'אפשר', 'תעשה', 'עשה', 'לי', 'צבע', 'צבעים',
    'בצבע', 'בצבעי', 'בצבעים', 'תן', 'ערכת', 'סגנון', 'סדרה', 'הסדרות', 'כל', 'הכל',
]);

const TYPO_REPLACEMENTS = new Map([
    ['lable', 'label'], ['lables', 'labels'], ['lebels', 'labels'], ['labals', 'labels'],
    ['colore', 'color'], ['colour', 'color'], ['colours', 'colors'], ['collor', 'color'],
    ['legand', 'legend'], ['legende', 'legend'], ['chrt', 'chart'], ['grph', 'graph'],
    ['peichart', 'pie chart'], ['piechart', 'pie chart'], ['barchart', 'bar chart'],
    ['linechart', 'line chart'], ['dont', 'not'], ['donut', 'donut'], ['dougnut', 'donut'],
]);

function normalize(text) {
    const words = String(text || '')
        .toLowerCase()
        .replace(/\b(?:don['’]?t|do not|dont)\b/g, 'not')
        .replace(/[“”"'`.,!?;:()\[\]{}\-–—/\\]+/g, ' ')
        .split(/\s+/)
        .filter(Boolean)
        .map((word) => TYPO_REPLACEMENTS.get(word) || word);
    return ` ${words.join(' ')} `;
}

// Phrase helpers. Hebrew has no \b support, so phrases are bounded by the
// single spaces `normalize` guarantees, with optional attached prefixes.
const HE_PREFIX = '(?:ו?(?:ל|ב|ה|כ|ש|מ)?)';
function phrase(alternatives) {
    return new RegExp(`(?<= )(?:${alternatives.join('|')})(?= )`, 'u');
}
function he(word) { return `${HE_PREFIX}${word}`; }

const ON = '(?:show|display|add|enable|turn on|with|on|all|הצג|תראה|הראה|הוסף|הפעל|עם)';
// "not show" comes from normalising "don't show" / "do not show".
const OFF = '(?:not (?:show|display|add|enable|turn on|use|want)|hide|remove|disable|turn off|without|no|off|הסתר|הסר|בטל|כבה|בלי|ללא|אל תציג|אל תראה)';

/**
 * Each rule: regex over the normalized text, factory returning operations.
 * Order matters: longer/more specific phrases first so "stacked bar" is not
 * split into "stacked" + "bar".
 */
const RESET_AND_OVERLAY_RULES = [
    // ── Reset ────────────────────────────────────────────────────────────
    {
        re: phrase(['reset(?: the)?(?: chart)?', 'undo(?: all)?(?: changes)?', 'start over', 'revert',
            he('אפס'), he('איפוס'), 'בטל(?: את)? השינויים', 'חזור למקור']),
        ops: () => [{ op: '__reset' }],
    },
    // ── Overlay / scenario removal ───────────────────────────────────────
    {
        re: phrase([`${OFF} (?:the )?(?:trend(?: line)?|linear trend)`, `${OFF} (?:the )?(?:קו )?מגמה`]),
        ops: () => [{ op: 'remove_overlay', operator: 'linear_trend' }],
    },
    {
        re: phrase([`${OFF} (?:the )?moving average`, `${OFF} (?:the )?ממוצע נע`]),
        ops: () => [{ op: 'remove_overlay', operator: 'moving_avg' }],
    },
    {
        re: phrase([`${OFF} (?:the )?scenarios?`, `${OFF} (?:the )?what if`, `${OFF} (?:את )?${he('תרחיש(?:ים)?')}`]),
        ops: () => [{ op: 'scenario_clear' }],
    },
];

const CHART_TYPE_RULES = [
    // ── Chart types (specific first) ────────────────────────────────────
    {
        re: phrase(['stacked bars?(?: chart)?', 'stacked columns?', 'עמודות מוערמות', 'ברים מוערמים']),
        ops: () => [{ op: 'set_chart_type', chart_type: 'stacked_bar' }],
    },
    {
        re: phrase(['stacked areas?(?: chart)?', 'שטח מוערם']),
        ops: () => [{ op: 'set_chart_type', chart_type: 'stacked_area' }],
    },
    {
        re: phrase(['horizontal bars?(?: chart)?', 'horizontal columns?', 'bars? horizontal(?:ly)?', 'sideways bars?',
            'עמודות אופקיות', he('אופקי'), he('אופקיות')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'horizontal_bar' }],
    },
    {
        re: phrase(['donut(?: chart)?', 'doughnut(?: chart)?', 'ring chart', he('דונאט'), he('טבעת')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'donut' }],
    },
    {
        re: phrase(['pie(?: chart)?', he('עוגה'), he('פאי')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'pie' }],
    },
    {
        re: phrase(['scatter(?: plot| chart)?', 'dots?(?: chart)?', 'bubbles?(?: chart)?', he('פיזור'), he('נקודות')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'scatter' }],
    },
    {
        re: phrase(['area(?: chart)?', he('שטח')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'area' }],
    },
    {
        re: phrase(['lines?(?: chart| graph)?', he('קו'), he('קווי'), he('קווים')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'line' }],
    },
    {
        re: phrase(['bars?(?: chart)?', 'columns?(?: chart)?', 'vertical bars?', he('עמודות'), he('ברים')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'bar' }],
    },
    {
        re: phrase(['heat ?map', he('מפת חום')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'heatmap' }],
    },
    {
        re: phrase(['gauge(?: chart)?', he('מד'), he('שעון')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'gauge' }],
    },
    {
        re: phrase(['combo(?: chart)?', 'combined chart', 'bar and line', he('משולב')]),
        ops: () => [{ op: 'set_chart_type', chart_type: 'combo' }],
    },
    // ── Stacking ─────────────────────────────────────────────────────────
    {
        re: phrase(['unstack(?:ed)?(?: the)?(?: bars| series)?', 'not stacked', 'side by side', 'בטל(?: את)? הערימה', 'לא מוערם']),
        ops: () => [{ op: 'set_stack', stacked: false }],
    },
    {
        re: phrase(['stack(?:ed)?(?: the)?(?: bars| series)?', he('מוערם'), he('מוערמות'), 'ערום']),
        ops: () => [{ op: 'set_stack', stacked: true }],
    },
];

const VIEW_RULES = [
    // ── Toggles ──────────────────────────────────────────────────────────
    {
        re: phrase([`${OFF} (?:the )?legend`, `legend off`, `${OFF} (?:את )?${he('מקרא')}`]),
        ops: () => [{ op: 'set_toggle', key: 'legend', value: false }],
    },
    {
        re: phrase([`${ON} (?:the |a )?legend`, `legend on`, `${ON} (?:את )?${he('מקרא')}`]),
        ops: () => [{ op: 'set_toggle', key: 'legend', value: true }],
    },
    {
        re: phrase([`${OFF} (?:the )?(?:data )?(?:labels|values)(?: on (?:the )?(?:bars|points|chart))?`, `labels off`,
            `${OFF} (?:את )?${he('תוויות')}`, `${OFF} (?:את )?${he('ערכים')}`]),
        ops: () => [{ op: 'set_toggle', key: 'dataLabels', value: false }],
    },
    {
        re: phrase([`${ON} (?:the )?(?:data )?(?:labels|values|numbers)(?: (?:on|above|over) (?:the )?(?:top of (?:the |each )?)?(?:bars|points|columns|lines|slices|chart|top))?`, `labels on`,
            `(?:values|labels|numbers) (?:on|above|over) (?:the )?(?:top of (?:the |each )?)?(?:bars|points|columns|lines|slices|top)`,
            `${ON} (?:את )?${he('תוויות')}`, `${ON} (?:את )?${he('ערכים')}`]),
        ops: () => [{ op: 'set_toggle', key: 'dataLabels', value: true }],
    },
    {
        re: phrase([`${OFF} (?:the )?zoom(?: slider| bar)?`, `zoom off`, `${OFF} (?:את )?${he('זום')}`]),
        ops: () => [{ op: 'set_toggle', key: 'dataZoom', value: false }],
    },
    {
        re: phrase([`${ON} (?:a |the )?zoom(?: slider| bar)?`, `zoom on`, `zoom slider`, `${ON} ${he('זום')}`]),
        ops: () => [{ op: 'set_toggle', key: 'dataZoom', value: true }],
    },
    // ── Sort ─────────────────────────────────────────────────────────────
    {
        re: phrase(['(?:sort(?:ed)? )?(?:desc(?:ending)?|high(?:est)? to low(?:est)?|largest to smallest|biggest to smallest|big to small)',
            '(?:sort(?:ed)? )?(?:highest|biggest|largest|top) first', 'sort(?:ed)? down',
            '(?:סדר|מיין|סדרי|מסודר) מהגדול לקטן', 'מהגדול לקטן', 'בסדר יורד', he('יורד')]),
        ops: () => [{ op: 'set_sort', direction: 'desc' }],
    },
    {
        re: phrase(['(?:sort(?:ed)? )?(?:asc(?:ending)?|low(?:est)? to high(?:est)?|smallest to largest|small to big)',
            '(?:sort(?:ed)? )?(?:lowest|smallest) first', 'sort(?:ed)? up',
            '(?:סדר|מיין|סדרי|מסודר) מהקטן לגדול', 'מהקטן לגדול', 'בסדר עולה', he('עולה')]),
        ops: () => [{ op: 'set_sort', direction: 'asc' }],
    },
    {
        re: phrase(['(?:restore |keep |back to )?(?:the )?original order', 'unsort(?:ed)?', 'no sort(?:ing)?', 'reset (?:the )?(?:sort|order)',
            'סדר מקורי', 'בטל(?: את)? המיון', 'ללא מיון']),
        ops: () => [{ op: 'set_sort', direction: 'none' }],
    },
    // ── Format ───────────────────────────────────────────────────────────
    {
        re: phrase(['(?:as |in )?percent(?:ages?)?', '%', he('אחוזים'), he('אחוז')]),
        ops: () => [{ op: 'set_format', kind: 'percent', compact: true, symbol: '' }],
    },
    {
        re: phrase(['full numbers?', 'no abbreviations?', 'without abbreviations?', 'not abbreviated', 'without k and m', 'no k and m', 'exact numbers?',
            'מספרים מלאים', 'בלי קיצורים', 'ללא קיצורים']),
        ops: () => [{ op: 'set_format', kind: 'number', compact: false, symbol: '' }],
    },
    {
        re: phrase(['compact numbers?', 'abbreviated numbers?', 'abbreviate(?: the)?(?: numbers| values)?', 'k and m', 'מספרים מקוצרים']),
        ops: () => [{ op: 'set_format', kind: 'number', compact: true, symbol: '' }],
    },
    ...CURRENCY_WORDS.map(([symbol, words]) => ({
        re: phrase([`(?:as |in |format as |formatted as |currency )?${HE_PREFIX}${words.source}`]),
        ops: () => [{ op: 'set_format', kind: 'currency', compact: true, symbol }],
    })),
];

const COLOR_RULES = [
    // ── Palettes ─────────────────────────────────────────────────────────
    {
        re: phrase([
            ...Object.keys(GENERIC_PALETTES).map((name) => `${he(name)}(?: ${PALETTE_QUALIFIER})?`),
            ...Object.keys(NAMED_PALETTES).map((name) => `${he(name)} ${PALETTE_QUALIFIER}`),
            ...Object.keys(COLOR_WORDS).map((word) => `(?:shades of|tones of|gradient of|גווני|גוונים של) ${he(word)}`),
            ...Object.keys(COLOR_WORDS).map((word) => `${he(word)} (?:shades|tones|gradient|גוונים)`),
            ...Object.keys(COLOR_WORDS).filter((word) => /^[a-z]+$/.test(word)).map((word) => `${word}s`),
        ]),
        ops: (match) => {
            const words = match[0].trim().split(' ').map((word) => word.replace(/^[ובלהכשמ](?=.{3})/u, ''));
            const candidates = match[0].trim().split(' ').concat(words);
            const generic = candidates.find((word) => GENERIC_PALETTES[word]);
            if (generic) return [{ op: 'set_palette', colors: GENERIC_PALETTES[generic].slice() }];
            const named = candidates.find((word) => NAMED_PALETTES[word]);
            if (named) return [{ op: 'set_palette', colors: NAMED_PALETTES[named].slice() }];
            const colour = candidates.find((word) => COLOR_WORDS[word])
                || candidates.map((word) => word.replace(/s$/, '')).find((word) => COLOR_WORDS[word]);
            return colour ? [{ op: 'set_palette', colors: shades(COLOR_WORDS[colour]) }] : null;
        },
    },
    // ── Single colour → every series ─────────────────────────────────────
    {
        re: phrase(Object.keys(COLOR_WORDS).map((word) => `(?:in |as |all |everything )?${he(word)}`)),
        ops: (match) => {
            const word = match[0].trim().split(' ').pop().replace(/^[ובלהכשמ]/u, '');
            const hex = COLOR_WORDS[word] || COLOR_WORDS[match[0].trim().split(' ').pop()];
            return hex ? [{ op: 'set_color', target: 'all', color: hex }] : null;
        },
    },
];

// View rules run before chart types so "zoom bar" / "trend line" are not read
// as a bar or line chart; chart types run before stacking so "stacked bar"
// stays one phrase.
const RULES = [...RESET_AND_OVERLAY_RULES, ...VIEW_RULES, ...CHART_TYPE_RULES, ...COLOR_RULES];

/** Five tints of one colour, dark to light, for "shades of blue". */
function shades(hex) {
    const n = parseInt(hex.slice(1), 16);
    const r = (n >> 16) & 255; const g = (n >> 8) & 255; const b = n & 255;
    const mix = (channel, amount) => Math.round(channel + (255 - channel) * amount);
    const toHex = (value) => value.toString(16).padStart(2, '0');
    return [0, 0.2, 0.4, 0.6, 0.78].map((amount) => (
        `#${toHex(mix(r, amount))}${toHex(mix(g, amount))}${toHex(mix(b, amount))}`
    ));
}

function residueIsFiller(text) {
    return text.split(' ').filter(Boolean).every((word) => (
        FILLERS.has(word)
        // Hebrew prefixes (ל/ב/ה/ו/כ/ש/מ) attach to the word: "לגרף" is "גרף".
        || (/^[ובלהכשמ]/u.test(word) && FILLERS.has(word.slice(1)))
        || (/^ו[לבהכשמ]/u.test(word) && FILLERS.has(word.slice(2)))
    ));
}

/**
 * @param {string} instruction
 * @param {{ chartKind?: 'sql'|'ml_band'|'ml_basic' }} [options]
 * @returns {{ operations: object[], reset?: boolean } | null}
 *   `null` means "not confident, ask the model".
 */
export function matchChartIntent(instruction, { chartKind = 'sql' } = {}) {
    let text = normalize(instruction);
    if (text.trim().length < 2 || text.length > 200) return null;
    const operations = [];
    for (const rule of RULES) {
        let guard = 0;
        let match = rule.re.exec(text);
        while (match && guard < 4) {
            const ops = rule.ops(match);
            if (!ops) return null;
            operations.push(...ops);
            text = `${text.slice(0, match.index)} ${text.slice(match.index + match[0].length)}`.replace(/\s+/g, ' ');
            guard += 1;
            match = rule.re.exec(text);
        }
    }
    if (!operations.length || !residueIsFiller(text)) return null;
    if (operations.some((operation) => operation.op === '__reset')) {
        return operations.length === 1 ? { operations: [], reset: true } : null;
    }
    // Structural edits are analysis-controlled on ML charts; let the model
    // explain instead of silently applying half of the request.
    if (chartKind !== 'sql' && operations.some((operation) => STRUCTURAL_OPS.has(operation.op))) {
        return null;
    }
    // Two conflicting chart types or duplicate toggles mean the sentence was
    // more nuanced than the tables; defer to the model.
    const seen = new Map();
    for (const operation of operations) {
        const key = operation.op === 'set_toggle' ? `${operation.op}:${operation.key}` : operation.op;
        if (['set_chart_type', 'set_sort', 'set_stack', 'set_palette', 'set_format', 'set_color'].includes(operation.op) || operation.op === 'set_toggle') {
            if (seen.has(key)) return null;
            seen.set(key, true);
        }
    }
    return { operations };
}

export const __test__ = { normalize, RULES, GENERIC_PALETTES, COLOR_WORDS };
