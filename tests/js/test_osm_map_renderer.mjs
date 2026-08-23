/**
 * Tests for pure OpenStreetMap renderer helpers.
 * Run with: node tests/js/test_osm_map_renderer.mjs
 */
import assert from 'node:assert/strict';
import {
    colorForValue,
    fitMapView,
    projectMercator,
    radiusForValue,
    unprojectMercator,
} from '../../src/static/chart-feature/utils/osmMapRenderer.js';

const point = projectMercator(32.0853, 34.7818, 8);
const roundTrip = unprojectMercator(point.x, point.y, 8);
assert.ok(Math.abs(roundTrip.lat - 32.0853) < 0.00001);
assert.ok(Math.abs(roundTrip.lng - 34.7818) < 0.00001);

const single = fitMapView(
    { minLat: 32.0853, maxLat: 32.0853, minLng: 34.7818, maxLng: 34.7818 },
    900,
    520,
);
assert.equal(single.zoom, 7);

const world = fitMapView(
    { minLat: -45, maxLat: 60, minLng: -100, maxLng: 120 },
    900,
    520,
);
assert.ok(world.zoom >= 1 && world.zoom <= 4);

assert.equal(radiusForValue(5, 5, 5), 16);
assert.ok(radiusForValue(10, 0, 10) > radiusForValue(0, 0, 10));
assert.match(colorForValue(5, 0, 10), /^rgb\(/);

console.log('osm map renderer JS tests passed');
