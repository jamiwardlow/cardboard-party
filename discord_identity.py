"""
Discord identity resolution — matching Discord users to Cardboard Party accounts.
No Flask context: pure functions over plain dicts.
"""
from db import list_users


def normalize_handle(h: str) -> str:
    """Normalise a Discord handle for comparison: drop a leading @, lower-case,
    and drop a legacy '#1234' discriminator."""
    h = (h or '').strip().lstrip('@').lower()
    return h.split('#', 1)[0] if '#' in h else h


def find_profile_for_discord(discord_id: str, username: str, display: str = '') -> dict | None:
    """Match a Discord user to an existing account: by a discord_id we've stored
    on the profile before (exact), else by the profile's saved Discord handle
    matching either the interaction's username or display name (people often save
    their display name as their handle). Returns the profile (with google_id) or
    None. Exact ID matches win over handle matches.

    A handle match also lets the caller store the numeric discord_id on the
    account (see register_player_via_discord), so subsequent links are exact and
    immune to the handle being a display name, edited, or a renamed username."""
    candidates = {h for h in (normalize_handle(username), normalize_handle(display)) if h}
    by_handle = None
    for u in list_users():
        if discord_id and u.get('discord_id') == discord_id:
            return u
        if candidates and not by_handle and normalize_handle(u.get('discord')) in candidates:
            by_handle = u
    return by_handle


def google_id_for_discord(discord_id: str):
    """The Google account (if any) linked to a Discord numeric ID — so we can also
    match players who registered on the web/were added by an organiser but have a
    linked Discord. Returns the google_id or None."""
    prof = find_profile_for_discord(discord_id, '')   # '' = match by discord_id only
    return prof.get('google_id') if prof else None


def resolve_discord_identity(discord_id: str, username: str = '', display: str = ''):
    """Resolve a Discord user to what we match players on: their linked google_id
    (by a stored numeric discord_id, or by the profile's saved handle matching the
    interaction's verified username/display) and the set of normalised handle
    candidates. Passing the username/display lets players who only have a Discord
    *handle* on file — registered on the web or added by the organiser, never
    linked by numeric ID — still be matched (e.g. for /report)."""
    prof = find_profile_for_discord(discord_id, username, display)
    gid = prof.get('google_id') if prof else None
    handles = {h for h in (normalize_handle(username), normalize_handle(display)) if h}
    return gid, handles
