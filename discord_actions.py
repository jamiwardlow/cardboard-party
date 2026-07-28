"""
Discord action layer — all mutations and queries triggered by Discord
interactions (slash commands, buttons, modals). No HTTP or Flask context:
every function takes plain arguments and returns plain data. Called by
routes/discord.py (Discord HTTP handler) and tested directly without a client.
"""
import datetime
import os
import threading
import time
import uuid

import discord_api
import event_queries
from db import (get_event, save_event, list_events, list_users,
                get_user_profile, save_user_profile, set_player_dropped,
                add_event_log, record_invite, recent_invite_count,
                target_invited_since, is_invite_opted_out)
from event_state import (_slugify, _assign_draft_seat, _is_full,
                         _self_registration_blocked, _now_iso, _validate_result)
from swiss import compute_standings, DRAW_RESULTS

COMMAND_NAME = os.environ.get('DISCORD_COMMAND_NAME', 'cparty')


def _registration_card_status(event: dict):
    """(state, note) for an announced event card — mirrors the self-registration
    gates so the posted card can show open / full / closed. state is
    'open' | 'full' | 'closed'."""
    blocked = _self_registration_blocked(event)
    if blocked:
        return 'closed', blocked
    if event.get('entry_code'):
        return 'closed', 'Entry code required — register on the web'
    cap = event.get('registration_cap', 0)
    active = len([p for p in event['players'] if not p.get('dropped')])
    if cap and active >= cap:
        return 'full', f'Full — {cap} players'
    return 'open', ''

def announce_event_to_channel(event_id: str, channel_id: str, base_url: str, message: str = '',
                              mention_role_id: str = None):
    """Post an event card (Register button + details link) to a channel and
    remember the message so its status can be kept current. `message` is an
    optional organiser note posted above the card; `mention_role_id` pings that
    role above the card. Returns (event_name, posted) — posted is False if the
    event is gone or the post failed (e.g. missing channel permission)."""
    e = get_event(event_id)
    if not e:
        return None, False
    base = (base_url or '').rstrip('/')
    state, note = _registration_card_status(e)
    embeds, components = discord_api.event_card(e, f'{base}/events/{event_id}', state, note)
    content = message or ''
    allowed = None
    if mention_role_id:
        content = (f'<@&{mention_role_id}> ' + content).rstrip()
        allowed = {'roles': [str(mention_role_id)]}
    msg = discord_api.post_message(channel_id, content=(content or None),
                                   components=components, embeds=embeds, allowed_mentions=allowed)
    if not msg:
        return e.get('name', 'the event'), False
    # Keep the note alongside the card so status refreshes (which re-send content)
    # don't wipe it.
    save_event(event_id, {'discord_announce': {
        'channel_id': channel_id, 'message_id': msg.get('id'), 'base_url': base,
        'message': content}})
    return e.get('name', 'the event'), True

def refresh_event_announcement(event: dict) -> None:
    """If this event has an announcement card posted, edit it to reflect the
    current registration status (open / full / closed). Best-effort no-op when
    there's no card. `event` must reflect the current players/registration."""
    ann = event.get('discord_announce') or {}
    if not ann.get('message_id'):
        return
    base = (ann.get('base_url') or '').rstrip('/')
    state, note = _registration_card_status(event)
    embeds, components = discord_api.event_card(
        event, f"{base}/events/{event['id']}", state, note)
    discord_api.edit_message(ann['channel_id'], ann['message_id'],
                             content=(ann.get('message') or None),
                             components=components, embeds=embeds)


def discord_registerable_events(limit: int = 25, owner_discord_id: str = None,
                                include_full: bool = False):
    """Events a Discord user can currently self-register for — open, not test,
    not invite-only/closed/expired, and with no entry code. Full events are excluded
    by default; pass `include_full=True` for announce/invite pickers. Soonest first.
    `owner_discord_id` restricts to that Discord user's own events."""
    owner_gid = _google_id_for_discord(owner_discord_id) if owner_discord_id else None
    return event_queries.registerable_for_discord(owner_gid=owner_gid,
                                                  include_full=include_full,
                                                  limit=limit)

def _normalize_handle(h: str) -> str:
    """Normalise a Discord handle for comparison: drop a leading @, lower-case,
    and drop a legacy '#1234' discriminator."""
    h = (h or '').strip().lstrip('@').lower()
    return h.split('#', 1)[0] if '#' in h else h

def _find_profile_for_discord(discord_id: str, username: str, display: str = '') -> dict | None:
    """Match a Discord user to an existing account: by a discord_id we've stored
    on the profile before (exact), else by the profile's saved Discord handle
    matching either the interaction's username or display name (people often save
    their display name as their handle). Returns the profile (with google_id) or
    None. Exact ID matches win over handle matches.

    A handle match also lets the caller store the numeric discord_id on the
    account (see register_player_via_discord), so subsequent links are exact and
    immune to the handle being a display name, edited, or a renamed username."""
    candidates = {h for h in (_normalize_handle(username), _normalize_handle(display)) if h}
    by_handle = None
    for u in list_users():
        if discord_id and u.get('discord_id') == discord_id:
            return u
        if candidates and not by_handle and _normalize_handle(u.get('discord')) in candidates:
            by_handle = u
    return by_handle

def register_player_via_discord(event_id: str, discord_id: str, discord_name: str,
                                discord_username: str = '', host_url: str = ''):
    """Register a Discord user as a player. When their Discord matches an existing
    account (by stored discord_id, or by the profile's Discord handle matching
    their username), the registration is linked to that account and uses its real
    name — otherwise a ghost player is created. Returns ({'player', 'event_name'},
    None) on success or (None, error_message)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    blocked = _self_registration_blocked(e)
    if blocked:
        return None, blocked
    if e.get('entry_code'):
        return None, 'This event needs an entry code — please register on the web.'
    cap = e.get('registration_cap', 0)
    active = [p for p in e['players'] if not p.get('dropped')]
    if cap and len(active) >= cap:
        return None, f'This event is full ({cap} players max).'
    # discord_name is the registrant's Discord display name; pass it as a second
    # handle candidate so an account that saved its display name as the handle
    # still links (and we then lock in the numeric ID below).
    profile = _find_profile_for_discord(discord_id, discord_username, discord_name)
    google_id = profile.get('google_id') if profile else None
    # Already in the event? If active, that's a no-op; if previously dropped,
    # re-activate their existing entry rather than refusing or duplicating them.
    existing = next((p for p in e['players']
                     if p.get('discord_id') == discord_id
                     or (google_id and p.get('google_id') == google_id)), None)
    if existing:
        if not existing.get('dropped'):
            return None, "You're already registered for this event."
        existing['dropped'] = False
        # Capture the verified @handle if we never recorded one (e.g. an entry made
        # before we started storing it, or a ghost re-activating).
        if discord_username and not existing.get('discord'):
            existing['discord'] = discord_username
        save_event(event_id, {'players': e['players']})
        if google_id and not (profile or {}).get('discord_id'):
            save_user_profile(google_id, {'discord_id': discord_id})
        refresh_event_announcement(e)
        _log_discord(event_id, existing.get('name', ''), 'register', 'registered via Discord')
        _dm_discord_reg_confirmation(discord_id, e, event_id, host_url)
        return {'player': existing, 'event_name': e.get('name', 'the event')}, None
    # Record the verified Discord @handle (username) so the organiser and other
    # players can see who registered — falling back to it when a matched account
    # has no handle saved, and using it directly for ghost (unlinked) players.
    if profile:
        name = (profile.get('name') or discord_name or 'Player').strip()[:80] or 'Player'
        discord_handle = profile.get('discord', '') or discord_username
    else:
        # No account yet — create a Discord-keyed profile (the same identity a
        # "Sign in with Discord" would produce) so this button-registrant has a real
        # cross-event profile and profile page even before they ever sign in. A later
        # Discord login resolves to this same account, linking their history.
        name = (discord_name or 'Player').strip()[:80] or 'Player'
        discord_handle = discord_username
        google_id = f'discord:{discord_id}'
        save_user_profile(google_id, {'discord_id': str(discord_id),
                                      'name': name, 'discord': discord_handle})
    player = {
        'id':         _slugify(name) + '_' + uuid.uuid4().hex[:8],
        'name':       name,
        'google_id':  google_id,
        'discord_id': discord_id,
        'discord':    discord_handle,
        'dropped':    False,
        'checked_in': False,
    }
    _assign_draft_seat(player, e)
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    # Remember the Discord ID on a matched (handle-linked) account so future links
    # are exact. The no-account case already saved its profile with the ID above.
    if profile and not profile.get('discord_id'):
        save_user_profile(google_id, {'discord_id': discord_id})
    refresh_event_announcement(e)   # may have just hit the cap → show "full"
    _log_discord(event_id, name, 'register', 'registered via Discord')
    _dm_discord_reg_confirmation(discord_id, e, event_id, host_url)
    return {'player': player, 'event_name': e.get('name', 'the event')}, None


def withdraw_player_via_discord(event_id: str, discord_id: str, username='', display=''):
    """Withdraw a Discord-registered player from an event (the toggle counterpart
    to register_player_via_discord). Mirrors the web withdraw: drop if rounds have
    started, else remove the entry. Returns (event_name, None) or (None, error)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    gid, handles = _discord_identity(discord_id, username, display)
    player = next((p for p in e['players']
                   if not p.get('dropped') and
                      (p.get('discord_id') == discord_id
                       or (gid and p.get('google_id') == gid)
                       or (handles and _normalize_handle(p.get('discord')) in handles))),
                  None)
    if not player:
        return None, "You're not registered for this event."
    if not e.get('self_service_drop_enabled', True):
        return None, 'Self-service drops are disabled for this event — contact the organiser.'
    unenroll_end = e.get('unenroll_end')
    if unenroll_end and datetime.date.today().isoformat() > unenroll_end:
        return None, 'The drop deadline has passed — contact the organiser.'
    if e['rounds']:
        set_player_dropped(event_id, player['id'], True)
    else:
        e['players'] = [p for p in e['players'] if p['id'] != player['id']]
        save_event(event_id, {'players': e['players']})
    refresh_event_announcement(get_event(event_id))   # a slot may have freed up
    _log_discord(event_id, player.get('name', ''), 'drop', 'dropped via Discord')
    return e.get('name', 'the event'), None


def discord_droppable_events(discord_id: str, username: str = '', display: str = '', limit: int = 25):
    """Events a Discord user is currently registered for (active, not dropped) — so
    they can drop themselves from the bot, including ghost players who registered
    via a button without an account. Soonest first; capped for the select menu.
    Gating (self-service allowed, deadline) is enforced on the drop itself, so the
    menu shows everything they're in and the action explains any refusal."""
    gid, handles = _discord_identity(discord_id, username, display)
    out = []
    for e in list_events():
        registered = any(
            not p.get('dropped') and
            (p.get('discord_id') == discord_id
             or (gid and p.get('google_id') == gid)
             or (handles and _normalize_handle(p.get('discord')) in handles))
            for p in e['players'])
        if registered:
            out.append(e)
    out.sort(key=lambda e: e.get('date', ''))
    return out[:limit]


def backfill_discord_profiles():
    """One-off migration: give button-registered ghost players (a discord_id but no
    account) a real `discord:<id>` profile and link their event entries to it —
    matching an existing account first (by stored discord_id or handle) so we don't
    duplicate. Firestore-only, idempotent. Returns a summary dict."""
    users = list_users()
    by_did    = {str(u['discord_id']): u['google_id'] for u in users if u.get('discord_id')}
    by_handle = {_normalize_handle(u['discord']): u['google_id'] for u in users if u.get('discord')}
    linked = profiles_created = 0
    for e in list_events():
        changed = False
        for p in e.get('players', []):
            did = p.get('discord_id')
            if not did or p.get('google_id'):
                continue                      # not a Discord ghost / already linked
            did = str(did)
            gid = by_did.get(did) or by_handle.get(_normalize_handle(p.get('discord', '')))
            if not gid:
                gid = f'discord:{did}'
                save_user_profile(gid, {'discord_id': did,
                                        'name': (p.get('name') or 'Player')[:80],
                                        'discord': p.get('discord', '')})
                by_did[did] = gid
                if p.get('discord'):
                    by_handle[_normalize_handle(p['discord'])] = gid
                profiles_created += 1
            p['google_id'] = gid
            changed = True
            linked += 1
        if changed:
            save_event(e['id'], {'players': e['players']})
    return {'linked_players': linked, 'profiles_created': profiles_created}

def fix_discord_handles():
    """Correct legacy `discord:<id>` profiles whose handle is a stale free-text value
    (from the old editable field, often a display name) — reset it to the @handle
    captured on their player entries at registration. Firestore-only, idempotent."""
    handle_by_gid = {}
    for e in list_events():
        for p in e.get('players', []):
            gid = p.get('google_id')
            if gid and str(gid).startswith('discord:') and p.get('discord'):
                handle_by_gid.setdefault(gid, p['discord'])
    fixed = 0
    for gid, handle in handle_by_gid.items():
        prof = get_user_profile(gid)
        if prof.get('discord_id') and prof.get('discord') != handle:
            save_user_profile(gid, {'discord': handle})
            fixed += 1
    return {'fixed': fixed}

def _log_discord(event_id: str, actor_name: str, action: str, detail: str = ''):
    """Event-log entry for a Discord-driven action (no web session to attribute)."""
    add_event_log(event_id, {'at': _now_iso(), 'action': action, 'detail': detail,
                             'actor_id': '', 'actor_name': actor_name or 'A Discord user'})

def _dm_discord_reg_confirmation(discord_id: str, event: dict, event_id: str, host_url: str = ''):
    """Fire a Discord DM confirmation for a Discord-button registration."""
    event_url = (host_url or '').rstrip('/') + f'/events/{event_id}'
    threading.Thread(
        target=discord_api.dm_registration_confirmation,
        args=(discord_id, event, event_url),
        daemon=True,
    ).start()

def waitlist_player_via_discord(event_id: str, discord_id: str, discord_name: str,
                                discord_username: str = ''):
    """Add a Discord user to a full event's waitlist (the Join Waitlist button on a
    card/invite DM). Links to an existing account when the Discord matches one.
    Returns ({'event_name','position'}, None) or (None, error_message)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    blocked = _self_registration_blocked(e)
    if blocked:
        return None, blocked
    if e.get('entry_code'):
        return None, 'This event needs an entry code — please register on the web.'
    if not _is_full(e):
        return None, 'This event has open spots — tap Register instead.'
    profile = _find_profile_for_discord(discord_id, discord_username, discord_name)
    gid = profile.get('google_id') if profile else None
    if any((p.get('discord_id') == discord_id or (gid and p.get('google_id') == gid))
           and not p.get('dropped') for p in e['players']):
        return None, "You're already registered for this event."
    waitlist = e.get('waitlist') or []
    if any(w.get('status') == 'waitlisted' and
           (w.get('discord_id') == discord_id or (gid and w.get('google_id') == gid))
           for w in waitlist):
        return None, "You're already on the waitlist."
    name = ((profile.get('name') if profile else discord_name) or 'Player').strip()[:80] or 'Player'
    record = {
        'id':         uuid.uuid4().hex,
        'google_id':  gid,
        'name':       name,
        'email':      profile.get('email', '') if profile else '',
        'discord':    (profile.get('discord', '') if profile else '') or discord_username,
        'discord_id': discord_id,
        'status':     'waitlisted',
        'joined_at':  _now_iso(),
    }
    waitlist.append(record)
    save_event(event_id, {'waitlist': waitlist})
    _log_discord(event_id, name, 'waitlist_join', f"{name} joined the waitlist via Discord")
    position = sum(1 for w in waitlist if w.get('status') == 'waitlisted')
    return {'event_name': e.get('name', 'the event'), 'position': position}, None

def waitlist_leave_via_discord(event_id: str, discord_id: str, username: str = '', display: str = ''):
    """Remove a Discord user from a waitlist (the Leave Waitlist toggle on a DM).
    Returns (event_name, None) or (None, error_message)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    gid, handles = _discord_identity(discord_id, username, display)
    waitlist = e.get('waitlist') or []
    rec = next((w for w in waitlist if w.get('status') == 'waitlisted' and
                (w.get('discord_id') == discord_id or (gid and w.get('google_id') == gid))), None)
    if not rec:
        return None, "You're not on the waitlist for this event."
    rec['status'] = 'removed_by_self'
    rec['removed_at'] = _now_iso()
    save_event(event_id, {'waitlist': waitlist})
    _log_discord(event_id, rec.get('name', ''), 'waitlist_leave',
                 f"{rec.get('name', 'A player')} left the waitlist via Discord")
    return e.get('name', 'the event'), None


# Anti-spam limits for Discord event invites. Anyone may invite anyone, so these
# keep one person from blasting invites and keep a recipient from being pestered
# about the same event repeatedly.
INVITE_RATE_LIMIT = 10              # max invites one sender may send per window
INVITE_RATE_WINDOW = 60 * 60       # ...within this many seconds (1 hour)
INVITE_DEDUPE_WINDOW = 7 * 24 * 60 * 60   # don't re-invite a target to the same event within 7 days

def invite_player_via_discord(event_id: str, inviter_id: str, target_id: str,
                              inviter_name: str, base_url: str = '', message: str = '',
                              inviter_username: str = ''):
    """DM `target_id` an invitation to register for an event, on behalf of
    `inviter_id`, with an optional personal `message`. Enforces the anti-spam
    guards (opt-out, already-registered, dedupe, sender rate limit) before
    sending. Returns (confirmation, None) on success or (None, error_message)."""
    if str(target_id) == str(inviter_id):
        return None, f"You can register yourself with `/{COMMAND_NAME} register` — no invite needed."
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    blocked = _self_registration_blocked(e)
    if blocked:
        return None, blocked
    if e.get('entry_code'):
        return None, 'This event needs an entry code, so it cannot be invited to from Discord.'
    # A full event isn't blocked — the invitation card offers Join Waitlist instead.

    # Recipient opted out of invites entirely.
    if is_invite_opted_out(target_id):
        return None, 'That person has chosen not to receive event invites.'

    # Already registered (by Discord ID or a linked account)?
    gid = _google_id_for_discord(target_id)
    if any(p.get('discord_id') == target_id for p in e['players']) or \
       (gid and any(p.get('google_id') == gid for p in e['players'])):
        return None, "They're already registered for this event."

    now = time.time()
    # Don't pester the same person about the same event repeatedly.
    if target_invited_since(target_id, event_id, now - INVITE_DEDUPE_WINDOW):
        return None, "They've already been invited to this event recently."
    # Rate-limit the sender.
    if recent_invite_count(inviter_id, now - INVITE_RATE_WINDOW) >= INVITE_RATE_LIMIT:
        return None, ("You've sent a lot of invites recently — please wait a bit "
                      "before sending more.")

    base = (base_url or '').rstrip('/')
    state, note = _registration_card_status(e)
    delivered = discord_api.dm_event_invite(
        target_id, e, f'{base}/events/{event_id}', inviter_name, state, note, message,
        inviter_username=inviter_username)
    if not delivered:
        return None, ("I couldn't DM them — they may not share a server with the bot "
                      "or have DMs from server members turned off.")
    record_invite(inviter_id, target_id, event_id, now)
    return f"Invitation sent for **{e.get('name', 'the event')}**.", None


def _google_id_for_discord(discord_id: str):
    """The Google account (if any) linked to a Discord numeric ID — so we can also
    match players who registered on the web/were added by an organiser but have a
    linked Discord. Returns the google_id or None."""
    prof = _find_profile_for_discord(discord_id, '')   # '' = match by discord_id only
    return prof.get('google_id') if prof else None

def _discord_identity(discord_id: str, username: str = '', display: str = ''):
    """Resolve a Discord user to what we match players on: their linked google_id
    (by a stored numeric discord_id, or by the profile's saved handle matching the
    interaction's verified username/display) and the set of normalised handle
    candidates. Passing the username/display lets players who only have a Discord
    *handle* on file — registered on the web or added by the organiser, never
    linked by numeric ID — still be matched (e.g. for /report)."""
    prof = _find_profile_for_discord(discord_id, username, display)
    gid = prof.get('google_id') if prof else None
    handles = {h for h in (_normalize_handle(username), _normalize_handle(display)) if h}
    return gid, handles

def _discord_match_ctx(e: dict, round_idx: int, match_idx: int, discord_id: str,
                       gid=None, handles=None):
    """Context for a Discord user's match in event `e`, or None if it's not their
    open (unreported, non-bye) match. Shared by the report picker and reporting.
    `gid` is the Google account linked to this Discord user (if known) and
    `handles` the verified handle candidates, so a web/organiser-added player who's
    linked their Discord OR just has a matching handle on file is also matched."""
    rounds = e.get('rounds', [])
    if not (0 <= round_idx < len(rounds)):
        return None
    rnd = rounds[round_idx]
    if not (0 <= match_idx < len(rnd)):
        return None
    m = rnd[match_idx]
    player = next((p for p in e['players']
                   if not p.get('dropped') and
                      (p.get('discord_id') == discord_id
                       or (gid and p.get('google_id') == gid)
                       or (handles and _normalize_handle(p.get('discord')) in handles))),
                  None)
    if not player or m.get('is_bye'):
        return None
    if player['id'] not in (m.get('player1_id'), m.get('player2_id')):
        return None
    if m.get('winner_id') or m.get('result') in DRAW_RESULTS:
        return None
    is_p1 = player['id'] == m.get('player1_id')
    opp_id = m['player2_id'] if is_p1 else m['player1_id']
    opp = next((p for p in e['players'] if p['id'] == opp_id), None)
    return {
        'event_id': e['id'], 'event_name': e.get('name', 'Event'),
        'round_idx': round_idx, 'match_idx': match_idx, 'round_num': round_idx + 1,
        'player_id': player['id'], 'opp_id': opp_id,
        'opponent': opp['name'] if opp else 'Opponent', 'is_p1': is_p1,
        'allow_id': bool(e.get('advanced')) and not e.get('intentional_draws_frowned'),
    }

def discord_open_matches(discord_id: str, username: str = '', display: str = '', limit: int = 25):
    """A Discord user's current open matches (latest round of each event they're
    in), for the /cbp report picker."""
    out = []
    gid, handles = _discord_identity(discord_id, username, display)
    for e in list_events():
        ridx = len(e.get('rounds', [])) - 1
        if ridx < 0:
            continue
        for midx in range(len(e['rounds'][ridx])):
            ctx = _discord_match_ctx(e, ridx, midx, discord_id, gid, handles)
            if ctx:
                out.append(ctx)
                break
    return out[:limit]

def discord_match_context(event_id: str, round_idx: int, match_idx: int, discord_id: str,
                          username: str = '', display: str = ''):
    e = get_event(event_id)
    if not e:
        return None
    gid, handles = _discord_identity(discord_id, username, display)
    return _discord_match_ctx(e, round_idx, match_idx, discord_id, gid, handles)

# Map a reporter-perspective result code to a stored result + summary.
_DISCORD_RESULT_CODES = {
    'w20': ('win',  2, 0), 'w21': ('win',  2, 1),
    'l02': ('lose', 0, 2), 'l12': ('lose', 1, 2),
    'draw': ('draw', None, None), 'id': ('id', None, None),
}

def report_result_via_discord(event_id, round_idx, match_idx, discord_id, code, base_url='',
                              username='', display=''):
    """Record a result a Discord player reports for their own match. `code` is
    from the reporter's perspective (see _DISCORD_RESULT_CODES). Returns
    (confirmation, None) or (None, error)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    gid, handles = _discord_identity(discord_id, username, display)
    ctx = _discord_match_ctx(e, round_idx, match_idx, discord_id, gid, handles)
    if not ctx:
        return None, "That doesn't look like an open match of yours anymore."
    spec = _DISCORD_RESULT_CODES.get(code)
    if not spec:
        return None, 'Unknown result.'
    kind, mine, theirs = spec
    if kind == 'draw':
        winner_id, result, summary = None, 'draw', 'a draw (1–1)'
    elif kind == 'id':
        if not ctx['allow_id']:
            return None, 'Intentional draws are not allowed for this event.'
        winner_id, result, summary = None, '0-0-3', 'an intentional draw (0–0–3)'
    else:
        winner_id = ctx['player_id'] if kind == 'win' else ctx['opp_id']
        # Stored result is from player1's perspective.
        a, b = (mine, theirs) if ctx['is_p1'] else (theirs, mine)
        result = f'{a}-{b}'
        summary = f"you {'won' if kind == 'win' else 'lost'} {max(mine, theirs)}–{min(mine, theirs)}"
    err = _validate_result(e['rounds'][round_idx][match_idx], winner_id, result)
    if err:
        return None, err
    m = e['rounds'][round_idx][match_idx]
    m['winner_id'] = winner_id
    m['result'] = result
    save_event(event_id, {'rounds': e['rounds']})
    # Turn the opponent's pairing DM into the result card (same as the web path).
    # The reporter is excluded — their own message is updated by the interaction
    # response (see routes/discord.py), so we don't fight that edit.
    discord_api.notify_result(e, round_idx, match_idx, base_url,
                              exclude_player_id=ctx['player_id'])
    names = {p['id']: p['name'] for p in e['players']}
    reporter = names.get(ctx['player_id'], display or 'A player')
    _log_discord(event_id, reporter, 'result',
                 f"reported round {round_idx + 1} vs {ctx['opponent']} ({result}) via Discord")
    return f"Recorded — {summary} vs {ctx['opponent']} ({ctx['event_name']}). GGs!", None


def discord_linkable_events(limit: int = 25, owner_discord_id: str = None):
    """Non-test events an organiser might link to a Discord channel (for the
    /cbp link picker), restricted to events that Discord user owns. Most recent first."""
    owner_gid = _google_id_for_discord(owner_discord_id) if owner_discord_id else None
    return event_queries.linkable_for_discord(owner_gid=owner_gid, limit=limit)

def set_event_discord_channel(event_id: str, channel_id: str):
    """Link an event so its pairings auto-post to this Discord channel. Returns
    the event name, or None if it's gone."""
    e = get_event(event_id)
    if not e:
        return None
    save_event(event_id, {'discord_channel_id': channel_id})
    return e.get('name', 'the event')

def discord_standings_events(limit: int = 25):
    """Non-test events that have started (have standings to show)."""
    return event_queries.with_standings(limit=limit)

def discord_standings_text(event_id: str, top: int = 16):
    """Formatted standings for a Discord message, or None if the event is gone."""
    e = get_event(event_id)
    if not e:
        return None
    standings = compute_standings(e['players'], e['rounds'])
    name = e.get('name', 'Event')
    if not standings:
        return f"**{name}** — no standings yet."
    lines = [f"**{name}** — standings"]
    for i, s in enumerate(standings[:top], 1):
        tag = ' _(dropped)_' if s.get('dropped') else ''
        lines.append(f"{i}. {s['name']} — {s['points']} pts{tag}")
    if len(standings) > top:
        lines.append(f"…and {len(standings) - top} more")
    return '\n'.join(lines)
