# Chart feature

Browser-side charting for query results and ML answers. Vanilla ES modules, no
build step. ECharts is vendored under `src/static/vendor/echarts`, so air-gapped
deployments need no CDN.

## How a chart is made

1. **Generate (server).** `POST /api/generate-chart` builds the chart spec and
   config on the server (`src/api/chart_builder.py`: chart type, value formats,
   ML band charts). The result is persisted as the turn's chart baseline.
2. **Render (client).** `chartManager.js` is the orchestrator. It analyses the
   rows (`utils/dataAnalyzer.js`), renders with `components/ChartContainer.js`,
   and applies value formatting (`utils/valueFormat.js`).
3. **Refine.** Users change the chart in three ways, none of which can alter the
   data:
   - **Quick options** (`ChartOptionsPanel`, `utils/chartQuickOptions.js`):
     column mapping, data labels, legend, zoom, sort, named palettes
     (`utils/chartPalettes.js`). No network call.
   - **Chart chat** (`components/ChartChat.js`): "Refine this chart" in plain
     language. `utils/chartIntentMatcher.js` resolves common requests (also in
     Hebrew) in code; anything else calls `POST /api/edit-chart` once with a
     compact, data-free manifest and gets back a list of validated operations
     (`utils/chartEditOperations.js`), applied to a cloned session and published
     only after a successful render.
   - **AI enhance** (`EnhanceButton`, `chartEnhancerPrompt.js`):
     `POST /api/enhance-chart` restyles the config; failures fall back to the
     generated chart.
4. **Derived series** are computed locally from the real rows: moving averages,
   trend lines and cumulative sums (`utils/chartOperators.js`), and what-if
   scenarios and annotations (`utils/chartScenarios.js`). The real series is
   never modified.
5. **Session state** (`utils/chartSession.js`) keeps the immutable baseline next
   to the working state, so switching turns keeps edits and **Reset** is exact.

## Maps

- Built-in ECharts maps (`assets/maps/`, `utils/mapAssets.js`) are served from
  `/static`; see `assets/maps/NOTICE.md` for data sources and licenses.
- Optional raster OpenStreetMap view (`utils/osmMapRenderer.js`) through a
  same-origin tile proxy (`/api/map-tiles/...`), so provider keys never reach
  the browser. Controls live in `components/MapOptionsPanel.js`. Enable and
  configure it with the `OSM_*` variables in `deployment/configuration.md`.
- Location search uses `/api/map-search`.

## Layout

```
chart-feature/
├── chartManager.js           orchestrator
├── chartTypes.js             canonical chart type list
├── components/               ChartContainer, ChartToggle, ChartTypeSelector,
│                             ChartOptionsPanel, MapOptionsPanel, ChartChat, EnhanceButton
├── services/                 chartConfigGenerator, chartEnhancerService
├── prompts/                  chartEnhancerPrompt.js
├── utils/                    analysis, formatting, palettes, operators, scenarios,
│                             suggestions, intent matcher, edit operations, session,
│                             series labels (ML), map renderer and assets
├── types/chart.types.js      JSDoc types
└── assets/                   map data and notices
```

## Languages

Interface strings come from the locale catalogs (`src/i18n/messages`), and the
ML band-chart series names are translated at render time
(`utils/seriesLabels.js`), so saved and restored charts follow the current
interface language.

## Prompts

The server-side editor prompts are `chart_editor` and `chart_map_editor` in
Settings → Prompts (see [`PROMPTS.md`](../../../PROMPTS.md)).

## Tests

Frontend unit tests are in `tests/js/` (for example `test_chart_chat.mjs`), the
server logic in `tests/unit/test_chart_*.py`, and the UI flows in
`tests/e2e/specs/`. See [`tests/README.md`](../../../tests/README.md).
