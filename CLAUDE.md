# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Cardboard Party is a Swiss-system tournament/league organizer for trading-card-game
events, built as a Flask app on Google App Engine with Firestore as the datastore.
It runs in GCP project `cardboard-party` (region us-west2) at
https://cardboard-party.wl.r.appspot.com.

## Workflow

After completing any meaningful unit of work:

1. **Commit to git** with a clean, descriptive commit message summarizing *why* the change was made (not just what).
2. **Push to GitHub** (`git push origin main`).
3. **Deploy to staging** (`gcloud app deploy staging.yaml --project=cardboard-party-staging`) and verify the change works before considering the task done.

Do not batch unrelated changes into one commit. Do not deploy to prod unless explicitly asked.

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

# Staging (separate project, isolated Firestore/bucket) — see DEPLOYING.md
gcloud app deploy staging.yaml --project=cardboard-party-staging

# Seed staging Firestore with sample events (safe — refuses to run against prod)
GOOGLE_CLOUD_PROJECT=cardboard-party-staging python seed_staging.py
```

There is **no linter or build step**. The app runs directly from source.
Local runs hit live Firestore unless you set `FIRESTORE_EMULATOR_HOST` and run the
Firestore emulator.

**Test suite** — `pytest` with `pytest-flask`. Install deps and run:

```bash
pip install -r requirements-test.txt
python -m pytest tests/ -v
python -m pytest tests/ --cov=. --cov-report=term-missing   # with coverage
python -m pytest tests/test_swiss.py -v                      # single file
python -m pytest tests/ -k "test_pair" -v                    # single test pattern
```

Tests live in `tests/`. The `conftest.py` provides three fixtures: `app` (Flask
test app), `client` (unauthenticated test client), and `auth_client` (pre-seeded
with a signed-in session whose user ID is `'test_uid'`). There is also a
`minimal_event(**overrides)` helper that returns a minimal valid event dict.
Firestore is never hit — mock `routes.events.get_event`, `routes.events.save_event`,
etc. with `unittest.mock.patch` as needed. `GOOGLE_CLOUD_PROJECT` is unset at test
startup so `gcp_secrets.get_secret` returns `''` without calling Secret Manager.

`tests/e2e/` holds Playwright browser tests. They require `playwright install` (done
once after `pip install -r requirements-test.txt`) and spin up a live in-process Flask
server. CI **excludes** them (`--ignore=tests/e2e`); run locally with
`python -m pytest tests/e2e -v`.

**Environments:** prod and staging run the *same code*, differing only by
environment config — `AVATARS_BUCKET` (GCS bucket, `storage.py`), `CANONICAL_HOST`
(set only in prod's `app.yaml`), and the per-project OAuth client + Secret Manager
secrets. Never hardcode environment-specific values; read them from the environment.
See **DEPLOYING.md** for the full setup and the `--no-promote` safe-deploy flow.

## Architecture

All HTML is rendered server-side via Jinja templates and driven client-side by `fetch()`
calls to a JSON API.

- **`main.py`** — Flask app; registers `auth_bp`, `events_bp`, `discord_bp`. Fails
  closed at startup if `FLASK_SECRET_KEY` is missing or the dev placeholder in production.
- **`db.py`** — all Firestore access (`events`, `users`, `invites` collections, plus
  singleton `config/admins` and `config/settings` docs).
- **`swiss.py`** — pure pairing/standings/bracket functions; no I/O, no Flask. The
  only part with self-contained, testable logic.
- **`routes/auth.py`** — Google + Discord OAuth2 (manual flows, no library). Provides
  `login_required` and `get_current_user()`.
- **`routes/events.py`** — everything else: pages, `/api/...` endpoints, registration,
  pairing, results, admin/profile management. Also exposes helpers `routes/discord.py` calls.
- **`routes/discord.py`** — Discord bot via HTTP Interactions (no gateway); verifies
  Ed25519 signatures. Thin HTTP layer only — delegates mutations/queries to `discord_actions.py`.
- **`discord_actions.py`** — mutations/queries triggered by Discord interactions. Pure
  functions over plain dicts (no HTTP/Flask context) — split out from `routes/events.py`
  so they're directly unit-testable.
- **`discord_api.py`** — outbound Discord REST calls (channel posts, DMs).
- **`discord_notify.py`** — round-label helpers (`_round_label`, `fmt_time`) shared by
  the web app and the bot.
- **`discord_identity.py`** — matches a Discord user to a Cardboard Party account (by
  stored numeric `discord_id`, then normalised handle). `resolve_discord_identity` is the
  entry point any Discord interaction uses to identify its invoker.
- **`discord_match.py`** — open-match lookup and result reporting for Discord; resolves
  the reporter via `discord_identity` before touching a match.
- **`event_actions.py`** — transport-free registration mutations (`register_player`,
  `unregister_player`, `join_waitlist`, `leave_waitlist`), each returning `(result, None)`
  or `(None, error_str)`. Called by both `routes/events.py` and `discord_actions.py`;
  transport-specific side effects (DMs, announcements) stay with the caller.
- **`event_state.py`** — pure predicates/utilities over the event dict (slugify,
  fullness checks, result validation, player-entry building, auto check-in). Lives here,
  not in `routes/events.py`, so `discord_actions.py` can import it without a circular import.
- **`event_announcements.py`** — posts/refreshes the Discord event-card (Register
  button); no Flask context.
- **`event_view.py`** — `build_event_view(event, current_user)` enriches an event
  before any API endpoint returns it: standings, strips sensitive fields, applies
  visibility rules, computes `can_manage`/`is_full`/etc. Every route returning event
  state to clients should call this.
- **`event_queries.py`** — semantic query functions over `db.list_events()`; prefer
  these over inline filters.
- **`routes/event_fields.py`** — `clean_event_fields(raw, partial=False)` validates/
  normalises event fields, returns `(cleaned, errors)`.
- **`decklist.py`** — network-free `parse_decklist` + Scryfall-calling `validate_decklist`;
  Moxfield import via `import_moxfield`.
- **`storage.py`** — GCS avatar upload (validate/center-crop/resize via Pillow).
- **`limiter.py`** — per-IP rate limiting (`flask-limiter`, in-process memory — effective
  global limit ≈ instances × per-instance limit). No blanket default; decorate endpoints individually.
- **`gcp_secrets.py`** — `get_secret(name)`: env var → Secret Manager fallback, `@lru_cache`d.

### The event document is the unit of state

One aggregate: `events/<id>`, holding `players` and `rounds` (list of match dicts:
`player1_id`/`player2_id`, `winner_id` (`None` = unplayed), `result` (score like
`'2-1-0'`, `'draw'`, or `'2-0-0'` for a bye), `is_bye`). Mutations read-modify-write via
`save_event(id, {field: value})` (Firestore `merge=True`) — no optimistic locking, so
concurrent writers can clobber each other.

Firestore can't store arrays of arrays, so `rounds` is flattened on write / unflattened
on read (`_flatten_rounds`/`_unflatten_rounds` in `db.py`, transparent through
`create_event`/`save_event`/`get_event`/`list_events`). Always use those helpers; treat
`rounds` as list-of-lists everywhere else.

### Player identity is twofold

`players[].id` is a per-event slug, used only within that event's matches/standings.
`players[].google_id` links to a real account (`None` for organiser-added "ghost"
players) and is what cross-event identity (profiles, history) keys on — `users/<google_id>`
is the source of truth for display name/Discord handle; per-event entries are just
registration-time snapshots.

### Permission model

`_can_manage(event)` = global admin, the event's `owner_id`, or listed in
`co_organizer_ids` — used for organiser actions (pairing, editing results/pairings,
adding/dropping players). Registered players may report results for **their own**
matches only (matched by `google_id`). Anyone can view events and standings.

### Admin bootstrapping via "pending" entries

Admins/co-organizers can be added by **email** before the person ever signs in, stored
as `pending:<email>` (in the admins config doc, or in `co_organizer_ids`). On next OAuth
login, `_resolve_pending_admin` (`auth.py`) / `resolve_pending_co_organizer` (`db.py`)
swap the entry for their real Google ID.

### Waitlist

When an event hits its player cap, new registrants are added to `event.waitlist` (a
list of waitlist-entry dicts with `id`, `status`, `name`, `google_id`, `discord_id`,
etc.). Promotion to `players` is done by the organiser and is transactional in
`db.promote_waitlist_entry` to prevent double-promotion.

### Event types

- **One-day** — standard Swiss, all rounds in one session.
- **League** — Swiss, but skips the "previous round must be complete" check so rounds
  can be paired before all results are in.
- **Draft** — first round paired by pod seat via `pair_draft_r1`; subsequent rounds are
  standard Swiss. Optionally followed by a single-elimination bracket cut.

### Swiss / bracket logic (`swiss.py`)

Swiss: greedy pairing — sort active players by points (win=3, draw=1) desc, pair each
with the highest unpaired player they haven't faced (falls back to a repeat match if
forced). Odd count → lowest-ranked player without a prior bye gets a bye (scored as a
2-0-0 win against `BYE_PLAYER_ID == '__bye__'`). Standings use USCF tiebreakers (OMW%,
GW%, OGW%) with the standard 1/3 floor.

Playoff bracket: `make_bracket` seeds the top-N players into single-elimination;
`next_bracket_round` advances winners. Bracket matches are tagged `stage == 'bracket'`
in the match dict and appended to `rounds`; `_round_label` in `discord_notify.py`
renders them as "Finals/Semifinals/Quarterfinals/Top N".

## Secrets

All secrets live in Google Secret Manager (project `cardboard-party` or
`cardboard-party-staging`). `gcp_secrets.py` prefers a same-named env var (local dev)
and falls back to Secret Manager in production. All fetched values are `@lru_cache`d —
**a new secret version requires a redeploy/restart to take effect**.

| Secret name | Used by |
|---|---|
| `GOOGLE_CLIENT_SECRET` | Google OAuth login |
| `DISCORD_PUBLIC_KEY` | Ed25519 signature verification (`routes/discord.py`) |
| `DISCORD_BOT_TOKEN` | Outbound Discord REST (`discord_api.py`) |
| `DISCORD_APP_ID` | Discord OAuth login + bot identity |
| `DISCORD_CLIENT_SECRET` | Discord OAuth login |
| `MOXFIELD_USER_AGENT` | Moxfield deck import (`decklist.py`) |
| `MAPS_API_KEY` | Google Maps autocomplete on event location field |

- `GOOGLE_CLIENT_ID` stays in `app.yaml` env_variables — it's public (sent to browsers).
- Discord prod uses command name `cparty`; staging uses `cpstaging` (set via
  `DISCORD_COMMAND_NAME` in `staging.yaml`).
- Rotate by adding a new version (`gcloud secrets versions add ...` or the Console UI),
  then redeploy.

## Avatar storage (GCS)

Custom profile pictures live in the public-read Cloud Storage bucket
`cardboard-party-avatars` (us-west2). `storage.py` validates/center-crops/resizes
uploads with Pillow and writes a unique object per upload; the App Engine service
account `cardboard-party@appspot.gserviceaccount.com` has `roles/storage.objectAdmin`
on the bucket, and `allUsers` has `objectViewer` (avatars are public). A user's
effective avatar = `users/<id>.avatar_url` (custom) or `.google_picture` (captured at
login); the client-side cropper in `player.html` sends a pre-cropped 512² JPEG.

## Frontend security conventions

The dynamic pages (`index.html`, `event.html`) render by building HTML strings and
assigning to `innerHTML`. Player names, discord handles, and event names/formats are all
free-text, so **any interpolation of user data into `innerHTML` must go through
`escapeHtml()`** (defined in `static/js/app.js`) — otherwise you reintroduce stored XSS.
Server-rendered Jinja (`player.html`, `admin.html`, etc.) auto-escapes, so it's safe by
default there. The OAuth flow uses a `state` nonce (CSRF) and only honors a post-login
`next` if `_is_safe_redirect` says it's same-host (no open redirects); session cookies are
`HttpOnly` + `SameSite=Lax`, and `Secure` in production.


## Agent skills

### Issue tracker

Issues live in GitHub Issues (`gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
