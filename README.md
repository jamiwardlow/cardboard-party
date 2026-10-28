# Cardboard Party

A Swiss-system tournament and league organizer for trading-card-game events
(primarily Magic: The Gathering). Cardboard Party handles the administrative core of
an event so a volunteer organizer doesn't have to do it by hand or in a spreadsheet.

**Live:** https://cardboardparty.gg

## Features

- **Events:** one-day Swiss, leagues (rounds can be paired before all results are in),
  and drafts (round 1 paired by pod seat), with an optional single-elimination playoff cut
- **Registration:** player caps, a waitlist with organizer promotion, and organizer-added players
- **Pairing and standings:** greedy Swiss pairing with byes, USCF tiebreakers (OMW%, GW%, OGW%),
  table assignments, and manual pairing edits
- **Results:** players report their own matches, and organizers can edit any result
- **Decklists:** constructed decklist upload with Scryfall/Premodern validation, private per player
- **Discord-native:** players can register, get pairings, and report results through slash
  commands and DMs without opening the web app. Organizers can assign a Discord role to event members.
- **Login:** Google or Discord OAuth
- **Location:** event location with Google Maps autocomplete

## Stack

Flask on Google App Engine, Firestore for storage, Cloud Storage for avatars, and Secret Manager
for secrets. Pages are rendered server-side with Jinja and driven by `fetch()` calls to a JSON API.
The Discord bot uses HTTP Interactions, not a gateway connection.

## Getting started

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...

python main.py   # http://localhost:8080
```

Local runs hit live Firestore unless `FIRESTORE_EMULATOR_HOST` is set and the Firestore
emulator is running. To make yourself the first admin, run this before you sign in for the
first time:

```bash
GOOGLE_CLOUD_PROJECT=<project> python bootstrap_admin.py your@gmail.com "Your Name"
```

## Tests

```bash
pip install -r requirements-test.txt
python -m pytest tests/ -v
```

Firestore is never touched in tests. Browser tests live in `tests/e2e/`. They need
`playwright install` and are excluded from CI (`--ignore=tests/e2e`).

## Project layout

| Path | Purpose |
|---|---|
| `main.py` | Flask app setup |
| `swiss.py` | Pure pairing, standings, and bracket logic |
| `db.py` | All Firestore access |
| `routes/` | Auth, event pages and API, Discord interactions |
| `player_lifecycle.py`, `discord_actions.py` | Transport-free mutations shared by web and Discord |
| `event_view.py` | Builds the event payload returned to clients |
| `templates/`, `static/` | Jinja templates, JS, and CSS |

See [CLAUDE.md](CLAUDE.md) for the full architecture notes, [DEPLOYING.md](DEPLOYING.md)
for environments and deploys, and [DESIGN.md](DESIGN.md) and [PRODUCT.md](PRODUCT.md)
for the visual system and product context.
