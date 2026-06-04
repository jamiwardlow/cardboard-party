# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Cardboard Party is a Swiss-system tournament/league organizer for trading-card-game
events, built as a Flask app on Google App Engine with Firestore as the datastore.
It runs in GCP project `cardboard-party` (region us-west2) at
https://cardboard-party.wl.r.appspot.com.

## Commands

```bash
# Setup
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# OAuth credentials must be in the environment before running (Google login)
export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...

# Run locally (http://localhost:8080, debug mode)
python main.py

# Add the first global admin (run once, before that account ever signs in).
# bootstrap_admin.py writes to Firestore via the default project, so set it:
GOOGLE_CLOUD_PROJECT=cardboard-party python bootstrap_admin.py your@gmail.com "Your Name"

# Deploy to App Engine (project cardboard-party → cardboard-party.wl.r.appspot.com)
gcloud app deploy --project=cardboard-party
gcloud app browse --project=cardboard-party
```

There is **no test suite, linter, or build step**. The app runs directly from source.
Local runs hit live Firestore unless you set `FIRESTORE_EMULATOR_HOST` and run the
Firestore emulator.

## Architecture

Three Python layers, with all HTML rendered server-side via Jinja templates and
driven client-side by `fetch()` calls to a JSON API.

- **`main.py`** — Flask app, registers `auth_bp` and `events_bp`. (Note: `app.secret_key`
  is a hardcoded placeholder; sessions are cookie-based.)
- **`db.py`** — all Firestore access. Collections: `events`, `users`, and two singleton
  config docs `config/admins` and `config/settings`.
- **`swiss.py`** — pure functions for pairing and standings; no I/O, no Flask. This is
  the only part with self-contained, testable logic.
- **`routes/auth.py`** — Google OAuth2 (manual flow, not a library) + `login_required`
  decorator + `get_current_user()` (reads `session['user']`).
- **`routes/events.py`** — everything else: page routes, the `/api/...` JSON endpoints,
  registration, pairing, results, admin and profile management.

### The event document is the unit of state

There is essentially one aggregate: the `events/<id>` document. It holds `players`
(list of player snapshots) and `rounds` (list of rounds, each a list of match dicts).
A **match dict** looks like:

```python
{'player1_id': str, 'player2_id': str,
 'winner_id': str | None,   # None = unplayed; a draw is result=='draw'
 'result': str | None,      # game score like '2-1-0', or 'draw', or '2-0-0' for a bye
 'is_bye': bool}
```

Mutations read the whole event, edit the in-memory dict, and write it back with
`save_event(id, {field: value})` (a Firestore `merge=True` set). There is no
optimistic locking, so concurrent writers can clobber each other.

### Firestore nested-array workaround (critical)

Firestore cannot store an array of arrays. `rounds` is therefore **flattened** on write
(each match gets a `round_num` field, all matches in one flat list) and **unflattened**
on read, via `_flatten_rounds`/`_unflatten_rounds` in `db.py`. `create_event`,
`save_event`, `get_event`, and `list_events` all handle this transparently — so always
go through those helpers and treat `rounds` as a list-of-lists everywhere else.

### Player identity is twofold

- `players[].id` — a per-event slug (`_slugify(name) + '_' + index`). Used inside that
  event's matches and standings only.
- `players[].google_id` — links a player entry to a real Google account (may be `None`
  for organiser-added "ghost" players). Cross-event identity (profiles, history) keys on
  this. The `users/<google_id>` collection is the source of truth for display name and
  Discord handle going forward; per-event entries are registration-time snapshots.

### Permission model

`_can_manage(event)` = current user is a global admin **or** the event's `owner_id`.
Used for organiser actions (pairing, editing results/pairings, adding/dropping players).
Players who are registered (matched by `google_id`) may report results for **their own**
matches only. Anyone can view events and standings.

### Admin bootstrapping via "pending" entries

Admins are added by **email** before the person has ever signed in, stored as
`id = "pending:<email>"`. On their next OAuth login, `_resolve_pending_admin` in
`auth.py` swaps that entry for their real Google ID. This is why `bootstrap_admin.py`
and `/api/admins` both write `pending:` IDs.

### Swiss logic (`swiss.py`)

Greedy pairing: sort active players by points (win=3, draw=1) desc, pair each with the
highest unpaired player they haven't faced (falls back to a repeat match if forced).
Odd count → lowest-ranked player without a prior bye gets a bye (scored as a 2-0-0 win
against `BYE_PLAYER_ID == '__bye__'`). Standings use USCF tiebreakers (OMW%, GW%, OGW%)
with the standard 1/3 floor. `League` event_type skips the "previous round must be
complete" check before pairing.

## Secrets

`GOOGLE_CLIENT_SECRET` is **not** stored in the repo. It lives in Google Secret Manager
(secret name `GOOGLE_CLIENT_SECRET`) and is fetched at startup by `gcp_secrets.py`, which
prefers a same-named env var (local dev) and falls back to Secret Manager in production
(project from `GOOGLE_CLOUD_PROJECT`, which App Engine sets automatically). The App Engine
default service account `cardboard-party@appspot.gserviceaccount.com` has
`roles/secretmanager.secretAccessor` on the secret.

- `GOOGLE_CLIENT_ID` stays in `app.yaml` env_variables — it's public (sent to browsers).
- The fetched value is `@lru_cache`d, so **a new secret version requires a redeploy/restart**
  to take effect; running instances keep the value they read at startup.
- Rotate by adding a new version (`gcloud secrets versions add ...` or the Console UI),
  then redeploy. `main.py`'s `app.secret_key` is still a hardcoded placeholder — move it to
  Secret Manager too before this matters for session security.

## Frontend security conventions

The dynamic pages (`index.html`, `event.html`) render by building HTML strings and
assigning to `innerHTML`. Player names, discord handles, and event names/formats are all
free-text, so **any interpolation of user data into `innerHTML` must go through
`escapeHtml()`** (defined in `static/js/app.js`) — otherwise you reintroduce stored XSS.
Server-rendered Jinja (`player.html`, `admin.html`, etc.) auto-escapes, so it's safe by
default there. The OAuth flow uses a `state` nonce (CSRF) and only honors a post-login
`next` if `_is_safe_redirect` says it's same-host (no open redirects); session cookies are
`HttpOnly` + `SameSite=Lax`, and `Secure` in production.

## Gotchas

- The `{static` directory at the repo root is junk from a botched `mkdir` brace
  expansion — ignore/delete it; real assets live in `static/`.
