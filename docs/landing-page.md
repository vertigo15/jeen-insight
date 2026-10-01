# Marketing Landing Page

`/landing` is the public, unauthenticated introduction to **Jeen Insights**. It
positions the product as the natural-language analytics workspace powered by
the **Jeen Data semantic layer**, demonstrates the real interface, and provides
separate paths for existing users and prospects.

The page is plain Flask-rendered HTML and CSS. It has no JavaScript, frontend
framework, external font request, or build step.

## Product positioning

Customer-facing copy uses **Jeen Data semantic layer** for the shared curated
catalog. In the implementation and operator interface, that catalog is managed
through **Schema Modeler** and delivered from the shared metadata database or a
configured catalog service.

Jeen Data supplies the per-connection tables, columns, relationships, business
terms, profiles, and knowledge pairs that ground an answer. Jeen Insights loads
that context before planning governed, read-only execution against a registered
source.

The page therefore follows five messages:

1. Ask a direct business question in plain language.
2. Continue with follow-up questions that retain the relevant result and
   conversation context.
3. Move into governed ML or statistical analysis when one query is not enough.
4. Ground every path in the curated Jeen Data catalog and execute through the
   configured governance and read-only controls.
5. Return the answer together with inspectable rows, SQL or Python, findings,
   and run evidence.

## Question spectrum and advanced analysis

The `#how` section uses three concrete questions to explain the product range:

- **Simple:** show sales by retailer for the current quarter.
- **Follow-up:** ask which products drove the change in the same conversation.
- **Decision support:** forecast demand by retailer to inform next-month
  inventory planning.

The final example is deliberately forecast-informed decision support, not an
inventory-optimization claim. Jeen Insights provides projections, prediction
intervals, trends, and model evidence; planning teams still apply lead times,
service levels, costs, and operational constraints.

The `#capabilities` section mirrors the skill registry in
`src/analysis/contracts.py`. It presents the 12 supported skills in three
customer-facing groups:

- **Predict and monitor:** forecast, anomaly detection, changepoint detection,
  and seasonality.
- **Explain and quantify:** contribution analysis, correlation, driver
  analysis, and regression.
- **Segment and evaluate:** clustering, classification, cohort retention, and
  A/B testing.

Business outcomes lead each group, with engine names such as AutoARIMA,
AutoETS, MSTL, K-means, HDBSCAN, gradient boosting, OLS, logistic regression,
and statistical tests as supporting detail. The copy distinguishes statistical
analysis from fitted ML, avoids causal claims, and explains that enabled skills
depend on deployment configuration and data suitability. The user reviews or
adjusts the proposed setup before execution.

## Route and access

The GET-only Flask route in `src/ui_app.py` renders `landing.html`. `/landing`
is present in `_PUBLIC_EXACT`, so it remains anonymous without weakening the
auth boundary around the product at `/`.

The landing page is intentionally English-only. Its document attributes are
`lang="en" dir="ltr"`, and the HTML response is pinned to
`Content-Language: en` even when an anonymous browser negotiates Hebrew. Other
UI routes continue to use the normal account/cookie/header locale resolution.

## Files

- `src/templates/landing.html` — semantic content, navigation, product proof,
  CTAs, FAQ, and screenshot markup.
- `src/static/landing/landing.css` — responsive page layout, screenshot framing,
  focus states, and Jeen-aligned presentation.
- `src/static/fonts/fonts.css` — self-hosted Urbanist and Noto Sans Hebrew font
  faces.
- `src/static/design-tokens.css` — shared product colors and typography aliases.
- `src/static/landing/insights-answer.png` — answered workspace hero.
- `src/static/landing/jeen-data-catalog.png` — catalog source and semantic-layer
  proof.
- `src/static/landing/inspectable-sql.png` — result rows, grounded filter, and
  generated SQL.
- `src/static/landing/advanced-analytics.png` — anomaly-analysis result.

The production screenshots are size-limited copies of the corresponding
`.preview/shots/` references. The hero is loaded eagerly; below-fold images use
native lazy loading and all images include intrinsic dimensions, useful alt
text, and captions.

## Page structure

1. Sticky brand header with persistent section navigation.
2. Product definition, dual CTAs, and a real answered-workspace screenshot.
3. Trust strip: Jeen Data grounding, read-only execution, inspectable evidence,
   and deployment control.
4. Three-step simple-question, contextual-follow-up, and decision-support flow.
5. `#jeen-data` explanation with the catalog screenshot.
6. SQL and run-evidence product proof.
7. `#capabilities` advanced-analysis story, anomaly screenshot, and complete
   grouped skill overview.
8. Complementary workflow features for chart refinement, evidence, and history.
9. `#trust` outcomes: semantic grounding, governed execution, traceability.
10. Accessible FAQ disclosures.
11. Closing CTA and footer.

## Calls to action

- **Open Jeen Insights** links to `/` for an existing user.
- **Request a demo** opens an email to `sales@jeen.ai` with a prefilled subject.

The labels remain consistent in the header, hero, and closing block.

## Visual and accessibility conventions

- The page is pinned to the light theme for a deterministic public surface.
- Urbanist, the shared color tokens, near-black primary actions, restrained
  accents, bordered cards, and low/middle elevation align it with Jeen UI.
- Layout breakpoints follow the shared design system at approximately 1280px,
  1023px, and 767px.
- Navigation remains available on narrow screens and can scroll horizontally.
- Interactive targets are at least 44px where space permits and have visible
  `:focus-visible` halos.
- A skip link, semantic headings, list markup, `<details>` FAQ controls,
  descriptive alt text, and reduced-motion handling support keyboard and
  assistive-technology users.

## Verification

`tests/unit/test_ui_locale_routes.py` verifies that the page:

- is anonymously reachable;
- remains English under a Hebrew cookie and `Accept-Language`;
- states the Jeen Data semantic-layer relationship;
- states the simple-to-complex question progression and forecast-informed
  inventory-planning boundary;
- names all 12 registered analysis capabilities and preserves review,
  deployment, and non-causal qualifications;
- exposes the product and demo CTAs; and
- references product screenshots that are served successfully.

In addition to the unit test, render the full page at desktop, tablet, and
mobile widths and check image legibility, keyboard focus, layout stability, and
horizontal overflow.
