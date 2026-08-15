"""
Event view layer — builds the enriched event dict served by the API.
Flask-free: accepts plain dicts, returns the enriched dict.
"""
from db import get_user_profile, is_admin
from swiss import compute_standings, default_num_rounds, id_safe_players
from event_state import _is_full


def _active_waitlist(event: dict) -> list[dict]:
    """Still-waiting waitlist records, oldest first."""
    wl = [w for w in (event.get('waitlist') or []) if w.get('status') == 'waitlisted']
    wl.sort(key=lambda w: w.get('joined_at', ''))
    return wl


def _viewer_can_manage(event: dict, current_user: dict | None) -> bool:
    """True if current_user is the event owner, a co-organizer, or a global admin.
    Flask-free: takes the user dict directly."""
    if not current_user:
        return False
    uid = current_user['id']
    return (uid == event.get('owner_id')
            or is_admin(uid)
            or uid in event.get('co_organizer_ids', []))


def redact_players(event: dict) -> None:
    """Strip guest self-report tokens before sending an event to clients. The
    token is a bearer secret — anyone holding it can report as that player and
    claim their identity via the magic link — so it must never appear in any
    public event payload. It's only ever returned once, to the joiner.

    Also strip discord_id — it's only used server-side to match a player to the
    Discord user reporting, and there's no need to expose the player↔Discord
    mapping in public payloads. Decklists are private (owner + organiser only), so
    the content is replaced with a `has_decklist` flag for status badges."""
    for p in event.get('players', []):
        p.pop('guest_token', None)
        p.pop('discord_id', None)
        dl = p.get('decklist') or {}
        p['has_decklist'] = bool(dl.get('text', '').strip())
        p['decklist_status'] = ((dl.get('validation') or {}).get('status', 'none')
                                if p['has_decklist'] else None)
        p.pop('decklist', None)


def enrich_players_discord(event: dict) -> None:
    """Set each player's displayed `discord` to their account's handle (the source of
    truth), then show it only if it looks like a real handle — a single token. A
    value with a space is a display name typed into the old free-text field, not a
    handle, so it's blanked. Mutates the response copy only; profiles read once/id."""
    cache = {}
    for p in event.get('players', []):
        gid = p.get('google_id')
        handle = p.get('discord', '') or ''
        if gid:
            if gid not in cache:
                cache[gid] = get_user_profile(gid).get('discord', '')
            handle = cache[gid] or handle
        p['discord'] = handle if (handle and ' ' not in handle) else ''


def players_missing_discord_handle(event: dict) -> bool:
    """True if any non-dropped player registered via Discord (has a discord_id) but
    has no @handle recorded — the case backfill_discord_handles repairs."""
    return any(p.get('discord_id') and not p.get('discord')
               for p in event.get('players', []) if not p.get('dropped'))


def build_event_view(event: dict, current_user: dict | None = None) -> dict:
    """Return `event` enriched with computed standings, visibility rules, and
    redacted sensitive fields. Mutates and returns the passed-in dict (callers
    always pass the ephemeral result of get_event())."""
    e = event
    e['standings'] = compute_standings(e['players'], e['rounds'])
    _num_rounds = e.get('num_rounds') or default_num_rounds(len(e['players']))
    e['id_safe_ids'] = list(id_safe_players(e['players'], e['rounds'],
                                            _num_rounds, e.get('planned_cut_size') or 0))
    e['can_manage'] = _viewer_can_manage(e, current_user)
    owner_profile = get_user_profile(e.get('owner_id', ''))
    e['owner_name'] = owner_profile.get('name', '') or e.get('owner_id', '')
    e['owner_discord'] = owner_profile.get('discord', '')
    co_org_names = []
    for cid in e.get('co_organizer_ids', []):
        if cid.startswith('pending:'):
            co_org_names.append({'id': cid, 'name': cid[len('pending:'):], 'discord': '', 'pending': True})
        else:
            p = get_user_profile(cid)
            co_org_names.append({'id': cid, 'name': p.get('name', cid),
                                 'discord': p.get('discord', ''), 'pending': False})
    e['co_organizers'] = co_org_names
    e['entry_code_required'] = bool(e.get('entry_code'))
    enrich_players_discord(e)
    e['is_full'] = _is_full(e)
    active_wl = _active_waitlist(e)
    uid = current_user.get('id') if current_user else None
    mine = next((w for w in active_wl if w.get('google_id') == uid), None) if uid else None
    e['my_waitlist'] = ({'status': 'waitlisted',
                         'position': active_wl.index(mine) + 1,
                         'total': len(active_wl)} if mine else None)
    if e['can_manage']:
        e['waitlist_count'] = len(active_wl)
    else:
        e.pop('waitlist', None)
    if not e['can_manage']:
        e.pop('entry_code', None)
        if e.get('delay_pairings') and not e.get('pairings_released', True) and e['rounds']:
            e['pairings_hidden'] = True
            e['rounds'] = e['rounds'][:-1]
            e['id_safe_ids'] = []
        if e.get('delay_standings') and not e.get('standings_released', True):
            e['standings_hidden'] = True
            e['standings'] = []
    redact_players(e)
    return e
