# Marketing Landing Page

A public, unauthenticated marketing page for **Jeen Insights**, served by the
Flask UI layer at `/landing`. It reuses the app's own design tokens and fonts so
the marketing surface and the product share one visual language, with no new
dependencies and no build step.

## Route & access

The page is a plain Flask route that renders a static template — GET-only, no
auth:

```746:750:src/ui_app.py
@app.route("/landing")
def landing():
    """Public marketing landing page. No auth (see _PUBLIC_EXACT); GET-only, so
    ...
    return render_template("landing.html")
```

`/landing` is added to the UI's public allow-list so the auth gate lets it
through anonymously:

```173:178:src/ui_app.py
_PUBLIC_EXACT    = {
    ...
    "/landing",
```

Everything else (the product itself at `/`) remains behind login; only this
marketing page and the usual static/health prefixes are public.

## Files

| File | Purpose |
| --- | --- |
| `src/templates/landing.html` | The page markup (11 sections, see below). |
| `src/static/landing/landing.css` | Page-specific layout/styling. |
| `src/static/design-tokens.css` | Shared color/spacing/type tokens + `@font-face` (imported by both the product and the landing page). |

The template links `design-tokens.css` first, then `landing/landing.css`, both
cache-busted with `?v=`.

## Structure

The page is 11 ordered sections (comments in the template mark each one):

1. **Sticky header** — brand mark + anchor nav (How it works, Capabilities,
   Connections, Architecture) + a primary CTA.
2. **Hero** — headline, subcopy, and the main call-to-action.
3. **Product shot** — a framed screenshot slot for the workspace UI.
4. **Stat strip** — headline metrics.
5. **How it works** (`#how`) — the natural-language → SQL → answer flow.
6. **Capabilities** (`#capabilities`) — feature grid.
7. **Connections** (`#connections`) — supported data sources.
8. **Architecture** (`#architecture`) — the two-container / internal-auth story.
9. **FAQ** — common questions.
10. **Final CTA** — closing conversion block.
11. **Footer** — links and legal.

## Theming

The landing page is pinned to the **light theme** (it does not follow the
product's dark-mode toggle) so the marketing surface is deterministic. All
colors, spacing, radii, shadows and type come from `design-tokens.css` — there
are **no hardcoded hex values or inline styles** in the template.

## Constraints honored

- No new dependencies, no build step — plain HTML/CSS served by Flask.
- Only the shared design tokens and fonts; no ad-hoc literals.
- Public by explicit allow-list entry, not by weakening the global auth gate.
