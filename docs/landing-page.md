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

The page therefore follows four messages:

1. Ask a business question in plain language.
2. Ground the work in the curated Jeen Data catalog.
3. Execute through the configured governance and read-only controls.
4. Return the answer together with inspectable rows, SQL or Python, findings,
   and run evidence.

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
4. Three-step question-to-answer flow.
5. `#jeen-data` explanation with the catalog screenshot.
6. SQL and run-evidence product proof.
7. Advanced Python analytics product proof.
8. `#capabilities` feature grid.
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
- exposes the product and demo CTAs; and
- references product screenshots that are served successfully.

In addition to the unit test, render the full page at desktop, tablet, and
mobile widths and check image legibility, keyboard focus, layout stability, and
horizontal overflow.
