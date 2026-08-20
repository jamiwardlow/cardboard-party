"""
Discord identity resolution — matching Discord users to Cardboard Party accounts.
No Flask context: pure functions over plain dicts.
"""
from db import list_users, save_user_profile


def normalize_handle(h: str) -> str:
    """Normalise a Discord handle for comparison: drop a leading @, lower-case,
    and drop a legacy '#1234' discriminator."""
    h = (h or '').strip().lstrip('@').lower()
    return h.split('#', 1)[0] if '#' in h else h


def find_profile_for_discord(discord_id: str, username: str, display: str = '') -> dict | None:
    """Match a Discord user to an existing account: by a discord_id we've stored
    on the profile before (exact), else by the profile's saved Discord handle
    matching the interaction's verified *username*. Returns the profile (with
    google_id) or None. Exact ID matches win over handle matches.

    `display` (nickname/global display name) is intentionally NOT matched on: unlike
    the account username, it isn't unique and anyone can set theirs to any string at
    will, so matching it would let an attacker impersonate another player just by
    renaming themselves. `username` is Discord's globally-unique @handle and can't be
    claimed by someone else while the real owner holds it.

    A handle match locks the verified discord_id onto the account immediately, so
    every subsequent lookup is an exact ID match — closing the window a same-named
    future account could otherwise exploit if the handle is ever renamed/released."""
    handle = normalize_handle(username)
    by_handle = None
    for u in list_users():
        if discord_id and u.get('discord_id') == discord_id:
            return u
        if handle and not by_handle and normalize_handle(u.get('discord')) == handle:
            by_handle = u
    if by_handle and discord_id and not by_handle.get('discord_id'):
        save_user_profile(by_handle['google_id'], {'discord_id': discord_id})
        by_handle['discord_id'] = discord_id
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
    interaction's verified username) and the set of normalised handle candidates.
    Passing the verified username lets players who only have a Discord *handle* on
    file — registered on the web or added by the organiser, never linked by numeric
    ID — still be matched (e.g. for /report). `display` is accepted for
    backward-compatible call signatures but not used for matching — see
    find_profile_for_discord for why (it's spoofable; username isn't)."""
    prof = find_profile_for_discord(discord_id, username, display)
    gid = prof.get('google_id') if prof else None
    handle = normalize_handle(username)
    handles = {handle} if handle else set()
    return gid, handles
