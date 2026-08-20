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
                target_invited_since, is_invite_opted_out, is_admin)
from event_state import (_slugify, _assign_draft_seat, _is_full,
                         _self_registration_blocked, _now_iso, _validate_result,
                         make_player_entry)
from event_actions import (register_player, unregister_player,
                           join_waitlist, leave_waitlist)
from swiss import compute_standings, DRAW_RESULTS
from event_announcements import refresh_event_announcement, registration_card_status
from discord_identity import (normalize_handle, find_profile_for_discord,
                              google_id_for_discord, resolve_discord_identity)

COMMAND_NAME = os.environ.get('DISCORD_COMMAND_NAME', 'cparty')


def discord_registerable_events(limit: int = 25, owner_discord_id: str = None,
                                include_full: bool = False):
    """Events a Discord user can currently self-register for — open, not test,
    not invite-only/closed/expired, and with no entry code. Full events are excluded
    by default; pass `include_full=True` for announce/invite pickers. Soonest first.
    `owner_discord_id` restricts to that Discord user's own events."""
    owner_gid = google_id_for_discord(owner_discord_id) if owner_discord_id else None
    return event_queries.registerable_for_discord(owner_gid=owner_gid,
                                                  include_full=include_full,
                                                  limit=limit)

def register_player_via_discord(event_id: str, discord_id: str, discord_name: str,
                                discord_username: str = '', host_url: str = ''):
    """Register a Discord user as a player. When their Discord matches an existing
    account (by stored discord_id, or by the profile's Discord handle matching
    their username), the registration is linked to that account and uses its real
    name — otherwise a ghost player is created. Returns ({'player', 'event_name'},
    None) on success or (None, error_message)."""
    # Pre-flight: Discord can't enter event codes — refuse before the action module.
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    if e.get('entry_code'):
        return None, 'This event needs an entry code — please register on the web.'
    # Identity resolution: find or create a profile for this Discord user. Matches
    # by stored discord_id or by the profile's saved handle == the verified Discord
    # username (find_profile_for_discord also locks the discord_id in on a handle
    # match, so this account resolves by exact ID from here on).
    profile = find_profile_for_discord(discord_id, discord_username, discord_name)
    google_id = profile.get('google_id') if profile else None
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
    player, err = register_player(
        event_id,
        name=name, google_id=google_id, discord=discord_handle, discord_id=discord_id,
    )
    if err:
        return None, err
    event_name = e.get('name', 'the event')
    refresh_event_announcement(get_event(event_id))
    _log_discord(event_id, player.get('name', ''), 'register', 'registered via Discord')
    _dm_discord_reg_confirmation(discord_id, e, event_id, host_url)
    return {'player': player, 'event_name': event_name}, None


def withdraw_player_via_discord(event_id: str, discord_id: str, username='', display=''):
    """Withdraw a Discord-registered player from an event (the toggle counterpart
    to register_player_via_discord). Mirrors the web withdraw: drop if rounds have
    started, else remove the entry. Returns (event_name, None) or (None, error)."""
    # Discord handle normalization must happen before the action module lookup, since
    # handle-matched players (verified @handle, no discord_id stored) can only be
    # found here where the normalization logic lives.
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    event_name = e.get('name', 'the event')
    gid, handles = resolve_discord_identity(discord_id, username, display)
    player = next((p for p in e['players']
                   if not p.get('dropped') and
                      (p.get('discord_id') == discord_id
                       or (gid and p.get('google_id') == gid)
                       or (handles and normalize_handle(p.get('discord')) in handles))),
                  None)
    if not player:
        return None, "You're not registered for this event."
    result, err = unregister_player(event_id, player_id=player['id'])
    if err:
        return None, err
    refresh_event_announcement(get_event(event_id))
    _log_discord(event_id, player.get('name', ''), 'drop', 'dropped via Discord')
    return event_name, None


def discord_droppable_events(discord_id: str, username: str = '', display: str = '', limit: int = 25):
    """Events a Discord user is currently registered for (active, not dropped) — so
    they can drop themselves from the bot, including ghost players who registered
    via a button without an account. Soonest first; capped for the select menu.
    Gating (self-service allowed, deadline) is enforced on the drop itself, so the
    menu shows everything they're in and the action explains any refusal."""
    gid, handles = resolve_discord_identity(discord_id, username, display)
    out = []
    for e in list_events():
        registered = any(
            not p.get('dropped') and
            (p.get('discord_id') == discord_id
             or (gid and p.get('google_id') == gid)
             or (handles and normalize_handle(p.get('discord')) in handles))
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
    by_handle = {normalize_handle(u['discord']): u['google_id'] for u in users if u.get('discord')}
    linked = profiles_created = 0
    for e in list_events():
        changed = False
        for p in e.get('players', []):
            did = p.get('discord_id')
            if not did or p.get('google_id'):
                continue                      # not a Discord ghost / already linked
            did = str(did)
            gid = by_did.get(did) or by_handle.get(normalize_handle(p.get('discord', '')))
            if not gid:
                gid = f'discord:{did}'
                save_user_profile(gid, {'discord_id': did,
                                        'name': (p.get('name') or 'Player')[:80],
                                        'discord': p.get('discord', '')})
                by_did[did] = gid
                if p.get('discord'):
                    by_handle[normalize_handle(p['discord'])] = gid
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
    # Pre-flight: Discord can't enter event codes.
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    if e.get('entry_code'):
        return None, 'This event needs an entry code — please register on the web.'
    # Identity resolution.
    profile = find_profile_for_discord(discord_id, discord_username, discord_name)
    gid = profile.get('google_id') if profile else None
    name = ((profile.get('name') if profile else discord_name) or 'Player').strip()[:80] or 'Player'
    discord_handle = (profile.get('discord', '') if profile else '') or discord_username
    email = profile.get('email', '') if profile else ''
    result, err = join_waitlist(
        event_id,
        google_id=gid, discord_id=discord_id, name=name, discord=discord_handle, email=email,
    )
    if err:
        return None, err
    _log_discord(event_id, name, 'waitlist_join', f"{name} joined the waitlist via Discord")
    return {'event_name': e.get('name', 'the event'), 'position': result['position']}, None

def waitlist_leave_via_discord(event_id: str, discord_id: str, username: str = '', display: str = ''):
    """Remove a Discord user from a waitlist (the Leave Waitlist toggle on a DM).
    Returns (event_name, None) or (None, error_message)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    event_name = e.get('name', 'the event')
    # Use discord_id for the action module lookup; handle normalization is only needed
    # for player lookups (withdraw), not waitlist entries which always store discord_id.
    result, err = leave_waitlist(event_id, discord_id=discord_id)
    if err:
        # Fall back: try google_id resolution in case the entry predates discord_id storage.
        gid, _ = resolve_discord_identity(discord_id, username, display)
        if gid:
            result, err = leave_waitlist(event_id, google_id=gid)
    if err:
        return None, err
    _log_discord(event_id, '', 'waitlist_leave', 'left the waitlist via Discord')
    return event_name, None


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
    gid = google_id_for_discord(target_id)
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
    state, note = registration_card_status(e)
    delivered = discord_api.dm_event_invite(
        target_id, e, f'{base}/events/{event_id}', inviter_name, state, note, message,
        inviter_username=inviter_username)
    if not delivered:
        return None, ("I couldn't DM them — they may not share a server with the bot "
                      "or have DMs from server members turned off.")
    record_invite(inviter_id, target_id, event_id, now)
    return f"Invitation sent for **{e.get('name', 'the event')}**.", None


def discord_linkable_events(limit: int = 25, owner_discord_id: str = None):
    """Non-test events an organiser might link to a Discord channel (for the
    /cbp link picker), restricted to events that Discord user owns. Most recent first."""
    owner_gid = google_id_for_discord(owner_discord_id) if owner_discord_id else None
    return event_queries.linkable_for_discord(owner_gid=owner_gid, limit=limit)

def set_event_discord_channel(event_id: str, channel_id: str, discord_id: str = None):
    """Link an event so its pairings auto-post to this Discord channel. Only the
    event's owner, a co-organizer, or an admin may link it — the /cbp link picker
    only *lists* the caller's own events, but a component interaction's selected
    value isn't otherwise re-checked, so this must enforce it itself. Returns the
    event name, or None if it's gone or the caller isn't allowed to manage it."""
    e = get_event(event_id)
    if not e:
        return None
    gid = google_id_for_discord(discord_id) if discord_id else None
    if not (gid and (gid == e.get('owner_id')
                     or gid in e.get('co_organizer_ids', [])
                     or is_admin(gid))):
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
