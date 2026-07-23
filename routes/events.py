"""
Event routes.

Permission model:
  can_manage(event) = is_admin(user) OR user is event owner
  Players can register/unregister themselves and report their own results.
  Anyone can view events and standings.
"""

from flask import Blueprint, request, jsonify, render_template, abort, session
from db import (create_event, get_event, save_event, list_events, delete_event,
                set_player_dropped, set_player_field,
                get_admins, is_admin, add_admin, remove_admin,
                get_user_profile, save_user_profile, delete_user_profile, list_users,
                find_user_by_email, find_user_by_discord_handle,
                record_invite, recent_invite_count, target_invited_since,
                set_invite_optout, is_invite_opted_out,
                add_event_log, list_event_log, promote_waitlist_entry)
from swiss import (pair_round, compute_standings, default_num_rounds, BYE_PLAYER_ID,
                   make_bracket, next_bracket_round, CUT_SIZES, DRAW_RESULTS, id_safe_players,
                   assign_tables)
from routes.auth import get_current_user, login_required, discord_login_enabled
from gcp_secrets import get_secret
import discord_api
from decklist import parse_decklist, validate_decklist, import_moxfield, VALIDATION_FORMATS
from storage import upload_avatar, upload_brand_image, delete_object
import datetime
import os
import re
import requests
import threading
import time
import uuid
from urllib.parse import urlparse

events_bp = Blueprint('events', __name__)

# Per-environment slash-command name (see routes/discord.py); 'cparty' in prod,
# 'cpstaging' on staging. Used in user-facing text and the /discord-bot page.
COMMAND_NAME = os.environ.get('DISCORD_COMMAND_NAME', 'cparty')

# Advanced-creation vocabularies. Kept server-side so the stored values are
# validated against an allow-list rather than trusting whatever the client sends.
TOURNAMENT_TAGS = ['Weekly Play', 'Prerelease', 'Regional Championship Qualifier',
                   'Spotlight Series']
STRUCTURES = ['swiss', 'swiss_top_cut', 'custom']


def _clean_tags(raw) -> list:
    """Keep only recognised tags, preserving the canonical order."""
    chosen = set(raw or [])
    return [t for t in TOURNAMENT_TAGS if t in chosen]

# Plain-text (not rich-text) player-communication fields. Stored verbatim and
# rendered with white-space:pre-wrap + autoescape, so they're safe by default —
# no HTML, no sanitizer needed. Capped to keep documents reasonable.
COMMS_FIELDS = ('rules', 'schedule', 'prizes', 'contact')
_COMMS_MAX = 5000

def _clean_comms(data: dict) -> dict:
    out = {}
    for f in COMMS_FIELDS:
        if f in data:
            out[f] = str(data.get(f) or '')[:_COMMS_MAX]
    return out

# ── Table assignments ────────────────────────────────────────────────────────
# Tables live on the event: a numeric range [table_start..table_end] minus any
# reserved/unavailable numbers (tables_excluded), with optional labels
# (table_labels, e.g. "Feature Match"). The client sends already-parsed
# structures; these normalise and bound-check them.
_MAX_TABLE = 999

def _clean_table_list(raw) -> list:
    """Sorted, de-duplicated, bounded list of table numbers (reserved/unavailable)."""
    out = set()
    for v in (raw or []):
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= _MAX_TABLE:
            out.add(n)
    return sorted(out)

def _clean_table_labels(raw) -> dict:
    """{str(table_number): label} map, numeric keys validated, labels trimmed/capped."""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                continue
            label = str(v or '').strip()[:40]
            if 1 <= n <= _MAX_TABLE and label:
                out[str(n)] = label
    return out

def _bounded_table(v, lo):
    """An int table number within [lo, _MAX_TABLE], else the floor `lo`."""
    return v if isinstance(v, int) and lo <= v <= _MAX_TABLE else lo

def _coord(v, lo: float, hi: float):
    """A float latitude/longitude within [lo, hi], else None (no/invalid coordinate).
    Captured from a Google Places selection; used for the 'events near me' filter."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None

REGISTRATION_TYPES = ('open', 'invite_only')
PROXY_POLICIES = ('unlimited', 'limited', 'custom')   # event proxy policy modes

def _self_registration_blocked(event: dict) -> str | None:
    """Why a player can't self-register right now, or None if they can. Covers the
    manual open/closed toggle, invite-only type, and the scheduled date window.
    (Organisers adding players bypass this — it gates self-service only.)"""
    if event.get('registration') != 'open':
        return 'Registration is closed'
    if event.get('registration_type') == 'invite_only':
        return 'This event is by invitation — contact the organiser to be added'
    today = datetime.date.today().isoformat()
    start = event.get('registration_start')
    end   = event.get('registration_end')
    if start and today < start:
        return f'Registration opens on {start}'
    if end and today > end:
        return f'Registration closed on {end}'
    return None

def _active_count(event: dict) -> int:
    """Number of active (non-dropped) participants — what counts against the cap."""
    return len([p for p in event.get('players', []) if not p.get('dropped')])

def _is_full(event: dict) -> bool:
    """True when a cap is set and active participants have reached it."""
    cap = event.get('registration_cap', 0)
    return bool(cap) and _active_count(event) >= cap

def _active_waitlist(event: dict) -> list[dict]:
    """Still-waiting waitlist records, oldest first (first come, first served)."""
    wl = [w for w in (event.get('waitlist') or []) if w.get('status') == 'waitlisted']
    wl.sort(key=lambda w: w.get('joined_at', ''))
    return wl

def _log_action(event_id: str, action: str, detail: str = '', target: str = '', actor_name: str = None):
    """Write an event-log entry. Stamps the current signed-in user as the actor,
    unless `actor_name` is given (for guest/self-report flows with no web session)."""
    u = get_current_user() or {}
    add_event_log(event_id, {
        'at': _now_iso(), 'action': action, 'detail': detail, 'target': target,
        'actor_id': u.get('id', ''), 'actor_name': actor_name or u.get('name', '') or 'Someone'})

def _entry_code_error(event: dict, data: dict) -> str | None:
    """If the event requires an entry code, check the supplied one. None if OK."""
    code = event.get('entry_code')
    if code and str((data or {}).get('entry_code') or '').strip() != code:
        return 'Incorrect entry code'
    return None


# ── Discord registration (used by the Discord bot, routes/discord.py) ──────────

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
    not invite-only/closed/expired, and with no entry code (codes aren't handled
    in the Discord flow yet). Full events are excluded by default; pass
    `include_full=True` for the announce/invite pickers, where a full event is
    still valid (its card offers the waitlist). Soonest first; capped for the
    select menu. `owner_discord_id` restricts to that Discord user's own events."""
    owner_gid = _google_id_for_discord(owner_discord_id) if owner_discord_id else None
    out = []
    for e in list_events():
        if owner_discord_id and e.get('owner_id') != owner_gid:
            continue
        if e.get('test_mode') or e.get('entry_code'):
            continue
        if _self_registration_blocked(e):
            continue
        if not include_full and _is_full(e):
            continue
        out.append(e)
    out.sort(key=lambda e: e.get('date', ''))
    return out[:limit]

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
                                discord_username: str = ''):
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
        _dm_discord_reg_confirmation(discord_id, e, event_id)
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
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    # Remember the Discord ID on a matched (handle-linked) account so future links
    # are exact. The no-account case already saved its profile with the ID above.
    if profile and not profile.get('discord_id'):
        save_user_profile(google_id, {'discord_id': discord_id})
    refresh_event_announcement(e)   # may have just hit the cap → show "full"
    _log_discord(event_id, name, 'register', 'registered via Discord')
    _dm_discord_reg_confirmation(discord_id, e, event_id)
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

def _dm_registration_confirmation(google_id: str, event: dict, event_id: str, host_url: str):
    """Fire a Discord DM confirmation in a background thread if the user has a linked Discord ID."""
    did = get_user_profile(google_id).get('discord_id')
    if not did:
        return
    event_url = host_url.rstrip('/') + f'/events/{event_id}'
    threading.Thread(
        target=discord_api.dm_registration_confirmation,
        args=(did, event, event_url),
        daemon=True,
    ).start()


def _dm_discord_reg_confirmation(discord_id: str, event: dict, event_id: str):
    """Fire a Discord DM confirmation for a Discord-button registration. discord_id is already known."""
    event_url = request.host_url.rstrip('/') + f'/events/{event_id}'
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
    out = [e for e in list_events()
           if not e.get('test_mode')
           and (not owner_discord_id or e.get('owner_id') == owner_gid)]
    out.sort(key=lambda e: e.get('date', ''), reverse=True)
    return out[:limit]

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
    out = [e for e in list_events() if e.get('rounds') and not e.get('test_mode')]
    out.sort(key=lambda e: e.get('date', ''), reverse=True)
    return out[:limit]

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


def _normalize_payment_url(raw) -> tuple:
    """Validate/normalize a payment link so it's safe to render as a clickable
    <a href>. Returns (url, error): an empty string for no link, an http(s)
    URL (a missing scheme is assumed https), or (None, msg) if it's not a
    valid web URL — which blocks javascript:/data: and other unsafe schemes.
    """
    url = (raw or '').strip()
    if not url:
        return '', None
    if not urlparse(url).scheme:
        url = 'https://' + url
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None, 'Payment link must be a valid http(s) URL'
    return url, None


# ── Permission helpers ─────────────────────────────────────────────────────────

def _can_manage(event: dict) -> bool:
    """True if the current user is a global admin, the event owner, or a co-organizer."""
    user = get_current_user()
    if not user:
        return False
    uid = user['id']
    return (uid == event.get('owner_id')
            or is_admin(uid)
            or uid in event.get('co_organizer_ids', []))

def _require_manage(event: dict):
    if not _can_manage(event):
        abort(403)

def _find_player_by_google_id(event: dict, google_id: str) -> dict | None:
    return next((p for p in event['players']
                 if p.get('google_id') == google_id), None)

def _find_player_by_guest_token(event: dict, token: str) -> dict | None:
    if not token:
        return None
    return next((p for p in event['players']
                 if p.get('guest_token') and p['guest_token'] == token), None)

def _current_participant(event: dict):
    """The player entry for whoever is making the request — a signed-in Google
    player, or a guest holding their self-report token (X-Guest-Token header,
    `guest_token` in the JSON body, or `t` query param). None if neither."""
    user = get_current_user()
    if user:
        p = _find_player_by_google_id(event, user['id'])
        if p:
            return p
    token = (request.headers.get('X-Guest-Token')
             or (request.get_json(silent=True) or {}).get('guest_token')
             or request.args.get('t'))
    return _find_player_by_guest_token(event, token)

def _decklist_locked(event: dict) -> bool:
    """True once the decklist submission deadline has passed (edits read-only)."""
    dd = (event.get('decklist_deadline') or '').strip()
    return bool(dd and datetime.date.today().isoformat() > dd)


_PRINT_POLICY_KEYS = ('allow_proxies', 'allow_gold_border', 'allow_ce', 'allow_ie',
                      'proxy_policy', 'proxy_limit')

def _print_policy(event: dict) -> dict:
    """The event's print-legality policy passed to validate_decklist (tag checks)."""
    return {k: event.get(k) for k in _PRINT_POLICY_KEYS}

def _redact_players(event: dict) -> None:
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
        # Validation status for the organiser roster badge (None when no list yet):
        # 'valid' | 'warnings' | 'errors' | 'unchecked' | 'none'.
        p['decklist_status'] = ((dl.get('validation') or {}).get('status', 'none')
                                if p['has_decklist'] else None)
        p.pop('decklist', None)

def _enrich_players_discord(event: dict) -> None:
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


def _players_missing_discord_handle(event: dict) -> bool:
    """True if any non-dropped player registered via Discord (has a discord_id) but
    has no @handle recorded — the case backfill_discord_handles repairs."""
    return any(p.get('discord_id') and not p.get('discord')
               for p in event.get('players', []) if not p.get('dropped'))


def backfill_discord_handles(event_id: str) -> None:
    """Look up the @handle of players who registered via the bot before we captured
    it (a discord_id on file but no `discord`) and store it, so it shows on the
    event page. Best-effort, idempotent; intended to run in a background thread."""
    e = get_event(event_id)
    if not e:
        return
    changed = False
    for p in e.get('players', []):
        if p.get('discord') or not p.get('discord_id'):
            continue
        handle = (discord_api.get_user(p['discord_id']) or {}).get('username', '')
        if handle:
            p['discord'] = handle
            changed = True
    if changed:
        save_event(event_id, {'players': e['players']})

def _can_report_match(event: dict, match: dict, data: dict) -> bool:
    """Who may report a match result: a manager, the registered Google player
    in the match, or a guest holding that player's self-report token."""
    if _can_manage(event):
        return True
    sides = (match.get('player1_id'), match.get('player2_id'))
    user = get_current_user()
    if user:
        gp = _find_player_by_google_id(event, user['id'])
        if gp and gp['id'] in sides:
            return True
    token = request.headers.get('X-Guest-Token') or (data or {}).get('guest_token')
    gp = _find_player_by_guest_token(event, token)
    return bool(gp and gp['id'] in sides)

def _slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

_RESULT_RE = re.compile(r'^(\d+)-(\d+)$')

def _validate_result(match: dict, winner_id, result) -> str | None:
    """
    Validate a reported result against a match. Returns an error string, or None
    if valid. Enforces that the winner matches the score (result is recorded from
    player1's perspective, i.e. 'p1games-p2games'), so a player can't report a
    score for one player while crediting the win to the other.
    """
    p1, p2 = match.get('player1_id'), match.get('player2_id')
    if result in DRAW_RESULTS:
        return None if winner_id is None else 'A draw cannot have a winner'
    m = _RESULT_RE.match(str(result or ''))
    if not m:
        return 'Invalid result format'
    a, b = int(m.group(1)), int(m.group(2))
    if a == b:
        return 'Use a draw for an equal score'
    expected = p1 if a > b else p2
    if winner_id != expected:
        return 'Winner does not match the score'
    return None

def _now_iso() -> str:
    """Current UTC time as an ISO string (used to stamp round-timer starts)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _new_round_updates(event: dict) -> dict:
    """Fields to set whenever a new round is created: reset the round timer and,
    where delivery is delayed, re-hide pairings/standings until the organiser
    releases them.

    The timer normally waits for the organiser's Start-timer button. With the
    opt-in `auto_start_timer`, it instead starts the moment pairings go live: now
    if they post immediately, or on release if delayed (see api_release_delivery),
    so the clock never runs while players can't yet see their pairings."""
    u = {'round_started_at': ''}
    if event.get('delay_pairings'):
        u['pairings_released'] = False
    if event.get('delay_standings'):
        u['standings_released'] = False
    if (event.get('auto_start_timer') and event.get('round_timer_minutes')
            and not event.get('delay_pairings')):
        u['round_started_at'] = _now_iso()
    return u


def _deliver_pairings(event: dict, round_num: int, base_url: str):
    """Announce a round's pairings to the linked Discord channel and DM each player.
    Withheld when 'Delay pairings' is on — a freshly paired round is then unreleased,
    so delivery waits until the organiser releases it (see api_release_delivery),
    keeping Discord in step with the web view that already hides delayed pairings."""
    if event.get('delay_pairings'):
        return
    discord_api.announce_round(event, round_num, base_url)
    discord_api.dm_round_pairings(event, round_num, base_url)

def _is_bracket_round(rnd: list) -> bool:
    """A round belongs to the single-elimination playoff if its matches are tagged."""
    return bool(rnd) and rnd[0].get('stage') == 'bracket'

def _swiss_complete(event: dict) -> bool:
    """True once every Swiss round has been paired and fully scored (a playoff
    can only start after this)."""
    swiss = [r for r in event['rounds'] if not _is_bracket_round(r)]
    num_rounds = event.get('num_rounds') or default_num_rounds(len(event['players']))
    if len(swiss) < num_rounds:
        return False
    last = swiss[-1] if swiss else []
    return all(m.get('is_bye') or m.get('winner_id') or m.get('result') in DRAW_RESULTS
               for m in last)

def _event_complete(event: dict) -> bool:
    """True once the event is finished — a decided playoff final, an explicit
    'finished' status, or all Swiss rounds paired AND fully scored. Mirrors the
    event page's stage logic so the Events-page card doesn't call an event with an
    unscored final round 'Completed'."""
    rounds = event.get('rounds') or []
    if not rounds:
        return False
    last = rounds[-1]
    if _is_bracket_round(last):
        return len(last) == 1 and bool(last[0].get('winner_id'))
    if event.get('status') == 'finished':
        return True
    return _swiss_complete(event)


# ── Pages ──────────────────────────────────────────────────────────────────────

@events_bp.route('/')
def index():
    return render_template('index.html', user=get_current_user())


_decklists_nav_cache = {'v': None, 'at': 0}

def has_public_decklists() -> bool:
    """Return True if the public decklist browser has anything to show.
    Result is cached for 5 minutes so the check doesn't scan events on every request."""
    now = time.time()
    if now - _decklists_nav_cache['at'] < 300:
        return _decklists_nav_cache['v']
    found = False
    for e in list_events():
        if not e.get('requires_decklists') or not _event_complete(e):
            continue
        if e.get('closed_decklists') and not e.get('decklists_released'):
            continue
        if any((p.get('decklist') or {}).get('text', '').strip() for p in e.get('players', [])):
            found = True
            break
    _decklists_nav_cache['v'] = found
    _decklists_nav_cache['at'] = now
    return found


@events_bp.route('/decklists')
def public_decklists_page():
    return render_template('decklists_browse.html', user=get_current_user())


@events_bp.route('/api/decklists')
def api_public_decklists():
    """Public: flat list of all deck summaries from completed events with decklists."""
    decks = []
    for e in list_events():
        if not e.get('requires_decklists') or not _event_complete(e):
            continue
        if e.get('closed_decklists') and not e.get('decklists_released'):
            continue
        event_rounds = e.get('rounds', [])
        standings = compute_standings(e['players'], event_rounds)
        player_map = {p['id']: p for p in e['players']}
        rank = 0
        for s in standings:
            p = player_map.get(s['id'])
            if not p or p.get('dropped'):
                continue
            rank += 1
            dl = p.get('decklist') or {}
            if not (dl.get('text') or '').strip():
                continue
            wins = losses = draws = 0
            for rnd in event_rounds:
                for match in rnd:
                    pid = p['id']
                    if pid not in (match['player1_id'], match['player2_id']):
                        continue
                    if match.get('is_bye') or not match.get('result'):
                        continue
                    winner_id = match.get('winner_id')
                    if match['result'] in DRAW_RESULTS or winner_id is None:
                        draws += 1
                    elif winner_id == pid:
                        wins += 1
                    else:
                        losses += 1
            decks.append({
                'event_id':     e['id'],
                'event_name':   e.get('name', ''),
                'event_date':   e.get('date', ''),
                'event_format': e.get('format', ''),
                'player_id':    p['id'],
                'player_name':  p.get('name', ''),
                'google_id':    p.get('google_id'),
                'deck_name':    dl.get('name', ''),
                'rank': rank, 'wins': wins, 'losses': losses, 'draws': draws,
            })
    # Sort by event date descending (stable, so rank order within event preserved)
    decks.sort(key=lambda d: d['rank'])
    decks.sort(key=lambda d: d['event_date'] or '', reverse=True)
    return jsonify({'decks': decks})


@events_bp.route('/api/decklists/<event_id>/<player_id>')
def api_public_player_decklist(event_id, player_id):
    """Public: full decklist text for one player in a completed event."""
    e = get_event(event_id)
    if not e or not e.get('requires_decklists') or not _event_complete(e):
        return jsonify({'error': 'Not found'}), 404
    if e.get('closed_decklists') and not e.get('decklists_released'):
        return jsonify({'error': 'Not found'}), 404
    p = next((x for x in e['players'] if x['id'] == player_id), None)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    dl = p.get('decklist') or {}
    text = (dl.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'No decklist'}), 404
    return jsonify({'player_name': p.get('name', ''), 'deck_name': dl.get('name', ''), 'text': text})

# Community Discord server for the "join us" card on /about. Same server across
# environments, so it's a constant with an env override rather than per-env config.
DISCORD_COMMUNITY_GUILD_ID = os.environ.get('DISCORD_COMMUNITY_GUILD_ID',
                                            '1512133524000608276')

@events_bp.route('/about')
def about():
    return render_template('about.html', user=get_current_user(),
                           discord=discord_api.get_widget_info(DISCORD_COMMUNITY_GUILD_ID))

@events_bp.route('/terms')
def terms():
    return render_template('terms.html', user=get_current_user())

@events_bp.route('/privacy')
def privacy():
    return render_template('privacy.html', user=get_current_user())

@events_bp.route('/discord-bot')
def discord_bot():
    # Invite link for this environment's bot (prod vs staging app), built from the
    # app ID in Secret Manager. Omitted if the bot isn't configured for this env.
    app_id = get_secret('DISCORD_APP_ID')
    # permissions=268453888 = Send Messages + Embed Links + Manage Roles (the last
    # lets the bot assign an event role to members; it must sit above that role).
    invite_url = (f'https://discord.com/oauth2/authorize?client_id={app_id}'
                  '&scope=bot+applications.commands&permissions=268453888') if app_id else ''
    # User-install link ("Add to your account"): integration_type=1, commands-only
    # scope (no bot/permissions — it isn't joining a server). Lets someone run the
    # /cparty commands anywhere, even in servers PartyBot isn't in.
    account_url = (f'https://discord.com/oauth2/authorize?client_id={app_id}'
                   '&integration_type=1&scope=applications.commands') if app_id else ''
    return render_template('discord_bot.html', user=get_current_user(),
                           cmd=COMMAND_NAME, invite_url=invite_url, account_url=account_url)

@events_bp.route('/events/<event_id>')
def event_detail(event_id):
    event = get_event(event_id)
    if not event:
        return 'Event not found', 404
    return render_template('event.html', user=get_current_user(), event=event)

@events_bp.route('/events/new')
@login_required
def new_event_page():
    import datetime
    mode = request.args.get('mode')
    if mode not in ('simple', 'advanced'):
        mode = None
    duplicate_id = request.args.get('duplicate')
    source = None
    if duplicate_id:
        src = get_event(duplicate_id)
        if src:
            source = src
            if mode is None:
                mode = 'advanced' if src.get('advanced') else 'simple'
    return render_template('new_event.html', user=get_current_user(), mode=mode,
                           source=source, today=datetime.date.today().isoformat())

@events_bp.route('/events/<event_id>/edit')
@login_required
def edit_event_page(event_id):
    e = get_event(event_id)
    if not e:
        return 'Event not found', 404
    _require_manage(e)
    active_count = len([p for p in e.get('players', []) if not p.get('dropped')])
    return render_template('edit_event.html', user=get_current_user(), event=e,
                           active_count=active_count)

@events_bp.route('/events/<event_id>/decklists')
def decklists_page(event_id):
    """Decklists table. Open to anyone when decklists are public (open or released);
    organizer-only when closed and not yet released."""
    event = get_event(event_id)
    if not event:
        return 'Event not found', 404
    user = get_current_user()
    can_manage = _can_manage(event)
    closed = bool(event.get('closed_decklists'))
    released = bool(event.get('decklists_released'))
    page_locked = not can_manage and closed and not released
    return render_template('decklists.html', user=user, event=event,
                           can_manage=can_manage, page_locked=page_locked)


@events_bp.route('/events/<event_id>/submit-tcdecks')
@login_required
def submit_tcdecks_page(event_id):
    """Organiser: review + relay form that POSTs event results to tcdecks.net."""
    e = get_event(event_id)
    if not e:
        return 'Event not found', 404
    if not _can_manage(e):
        return 'Organizers only.', 403

    standings = compute_standings(e['players'], e.get('rounds', []))
    player_map = {p['id']: p for p in e['players']}

    def deck_lines(entries):
        return '\n'.join(f"{count} {name}" for count, name, _tag in entries)

    decks = []
    for s in standings:
        p = player_map.get(s['id'])
        if not p or p.get('dropped'):
            continue
        dl = p.get('decklist') or {}
        text = (dl.get('text') or '').strip()
        if text:
            main_entries, side_entries = parse_decklist(text)
            main_text = deck_lines(main_entries)
            side_text = deck_lines(side_entries)
        else:
            main_text = side_text = ''
        decks.append({'player': p.get('name', ''),
                      'deck_name': dl.get('name', ''),
                      'main': main_text, 'side': side_text,
                      'has_decklist': bool(text)})

    return render_template('submit_tcdecks.html',
                           user=get_current_user(), event=e, decks=decks)


@events_bp.route('/events/<event_id>/submit-mtgtop8')
@login_required
def submit_mtgtop8_page(event_id):
    """Organiser: review + relay form that POSTs event results to mtgtop8.com."""
    e = get_event(event_id)
    if not e:
        return 'Event not found', 404
    if not _can_manage(e):
        return 'Organizers only.', 403

    standings = compute_standings(e['players'], e.get('rounds', []))
    player_map = {p['id']: p for p in e['players']}

    def deck_lines(entries):
        return '\n'.join(f"{count} {name}" for count, name, _tag in entries)

    decks = []
    rank = 0
    for s in standings:
        p = player_map.get(s['id'])
        if not p or p.get('dropped'):
            continue
        rank += 1
        dl = p.get('decklist') or {}
        text = (dl.get('text') or '').strip()
        if text:
            main_entries, side_entries = parse_decklist(text)
            cards = deck_lines(main_entries)
            if side_entries:
                cards += '\nSideboard\n' + deck_lines(side_entries)
        else:
            cards = ''
        decks.append({'player': p.get('name', ''),
                      'deck_name': dl.get('name', ''),
                      'cards': cards,
                      'rank': rank,
                      'has_decklist': bool(text)})

    event_url = request.host_url.rstrip('/') + f'/events/{event_id}'
    return render_template('submit_mtgtop8.html',
                           user=get_current_user(), event=e, decks=decks,
                           event_url=event_url)


@events_bp.route('/events/<event_id>/submit-mtggoldfish')
@login_required
def submit_mtggoldfish_page(event_id):
    """Organiser: review + relay form that POSTs event results to mtggoldfish.com."""
    e = get_event(event_id)
    if not e:
        return 'Event not found', 404
    if not _can_manage(e):
        return 'Organizers only.', 403

    rounds = e.get('rounds', [])
    standings = compute_standings(e['players'], rounds)
    player_map = {p['id']: p for p in e['players']}

    def deck_lines(entries):
        return '\n'.join(f"{count} {name}" for count, name, _tag in entries)

    def match_record(player_id):
        wins = losses = draws = 0
        for rnd in rounds:
            for match in rnd:
                p1, p2 = match['player1_id'], match['player2_id']
                if player_id not in (p1, p2):
                    continue
                if match.get('is_bye') or not match.get('result'):
                    continue
                winner_id = match.get('winner_id')
                if match['result'] in DRAW_RESULTS or winner_id is None:
                    draws += 1
                elif winner_id == player_id:
                    wins += 1
                else:
                    losses += 1
        return wins, losses, draws

    decks = []
    rank = 0
    for s in standings:
        p = player_map.get(s['id'])
        if not p or p.get('dropped'):
            continue
        rank += 1
        dl = p.get('decklist') or {}
        text = (dl.get('text') or '').strip()
        if text:
            main_entries, side_entries = parse_decklist(text)
            main_text = deck_lines(main_entries)
            side_text = deck_lines(side_entries)
        else:
            main_text = side_text = ''
        wins, losses, draws = match_record(p['id'])
        decks.append({'player': p.get('name', ''),
                      'deck_name': dl.get('name', ''),
                      'main': main_text, 'side': side_text,
                      'rank': rank,
                      'wins': wins, 'losses': losses, 'draws': draws,
                      'has_decklist': bool(text)})

    event_url = request.host_url.rstrip('/') + f'/events/{event_id}'
    return render_template('submit_mtggoldfish.html',
                           user=get_current_user(), event=e, decks=decks,
                           event_url=event_url)

@events_bp.route('/events/<event_id>/rounds/<int:round_num>/pairings/print')
def print_pairings(event_id, round_num):
    """A clean, print-friendly pairings sheet for one round, sorted by table number
    (byes and unassigned matches last). Public, like the event page. Renders the
    table column only when the event uses table assignments."""
    from discord_notify import _round_label
    event = get_event(event_id)
    if not event:
        return 'Event not found', 404
    rounds = event.get('rounds', [])
    idx = round_num - 1
    if not (0 <= idx < len(rounds)):
        return 'Round not found', 404
    rnd = rounds[idx]
    names = {p['id']: p['name'] for p in event.get('players', [])}
    rows = []
    for m in rnd:
        rows.append({
            'table':   m.get('table'),
            'label':   (event.get('table_labels') or {}).get(str(m.get('table'))) if m.get('table') else None,
            'p1':      names.get(m.get('player1_id'), '?'),
            'p2':      None if m.get('is_bye') else names.get(m.get('player2_id'), '?'),
            'is_bye':  bool(m.get('is_bye')),
        })
    # Sort by table number; unassigned/byes (no table) fall to the end.
    rows.sort(key=lambda r: (r['table'] is None, r['table'] or 0))
    return render_template('print_pairings.html', event=event, rows=rows,
                           tables_enabled=bool(event.get('tables_enabled')),
                           round_label=_round_label(round_num, rnd))

@events_bp.route('/admin')
@login_required
def admin_page():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    return render_template('admin.html', user=user, admins=get_admins())

@events_bp.route('/admin/users')
@login_required
def users_page():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)

    # Start from the user directory (email captured at login, profile name/discord).
    directory = {u['google_id']: {
        'google_id': u['google_id'],
        'name':      u.get('name', ''),
        'email':     u.get('email', ''),
        'discord':   u.get('discord', ''),
        'events':    0,
    } for u in list_users()}

    # Enrich/backfill from event registrations: count participation and fill in
    # name/discord for anyone who registered before the directory captured them.
    # Sort ascending by date so later events win for the "latest known" values.
    counts: dict = {}
    for e in sorted(list_events(), key=lambda x: x.get('date', '')):
        for p in e.get('players', []):
            gid = p.get('google_id')
            if not gid:
                continue  # organiser-added ghost player, no account
            counts[gid] = counts.get(gid, 0) + 1
            entry = directory.setdefault(gid, {
                'google_id': gid, 'name': '', 'email': '', 'discord': '', 'events': 0,
            })
            if not entry['name'] and p.get('name'):
                entry['name'] = p['name']
            if not entry['discord'] and p.get('discord'):
                entry['discord'] = p['discord']

    users = list(directory.values())
    for u in users:
        u['events'] = counts.get(u['google_id'], 0)
    users.sort(key=lambda u: (u['name'] or u['email']).lower())
    return render_template('users.html', user=user, users=users)


# ── API: events ────────────────────────────────────────────────────────────────

# Event fields the index cards / filters read. The listing sends only these plus
# slim players/rounds (below), not the whole event document — the full rounds
# (every match) and player snapshots are large and unused by the listing, so
# trimming them cuts payload and serialization as the event count grows.
_LIST_FIELDS = ('id', 'name', 'owner_id', 'co_organizer_ids', 'game', 'event_type', 'format', 'date',
                'start_time', 'location', 'lat', 'lng', 'description', 'entry_cost',
                'registration', 'registration_cap', 'status', 'num_rounds', 'tags',
                'test_mode')

@events_bp.route('/api/events', methods=['GET'])
def api_list_events():
    # Test-mode events stay out of public discovery — only their owner (and global
    # admins) see them listed, so organisers can rehearse setup without publishing
    # fake events to players.
    user  = get_current_user()
    uid   = user['id'] if user else None
    admin = bool(uid) and is_admin(uid)
    events = []
    for e in list_events():
        if e.get('test_mode') and not admin and e.get('owner_id') != uid:
            continue
        rounds = e.get('rounds') or []
        # Don't leak delayed pairings here either — hide the latest round from
        # non-managers until it's released.
        pairings_hidden = (e.get('delay_pairings') and not e.get('pairings_released', True)
                           and rounds and not _can_manage(e))
        if pairings_hidden:
            rounds = rounds[:-1]
        slim = {k: e.get(k) for k in _LIST_FIELDS}
        # Completion is computed server-side (the slim rounds below drop `result`,
        # so the client can't tell a finished round from an unscored one).
        slim['complete'] = _event_complete(e)
        # Slim players → only what the cards/filters need (active count, the "my
        # registered" check). No snapshots/decklists/tokens, so nothing to redact.
        slim['players'] = [{'google_id': p.get('google_id'), 'dropped': p.get('dropped', False)}
                           for p in (e.get('players') or [])]
        # Slim matches → only the fields the listing reads (round count, last-round
        # bracket detection and its winner). Array shape/lengths are preserved.
        slim['rounds'] = [[{'stage': m.get('stage'), 'winner_id': m.get('winner_id')}
                           for m in rnd] for rnd in rounds]
        if pairings_hidden:
            slim['pairings_hidden'] = True
        events.append(slim)
    return jsonify(events)

@events_bp.route('/api/events', methods=['POST'])
@login_required
def api_create_event():
    user = get_current_user()
    data = request.json or {}
    if not (data.get('name') or '').strip():
        return jsonify({'error': 'Event name is required'}), 400
    payment_url, err = _normalize_payment_url(data.get('payment_url'))
    if err:
        return jsonify({'error': err}), 400
    event = {
        'name':         data.get('name', 'New Event'),
        'game':         (data.get('game') or '').strip(),     # '' for Simple (no game yet)
        'test_mode':    bool(data.get('test_mode', False)),    # hidden from public discovery
        'tags':         _clean_tags(data.get('tags')),
        'structure':    data.get('structure') if data.get('structure') in STRUCTURES else '',
        # Intended top-cut size (4/8/16) for a Swiss + Top Cut event. This pre-fills
        # the in-event "Cut to Top N" action; the actual cut still executes later.
        'planned_cut_size': data.get('planned_cut_size') if data.get('planned_cut_size') in (4, 8, 16) else 0,
        'requires_decklists': bool(data.get('requires_decklists', False)),
        'decklists_required': bool(data.get('decklists_required', False)),
        'closed_decklists': bool(data.get('closed_decklists', False)),
        'decklist_visibility_note': str(data.get('decklist_visibility_note') or '')[:500],
        'decklists_released': False,
        'decklist_deadline': (data.get('decklist_deadline') or '').strip(),  # '' = no cutoff
        'validation_format': data.get('validation_format') if data.get('validation_format') in VALIDATION_FORMATS else 'none',
        # Print-legality policy (Premodern/Old School): which physical printings are
        # allowed. Default off — the organiser opts each in.
        'allow_proxies':     bool(data.get('allow_proxies', False)),
        'allow_gold_border': bool(data.get('allow_gold_border', False)),
        'allow_ce':          bool(data.get('allow_ce', False)),   # Collector's Edition
        'allow_ie':          bool(data.get('allow_ie', False)),   # International Edition
        # Proxy policy (only meaningful when allow_proxies): unlimited | limited | custom.
        'proxy_policy': data.get('proxy_policy') if data.get('proxy_policy') in PROXY_POLICIES else 'unlimited',
        'proxy_limit':  data.get('proxy_limit') if isinstance(data.get('proxy_limit'), int) and data.get('proxy_limit') >= 0 else 0,
        'proxy_note':   str(data.get('proxy_note') or '').strip()[:500],
        # Table assignments: a venue's physical-table range + reserved/labeled tables.
        'tables_enabled':  bool(data.get('tables_enabled', False)),
        'table_start':     _bounded_table(data.get('table_start'), 1),
        'table_end':       _bounded_table(data.get('table_end'), 0),   # 0 = unset
        'tables_excluded': _clean_table_list(data.get('tables_excluded')),
        'table_labels':    _clean_table_labels(data.get('table_labels')),
        'round_timer_minutes': data.get('round_timer_minutes') if isinstance(data.get('round_timer_minutes'), int) and data.get('round_timer_minutes') >= 0 else 0,
        # Opt-in (Advanced): start the round timer automatically when pairings go
        # live — immediately if posted right away, or on release when delayed.
        'auto_start_timer': bool(data.get('auto_start_timer', False)),
        'round_started_at': '',   # ISO time the current round's timer started
        # Delayed delivery: hide newly-paired pairings / fresh standings from
        # players until the organiser releases them (prevents stream spoilers).
        'delay_pairings':  bool(data.get('delay_pairings', False)),
        'delay_standings': bool(data.get('delay_standings', False)),
        'pairings_released':  True,
        'standings_released': True,
        # Waitlist records (people who joined after the cap filled). Status is one
        # of: waitlisted | promoted | removed | removed_by_self. Kept for history.
        'waitlist': [],
        # When True, only checked-in players are paired (attendance gate).
        'require_check_in': bool(data.get('require_check_in', False)),
        'brand_text':       str(data.get('brand_text') or '')[:300],
        'brand_image_url':  '',       # set via the brand-image upload endpoint
        'brand_image_object': '',     # GCS object name (for replacing/deleting)
        'entry_code':   str(data.get('entry_code') or '').strip()[:64],  # '' = none required
        'advanced':     bool(data.get('advanced', False)),   # created via the Advanced flow
        # When True, the "0-0-3 Intentional draw" result option is hidden/rejected
        # (Advanced events only). Default False = intentional draws allowed.
        'intentional_draws_frowned': bool(data.get('intentional_draws_frowned', False)),
        'prize_deadline_days': data.get('prize_deadline_days') if isinstance(data.get('prize_deadline_days'), int) and data.get('prize_deadline_days') >= 0 else 0,
        'rules':        str(data.get('rules') or '')[:_COMMS_MAX],
        'schedule':     str(data.get('schedule') or '')[:_COMMS_MAX],
        'prizes':       str(data.get('prizes') or '')[:_COMMS_MAX],
        'contact':      str(data.get('contact') or '')[:_COMMS_MAX],
        'event_type':   data.get('event_type', 'One-day'),
        'format':       data.get('format', 'Limited: Draft'),
        'description':  data.get('description', ''),
        'entry_cost':   data.get('entry_cost', ''),
        'payment_url':  payment_url,
        'date':         data.get('date', str(datetime.date.today())),
        'start_time':   (data.get('start_time') or '').strip()[:5],
        'location':     str(data.get('location') or '').strip()[:200],
        'lat':          _coord(data.get('lat'), -90, 90),     # from a Places selection
        'lng':          _coord(data.get('lng'), -180, 180),
        'place_id':     str(data.get('place_id') or '').strip()[:300],
        'owner_id':     user['id'],
        'owner_name':   user['name'],
        'players':      [],
        'rounds':       [],
        'num_rounds':   data.get('num_rounds', 0),
        'cut_size':     0,            # 0 = no playoff cut; else 4/8/16
        'cut_seeds':    {},           # player_id -> seed, set when the cut starts
        'status':       'setup',
        'registration': 'open',
        'registration_type':  data.get('registration_type') if data.get('registration_type') in REGISTRATION_TYPES else 'open',
        'registration_start': (data.get('registration_start') or '').strip(),
        'registration_end':   (data.get('registration_end') or '').strip(),
        'unenroll_end':       (data.get('unenroll_end') or '').strip(),   # = the drop deadline
        'registration_cap': data.get('registration_cap', 0),  # 0 = no cap
        # Drop / refund policy. self-service drops on by default (matches prior
        # behaviour); refund text/window are display-only (no payment processing).
        'self_service_drop_enabled': bool(data.get('self_service_drop_enabled', True)),
        'drop_policy_text':   str(data.get('drop_policy_text') or '')[:_COMMS_MAX],
        'refund_policy_text': str(data.get('refund_policy_text') or '')[:_COMMS_MAX],
        'refund_window_end':  (data.get('refund_window_end') or '').strip(),
    }
    eid = create_event(event)
    event['id'] = eid
    return jsonify(event), 201

@events_bp.route('/api/events/<event_id>', methods=['GET'])
def api_get_event(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    e['standings'] = compute_standings(e['players'], e['rounds'])
    # Players who can intentionally draw the final Swiss round and still lock a
    # top-cut spot (empty unless this is the final round of a Swiss + Top Cut event).
    _num_rounds = e.get('num_rounds') or default_num_rounds(len(e['players']))
    e['id_safe_ids'] = list(id_safe_players(e['players'], e['rounds'],
                                            _num_rounds, e.get('planned_cut_size') or 0))
    e['can_manage'] = _can_manage(e)
    owner_profile = get_user_profile(e.get('owner_id', ''))
    # Surface the organizer's discord (if known) so players know how to reach them.
    e['owner_discord'] = owner_profile.get('discord', '')
    # Resolve co-organizer IDs to display names for the event page.
    co_org_names = []
    for cid in e.get('co_organizer_ids', []):
        if cid.startswith('pending:'):
            co_org_names.append({'id': cid, 'name': cid[len('pending:'):], 'discord': '', 'pending': True})
        else:
            p = get_user_profile(cid)
            co_org_names.append({'id': cid, 'name': p.get('name', cid),
                                 'discord': p.get('discord', ''), 'pending': False})
    e['co_organizers'] = co_org_names
    # Expose only whether a code is needed; the code itself is a secret the
    # organiser shares out-of-band, so never send it to non-managers.
    e['entry_code_required'] = bool(e.get('entry_code'))
    _enrich_players_discord(e)
    # Repair handles for players who registered via the bot before we captured
    # them — fetched from Discord in the background, so they appear on next load.
    if e['can_manage'] and _players_missing_discord_handle(e):
        eid = e['id']
        threading.Thread(target=backfill_discord_handles, args=(eid,), daemon=True).start()
    # Waitlist: managers see the full records (names/emails) to manage seats;
    # everyone else sees only their own status + queue position, never others'.
    e['is_full'] = _is_full(e)
    active_wl = _active_waitlist(e)
    uid = (get_current_user() or {}).get('id')
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
        # Delayed delivery: hide the latest round's pairings / the standings from
        # players until released. Managers always see everything.
        if e.get('delay_pairings') and not e.get('pairings_released', True) and e['rounds']:
            e['pairings_hidden'] = True
            e['rounds'] = e['rounds'][:-1]
            e['id_safe_ids'] = []
        if e.get('delay_standings') and not e.get('standings_released', True):
            e['standings_hidden'] = True
            e['standings'] = []
    _redact_players(e)
    return jsonify(e)

@events_bp.route('/api/events/<event_id>', methods=['PUT'])
@login_required
def api_update_event(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    data = request.json or {}
    allowed = {'name', 'advanced', 'game', 'test_mode', 'tags', 'structure', 'planned_cut_size',
               'requires_decklists', 'decklists_required', 'closed_decklists', 'decklist_visibility_note',
               'entry_code', 'intentional_draws_frowned',
               'round_timer_minutes', 'auto_start_timer',
               'delay_pairings', 'delay_standings', 'require_check_in',
               'brand_text',
               'prize_deadline_days', 'rules', 'schedule', 'prizes', 'contact',
               'event_type', 'format', 'description', 'entry_cost',
               'payment_url', 'date', 'start_time', 'location', 'lat', 'lng', 'place_id', 'num_rounds',
               'status', 'registration', 'registration_cap',
               'registration_type', 'registration_start', 'registration_end', 'unenroll_end',
               'self_service_drop_enabled', 'drop_policy_text', 'refund_policy_text', 'refund_window_end',
               'decklist_deadline', 'validation_format',
               'allow_proxies', 'allow_gold_border', 'allow_ce', 'allow_ie',
               'proxy_policy', 'proxy_limit', 'proxy_note',
               'tables_enabled', 'table_start', 'table_end', 'tables_excluded', 'table_labels'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if 'name' in updates and not str(updates['name']).strip():
        return jsonify({'error': 'Event name is required'}), 400
    if 'test_mode' in updates:
        updates['test_mode'] = bool(updates['test_mode'])
    if 'advanced' in updates:
        # One-way: an event can be upgraded Simple→Advanced but never downgraded.
        updates['advanced'] = bool(updates['advanced']) or bool(e.get('advanced'))
    if 'self_service_drop_enabled' in updates:
        updates['self_service_drop_enabled'] = bool(updates['self_service_drop_enabled'])
    for f in ('drop_policy_text', 'refund_policy_text'):
        if f in updates:
            updates[f] = str(updates[f] or '')[:_COMMS_MAX]
    if 'refund_window_end' in updates:
        updates['refund_window_end'] = str(updates['refund_window_end'] or '').strip()
    if 'entry_code' in updates:
        updates['entry_code'] = str(updates['entry_code'] or '').strip()[:64]
    if 'intentional_draws_frowned' in updates:
        updates['intentional_draws_frowned'] = bool(updates['intentional_draws_frowned'])
    if 'registration_type' in updates and updates['registration_type'] not in REGISTRATION_TYPES:
        updates['registration_type'] = 'open'
    if 'requires_decklists' in updates:
        updates['requires_decklists'] = bool(updates['requires_decklists'])
    if 'decklists_required' in updates:
        updates['decklists_required'] = bool(updates['decklists_required'])
    if 'closed_decklists' in updates:
        updates['closed_decklists'] = bool(updates['closed_decklists'])
    if 'decklist_visibility_note' in updates:
        updates['decklist_visibility_note'] = str(updates['decklist_visibility_note'] or '')[:500]
    for f in ('allow_proxies', 'allow_gold_border', 'allow_ce', 'allow_ie'):
        if f in updates:
            updates[f] = bool(updates[f])
    if 'proxy_policy' in updates and updates['proxy_policy'] not in PROXY_POLICIES:
        updates['proxy_policy'] = 'unlimited'
    if 'proxy_limit' in updates:
        v = updates['proxy_limit']
        updates['proxy_limit'] = v if isinstance(v, int) and v >= 0 else 0
    if 'proxy_note' in updates:
        updates['proxy_note'] = str(updates['proxy_note'] or '').strip()[:500]
    if 'round_timer_minutes' in updates:
        v = updates['round_timer_minutes']
        updates['round_timer_minutes'] = v if isinstance(v, int) and v >= 0 else 0
    for f in ('delay_pairings', 'delay_standings', 'require_check_in', 'auto_start_timer'):
        if f in updates:
            updates[f] = bool(updates[f])
    if 'brand_text' in updates:
        updates['brand_text'] = str(updates['brand_text'] or '')[:300]
    if 'start_time' in updates:
        updates['start_time'] = str(updates['start_time'] or '').strip()[:5]
    if 'location' in updates:
        updates['location'] = str(updates['location'] or '').strip()[:200]
    if 'lat' in updates:
        updates['lat'] = _coord(updates['lat'], -90, 90)
    if 'lng' in updates:
        updates['lng'] = _coord(updates['lng'], -180, 180)
    if 'place_id' in updates:
        updates['place_id'] = str(updates['place_id'] or '').strip()[:300]
    if 'decklist_deadline' in updates:
        updates['decklist_deadline'] = str(updates['decklist_deadline'] or '').strip()
    if 'validation_format' in updates and updates['validation_format'] not in VALIDATION_FORMATS:
        updates['validation_format'] = 'none'
    if 'tables_enabled' in updates:
        updates['tables_enabled'] = bool(updates['tables_enabled'])
    if 'table_start' in updates:
        updates['table_start'] = _bounded_table(updates['table_start'], 1)
    if 'table_end' in updates:
        updates['table_end'] = _bounded_table(updates['table_end'], 0)
    if 'tables_excluded' in updates:
        updates['tables_excluded'] = _clean_table_list(updates['tables_excluded'])
    if 'table_labels' in updates:
        updates['table_labels'] = _clean_table_labels(updates['table_labels'])
    if 'prize_deadline_days' in updates:
        v = updates['prize_deadline_days']
        updates['prize_deadline_days'] = v if isinstance(v, int) and v >= 0 else 0
    updates.update(_clean_comms(updates))   # cap the long-text fields
    if 'tags' in updates:
        updates['tags'] = _clean_tags(updates['tags'])
    if 'structure' in updates and updates['structure'] not in STRUCTURES:
        updates['structure'] = ''
    if 'planned_cut_size' in updates and updates['planned_cut_size'] not in (4, 8, 16):
        updates['planned_cut_size'] = 0
    if 'payment_url' in updates:
        updates['payment_url'], err = _normalize_payment_url(updates['payment_url'])
        if err:
            return jsonify({'error': err}), 400
    old_format = e.get('validation_format', 'none')
    old_policy = {k: e.get(k) for k in _PRINT_POLICY_KEYS}
    save_event(event_id, updates)
    e.update(updates)
    # Re-check every submitted decklist when the validation format OR the print-legality
    # policy changes, so stored statuses (and proxy flags) stay accurate.
    policy_changed = any(k in updates and updates[k] != old_policy[k] for k in _PRINT_POLICY_KEYS)
    if updates.get('validation_format', old_format) != old_format or policy_changed:
        revalidated = False
        for p in e.get('players', []):
            dl = p.get('decklist')
            if dl and (dl.get('text') or '').strip():
                dl['validation'] = validate_decklist(dl['text'], e.get('validation_format', 'none'),
                                                      _print_policy(e))
                revalidated = True
        if revalidated:
            save_event(event_id, {'players': e['players']})
    _CARD_FIELDS = {'name', 'event_type', 'format', 'date', 'start_time', 'entry_cost', 'description',
                    'registration', 'registration_cap', 'registration_type',
                    'registration_start', 'registration_end', 'entry_code'}
    if e.get('discord_announce') and _CARD_FIELDS & updates.keys():
        refresh_event_announcement(e)
    _redact_players(e)
    return jsonify({**e, **updates})

@events_bp.route('/api/events/<event_id>', methods=['DELETE'])
@login_required
def api_delete_event(event_id):
    """Permanently delete an event. Allowed for the event owner or a global admin."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    delete_event(event_id)
    return jsonify({'ok': True})


# ── API: player registration ───────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/register', methods=['POST'])
@login_required
def api_register(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    blocked = _self_registration_blocked(e)
    if blocked:
        return jsonify({'error': blocked}), 400
    code_err = _entry_code_error(e, request.json or {})
    if code_err:
        return jsonify({'error': code_err}), 400

    # Enforce registration cap
    cap = e.get('registration_cap', 0)
    active = [p for p in e['players'] if not p.get('dropped')]
    if cap and len(active) >= cap:
        return jsonify({'error': f'This event is full ({cap} players max)'}), 400
    user = get_current_user()
    data = request.json or {}
    display_name = data.get('display_name', '').strip() or user['name']
    # The form hides the Discord field when the user already has one on file, so
    # fall back to their saved handle when none is submitted.
    discord      = data.get('discord', '').strip() or get_user_profile(user['id']).get('discord', '')
    # Re-activate a previously dropped entry instead of refusing or duplicating.
    existing = _find_player_by_google_id(e, user['id'])
    if existing:
        if not existing.get('dropped'):
            return jsonify({'error': 'Already registered'}), 400
        set_player_dropped(event_id, existing['id'], False)
        if discord:
            save_user_profile(user['id'], {'discord': discord})
        refresh_event_announcement(get_event(event_id))
        existing['dropped'] = False
        _log_action(event_id, 'register', 'registered')
        _dm_registration_confirmation(user['id'], e, event_id, request.host_url)
        return jsonify(existing), 200
    player = {
        'id':        _slugify(display_name) + '_' + uuid.uuid4().hex[:8],
        'name':      display_name,
        'google_id': user['id'],
        'discord':   discord,
        'dropped':   False,
        'checked_in': False,
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    refresh_event_announcement(e)   # may have just hit the cap → show "full"
    # Keep the user directory current: capture the discord handle they gave.
    if discord:
        save_user_profile(user['id'], {'discord': discord})
    _log_action(event_id, 'register', 'registered')
    _dm_registration_confirmation(user['id'], e, event_id, request.host_url)
    return jsonify(player), 201

@events_bp.route('/api/events/<event_id>/unregister', methods=['POST'])
@login_required
def api_unregister(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    user = get_current_user()
    player = _find_player_by_google_id(e, user['id'])
    if not player:
        return jsonify({'error': 'Not registered'}), 400
    if not e.get('self_service_drop_enabled', True):
        return jsonify({'error': 'Self-service drops are disabled for this event — contact the organiser.'}), 400
    unenroll_end = e.get('unenroll_end')
    if unenroll_end and datetime.date.today().isoformat() > unenroll_end:
        return jsonify({'error': 'The drop deadline has passed — contact the organiser.'}), 400
    if e['rounds']:
        set_player_dropped(event_id, player['id'], True)
    else:
        e['players'] = [p for p in e['players'] if p.get('google_id') != user['id']]
        save_event(event_id, {'players': e['players']})
    refresh_event_announcement(get_event(event_id))   # a slot may have freed up
    _log_action(event_id, 'drop', 'dropped')
    return jsonify({'ok': True})


# ── API: waitlist ────────────────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/waitlist', methods=['POST'])
@login_required
def api_join_waitlist(event_id):
    """Join the waitlist for a full event (signed-in accounts only). No-op-safe:
    refuses if the event isn't full, registration is otherwise closed, the user is
    already registered, or they're already waiting."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    blocked = _self_registration_blocked(e)
    if blocked:
        return jsonify({'error': blocked}), 400
    code_err = _entry_code_error(e, request.json or {})
    if code_err:
        return jsonify({'error': code_err}), 400
    if not _is_full(e):
        return jsonify({'error': 'This event still has open spots — register normally.'}), 400
    user = get_current_user()
    existing = _find_player_by_google_id(e, user['id'])
    if existing and not existing.get('dropped'):
        return jsonify({'error': "You're already registered for this event."}), 400
    waitlist = e.get('waitlist') or []
    if any(w.get('google_id') == user['id'] and w.get('status') == 'waitlisted' for w in waitlist):
        return jsonify({'error': "You're already on the waitlist."}), 400
    profile = get_user_profile(user['id'])
    data = request.json or {}
    discord = data.get('discord', '').strip() or profile.get('discord', '')
    record = {
        'id':         uuid.uuid4().hex,
        'google_id':  user['id'],
        'name':       (data.get('display_name', '').strip() or user['name'])[:80],
        'email':      profile.get('email', '') or user.get('email', ''),
        'discord':    discord,
        'status':     'waitlisted',
        'joined_at':  _now_iso(),
    }
    waitlist.append(record)
    save_event(event_id, {'waitlist': waitlist})
    if discord:
        save_user_profile(user['id'], {'discord': discord})
    _log_action(event_id, 'waitlist_join', f"{record['name']} joined the waitlist")
    position = sum(1 for w in waitlist if w.get('status') == 'waitlisted')
    return jsonify({'ok': True, 'position': position}), 201

@events_bp.route('/api/events/<event_id>/waitlist/leave', methods=['POST'])
@login_required
def api_leave_waitlist(event_id):
    """Remove yourself from the waitlist (status → removed_by_self, kept for history)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    user = get_current_user()
    waitlist = e.get('waitlist') or []
    rec = next((w for w in waitlist
                if w.get('google_id') == user['id'] and w.get('status') == 'waitlisted'), None)
    if not rec:
        return jsonify({'error': "You're not on the waitlist."}), 400
    rec['status'] = 'removed_by_self'
    rec['removed_at'] = _now_iso()
    save_event(event_id, {'waitlist': waitlist})
    _log_action(event_id, 'waitlist_leave', f"{rec.get('name', 'A player')} left the waitlist")
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/waitlist/<wid>/promote', methods=['POST'])
@login_required
def api_promote_waitlist(event_id, wid):
    """Promote a waitlisted player into the event. Transactional so two organisers
    can't overfill the last seat; refuses when the event is already at capacity."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    user = get_current_user()
    promoter = {'id': user['id'], 'name': user['name']}

    def build_player(rec, index):
        player = {
            'id':         _slugify(rec.get('name') or 'player') + '_' + uuid.uuid4().hex[:8],
            'name':       rec.get('name') or 'Player',
            'google_id':  rec.get('google_id'),
            'discord':    rec.get('discord', ''),
            'dropped':    False,
            'checked_in': not e.get('require_check_in') or bool(e.get('rounds')),
        }
        if rec.get('discord_id'):   # keep the Discord link so they can report via DM/channel
            player['discord_id'] = rec['discord_id']
        return player

    status, player = promote_waitlist_entry(event_id, wid, build_player, promoter, _now_iso())
    if status == 'gone':
        return jsonify({'error': 'That event no longer exists.'}), 404
    if status == 'missing':
        return jsonify({'error': 'That player is no longer on the waitlist.'}), 400
    if status == 'full':
        return jsonify({'error': 'The event is already at capacity — drop a player first.'}), 400
    _log_action(event_id, 'waitlist_promote',
                f"Promoted {player['name']} from the waitlist", target=player['name'])
    refresh_event_announcement(get_event(event_id))
    return jsonify({'ok': True, 'player': player})

@events_bp.route('/api/events/<event_id>/waitlist/<wid>/remove', methods=['POST'])
@login_required
def api_remove_waitlist(event_id, wid):
    """Organiser removes a waitlisted player (status → removed, kept for history)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    waitlist = e.get('waitlist') or []
    rec = next((w for w in waitlist if w.get('id') == wid), None)
    if not rec:
        return jsonify({'error': 'No such waitlist entry.'}), 404
    if rec.get('status') != 'waitlisted':
        return jsonify({'error': 'That entry is no longer active.'}), 400
    rec['status'] = 'removed'
    rec['removed_at'] = _now_iso()
    save_event(event_id, {'waitlist': waitlist})
    _log_action(event_id, 'waitlist_remove',
                f"Removed {rec.get('name', 'a player')} from the waitlist", target=rec.get('name', ''))
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/log', methods=['GET'])
@login_required
def api_event_log(event_id):
    """The event's activity log (organiser/admin only)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    return jsonify(list_event_log(event_id))

@events_bp.route('/api/events/<event_id>/join', methods=['POST'])
def api_join_guest(event_id):
    """Self-join without a Google account. Creates a guest player and returns a
    private self-report token (also usable as a magic link) so they can report
    their own match results. Public — anyone with the event link may join while
    registration is open."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    blocked = _self_registration_blocked(e)
    if blocked:
        return jsonify({'error': blocked}), 400
    code_err = _entry_code_error(e, request.json or {})
    if code_err:
        return jsonify({'error': code_err}), 400
    cap = e.get('registration_cap', 0)
    active = [p for p in e['players'] if not p.get('dropped')]
    if cap and len(active) >= cap:
        return jsonify({'error': f'This event is full ({cap} players max)'}), 400
    data = request.json or {}
    name = data.get('display_name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    token = uuid.uuid4().hex
    player = {
        'id':          _slugify(name) + '_' + uuid.uuid4().hex[:8],
        'name':        name,
        'google_id':   None,
        'discord':     data.get('discord', '').strip(),
        'dropped':     False,
        'checked_in':  False,
        'guest_token': token,
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    refresh_event_announcement(e)   # may have just hit the cap → show "full"
    _log_action(event_id, 'register', 'registered as a guest', actor_name=name)
    echo = {k: v for k, v in player.items() if k != 'guest_token'}
    return jsonify({'player': echo, 'token': token}), 201

@events_bp.route('/api/events/<event_id>/guest', methods=['GET'])
def api_guest_whoami(event_id):
    """Resolve a guest self-report token to its player (for magic-link re-entry
    on another device). Returns only public fields, never the token itself."""
    token = request.args.get('token', '')
    if not token:
        return jsonify({'error': 'Missing token'}), 400
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    p = _find_player_by_guest_token(e, token)
    if not p:
        return jsonify({'error': 'Unknown token'}), 404
    return jsonify({'player_id': p['id'], 'name': p['name']})


# ── API: organiser player management ──────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/players', methods=['POST'])
@login_required
def api_add_player(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    user = get_current_user()
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    # Attach a google_id when adding an existing user: 'self' for the organiser,
    # or a google_id picked from the player search. Linking lets that person see
    # the event on their profile and report their own results.
    google_id = None
    discord   = data.get('discord', '').strip()
    if data.get('self'):
        google_id = user['id']
    elif data.get('google_id'):
        google_id = data['google_id']
        # Trust the directory, not the client, for the linked user's details.
        profile = get_user_profile(google_id)
        name    = profile.get('name') or name
        discord = profile.get('discord', '')
    if google_id and any(p.get('google_id') == google_id for p in e['players']):
        return jsonify({'error': 'That player is already in this event'}), 400
    # At capacity → route the add onto the waitlist rather than overfilling. This
    # applies to organisers adding themselves or walk-ins too; promote later to
    # seat them when a spot opens.
    if _is_full(e):
        waitlist = e.get('waitlist') or []
        if google_id and any(w.get('google_id') == google_id and w.get('status') == 'waitlisted'
                             for w in waitlist):
            return jsonify({'error': 'That player is already on the waitlist'}), 400
        record = {
            'id':        uuid.uuid4().hex,
            'google_id': google_id,
            'name':      name[:80],
            'email':     get_user_profile(google_id).get('email', '') if google_id else '',
            'discord':   discord,
            'status':    'waitlisted',
            'joined_at': _now_iso(),
            'added_by_organizer': True,
        }
        waitlist.append(record)
        save_event(event_id, {'waitlist': waitlist})
        _log_action(event_id, 'waitlist_join', f"{record['name']} added to the waitlist (event full)")
        return jsonify({'waitlisted': True, 'name': record['name']}), 201
    player = {
        'id':        _slugify(name) + '_' + uuid.uuid4().hex[:8],
        'name':      name,
        'google_id': google_id,
        'discord':   discord,
        'dropped':   False,
        # Check-in events need an explicit check-in (show the "Check in" button), so
        # only auto-mark present when check-in isn't required or rounds have already started.
        'checked_in': not e.get('require_check_in') or bool(e.get('rounds')),
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    refresh_event_announcement(e)   # may have just hit the cap → show "full"
    _log_action(event_id, 'register', f'added {name}', target=name)
    if google_id:
        _dm_registration_confirmation(google_id, e, event_id, request.host_url)
    return jsonify(player), 201

@events_bp.route('/api/events/<event_id>/player-search', methods=['GET'])
@login_required
def api_player_search(event_id):
    """Manager-only typeahead over the user directory, for linking an existing
    person when adding a player. Excludes users already in the event; returns
    only name + discord + id (no email)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    q = request.args.get('q', '').strip().lower()
    if len(q) < 2:
        return jsonify([])
    in_event = {p.get('google_id') for p in e['players'] if p.get('google_id')}
    matches = []
    for u in list_users():
        gid = u.get('google_id')
        if not gid or gid in in_event:
            continue
        name, discord = u.get('name', ''), u.get('discord', '')
        if q in name.lower() or (discord and q in discord.lower()):
            matches.append({'google_id': gid, 'name': name, 'discord': discord})
            if len(matches) >= 8:
                break
    return jsonify(matches)

@events_bp.route('/api/events/<event_id>/players/<player_id>', methods=['DELETE'])
@login_required
def api_remove_player(event_id, player_id):
    """Remove a player entirely — only before pairing has started, since once a
    round exists the player is referenced by matches (drop them instead)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    if e['rounds']:
        return jsonify({'error': 'Drop the player instead once rounds have started'}), 400
    removed = next((p for p in e['players'] if p['id'] == player_id), None)
    remaining = [p for p in e['players'] if p['id'] != player_id]
    if len(remaining) == len(e['players']):
        return jsonify({'error': 'Player not found'}), 404
    e['players'] = remaining
    save_event(event_id, {'players': e['players']})
    refresh_event_announcement(e)   # a slot may have freed up
    _log_action(event_id, 'drop', f"removed {removed['name']}", target=removed['name'])
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/players/<player_id>/drop', methods=['POST'])
@login_required
def api_drop_player(event_id, player_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    target = set_player_dropped(event_id, player_id, True)
    if not target:
        return jsonify({'error': 'Player not found'}), 404
    refresh_event_announcement(get_event(event_id))   # a slot may have freed up
    _log_action(event_id, 'drop', f"dropped {target['name']}", target=target['name'])
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/players/<player_id>/undrop', methods=['POST'])
@login_required
def api_undrop_player(event_id, player_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    target = set_player_dropped(event_id, player_id, False)
    if not target:
        return jsonify({'error': 'Player not found'}), 404
    refresh_event_announcement(get_event(event_id))   # may have re-hit the cap
    _log_action(event_id, 'undrop', f"returned {target['name']} to the event", target=target['name'])
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/players/<player_id>/rename', methods=['POST'])
def api_rename_player(event_id, player_id):
    """Change the name shown for a player on this event (e.g. a real name for a
    larger event). Allowed for a manager, or for the player editing their own
    entry (matched by Google account or guest token). Only the display name
    changes — the player's id, matches and standings are unaffected."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    name = str((request.json or {}).get('name') or '').strip()[:80]
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    player = next((p for p in e['players'] if p['id'] == player_id), None)
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    allowed = _can_manage(e)
    if not allowed:
        user = get_current_user()
        if user and player.get('google_id') == user['id']:
            allowed = True
    if not allowed:
        token = request.headers.get('X-Guest-Token') or (request.json or {}).get('guest_token')
        if token and player.get('guest_token') == token:
            allowed = True
    if not allowed:
        abort(403)
    player['name'] = name
    save_event(event_id, {'players': e['players']})
    return jsonify({'ok': True, 'name': name})

@events_bp.route('/api/events/<event_id>/players/<player_id>/fixed-table', methods=['POST'])
@login_required
def api_set_fixed_table(event_id, player_id):
    """Organiser pins (or clears) a player's fixed seat, so they keep the same
    table every round (e.g. a mobility accommodation). Pass {table: <n>} to set,
    or {table: null} / {table: 0} to clear. Applies from the next pairing."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    player = next((p for p in e['players'] if p['id'] == player_id), None)
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    raw = (request.json or {}).get('table')
    table = None
    if raw not in (None, '', 0, '0'):
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return jsonify({'error': 'Table must be a number'}), 400
        if not (1 <= n <= _MAX_TABLE):
            return jsonify({'error': f'Table must be between 1 and {_MAX_TABLE}'}), 400
        table = n
    if table is None:
        player.pop('fixed_table', None)
    else:
        player['fixed_table'] = table
    save_event(event_id, {'players': e['players']})
    _log_action(event_id, 'table',
                f"set {player['name']}'s fixed table to {table}" if table
                else f"cleared {player['name']}'s fixed table", target=player['name'])
    return jsonify({'ok': True, 'fixed_table': table})

@events_bp.route('/api/events/<event_id>/players/<player_id>/checkin', methods=['POST'])
@login_required
def api_check_in(event_id, player_id):
    """Organiser marks a player checked in (or not) for attendance gating."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    value = bool((request.json or {}).get('checked_in', True))
    player = set_player_field(event_id, player_id, 'checked_in', value)
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    return jsonify(player)


# ── API: pairing ───────────────────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/pair', methods=['POST'])
@login_required
def api_pair_round(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)

    # If a playoff bracket is underway, "pair" advances it to the next round by
    # pairing the winners. Every match needs a winner first (single elimination
    # has no draws — a drawn match must be resolved to a decisive result).
    if e['rounds'] and _is_bracket_round(e['rounds'][-1]):
        last = e['rounds'][-1]
        if len(last) <= 1:
            return jsonify({'error': 'The playoff is already complete'}), 400
        if any(m.get('winner_id') is None for m in last):
            return jsonify({'error': 'Every playoff match needs a winner before advancing '
                                     '(resolve any draws first)'}), 400
        new_round = next_bracket_round(last)
        assign_tables(new_round, e['players'], e)
        e['rounds'].append(new_round)
        ru = _new_round_updates(e)
        save_event(event_id, {'rounds': e['rounds'], **ru})
        e.update(ru)   # so _deliver_pairings sees an auto-started timer
        round_num = len(e['rounds'])
        _deliver_pairings(e, round_num, request.host_url)
        return jsonify({'round_num': round_num, 'pairings': new_round})

    if e['rounds'] and e.get('event_type') != 'League':
        last = e['rounds'][-1]
        unfinished = [m for m in last
                      if not m.get('is_bye')
                      and m.get('winner_id') is None
                      and m.get('result') not in DRAW_RESULTS]
        if unfinished:
            return jsonify({'error': 'Previous round has unrecorded results'}), 400
    num_rounds = e['num_rounds'] or default_num_rounds(len(e['players']))
    if len(e['rounds']) >= num_rounds:
        return jsonify({'error': 'All rounds already paired'}), 400
    # When check-in is required, only checked-in players are paired.
    players_to_pair = e['players']
    if e.get('require_check_in'):
        players_to_pair = [p for p in e['players'] if p.get('checked_in')]
        if len([p for p in players_to_pair if not p.get('dropped')]) < 2:
            return jsonify({'error': 'Need at least 2 checked-in players to pair'}), 400
    new_round = pair_round(players_to_pair, e['rounds'])
    assign_tables(new_round, e['players'], e)
    e['rounds'].append(new_round)
    updates = {'rounds': e['rounds'], 'status': 'active', 'registration': 'closed',
               **_new_round_updates(e)}
    save_event(event_id, updates)
    e.update(updates)   # reflect status/registration + any auto-started timer in-memory
    refresh_event_announcement(e)

    round_num  = len(e['rounds'])
    _deliver_pairings(e, round_num, request.host_url)

    return jsonify({'round_num': round_num, 'pairings': new_round})


@events_bp.route('/api/events/<event_id>/cut', methods=['POST'])
@login_required
def api_cut_to_top(event_id):
    """Start a single-elimination playoff for the top `cut_size` players."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    if e['rounds'] and _is_bracket_round(e['rounds'][-1]):
        return jsonify({'error': 'A playoff bracket has already started'}), 400
    if not _swiss_complete(e):
        return jsonify({'error': 'Finish all rounds before cutting to a playoff'}), 400

    cut_size = (request.json or {}).get('cut_size')
    if cut_size not in CUT_SIZES:
        return jsonify({'error': 'Cut size must be 4, 8, or 16'}), 400
    active = [p for p in e['players'] if not p.get('dropped')]
    if cut_size > len(active):
        return jsonify({'error': f'Only {len(active)} active players — '
                                 f'not enough for a top {cut_size}'}), 400

    standings = compute_standings(e['players'], e['rounds'])
    new_round, seeds = make_bracket(standings, cut_size)
    assign_tables(new_round, e['players'], e)
    e['rounds'].append(new_round)
    save_event(event_id, {'rounds': e['rounds'], 'cut_size': cut_size, 'cut_seeds': seeds,
                          **_new_round_updates(e)})

    round_num = len(e['rounds'])
    _deliver_pairings(e, round_num, request.host_url)
    return jsonify({'round_num': round_num, 'cut_size': cut_size, 'pairings': new_round})


@events_bp.route('/api/events/<event_id>/timer', methods=['POST'])
@login_required
def api_restart_timer(event_id):
    """Restart the round timer from now (organiser action)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    now = _now_iso()
    save_event(event_id, {'round_started_at': now})
    e['round_started_at'] = now
    discord_api.update_round_pairings(e, request.host_url)   # show the live countdown on the pairings card
    return jsonify({'round_started_at': now})


@events_bp.route('/api/events/<event_id>/decklist', methods=['GET', 'POST'])
def api_my_decklist(event_id):
    """Submit/edit (POST) or read (GET) the requester's own decklist. Open to the
    registered Google player or a guest holding their self-report token."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    p = _current_participant(e)
    if not p:
        return jsonify({'error': "You're not registered for this event."}), 403
    locked = _decklist_locked(e)
    if request.method == 'GET':
        dl = p.get('decklist') or {}
        return jsonify({'text': dl.get('text', ''), 'name': dl.get('name', ''),
                        'updated_at': dl.get('updated_at', ''),
                        'validation': dl.get('validation'),
                        'locked': locked, 'deadline': e.get('decklist_deadline', '')})
    if locked:
        return jsonify({'error': 'The decklist deadline has passed.'}), 400
    text = str((request.json or {}).get('text', ''))[:20000]
    name = str((request.json or {}).get('name', '')).strip()[:120]
    validation = validate_decklist(text, e.get('validation_format', 'none'),
                                   _print_policy(e)) if text.strip() else None
    dl = {'text': text, 'name': name, 'updated_at': _now_iso(),
          'validation': validation} if text.strip() else None
    had = bool((p.get('decklist') or {}).get('text', '').strip())
    if set_player_field(event_id, p['id'], 'decklist', dl) is None:
        return jsonify({'error': 'Could not save your decklist.'}), 400
    if dl:
        _log_action(event_id, 'decklist',
                    'updated their decklist' if had else 'uploaded a decklist',
                    actor_name=p['name'])
    elif had:
        _log_action(event_id, 'decklist', 'removed their decklist', actor_name=p['name'])
    return jsonify({'ok': True, 'updated_at': dl['updated_at'] if dl else '',
                    'has_decklist': bool(dl), 'validation': validation})


@events_bp.route('/api/events/<event_id>/decklist/import-moxfield', methods=['POST'])
def api_import_moxfield(event_id):
    """Convert a public Moxfield deck URL to decklist text for the requester to
    review and save (we store the text, so it stays authoritative)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    if not _current_participant(e):
        return jsonify({'error': "You're not registered for this event."}), 403
    if _decklist_locked(e):
        return jsonify({'error': 'The decklist deadline has passed.'}), 400
    text, name, err = import_moxfield((request.json or {}).get('url', ''))
    if err:
        return jsonify({'error': err}), 400
    return jsonify({'text': text, 'name': name})


@events_bp.route('/api/events/<event_id>/players/<player_id>/decklist/import-moxfield', methods=['POST'])
@login_required
def api_organizer_import_moxfield(event_id, player_id):
    """Organiser: import a Moxfield deck URL for a specific player."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    text, name, err = import_moxfield((request.json or {}).get('url', ''))
    if err:
        return jsonify({'error': err}), 400
    return jsonify({'text': text, 'name': name})


@events_bp.route('/api/events/<event_id>/players/<player_id>/decklist', methods=['GET', 'POST'])
def api_player_decklist(event_id, player_id):
    """Read (GET) or set (POST) a specific player's decklist. GET is open when
    decklists are public; POST always requires organizer access."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    can_manage = _can_manage(e)
    if request.method == 'POST':
        if not can_manage:
            abort(403)
    else:
        if not can_manage:
            if e.get('closed_decklists') and not e.get('decklists_released'):
                return jsonify({'error': 'Decklists not yet public'}), 403
    p = next((x for x in e['players'] if x['id'] == player_id), None)
    if not p:
        return jsonify({'error': 'Player not found'}), 404
    if request.method == 'GET':
        dl = p.get('decklist') or {}
        return jsonify({'name': p.get('name', ''), 'deck_name': dl.get('name', ''),
                        'text': dl.get('text', ''), 'updated_at': dl.get('updated_at', ''),
                        'validation': dl.get('validation'),
                        'proxy_override': bool(dl.get('proxy_override')),
                        'proxy_override_note': dl.get('proxy_override_note', '')})
    data = request.json or {}
    text = str(data.get('text', ''))[:20000]
    name = str(data.get('name', '')).strip()[:120]
    had  = bool((p.get('decklist') or {}).get('text', '').strip())
    validation = validate_decklist(text, e.get('validation_format', 'none'),
                                   _print_policy(e)) if text.strip() else None
    dl = {'text': text, 'name': name, 'updated_at': _now_iso(),
          'validation': validation} if text.strip() else None
    if set_player_field(event_id, player_id, 'decklist', dl) is None:
        return jsonify({'error': 'Could not save the decklist.'}), 400
    action = ('updated' if had else 'uploaded') + f" {p.get('name','')}'s decklist"
    _log_action(event_id, 'decklist', action)
    return jsonify({'ok': True, 'has_decklist': bool(dl),
                    'updated_at': dl['updated_at'] if dl else '', 'validation': validation})


@events_bp.route('/api/events/<event_id>/players/<player_id>/decklist-name', methods=['POST'])
@login_required
def api_set_decklist_name(event_id, player_id):
    """Organiser sets/edits the deck name on a player's submitted decklist."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    p = next((x for x in e['players'] if x['id'] == player_id), None)
    if not p:
        return jsonify({'error': 'Player not found'}), 404
    dl = p.get('decklist')
    if not dl or not (dl.get('text') or '').strip():
        return jsonify({'error': 'That player has no decklist to name.'}), 400
    dl['name'] = str((request.json or {}).get('name', '')).strip()[:120]
    if set_player_field(event_id, player_id, 'decklist', dl) is None:
        return jsonify({'error': 'Could not save the deck name.'}), 400
    return jsonify({'ok': True, 'name': dl['name']})


@events_bp.route('/api/events/<event_id>/players/<player_id>/proxy-override', methods=['POST'])
@login_required
def api_proxy_override(event_id, player_id):
    """Organiser overrides proxy validation for a player's decklist (e.g. accepts an
    over-limit count), recording a required note. Pass {override: false} to clear."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    p = next((x for x in e['players'] if x['id'] == player_id), None)
    if not p:
        return jsonify({'error': 'Player not found'}), 404
    dl = p.get('decklist')
    if not dl or not (dl.get('text') or '').strip():
        return jsonify({'error': 'That player has no decklist.'}), 400
    data = request.json or {}
    override = bool(data.get('override'))
    note = str(data.get('note', '')).strip()[:300]
    if override and not note:
        return jsonify({'error': 'A note is required to override.'}), 400
    dl['proxy_override'] = override
    dl['proxy_override_note'] = note if override else ''
    if set_player_field(event_id, player_id, 'decklist', dl) is None:
        return jsonify({'error': 'Could not save the override.'}), 400
    return jsonify({'ok': True, 'proxy_override': override, 'proxy_override_note': dl['proxy_override_note']})


@events_bp.route('/api/events/<event_id>/decklists', methods=['GET'])
def api_decklists_table(event_id):
    """Per-player decklist summary. Open to anyone when decklists are public."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    can_manage = _can_manage(e)
    if not can_manage:
        if e.get('closed_decklists') and not e.get('decklists_released'):
            return jsonify({'error': 'Decklists not yet public'}), 403
    complete = _event_complete(e)
    rank_map = {}
    if complete:
        standings = compute_standings(e['players'], e.get('rounds', []))
        for i, s in enumerate(standings, 1):
            rank_map[s['id']] = i
    rows = []
    for p in e['players']:
        if p.get('dropped'):
            continue
        dl = p.get('decklist') or {}
        v = dl.get('validation') or {}
        rows.append({
            'player_id':   p['id'],
            'player_name': p.get('name', ''),
            'google_id':   p.get('google_id'),
            'has_decklist': bool((dl.get('text') or '').strip()),
            'deck_name':   dl.get('name', ''),
            'maindeck':    v.get('maindeck_count', 0),
            'sideboard':   v.get('sideboard_count', 0),
            'proxy_count': v.get('proxy_count', 0),
            'proxy_override': bool(dl.get('proxy_override')),
            'proxy_override_note': dl.get('proxy_override_note', ''),
            'status':      v.get('status'),
            'rank':        rank_map.get(p['id']),
        })
    return jsonify({'event_name': e.get('name', 'Event'), 'players': rows,
                    'allow_proxies': bool(e.get('allow_proxies')),
                    'is_complete': complete,
                    'decklists_required': bool(e.get('decklists_required',
                                                     e.get('requires_decklists', False)))})


@events_bp.route('/api/events/<event_id>/release', methods=['POST'])
@login_required
def api_release_delivery(event_id):
    """Reveal delayed pairings or standings to players."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    what = (request.json or {}).get('what')
    field = {'pairings': 'pairings_released', 'standings': 'standings_released'}.get(what)
    if not field:
        return jsonify({'error': 'Specify what to release: pairings or standings'}), 400
    was_released = e.get(field, True)
    save_event(event_id, {field: True})
    e[field] = True
    # Releasing pairings is when their Discord delivery (withheld at pairing time by
    # _deliver_pairings) actually goes out — only on the unreleased→released flip.
    if what == 'pairings' and not was_released and e.get('rounds'):
        round_num = len(e['rounds'])
        # Opt-in auto-start: with delayed pairings, the clock begins on release.
        if e.get('auto_start_timer') and e.get('round_timer_minutes'):
            now = _now_iso()
            save_event(event_id, {'round_started_at': now})
            e['round_started_at'] = now
        discord_api.announce_round(e, round_num, request.host_url)
        discord_api.dm_round_pairings(e, round_num, request.host_url)
    return jsonify({'ok': True, field: True})


@events_bp.route('/api/events/<event_id>/release-decklists', methods=['POST'])
@login_required
def api_release_decklists(event_id):
    """Make closed decklists public. Once released, the Decklists page is accessible
    to everyone and decklists appear on the aggregate /decklists page."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    save_event(event_id, {'decklists_released': True})
    _decklists_nav_cache['at'] = 0  # bust the nav-link cache
    _log_action(event_id, 'decklists', 'made decklists public')
    return jsonify({'ok': True})


@events_bp.route('/api/events/<event_id>/co-organizer-search', methods=['GET'])
@login_required
def api_co_org_search(event_id):
    """Manager typeahead over the user directory for adding a co-organizer.
    Excludes the owner and anyone already on the co-organizer list."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    q = request.args.get('q', '').strip().lower()
    if len(q) < 2:
        return jsonify([])
    excluded = {e.get('owner_id')} | set(e.get('co_organizer_ids', []))
    matches = []
    for u in list_users():
        gid = u.get('google_id')
        if not gid or gid in excluded:
            continue
        name, discord = u.get('name', ''), u.get('discord', '')
        if q in name.lower() or (discord and q in discord.lower()):
            matches.append({'google_id': gid, 'name': name, 'discord': discord})
            if len(matches) >= 8:
                break
    return jsonify(matches)


@events_bp.route('/api/events/<event_id>/co-organizers', methods=['POST'])
@login_required
def api_add_co_organizer(event_id):
    """Add a co-organizer by google_id (autocomplete pick), email, or Discord handle."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    data = request.json or {}

    profile = None
    pending_key = None

    if data.get('google_id'):
        # Direct pick from autocomplete — trust the ID, just verify it exists.
        gid = data['google_id'].strip()
        profile = get_user_profile(gid)
        if not profile:
            return jsonify({'error': 'User not found.'}), 404
        profile['google_id'] = gid
    else:
        query = data.get('query', '').strip()
        if not query:
            return jsonify({'error': 'Email or Discord handle required.'}), 400
        if '@' in query:
            profile = find_user_by_email(query)
            if not profile:
                pending_key = f'pending:{query.lower()}'
        else:
            profile = find_user_by_discord_handle(query)
            if not profile:
                return jsonify({'error': f'No account found for @{query.lstrip("@")}. '
                                         'They need to sign in to Cardboard Party at least once first.'}), 404

    resolved_id = profile['google_id'] if profile else pending_key

    if resolved_id == e.get('owner_id'):
        return jsonify({'error': 'That person is already the primary organizer.'}), 400
    existing = e.get('co_organizer_ids', [])
    if resolved_id in existing:
        return jsonify({'error': 'Already a co-organizer.'}), 400

    save_event(event_id, {'co_organizer_ids': existing + [resolved_id]})
    name = profile.get('name', data.get('query', resolved_id)) if profile else data.get('query', resolved_id)
    return jsonify({'id': resolved_id, 'name': name, 'pending': pending_key is not None}), 201


@events_bp.route('/api/events/<event_id>/co-organizers/<path:co_org_id>', methods=['DELETE'])
@login_required
def api_remove_co_organizer(event_id, co_org_id):
    """Remove a co-organizer by their stored ID (google_id or pending:<email>)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    existing = e.get('co_organizer_ids', [])
    if co_org_id not in existing:
        return jsonify({'error': 'Not a co-organizer.'}), 404
    new_ids = [x for x in existing if x != co_org_id]
    save_event(event_id, {'co_organizer_ids': new_ids})
    return jsonify({'ok': True})


@events_bp.route('/api/events/<event_id>/discord-role', methods=['POST', 'DELETE'])
@login_required
def api_discord_role(event_id):
    """Create (or re-sync) a Discord role for this event and assign it to every
    non-dropped player with a linked Discord ID — so the organiser can @mention all
    members. DELETE removes the role. Needs the event linked to a Discord channel and
    the bot to have Manage Roles."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)

    if request.method == 'DELETE':
        gid, rid = e.get('discord_guild_id'), e.get('discord_role_id')
        if gid and rid:
            discord_api.delete_guild_role(gid, rid)
        save_event(event_id, {'discord_role_id': '', 'discord_role_name': ''})
        return jsonify({'ok': True})

    channel_id = e.get('discord_channel_id')
    if not channel_id:
        return jsonify({'error': 'Link this event to a Discord channel first '
                                 f'(/{COMMAND_NAME} link in the channel).'}), 400
    guild_id = e.get('discord_guild_id') or discord_api.guild_id_for_channel(channel_id)
    if not guild_id:
        return jsonify({'error': "Couldn't find the linked Discord server — re-link "
                                 "the channel and try again."}), 400
    role_id, role_name = e.get('discord_role_id'), e.get('discord_role_name') or ''
    if not role_id:
        role_name = (str((request.json or {}).get('name', '')).strip()[:100]
                     or f"{e.get('name', 'Event')} players")
        role_id = discord_api.create_guild_role(guild_id, role_name)
        if role_id == '':
            return jsonify({'error': 'I need the Manage Roles permission (with my role '
                                     'above the new one) in your Discord server. Re-invite '
                                     'the bot with that permission, then try again.'}), 400
        if not role_id:
            return jsonify({'error': "Couldn't create the role in Discord."}), 400
    save_event(event_id, {'discord_guild_id': guild_id, 'discord_role_id': role_id,
                          'discord_role_name': role_name})
    e.update({'discord_guild_id': guild_id, 'discord_role_id': role_id,
              'discord_role_name': role_name})
    discord_api.sync_event_role(e, role_id)   # assign + post a summary to the channel
    return jsonify({'ok': True, 'role_id': role_id, 'role_name': role_name})


@events_bp.route('/api/events/<event_id>/discord/guilds', methods=['GET'])
@login_required
def api_discord_guilds(event_id):
    """Servers the bot is in *and* the organiser administers, for the web channel
    picker. Needs the organiser's Discord linked so we know their Discord user id."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    did = (get_user_profile(get_current_user()['id']).get('discord_id') or '').strip()
    if not did:
        return jsonify({'linked': False, 'guilds': []})
    guilds = [g for g in discord_api.list_bot_guilds()
              if discord_api.is_user_guild_admin(g['id'], did)]
    return jsonify({'linked': True, 'guilds': guilds})

@events_bp.route('/api/events/<event_id>/discord/channels', methods=['GET'])
@login_required
def api_discord_channels(event_id):
    """Text channels in the chosen server (organiser-only, and only a server the
    organiser administers)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    gid = request.args.get('guild_id', '').strip()
    if not gid:
        return jsonify([])
    did = (get_user_profile(get_current_user()['id']).get('discord_id') or '').strip()
    if not (did and discord_api.is_user_guild_admin(gid, did)):
        return jsonify([])
    return jsonify(discord_api.list_guild_text_channels(gid))

@events_bp.route('/api/events/<event_id>/discord/link', methods=['POST'])
@login_required
def api_discord_link(event_id):
    """Link this event to a Discord channel chosen on the web (no /cparty link needed)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    data = request.json or {}
    channel_id = str(data.get('channel_id', '')).strip()
    if not channel_id:
        return jsonify({'error': 'Choose a channel.'}), 400
    guild_id = str(data.get('guild_id', '')).strip() or (discord_api.guild_id_for_channel(channel_id) or '')
    did = (get_user_profile(get_current_user()['id']).get('discord_id') or '').strip()
    if not (did and guild_id and discord_api.is_user_guild_admin(guild_id, did)):
        return jsonify({'error': 'You can only connect a channel in a server you administer.'}), 403
    save_event(event_id, {'discord_channel_id': channel_id, 'discord_guild_id': guild_id,
                          'discord_channel_name': str(data.get('channel_name', '')).strip()[:100]})
    _log_action(event_id, 'discord', 'linked a Discord channel')
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/discord/unlink', methods=['POST'])
@login_required
def api_discord_unlink(event_id):
    """Disconnect Discord: forget the channel and (best-effort) delete the event role."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    gid, rid = e.get('discord_guild_id'), e.get('discord_role_id')
    if gid and rid:
        discord_api.delete_guild_role(gid, rid)
    save_event(event_id, {'discord_channel_id': '', 'discord_guild_id': '',
                          'discord_channel_name': '', 'discord_role_id': '',
                          'discord_role_name': '', 'discord_announce': None})
    _log_action(event_id, 'discord', 'disconnected Discord')
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/discord/announce', methods=['POST'])
@login_required
def api_discord_announce(event_id):
    """Post the event card (optionally with a message and/or an @role ping) to the
    linked channel — the web equivalent of /cparty announce."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    channel_id = e.get('discord_channel_id')
    if not channel_id:
        return jsonify({'error': 'Connect a Discord channel first.'}), 400
    data = request.json or {}
    message = str(data.get('message', '')).strip()[:1800]
    mention_role_id = e.get('discord_role_id') if data.get('mention_role') else None
    name, posted = announce_event_to_channel(event_id, channel_id, request.host_url,
                                             message, mention_role_id=mention_role_id)
    if not posted:
        return jsonify({'error': "Couldn't post — check I have permission to send messages "
                                 "in that channel."}), 400
    _log_action(event_id, 'announce', 'announced the event in Discord')
    return jsonify({'ok': True})


# ── API: edit pairings ─────────────────────────────────────────────────────────

def _pairing_state(rnd: list) -> dict:
    """Map each player in a round to their (opponent_id_or_'bye', table) — used to
    detect which players' pairings actually changed after an edit."""
    st = {}
    for m in rnd:
        t = m.get('table')
        if m.get('is_bye'):
            st[m.get('player1_id')] = ('bye', t)
        else:
            p1, p2 = m.get('player1_id'), m.get('player2_id')
            st[p1] = (p2, t)
            st[p2] = (p1, t)
    return st

@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/pairings', methods=['PUT'])
@login_required
def api_edit_pairings(event_id, round_num):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    idx = round_num - 1
    if idx < 0 or idx >= len(e['rounds']):
        return jsonify({'error': 'Round not found'}), 404
    if _is_bracket_round(e['rounds'][idx]):
        return jsonify({'error': 'Playoff pairings are set by the bracket and cannot be edited'}), 400
    has_results = any(
        m.get('winner_id') is not None or m.get('result') in DRAW_RESULTS
        for m in e['rounds'][idx] if not m.get('is_bye')
    )
    if has_results:
        return jsonify({'error': 'Cannot edit pairings after results have been entered'}), 400
    new_pairings = request.json or []
    valid_ids = {p['id'] for p in e['players']} | {BYE_PLAYER_ID}
    names = {p['id']: p['name'] for p in e['players']}
    seen: set = set()
    for match in new_pairings:
        for key in ('player1_id', 'player2_id'):
            if match.get(key) not in valid_ids:
                return jsonify({'error': f"Unknown player: {match.get(key)}"}), 400
        p1, p2 = match.get('player1_id'), match.get('player2_id')
        if match.get('is_bye'):
            # A bye is exactly one real player sitting out (vs the bye marker).
            reals = [x for x in (p1, p2) if x != BYE_PLAYER_ID]
            if len(reals) != 1:
                return jsonify({'error': 'A bye must include exactly one player'}), 400
        elif p1 == p2:
            return jsonify({'error': 'A player cannot be paired against themselves'}), 400
        for pid in (p1, p2):
            if pid == BYE_PLAYER_ID:
                continue
            if pid in seen:
                return jsonify({'error': f"{names.get(pid, pid)} is assigned to more than one match"}), 400
            seen.add(pid)

    # Every player who was in this round must still be assigned (to a match or a
    # bye) exactly once — multiple byes are fine, but no one may be left out.
    original = {pid for m in e['rounds'][idx]
                for pid in (m.get('player1_id'), m.get('player2_id'))
                if pid and pid != BYE_PLAYER_ID}
    missing = original - seen
    if missing:
        who = ', '.join(sorted(names.get(p, p) for p in missing))
        return jsonify({'error': f"{who} {'is' if len(missing) == 1 else 'are'} not in any match"}), 400
    if seen - original:
        return jsonify({'error': 'Pairings can only include players already in this round'}), 400

    old_state = _pairing_state(e['rounds'][idx])   # before re-seating/replacing
    assign_tables(new_pairings, e['players'], e)    # re-seat the rearranged matches
    e['rounds'][idx] = new_pairings
    save_event(event_id, {'rounds': e['rounds']})
    # DM the players whose opponent or table actually changed, so they know to move.
    new_state = _pairing_state(new_pairings)
    changed = [pid for pid, v in new_state.items() if pid and v != old_state.get(pid)]
    if changed:
        discord_api.dm_pairing_changed(e, round_num, changed, request.host_url)
    return jsonify({'round_num': round_num, 'pairings': new_pairings})


@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/matches/<int:match_idx>/table',
                 methods=['POST'])
@login_required
def api_set_match_table(event_id, round_num, match_idx):
    """Organiser sets (or clears) a single match's table — for venue/coverage needs,
    allowed any time during the event (even after results). Warns with 409 if the
    table is already used by another match in the same round; resend with
    {override: true} to assign it anyway. Byes are never seated. The change shows up
    in the player-facing pairings on their next load."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    idx = round_num - 1
    rounds = e.get('rounds', [])
    if not (0 <= idx < len(rounds)):
        return jsonify({'error': 'Round not found'}), 404
    rnd = rounds[idx]
    if not (0 <= match_idx < len(rnd)):
        return jsonify({'error': 'Match not found'}), 404
    match = rnd[match_idx]
    if match.get('is_bye'):
        return jsonify({'error': 'Byes are not seated at a table'}), 400
    data = request.json or {}
    raw = data.get('table')
    table = None
    if raw not in (None, '', 0, '0'):
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return jsonify({'error': 'Table must be a number'}), 400
        if not (1 <= n <= _MAX_TABLE):
            return jsonify({'error': f'Table must be between 1 and {_MAX_TABLE}'}), 400
        table = n
    # Warn (don't block) if another match in this round is already on that table —
    # the organiser can override with an explicit confirm.
    if table is not None and not data.get('override'):
        names = {p['id']: p['name'] for p in e['players']}
        for j, other in enumerate(rnd):
            if j != match_idx and not other.get('is_bye') and other.get('table') == table:
                who = ' vs '.join(names.get(other.get(k), '?')
                                  for k in ('player1_id', 'player2_id'))
                return jsonify({'error': 'duplicate', 'table': table, 'conflict': who}), 409
    match['table'] = table
    save_event(event_id, {'rounds': e['rounds']})
    # Keep Discord in sync: the posted channel pairings (no-op unless it's the
    # latest round) and the two players' pairing DMs for this match.
    discord_api.update_round_pairings(e, request.host_url)
    discord_api.update_pairing_dm_for_match(e, round_num, match_idx, request.host_url)
    nm = {p['id']: p['name'] for p in e['players']}
    pair = ' vs '.join(nm.get(match.get(k), '?') for k in ('player1_id', 'player2_id'))
    _log_action(event_id, 'table',
                f"set table {table} for {pair} (round {round_num})" if table
                else f"cleared the table for {pair} (round {round_num})")
    return jsonify({'ok': True, 'table': table})


@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/repair', methods=['POST'])
@login_required
def api_repair_round(event_id, round_num):
    """Regenerate the latest round's pairings from the current active players.

    Used to correct a round that was paired against a stale player list (e.g.
    a drop hadn't taken effect yet). Only the latest round can be re-paired —
    re-pairing an earlier round would invalidate the history later rounds were
    built on. Any results already recorded in the round are discarded (the
    client confirms first).
    """
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    idx = round_num - 1
    if idx != len(e['rounds']) - 1:
        return jsonify({'error': 'Only the latest round can be re-paired'}), 400
    if _is_bracket_round(e['rounds'][idx]):
        return jsonify({'error': 'Playoff rounds cannot be re-paired'}), 400
    # shuffle=True so a re-pair yields a *different* valid pairing (the initial
    # pair is deterministic, so re-pairing it unchanged would reproduce it exactly).
    new_round = pair_round(e['players'], e['rounds'][:idx], shuffle=True)
    assign_tables(new_round, e['players'], e)
    e['rounds'][idx] = new_round
    save_event(event_id, {'rounds': e['rounds'], **_new_round_updates(e)})

    _deliver_pairings(e, round_num, request.host_url)

    _log_action(event_id, 'repair', f're-paired round {round_num}')
    return jsonify({'round_num': round_num, 'pairings': new_round})


# ── API: results ───────────────────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/results', methods=['POST'])
def api_record_result(event_id, round_num):
    # No @login_required: guests report their own matches via a self-report token.
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    idx = round_num - 1
    if idx < 0 or idx >= len(e['rounds']):
        return jsonify({'error': 'Round not found'}), 404
    data        = request.json or {}
    match_index = data.get('match_index')
    winner_id   = data.get('winner_id')
    result      = data.get('result')
    rnd = e['rounds'][idx]
    if match_index is None or match_index < 0 or match_index >= len(rnd):
        return jsonify({'error': 'Invalid match_index'}), 400
    match = rnd[match_index]
    if not _can_report_match(e, match, data):
        return jsonify({'error': 'You can only report results for your own matches'}), 403
    if match.get('is_bye'):
        return jsonify({'error': 'Cannot record a result for a bye'}), 400
    if result == '0-0-3' and e.get('intentional_draws_frowned'):
        return jsonify({'error': 'Intentional draws are not allowed for this event'}), 400
    err = _validate_result(match, winner_id, result)
    if err:
        return jsonify({'error': err}), 400
    match['winner_id'] = winner_id
    match['result']    = result
    save_event(event_id, {'rounds': e['rounds']})
    # Tell both players over Discord: turn their pairing DM into a result card
    # (outcome + event link + a disabled "Result reported" button), or send a fresh
    # card if they have none. Covers TO/web entry, not just an opponent reporting.
    discord_api.notify_result(e, idx, match_index, request.host_url)
    # Log it, naming the reporter (signed-in user, or the guest who holds the token).
    u = get_current_user()
    actor = u['name'] if u else None
    if not actor:
        gp = _find_player_by_guest_token(e, request.headers.get('X-Guest-Token') or data.get('guest_token'))
        actor = gp['name'] if gp else None
    names = {p['id']: p['name'] for p in e['players']}
    summ = 'draw' if result in DRAW_RESULTS else (result or '')
    _log_action(event_id, 'result',
                f"reported round {round_num}: {names.get(match.get('player1_id'), '?')} vs "
                f"{names.get(match.get('player2_id'), '?')} ({summ})", actor_name=actor)
    return jsonify(match)


# ── API: admin management ──────────────────────────────────────────────────────

@events_bp.route('/api/admins', methods=['GET'])
@login_required
def api_list_admins():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    return jsonify(get_admins())

@events_bp.route('/api/admins', methods=['POST'])
@login_required
def api_add_admin():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    data  = request.json or {}
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({'error': 'Email required'}), 400

    # Look up their Google ID by fetching their profile via Google's API.
    # This only works if they've signed into Cardboard Party at least once,
    # since we store user info in the session but not in a users collection yet.
    # For now, store by email and resolve the ID on next sign-in.
    add_admin(google_id=f'pending:{email}', email=email, name=email)
    return jsonify({'ok': True, 'email': email}), 201

@events_bp.route('/api/admins/<path:admin_id>', methods=['DELETE'])
@login_required
def api_remove_admin(admin_id):
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    if admin_id == user['id']:
        return jsonify({'error': "You can't remove yourself"}), 400
    remove_admin(admin_id)
    return jsonify({'ok': True})


# ── Player profiles ────────────────────────────────────────────────────────────

@events_bp.route('/players/<google_id>')
def player_profile(google_id):
    all_events = list_events()
    profile = None
    event_history = []

    for e in sorted(all_events, key=lambda x: x.get('date', ''), reverse=True):
        player = next((p for p in e.get('players', [])
                       if p.get('google_id') == google_id), None)
        if not player:
            continue
        if not profile:
            profile = {'name': player['name'], 'discord': player.get('discord', '')}

        standings = compute_standings(e['players'], e['rounds'])
        rank = next((i + 1 for i, s in enumerate(standings)
                     if s['id'] == player['id']), None)

        wins = losses = draws = 0
        for rnd in e.get('rounds', []):
            for m in rnd:
                if m.get('is_bye'):
                    if m.get('player1_id') == player['id']:
                        wins += 1
                    continue
                if player['id'] not in (m.get('player1_id'), m.get('player2_id')):
                    continue
                if not m.get('result'):
                    continue
                if m.get('result') in DRAW_RESULTS:
                    draws += 1
                elif m.get('winner_id') == player['id']:
                    wins += 1
                else:
                    losses += 1

        standing = next((s for s in standings if s['id'] == player['id']), {})
        event_history.append({
            'id': e['id'], 'name': e['name'], 'date': e.get('date', ''),
            'rank': rank, 'total': len(standings),
            'wins': wins, 'losses': losses, 'draws': draws,
            'omw': standing.get('omw'),
            'gw':  standing.get('gw'),
            'ogw': standing.get('ogw'),
        })

    saved = get_user_profile(google_id)
    if not profile:
        # No event history yet. Build a profile from the saved users/<id> doc if
        # one exists (the person has signed in or been registered), so anyone can
        # view it. Fall back to the current user's session name for their own
        # brand-new profile.
        cur = get_current_user()
        if saved:
            profile = {'name': saved.get('name', ''),
                       'discord': saved.get('discord', '')}
        elif cur and cur['id'] == google_id:
            profile = {'name': cur.get('name', ''), 'discord': ''}
        else:
            return 'Player not found', 404

    # Merge with saved user profile (display name / discord edits)
    if saved.get('name'):
        profile['name'] = saved['name']
    if saved.get('discord'):
        profile['discord'] = saved['discord']
    # Effective avatar: custom upload if set, else the Google picture.
    profile['picture'] = (saved.get('avatar_url') or saved.get('google_picture')
                          or saved.get('discord_picture') or '')
    profile['about'] = saved.get('about', '')
    profile['pronouns'] = saved.get('pronouns', '')
    profile['pronunciation'] = saved.get('pronunciation', '')
    # Whether a Discord account is linked — drives the read-only handle vs the
    # "Link Discord" button on the edit screen (the handle is no longer free-text).
    profile['discord_linked'] = bool(saved.get('discord_id'))

    return render_template('player.html',
        user=get_current_user(),
        profile=profile,
        google_id=google_id,
        discord_enabled=discord_login_enabled(),
        event_history=event_history,
        total_wins=sum(h['wins'] for h in event_history),
        total_losses=sum(h['losses'] for h in event_history),
        total_draws=sum(h['draws'] for h in event_history),
    )


# ── Result editing (organiser only) ───────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/results/<int:match_index>', methods=['PUT'])
@login_required
def api_edit_result(event_id, round_num, match_index):
    """Organiser overwrites an already-recorded result."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)

    idx = round_num - 1
    if idx < 0 or idx >= len(e['rounds']):
        return jsonify({'error': 'Round not found'}), 404
    rnd = e['rounds'][idx]
    if match_index < 0 or match_index >= len(rnd):
        return jsonify({'error': 'Invalid match_index'}), 400

    data      = request.json or {}
    winner_id = data.get('winner_id')
    result    = data.get('result')

    if rnd[match_index].get('is_bye'):
        return jsonify({'error': 'Cannot edit a bye result'}), 400
    if result == '0-0-3' and e.get('intentional_draws_frowned'):
        return jsonify({'error': 'Intentional draws are not allowed for this event'}), 400
    err = _validate_result(rnd[match_index], winner_id, result)
    if err:
        return jsonify({'error': err}), 400

    rnd[match_index]['winner_id'] = winner_id
    rnd[match_index]['result']    = result
    save_event(event_id, {'rounds': e['rounds']})
    names = {p['id']: p['name'] for p in e['players']}
    m = rnd[match_index]
    summ = 'draw' if result in DRAW_RESULTS else (result or '')
    _log_action(event_id, 'result',
                f"edited round {round_num}: {names.get(m.get('player1_id'), '?')} vs "
                f"{names.get(m.get('player2_id'), '?')} ({summ})")
    return jsonify(rnd[match_index])


# ── User profile editing ───────────────────────────────────────────────────────

@events_bp.route('/api/profile', methods=['GET'])
@login_required
def api_get_profile():
    user = get_current_user()
    profile = get_user_profile(user['id'])
    return jsonify({
        'name':    profile.get('name', user['name']),
        'discord': profile.get('discord', ''),
        'pronouns':      profile.get('pronouns', ''),
        'pronunciation': profile.get('pronunciation', ''),
        'about':   profile.get('about', ''),
    })

# ── Admin: profile delete / merge ─────────────────────────────────────────────
# Global admins can clean up the account directory: merge a duplicate/ghost profile
# into the real one (reassigning all its event entries), or delete a spurious one.
# Player entries are never deleted — matches reference them by their per-event id —
# so merges/deletes only re-point or clear the entry's account link (google_id).

def _merge_profiles(source_id: str, target_id: str) -> dict:
    """Re-point every event entry from `source_id` to `target_id`, backfill any
    profile fields the target is missing from the source, then delete the source
    profile. A shared-event collision just leaves two target-linked entries (safe —
    deleting an entry would break its matches)."""
    reassigned = 0
    for e in list_events():
        changed = False
        for p in e.get('players', []):
            if p.get('google_id') == source_id:
                p['google_id'] = target_id
                changed = True
                reassigned += 1
        if changed:
            save_event(e['id'], {'players': e['players']})
    src, tgt = get_user_profile(source_id), get_user_profile(target_id)
    fill = {k: src[k] for k in ('discord', 'discord_id', 'discord_picture',
                                'google_picture', 'avatar_url', 'avatar_object',
                                'email', 'about', 'pronouns', 'pronunciation', 'name')
            if src.get(k) and not tgt.get(k)}
    if fill:
        save_user_profile(target_id, fill)
    delete_user_profile(source_id)
    return {'reassigned': reassigned}

def _delete_profile(profile_id: str) -> dict:
    """Delete the profile doc and unlink its event entries (google_id → None),
    keeping each as a named non-account player so results are preserved."""
    unlinked = 0
    for e in list_events():
        changed = False
        for p in e.get('players', []):
            if p.get('google_id') == profile_id:
                p['google_id'] = None
                changed = True
                unlinked += 1
        if changed:
            save_event(e['id'], {'players': e['players']})
    delete_user_profile(profile_id)
    return {'unlinked': unlinked}

@events_bp.route('/api/profiles/search', methods=['GET'])
@login_required
def api_profiles_search():
    """Admin-only directory search, for picking a merge target."""
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    q = (request.args.get('q') or '').strip().lower()
    if not q:
        return jsonify([])
    out = []
    for u in list_users():
        hay = ' '.join((u.get('name', ''), u.get('discord', ''), u.get('email', ''))).lower()
        if q in hay:
            out.append({'id': u['google_id'], 'name': u.get('name', ''),
                        'discord': u.get('discord', ''), 'email': u.get('email', '')})
        if len(out) >= 20:
            break
    return jsonify(out)

@events_bp.route('/api/profiles/<profile_id>/merge', methods=['POST'])
@login_required
def api_merge_profile(profile_id):
    """Admin: merge `profile_id` into the given target, then delete the source."""
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    target_id = (request.json or {}).get('target_id', '')
    if not target_id or target_id == profile_id:
        return jsonify({'error': 'Choose a different profile to merge into.'}), 400
    if profile_id == user['id']:
        return jsonify({'error': "You can't merge your own account."}), 400
    return jsonify({'ok': True, **_merge_profiles(profile_id, target_id)})

@events_bp.route('/api/profiles/<profile_id>', methods=['DELETE'])
@login_required
def api_delete_profile(profile_id):
    """Admin: delete a profile, unlinking (keeping) its event entries."""
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    if profile_id == user['id']:
        return jsonify({'error': "You can't delete your own account."}), 400
    return jsonify({'ok': True, **_delete_profile(profile_id)})

@events_bp.route('/api/profile', methods=['PUT'])
@login_required
def api_update_profile():
    user = get_current_user()
    data = request.json or {}
    updates = {}
    # Display name comes strictly from Google (refreshed on login) and the Discord
    # handle now comes only from linking a Discord account — neither is free-text
    # editable here, so any 'name'/'discord' in the request is ignored.
    if 'pronouns' in data:
        updates['pronouns'] = data['pronouns'].strip()[:40]
    if 'pronunciation' in data:
        updates['pronunciation'] = data['pronunciation'].strip()[:80]
    if 'about' in data:
        updates['about'] = data['about'].strip()[:1000]
    if not updates:
        return jsonify({'error': 'Nothing to update'}), 400
    save_user_profile(user['id'], updates)
    return jsonify({'ok': True, **updates})

@events_bp.route('/api/users/<google_id>/discord', methods=['PUT'])
@login_required
def api_admin_set_discord(google_id):
    """Global admins can set any user's Discord handle (e.g. to help a player
    fix or add a missing one). Limited to discord — names come from Google and
    everything else is the user's own to edit."""
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    discord = (request.json or {}).get('discord', '').strip()
    save_user_profile(google_id, {'discord': discord})
    return jsonify({'ok': True, 'discord': discord})


# ── Profile avatar (custom upload, overrides the Google picture) ──────────────

_MAX_AVATAR_BYTES = 6 * 1024 * 1024  # 6 MB before resizing

@events_bp.route('/api/profile/avatar', methods=['POST'])
@login_required
def api_upload_avatar():
    user = get_current_user()
    file = request.files.get('avatar')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400
    raw = file.read(_MAX_AVATAR_BYTES + 1)
    if not raw:
        return jsonify({'error': 'The file is empty'}), 400
    if len(raw) > _MAX_AVATAR_BYTES:
        return jsonify({'error': 'Image too large (max 6 MB)'}), 400
    try:
        url, obj = upload_avatar(user['id'], raw)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    profile = get_user_profile(user['id'])
    old_obj = profile.get('avatar_object')
    save_user_profile(user['id'], {'avatar_url': url, 'avatar_object': obj})
    if old_obj and old_obj != obj:
        delete_object(old_obj)
    session['user']['picture'] = url  # reflect in the nav this session
    return jsonify({'avatar_url': url})

@events_bp.route('/api/events/<event_id>/brand-image', methods=['POST'])
@login_required
def api_upload_brand_image(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    file = request.files.get('image')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400
    raw = file.read(_MAX_AVATAR_BYTES + 1)
    if not raw:
        return jsonify({'error': 'The file is empty'}), 400
    if len(raw) > _MAX_AVATAR_BYTES:
        return jsonify({'error': 'Image too large (max 6 MB)'}), 400
    try:
        url, obj = upload_brand_image(event_id, raw)
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400
    old_obj = e.get('brand_image_object')
    save_event(event_id, {'brand_image_url': url, 'brand_image_object': obj})
    if old_obj and old_obj != obj:
        delete_object(old_obj)
    return jsonify({'brand_image_url': url})

@events_bp.route('/api/events/<event_id>/brand-image', methods=['DELETE'])
@login_required
def api_delete_brand_image(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    old_obj = e.get('brand_image_object')
    save_event(event_id, {'brand_image_url': '', 'brand_image_object': ''})
    if old_obj:
        delete_object(old_obj)
    return jsonify({'ok': True})

@events_bp.route('/api/profile/avatar', methods=['DELETE'])
@login_required
def api_delete_avatar():
    user = get_current_user()
    profile = get_user_profile(user['id'])
    old_obj = profile.get('avatar_object')
    save_user_profile(user['id'], {'avatar_url': '', 'avatar_object': ''})
    if old_obj:
        delete_object(old_obj)
    google_pic = profile.get('google_picture', '')
    session['user']['picture'] = google_pic  # revert nav to Google picture
    return jsonify({'avatar_url': google_pic})


@events_bp.route('/api/events/<event_id>/submit-mtgdecks', methods=['POST'])
@login_required
def api_submit_mtgdecks(event_id):
    """Organiser: push completed event results and decklists to MTGDecks.net."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)

    api_key = get_secret('MTGDECKS_API_KEY').strip()
    if not api_key:
        return jsonify({'error': 'MTGDecks API key is not configured on this server.'}), 503

    rounds  = e.get('rounds', [])
    active  = [p for p in e.get('players', []) if not p.get('dropped')]
    standings = compute_standings(e['players'], rounds)
    rank_map  = {s['id']: i + 1 for i, s in enumerate(standings)}

    def match_record(pid):
        w = l = t = 0
        for rnd in rounds:
            for m in rnd:
                if m.get('is_bye'):
                    continue
                if pid not in (m['player1_id'], m['player2_id']):
                    continue
                result = m.get('result')
                if not result:
                    continue
                if result in DRAW_RESULTS:
                    t += 1
                elif m.get('winner_id') == pid:
                    w += 1
                else:
                    l += 1
        return w, l, t

    decks = []
    for p in active:
        dl   = p.get('decklist') or {}
        text = (dl.get('text') or '').strip()
        if not text:
            continue
        pid  = p['id']
        w, l, t = match_record(pid)
        deck = {'player': p.get('name', ''), 'rank': rank_map.get(pid, len(active)),
                'w': w, 'l': l, 't': t, 'txt_decklist': text}
        if dl.get('name'):
            deck['name'] = dl['name']
        decks.append(deck)

    if not decks:
        return jsonify({'error': 'No players have submitted decklists.'}), 400

    owner     = get_user_profile(e.get('owner_id', '')) or {}
    organizer = owner.get('name', '')
    event_obj = {'name': e['name'], 'date': e.get('date', ''),
                 'format': e.get('format', ''), 'attendee': len(active)}
    if organizer:
        event_obj['organizer'] = organizer

    try:
        resp = requests.post(
            'https://mtgdecks.net/api/importEvent',
            json={'Event': event_obj, 'Decks': decks},
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=30,
        )
    except Exception as ex:
        return jsonify({'error': f'Network error reaching MTGDecks: {ex}'}), 502

    if resp.status_code == 201:
        try:
            data = resp.json()
        except Exception:
            data = {}
        return jsonify({'ok': True, 'url': data.get('url', ''), 'submitted': len(decks)})

    try:
        err = resp.json()
    except Exception:
        err = {}
    errors = err.get('errors') or []
    if isinstance(errors, list) and errors:
        import re
        def _sub_deck(m):
            idx = int(m.group(1)) - 1  # MTGDecks uses 1-based indexing
            name = decks[idx]['player'] if 0 <= idx < len(decks) else m.group(0)
            return f"{name}'s deck"
        friendly = [re.sub(r'Decks\[(\d+)\]', _sub_deck, e) for e in errors]
        return jsonify({'error': f'MTGDecks rejected the submission ({resp.status_code})',
                        'errors': friendly}), 400
    msg = err.get('message') or err.get('error') or resp.text[:400] or 'Unknown error.'
    return jsonify({'error': f'MTGDecks {resp.status_code}: {msg}'}), 400
