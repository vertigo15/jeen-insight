/**
 * Shared, idempotent ECharts loader.
 *
 * The vendored bundle (`/static/vendor/echarts/echarts.min.js`) is a classic
 * script that installs `window.echarts`. Several features may need it at the
 * same time (result charts, the admin Analytics page), so the in-flight
 * promise is a module-level singleton: the script tag is injected once and
 * every caller awaits the same load.
 *
 * @module vendor-loaders/echarts
 */

const ECHARTS_SRC = '/static/vendor/echarts/echarts.min.js';

let _loading = null;

/**
 * Resolve with the global `echarts` namespace, loading the bundle on first use.
 * @returns {Promise<object>}
 */
export function loadECharts() {
    if (typeof window !== 'undefined' && window.echarts) return Promise.resolve(window.echarts);
    if (_loading) return _loading;
    _loading = new Promise((resolve, reject) => {
        const existing = document.querySelector(`script[src="${ECHARTS_SRC}"]`);
        const script = existing || document.createElement('script');
        const done = () => {
            if (window.echarts) resolve(window.echarts);
            else { _loading = null; reject(new Error('ECharts loaded but window.echarts is missing')); }
        };
        const fail = () => { _loading = null; reject(new Error('Failed to load ECharts')); };
        script.addEventListener('load', done, { once: true });
        script.addEventListener('error', fail, { once: true });
        if (!existing) {
            script.src = ECHARTS_SRC;
            script.async = true;
            document.head.appendChild(script);
        }
    });
    return _loading;
}
