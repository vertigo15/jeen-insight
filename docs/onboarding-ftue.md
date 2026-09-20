# Onboarding / First-Time User Experience (FTUE)

A layer on top of the v3 workspace that helps first-time users get to their first
answer: a welcome dialog, a guided tour, quick-start cards, a getting-started
checklist, and a post-answer nudge. Progress and the permanent opt-out are
persisted **server-side per user** so they hold across browsers and devices.
The one deliberate exception is the session-scoped "Skip for now", which has no
server equivalent and lives in a user-scoped `sessionStorage` key (see
[Frequency model](#frequency-model)). Nothing lives in `localStorage`.

No new dependencies and no build step: the client is a single vanilla-JS IIFE
loaded after `workspaceController.js`, reading the DOM the workspace already
builds and reacting to a small set of `jeen:onboarding:*` CustomEvents the app
already emits.

## Surfaces

| Surface | What it is |
| --- | --- |
| **Welcome dialog** | First-run modal introducing the product, with "Take a 30-second tour", "Skip for now" (mutes every surface for this session), and a "Don't show this again" checkbox (permanent opt-out of every surface). |
| **Guided tour** | A four-step coach-mark tour (connection → suggestions → composer → tables). Non-blocking; repositions on resize/scroll; Escape/Skip exits. |
| **Quick-start cards** | Replace the empty result placeholder with runnable example questions + a "browse tables" card. Retire after the third answer; reset on connection change. |
| **Getting-started checklist** | A four-item progress card above the composer. Items auto-complete from real usage signals. |
| **Post-answer nudge** | A one-time hint after the first answer, pointing at pin / SQL details. |

## The checklist

Four items, tracked as a flat map of `item -> true`:

1. `pick_connection` — Pick a connection
2. `ask_first_question` — Ask your first question *(action word: "Start")*
3. `open_sql` — Open the SQL behind an answer
4. `pin_question` — Pin a question you will reuse *(action word: "Show me")*

### Actionable action words

The "Start" and "Show me" labels are real buttons (pointer cursor, keyboard
focusable). Clicking them takes the user to the relevant control and fires a
small **coach-mark** (the same style as the tour) that spotlights it:

- **Start** → opens the conversation and focuses the composer; coach-mark points
  at the composer.
- **Show me** → opens the Pinned panel and spotlights a real ☆ pin star on a
  question (falling back to the Pinned tab if the list is empty).

The coach-mark is non-blocking, auto-clears after a few seconds, dismisses on
"Got it" / Escape, and yields to the full guided tour if that starts.

### Completion state

When all four items are done, the checklist shows a clear "done" state: the
progress ring becomes a solid check, the title flips to **"You're all set"**, the
count/chevron hide, and the card collapses (still dismissible via ×).

## How progress is detected

Completion is driven by CustomEvents the app already dispatches — no new
instrumentation:

| Event | Fired when | Marks |
| --- | --- | --- |
| `jeen:onboarding:pick_connection` | a connection is selected/loaded | `pick_connection` |
| `jeen:onboarding:ask_first_question` | a question returns an answer | `ask_first_question` |
| `jeen:onboarding:open_sql` | the SQL dock is opened | `open_sql` |
| `jeen:onboarding:pin_question` | a question is pinned | `pin_question` |

## Persistence

State is stored in Postgres (the shared metadata DB), one row per user, keyed by
the `user_id` stamped from the signed Flask session.

**Table** — `insights_user_onboarding`:

```14:22:db/migrations/insights/021_user_onboarding.sql
CREATE TABLE IF NOT EXISTS insights_user_onboarding (
    user_id                VARCHAR(255) PRIMARY KEY,
    welcome_seen_at        TIMESTAMPTZ,
    tour_completed_at      TIMESTAMPTZ,
    checklist              JSONB NOT NULL DEFAULT '{}'::jsonb,
    checklist_dismissed_at TIMESTAMPTZ,
    nudge_dismissed_at     TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

plus the permanent opt-out column added by `028_ftue_opt_out.sql`:

```sql
ALTER TABLE insights_user_onboarding
    ADD COLUMN IF NOT EXISTS ftue_opted_out_at TIMESTAMPTZ;
```

Field meaning:

- `checklist` — the four steps as `{item: true}`; all four true drives the
  "You're all set" state.
- `tour_completed_at` — set when the user finishes or exits the guided tour
  themselves (not when the tour is cancelled by a skip/opt-out underneath it).
- `welcome_seen_at` — set alongside `ftue_opted_out_at` when the user ticks
  "Don't show this again".
- `ftue_opted_out_at` — the **permanent opt-out**: suppresses every FTUE
  surface for that account. It is a dedicated column on purpose: a user can
  reach `welcome_seen_at + checklist_dismissed_at + nudge_dismissed_at` through
  ordinary independent dismissals, and that must not silently become a global
  preference.
- `checklist_dismissed_at` / `nudge_dismissed_at` — set when those cards are
  dismissed individually.

### Request path

```
Browser (onboarding.js)
   └─ GET/PATCH /api/user/onboarding
        └─ Flask proxy (src/ui_app.py) — stamps user_id from the session
             └─ FastAPI route (src/api/routes/onboarding.py)
                  └─ OnboardingService (src/agent/onboarding.py) — get-or-create + merge
                       └─ insights_user_onboarding
```

`GET` reads (creating an empty row on first access); `PATCH` merges partial
updates. The service is **fail-soft**: on a DB error it returns an empty state
with HTTP 200 rather than erroring the UI, and the client reconciles server rows
into local state without ever downgrading a known value to null.

## Frequency model

Left alone, the welcome dialog re-appears each new session, and the checklist,
cards and nudge persist until individually dismissed or completed. Two controls
on the welcome dialog change that for **every** surface at once:

| Control | Scope | Where it lives |
| --- | --- | --- |
| **Skip for now** (or Escape) | Mutes the whole FTUE — welcome, tour, checklist, quick-start cards, nudge, coach-mark hints — for the **current tab's session**. Anything already on screen is removed immediately; nothing remounts on reload or on a connection change; it returns in a genuinely new session (new tab/window). | Client only: an in-memory flag mirrored into `sessionStorage` under `jeen:onboarding:skip:<identity>`, written for every identity known to the page (`id:<user_id>` from the onboarding GET, `auth:<id>` / `email:<email>` from `/api/auth/me`) so the skip survives a reload where one of those sources fails soft. User-scoped so a logout/login in the same tab cannot inherit it; when no identity is known nothing is written and the in-memory flag alone covers the page. |
| **Don't show this again** (ticked, then Skip or Take a tour) | **Permanent** opt-out of the whole FTUE for that account, across browsers and devices. | Server: `ftue_opted_out_at` (via `PATCH {ftue_opted_out: true}`). The session flag is also set so nothing can flash back before the write lands. |

"Take a 30-second tour" always runs the tour the user just asked for, even when
the box is ticked; when it ends, the rest of the FTUE stays muted.

Known limits: `sessionStorage` is per tab (top-level browsing context), so a
skip in one tab does not affect other open tabs at all — each has its own
session (a tab opened *from* the skipped tab may start with a copy). The service is fail-soft
(a DB error returns an empty row with HTTP 200), so a lost permanent write is
still covered for the current session by the session flag and would re-show the
FTUE next session.

## Files

| File | Purpose |
| --- | --- |
| `src/static/workspace/onboarding.js` | The FTUE controller (checklist, cards, nudge, welcome, tour, coach-marks). |
| `src/static/workspace/onboarding.css` | FTUE styling (tokens only). |
| `src/api/routes/onboarding.py` | `GET`/`PATCH /api/user/onboarding`. |
| `src/agent/onboarding.py` | `OnboardingService` (get-or-create + merge). |
| `src/api/models.py` | `OnboardingPatch` request model. |
| `db/migrations/insights/021_user_onboarding.sql` | The state table. |
| `db/migrations/insights/028_ftue_opt_out.sql` | Adds `ftue_opted_out_at` (permanent opt-out). |
| `src/ui_app.py` | Flask proxy routes that stamp `user_id`. |

## Testing a fresh run

Because state is per-user in the DB, "reset to new" means clearing that user's
row (a fresh empty row is recreated on next load):

```sql
DELETE FROM insights_user_onboarding WHERE user_id = '<your-user-id>';
```

Then open a **fresh tab from the address bar** (a plain hard-refresh keeps the
tab's `sessionStorage`, so a "Skip for now" from earlier in that tab would still
mute the FTUE; a tab opened via a link from that tab may inherit a copy) or
remove the `jeen:onboarding:skip:*` keys from DevTools > Application > Session
Storage. A hard-refresh also picks up the latest `?v=` assets.
