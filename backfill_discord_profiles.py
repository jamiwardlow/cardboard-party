"""
One-off: create profiles for button-registered Discord ghost players and link
their event entries to a `discord:<id>` account (see
routes.events.backfill_discord_profiles). Firestore-only, idempotent — safe to
re-run.

    GOOGLE_CLOUD_PROJECT=cardboard-party-staging python backfill_discord_profiles.py
    GOOGLE_CLOUD_PROJECT=cardboard-party         python backfill_discord_profiles.py
"""

from routes.events import backfill_discord_profiles

if __name__ == '__main__':
    print(backfill_discord_profiles())
