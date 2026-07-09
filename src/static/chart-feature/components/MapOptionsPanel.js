/**
 * Dedicated controls for ECharts map views.
 *
 * These controls mutate view/style state on the live chart instance and avoid
 * the normal chart options pipeline, which would reset user pan/zoom state.
 */

export const MAP_PALETTES = {
    blue: ['#f7fbff', '#c6dbef', '#6baed6', '#2171b5', '#08306b'],
    green: ['#f7fcf5', '#c7e9c0', '#74c476', '#238b45', '#00441b'],
    purple: ['#fcfbfd', '#dadaeb', '#9e9ac8', '#6a51a3', '#3f007d'],
    orange: ['#fff7ec', '#fdd49e', '#fc8d59', '#d94801', '#7f2704'],
};

export class MapOptionsPanel {
    constructor(containerId, hooks = {}) {
        this.containerId = containerId;
        this.hooks = hooks;
        this.state = {
            labels: false,
            roam: true,
            noData: true,
            palette: 'blue',
        };
        this._mounted = false;
    }

    render() {
        const el = document.getElementById(this.containerId);
        if (!el) return;
        el.classList.add('map-options-panel-container');
        el.innerHTML = `
            <div class="map-options-panel">
                <div class="map-options-section">
                    <span class="chart-options-heading">Map controls</span>
                    <div class="map-options-row">
                        <button type="button" class="chart-opt-toggle" data-map-action="reset">Reset view</button>
                        <button type="button" class="chart-opt-toggle" data-map-action="fit">Fit</button>
                        <button type="button" class="chart-opt-toggle" data-map-action="zoom-in">Zoom +</button>
                        <button type="button" class="chart-opt-toggle" data-map-action="zoom-out">Zoom -</button>
                        <button type="button" class="chart-opt-toggle" data-map-toggle="labels">Labels</button>
                        <button type="button" class="chart-opt-toggle is-on" data-map-toggle="roam">Pan/zoom</button>
                        <button type="button" class="chart-opt-toggle is-on" data-map-toggle="noData">No-data areas</button>
                    </div>
                </div>
                <div class="map-options-section">
                    <label class="chart-options-field map-palette-field">
                        <span>Palette</span>
                        <select class="chart-options-select" id="map-opt-palette">
                            ${Object.keys(MAP_PALETTES).map((name) =>
                                `<option value="${name}">${name}</option>`
                            ).join('')}
                        </select>
                    </label>
                </div>
            </div>
        `;
        el.querySelectorAll('[data-map-action]').forEach((btn) => {
            btn.addEventListener('click', () => this._emit(btn.dataset.mapAction));
        });
        el.querySelectorAll('[data-map-toggle]').forEach((btn) => {
            btn.addEventListener('click', () => this._toggle(btn.dataset.mapToggle, btn));
        });
        el.querySelector('#map-opt-palette')?.addEventListener('change', (event) => {
            this.state.palette = event.target.value;
            this._emit('palette', this.state.palette);
        });
        this._mounted = true;
        this.hide();
    }

    syncFromConfig(config) {
        const meta = config?.jeenMap || {};
        this.state.labels = !!meta.showLabels;
        this.state.roam = mapTargets(config).some((target) => target.roam !== false);
        this.state.palette = meta.palette || this.state.palette || 'blue';
        this.state.noData = true;
        if (this._mounted) this._syncButtons();
    }

    show() {
        const el = document.getElementById(this.containerId);
        if (el) el.style.display = 'block';
    }

    hide() {
        const el = document.getElementById(this.containerId);
        if (el) el.style.display = 'none';
    }

    _toggle(key, btn) {
        if (!(key in this.state)) return;
        this.state[key] = !this.state[key];
        btn.classList.toggle('is-on', !!this.state[key]);
        this._emit(key, this.state[key]);
    }

    _emit(action, value = undefined) {
        if (typeof this.hooks.onMapControl === 'function') {
            this.hooks.onMapControl(action, value);
        }
    }

    _syncButtons() {
        const el = document.getElementById(this.containerId);
        if (!el) return;
        el.querySelectorAll('[data-map-toggle]').forEach((btn) => {
            const key = btn.dataset.mapToggle;
            btn.classList.toggle('is-on', !!this.state[key]);
        });
        const palette = el.querySelector('#map-opt-palette');
        if (palette) palette.value = this.state.palette;
    }
}

function mapTargets(config) {
    const targets = [];
    if (Array.isArray(config?.series)) {
        config.series.forEach((series) => {
            if (series?.type === 'map') targets.push(series);
        });
    }
    if (config?.geo && typeof config.geo === 'object' && !Array.isArray(config.geo)) {
        targets.push(config.geo);
    }
    return targets;
}
