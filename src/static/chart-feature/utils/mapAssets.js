/**
 * Lazy local map asset registration for ECharts.
 *
 * All assets are served from /static so air-gapped deployments never need a
 * CDN, tile server, or geocoding service.
 */

const MAP_ASSETS = {
    world: '/static/chart-feature/assets/maps/world.json?v=5',
    world_detailed: '/static/chart-feature/assets/maps/world_detailed.json?v=2',
    israel_districts: '/static/chart-feature/assets/maps/israel_districts.json?v=3',
};
const WORLD_MAPS = new Set(['world', 'world_detailed']);
const D3_ARRAY_URL = '/static/vendor/d3/d3-array.min.js?v=1';
const D3_GEO_URL = '/static/vendor/d3/d3-geo.min.js?v=1';

const registered = new Set();
const inFlight = new Map();
let projectionRuntimePromise = null;

function collectMapNames(option) {
    const names = new Set();
    const series = Array.isArray(option?.series) ? option.series : [];
    series.forEach((s) => {
        if (!s || typeof s !== 'object') return;
        if (s.type === 'map' && s.map) names.add(s.map);
    });
    if (option?.geo && typeof option.geo === 'object' && option.geo.map) {
        names.add(option.geo.map);
    }
    return [...names];
}

export function isMapOption(option) {
    return collectMapNames(option).length > 0;
}

export async function ensureMap(mapName) {
    if (!mapName || registered.has(mapName)) return;
    if (typeof echarts === 'undefined' || typeof echarts.registerMap !== 'function') {
        throw new Error('ECharts map support is not available');
    }
    const url = MAP_ASSETS[mapName];
    if (!url) {
        throw new Error(`Map asset not found: ${mapName}`);
    }
    if (!inFlight.has(mapName)) {
        inFlight.set(mapName, fetch(url, { cache: 'no-cache' })
            .then((resp) => {
                if (!resp.ok) throw new Error(`Map asset ${mapName} returned ${resp.status}`);
                return resp.json();
            })
            .then((geoJson) => {
                echarts.registerMap(mapName, { geoJSON: geoJson });
                registered.add(mapName);
            })
            .finally(() => {
                inFlight.delete(mapName);
            }));
    }
    await inFlight.get(mapName);
}

export async function ensureMapsForOption(option) {
    const names = collectMapNames(option);
    for (const name of names) {
        await ensureMap(name);
    }
    if (names.some((name) => WORLD_MAPS.has(name))) {
        await ensureProjectionRuntime();
        applyWorldProjection(option);
    }
}

function loadScript(src) {
    return new Promise((resolve, reject) => {
        if (document.querySelector(`script[data-jeen-src="${src}"]`)) {
            resolve();
            return;
        }
        const script = document.createElement('script');
        script.src = src;
        script.async = true;
        script.dataset.jeenSrc = src;
        script.onload = () => resolve();
        script.onerror = () => reject(new Error(`Failed to load ${src}`));
        document.head.appendChild(script);
    });
}

async function ensureProjectionRuntime() {
    if (typeof window === 'undefined') return;
    if (window.d3 && typeof window.d3.geoNaturalEarth1 === 'function') return;
    if (!projectionRuntimePromise) {
        projectionRuntimePromise = loadScript(D3_ARRAY_URL)
            .then(() => loadScript(D3_GEO_URL))
            .catch((err) => {
                console.warn('[mapAssets] d3-geo projection unavailable, using raw lon/lat map', err);
            });
    }
    await projectionRuntimePromise;
}

function projectionObject() {
    if (!window.d3 || typeof window.d3.geoNaturalEarth1 !== 'function') return null;
    const projection = window.d3.geoNaturalEarth1();
    return {
        project: (point) => projection(point),
        unproject: (point) => projection.invert(point),
    };
}

function applyWorldProjection(option) {
    const projection = projectionObject();
    if (!projection || !option || typeof option !== 'object') return;
    const apply = (target) => {
        if (!target || !WORLD_MAPS.has(target.map)) return;
        target.projection = projection;
    };
    if (Array.isArray(option.series)) {
        option.series.forEach((series) => {
            if (series?.type === 'map') apply(series);
        });
    }
    if (option.geo && typeof option.geo === 'object' && !Array.isArray(option.geo)) {
        apply(option.geo);
    }
}
