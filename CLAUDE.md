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

- **`main.py`** — Flask app, registers `auth_bp`, `events_bp`, and `discord_bp`.
  `app.secret_key` comes from Secret Manager (`FLASK_SECRET_KEY`); startup raises
  `RuntimeError` if the key is missing or the dev placeholder in production.
- **`db.py`** — all Firestore access. Collections: `events`, `users`, `invites`, and two
  singleton config docs `config/admins` and `config/settings`.
- **`swiss.py`** — pure functions for pairing, standings, and playoff brackets; no I/O,
  no Flask. The only part with self-contained, testable logic.
- **`routes/auth.py`** — Google OAuth2 **and** Discord OAuth2 (both manual flows, no
  library). Provides `login_required` decorator and `get_current_user()`.
- **`routes/events.py`** — everything else: page routes, `/api/...` JSON endpoints,
  registration, pairing, results, admin and profile management. Also exposes the
  helper functions called by `routes/discord.py`.
- **`routes/discord.py`** — Discord bot via HTTP Interactions (no gateway). Verifies
  Ed25519 signatures, handles slash commands (`/cparty`), buttons, select menus, and
  modals. Thin HTTP layer only — delegates all mutations and queries to `discord_actions.py`.
- **`discord_actions.py`** — all mutations/queries triggered by Discord interactions.
  No HTTP or Flask context: pure functions over plain dicts, directly testable. Called by
  `routes/discord.py`. Split from `routes/events.py` to enable direct unit testing.
- **`discord_api.py`** — outbound Discord REST calls (channel posts, DMs). Used by
  `routes/events.py` and `discord_actions.py` to post round pairings and send result DMs.
- **`discord_notify.py`** — round-label helpers (`_round_label`, `fmt_time`) shared
  between the web app and the bot. Originally sent webhook notifications; that is retired.
- **`discord_identity.py`** — pure functions for matching a Discord user to a Cardboard
  Party account: first by stored numeric `discord_id`, then by normalised handle
  candidates (username and display name). `resolve_discord_identity` returns
  `(google_id, handle_set)` and is the entry point for any Discord interaction that
  needs to identify its invoker.
- **`discord_match.py`** — open-match lookup and result reporting for Discord. Pure
  functions: `discord_open_matches` lists a user's current open matches across all
  events (used by the `/report` picker); `report_result_via_discord` records a result
  from the reporter's perspective. Uses `discord_identity` to resolve the reporter
  before touching any match.
- **`event_actions.py`** — transport-free registration mutations: `register_player`,
  `unregister_player`, `join_waitlist`, `leave_waitlist`. Each returns `(result, None)`
  on success or `(None, error_str)` on failure. Called by both `routes/events.py` and
  `discord_actions.py`; transport-specific side effects (DMs, announcements) stay with
  the caller.
- **`event_state.py`** — pure predicates and utilities over the event dict (`_slugify`,
  `_is_full`, `_self_registration_blocked`, `_validate_result`, `make_player_entry`,
  `auto_check_in`, etc.). Imported by both `routes/events.py` and `discord_actions.py`
  to avoid circular imports between them.
- **`event_announcements.py`** — Discord event-card posting and refresh. No Flask
  context. `announce_event_to_channel` posts a card with a Register button and persists
  `discord_announce` on the event; `refresh_event_announcement` edits the posted card
  whenever registration status changes (open / full / closed).
- **`event_view.py`** — `build_event_view(event, current_user)` enriches the raw event
  dict before it is returned by any API endpoint: computes standings, strips sensitive
  fields (`guest_token`, `discord_id`; replaces decklist content with `has_decklist` /
  `decklist_status` flags), applies `delay_pairings` / `delay_standings` visibility
  rules, and populates `can_manage`, `is_full`, `my_waitlist`, and co-organizer names.
  Every route that returns event state to clients should call this.
- **`event_queries.py`** — semantic query functions over `db.list_events()`. Callers
  should import named queries from here rather than calling `list_events()` with inline filters.
- **`routes/event_fields.py`** — `clean_event_fields(raw, partial=False)` validates and
  normalises all event creation/update fields. Returns `(cleaned, errors)`.
- **`decklist.py`** — network-free `parse_decklist` + Scryfall-calling `validate_decklist`.
  Handles Moxfield import via `import_moxfield` (requires `MOXFIELD_USER_AGENT` secret).
- **`storage.py`** — GCS avatar upload (validate, center-crop, resize via Pillow).
- **`limiter.py`** — per-IP rate limiting via `flask-limiter`. Uses in-process memory
  storage (effective global limit ≈ `num_instances × per-instance limit`). No default
  blanket limit — decorate specific endpoints with `@limiter.limit(...)`.
- **`gcp_secrets.py`** — `get_secret(name)`: env var → Secret Manager fallback, `@lru_cache`d.

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

`_can_manage(event)` = current user is a global admin, the event's `owner_id`, **or**
listed in `co_organizer_ids`. Used for organiser actions (pairing, editing results/pairings,
adding/dropping players).
Players who are registered (matched by `google_id`) may report results for **their own**
matches only. Anyone can view events and standings.

### Admin bootstrapping via "pending" entries

Admins are added by **email** before the person has ever signed in, stored as
`id = "pending:<email>"`. On their next OAuth login, `_resolve_pending_admin` in
`auth.py` swaps that entry for their real Google ID. This is why `bootstrap_admin.py`
and `/api/admins` both write `pending:` IDs.

The same pattern applies to **co-organizers**: stored as `pending:<email>` in
`co_organizer_ids`, resolved to a Google ID on login via `resolve_pending_co_organizer`
in `db.py`.

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
