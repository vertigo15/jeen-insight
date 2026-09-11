# Onboarding / First-Time User Experience (FTUE)

A layer on top of the v3 workspace that helps first-time users get to their first
answer: a welcome dialog, a guided tour, quick-start cards, a getting-started
checklist, and a post-answer nudge. State is persisted **server-side per user**
so "show once" / progress holds across browsers and devices — nothing lives in
`localStorage`.

No new dependencies and no build step: the client is a single vanilla-JS IIFE
loaded after `workspaceController.js`, reading the DOM the workspace already
builds and reacting to a small set of `jeen:onboarding:*` CustomEvents the app
already emits.

## Surfaces

| Surface | What it is |
| --- | --- |
| **Welcome dialog** | First-run modal introducing the product, with "Take a 30-second tour", "Skip for now", and a "Don't show this again" checkbox. |
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

Field meaning:

- `checklist` — the four steps as `{item: true}`; all four true drives the
  "You're all set" state.
- `tour_completed_at` — set when the guided tour finishes/exits.
- `welcome_seen_at` — set **only** when the user ticks "Don't show this again".
- `checklist_dismissed_at` / `nudge_dismissed_at` — set when those cards are
  dismissed.

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

The welcome dialog and guided tour **re-appear every session until the user
explicitly opts out** via the "Don't show this again" checkbox (which persists
`welcome_seen_at`). Skipping or taking the tour is temporary. The checklist and
nudge likewise persist across sessions until dismissed or completed.

## Files

| File | Purpose |
| --- | --- |
| `src/static/workspace/onboarding.js` | The FTUE controller (checklist, cards, nudge, welcome, tour, coach-marks). |
| `src/static/workspace/onboarding.css` | FTUE styling (tokens only). |
| `src/api/routes/onboarding.py` | `GET`/`PATCH /api/user/onboarding`. |
| `src/agent/onboarding.py` | `OnboardingService` (get-or-create + merge). |
| `src/api/models.py` | `OnboardingPatch` request model. |
| `db/migrations/insights/021_user_onboarding.sql` | The state table. |
| `src/ui_app.py` | Flask proxy routes that stamp `user_id`. |

## Testing a fresh run

Because state is per-user in the DB, "reset to new" means clearing that user's
row (a fresh empty row is recreated on next load):

```sql
DELETE FROM insights_user_onboarding WHERE user_id = '<your-user-id>';
```

Then hard-refresh the workspace (also picks up the latest `?v=` assets).
