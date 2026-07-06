"""
One-off: correct legacy discord:<id> profiles whose Discord handle is a stale
free-text value, resetting it to the @handle captured on their player entries
(see routes.events.fix_discord_handles). Firestore-only, idempotent.

    GOOGLE_CLOUD_PROJECT=cardboard-party-staging python fix_discord_handles.py
    GOOGLE_CLOUD_PROJECT=cardboard-party         python fix_discord_handles.py
"""

from routes.events import fix_discord_handles

if __name__ == '__main__':
    print(fix_discord_handles())
