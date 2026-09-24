/**
 * Tests for the ML band-chart series-label localizer.
 * Run with:  node tests/js/test_series_labels.mjs
 */
import assert from 'node:assert/strict';
import { localizeSeriesLabels } from '../../src/static/chart-feature/utils/seriesLabels.js';

const HE = {
    'charts.series.actual': 'בפועל',
    'charts.series.expected': 'צפוי',
    'charts.series.forecast': 'תחזית',
    'charts.series.flagged': 'חריגה',
    'charts.series.segmentLevel': 'רמת מקטע',
    'charts.series.changepoint': 'נקודת שינוי',
    'charts.series.lower': 'גבול תחתון',
    'charts.series.upper': 'גבול עליון',
    'charts.series.band': 'טווח צפוי {percent}%',
    'charts.series.interval': 'טווח חיזוי {percent}%',
    'charts.series.forecastStart': 'תחזית',
};
const he = (key, args) => (key in HE ? HE[key].replace('{percent}', args?.percent ?? '') : null);
const jsonClone = (o) => JSON.parse(JSON.stringify(o));
const names = (option) => option.series.map((s) => s.name);

// The shape chart_builder._band_option emits for an anomaly run.
function anomalyOption() {
    return {
        legend: { type: 'scroll', data: ['95% band', 'Expected', 'Actual', 'Flagged'] },
        series: [
            { name: '95% band (base)', jeenRole: 'interval_base' },
            { name: '95% band', jeenRole: 'interval' },
            { name: 'Lower', jeenRole: 'interval_bound' },
            { name: 'Upper', jeenRole: 'interval_bound' },
            { name: 'Expected', jeenRole: 'expected' },
            { name: 'Actual', jeenRole: 'actual' },
            { name: 'Flagged', jeenRole: 'flagged' },
        ],
    };
}

// ── series names and the legend are translated together ─────────────────────
{
    const out = localizeSeriesLabels(anomalyOption(), he);
    assert.deepEqual(names(out), [
        '95% band (base)', 'טווח צפוי 95%', 'גבול תחתון', 'גבול עליון', 'צפוי', 'בפועל', 'חריגה',
    ]);
    assert.deepEqual(out.legend.data, ['טווח צפוי 95%', 'צפוי', 'בפועל', 'חריגה']);
    assert.equal(out.series[5].jeenName, 'Actual', 'the server name is kept for re-renders');
}

// ── series hidden through the legend stay hidden under their new names ──────
{
    const option = anomalyOption();
    option.legend.selected = { Actual: false, '95% band': true };
    const out = localizeSeriesLabels(option, he);
    assert.deepEqual(out.legend.selected, { 'בפועל': false, 'טווח צפוי 95%': true });
}

// ── idempotent, including after the JSON clones of the render pipeline ──────
{
    const once = localizeSeriesLabels(anomalyOption(), he);
    const twice = localizeSeriesLabels(jsonClone(once), he);
    assert.deepEqual(names(twice), names(once));
    assert.deepEqual(twice.legend.data, once.legend.data);
}

// ── forecast: interval wording and the forecast-start marker ─────────────────
{
    const out = localizeSeriesLabels({
        legend: { data: ['80% interval', 'Actual', 'Forecast'] },
        series: [
            { name: '80% interval', jeenRole: 'interval' },
            { name: 'Actual', jeenRole: 'actual' },
            { name: 'Forecast', jeenRole: 'forecast', markLine: { label: { formatter: 'forecast', position: 'insideEndTop' } } },
        ],
    }, he);
    assert.deepEqual(out.legend.data, ['טווח חיזוי 80%', 'בפועל', 'תחזית']);
    assert.equal(out.series[2].markLine.label.formatter, 'תחזית');
    assert.equal(out.series[2].markLine.label.position, 'insideEndTop');
    const again = localizeSeriesLabels(jsonClone(out), he);
    assert.equal(again.series[2].markLine.label.formatter, 'תחזית');
}

// ── changepoint reuses roles with its own names: keyed by name, not role ─────
{
    const out = localizeSeriesLabels({
        legend: { data: ['Segment level', 'Actual', 'Changepoint'] },
        series: [
            { name: 'Segment level', jeenRole: 'expected' },
            { name: 'Actual', jeenRole: 'actual' },
            { name: 'Changepoint', jeenRole: 'flagged' },
        ],
    }, he);
    assert.deepEqual(out.legend.data, ['רמת מקטע', 'בפועל', 'נקודת שינוי']);
}

// ── untouched: series without a role, unknown names, missing catalog ─────────
{
    const plain = localizeSeriesLabels({ legend: { data: ['Actual'] }, series: [{ name: 'Actual' }] }, he);
    assert.deepEqual(names(plain), ['Actual'], 'user series without a server role keep their name');

    const custom = localizeSeriesLabels({ series: [{ name: 'SUM(salesamount)', jeenRole: 'expected' }] }, he);
    assert.deepEqual(names(custom), ['SUM(salesamount)']);

    const noCatalog = localizeSeriesLabels(anomalyOption(), () => null);
    assert.deepEqual(names(noCatalog), names(anomalyOption()));
    assert.deepEqual(noCatalog.legend.data, anomalyOption().legend.data);

    assert.equal(localizeSeriesLabels(null, he), null);
}

console.log('test_series_labels: all assertions passed');
