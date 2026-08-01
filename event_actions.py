"""
Transport-free registration mutations — register, unregister, join/leave waitlist.

All functions load the event themselves, own validation + mutation, and return
(result, None) on success or (None, error_str) on failure. Transport-specific
side effects (DMs, logging, refresh_event_announcement) are the caller's
responsibility.
"""
import datetime
import uuid

from db import get_event, save_event, set_player_dropped
from event_state import (
    _is_full, _now_iso, _self_registration_blocked,
    auto_check_in, make_player_entry,
)


def register_player(event_id: str, *, name: str, google_id=None, discord: str = '',
                    discord_id=None, checked_in: bool = False,
                    entry_code: str = '') -> tuple:
    """Validate and register a player. Re-activates dropped entries in place.
    Returns (player_dict, None) or (None, error_str)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    blocked = _self_registration_blocked(e)
    if blocked:
        return None, blocked
    event_code = e.get('entry_code')
    if event_code and str(entry_code or '').strip() != event_code:
        return None, 'Incorrect entry code'
    if _is_full(e):
        cap = e.get('registration_cap', 0)
        return None, f'This event is full ({cap} players max).'
    existing = next(
        (p for p in e['players']
         if (google_id and p.get('google_id') == google_id)
         or (discord_id and p.get('discord_id') == discord_id)),
        None,
    )
    if existing:
        if not existing.get('dropped'):
            return None, 'Already registered.'
        existing['dropped'] = False
        if discord_id and not existing.get('discord_id'):
            existing['discord_id'] = discord_id
        if discord and not existing.get('discord'):
            existing['discord'] = discord
        save_event(event_id, {'players': e['players']})
        return existing, None
    player = make_player_entry(
        name, e,
        google_id=google_id, discord=discord, discord_id=discord_id,
        checked_in=checked_in,
    )
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    return player, None


def unregister_player(event_id: str, *, player_id: str = None,
                      google_id: str = None) -> tuple:
    """Validate and drop or remove a player. Returns (True, None) or (None, error_str).
    Pass player_id when the caller has already resolved identity (e.g. Discord handle
    lookup); pass google_id for web callers where google identity is authoritative."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    if player_id:
        player = next((p for p in e['players']
                       if p['id'] == player_id and not p.get('dropped')), None)
    elif google_id:
        player = next((p for p in e['players']
                       if p.get('google_id') == google_id and not p.get('dropped')), None)
    else:
        return None, 'Not registered for this event.'
    if not player:
        return None, 'Not registered for this event.'
    if not e.get('self_service_drop_enabled', True):
        return None, 'Self-service drops are disabled — contact the organiser.'
    unenroll_end = e.get('unenroll_end')
    if unenroll_end and datetime.date.today().isoformat() > unenroll_end:
        return None, 'The drop deadline has passed — contact the organiser.'
    if e['rounds']:
        set_player_dropped(event_id, player['id'], True)
    else:
        e['players'] = [p for p in e['players'] if p['id'] != player['id']]
        save_event(event_id, {'players': e['players']})
    return True, None


def join_waitlist(event_id: str, *, google_id: str = None, discord_id: str = None,
                  name: str, discord: str = '', email: str = '',
                  entry_code: str = '', extra_fields: dict = None) -> tuple:
    """Validate and add a player to the waitlist.
    Returns ({'position': int}, None) or (None, error_str)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    blocked = _self_registration_blocked(e)
    if blocked:
        return None, blocked
    event_code = e.get('entry_code')
    if event_code and str(entry_code or '').strip() != event_code:
        return None, 'Incorrect entry code'
    if not _is_full(e):
        return None, 'This event still has open spots — register normally.'
    if any(not p.get('dropped') and
           ((google_id and p.get('google_id') == google_id)
            or (discord_id and p.get('discord_id') == discord_id))
           for p in e['players']):
        return None, "You're already registered for this event."
    waitlist = e.get('waitlist') or []
    if any(w.get('status') == 'waitlisted' and
           ((google_id and w.get('google_id') == google_id)
            or (discord_id and w.get('discord_id') == discord_id))
           for w in waitlist):
        return None, "You're already on the waitlist."
    record = {
        'id':        uuid.uuid4().hex,
        'google_id': google_id,
        'name':      name[:80],
        'email':     email,
        'discord':   discord,
        'status':    'waitlisted',
        'joined_at': _now_iso(),
    }
    if discord_id:
        record['discord_id'] = discord_id
    if extra_fields:
        record.update(extra_fields)
    waitlist.append(record)
    save_event(event_id, {'waitlist': waitlist})
    position = sum(1 for w in waitlist if w.get('status') == 'waitlisted')
    return {'position': position}, None


def leave_waitlist(event_id: str, *, google_id: str = None,
                   discord_id: str = None) -> tuple:
    """Mark a waitlist entry removed_by_self.
    Returns (True, None) or (None, error_str)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    waitlist = e.get('waitlist') or []
    rec = next(
        (w for w in waitlist
         if w.get('status') == 'waitlisted' and
            ((google_id and w.get('google_id') == google_id)
             or (discord_id and w.get('discord_id') == discord_id))),
        None,
    )
    if not rec:
        return None, "You're not on the waitlist for this event."
    rec['status'] = 'removed_by_self'
    save_event(event_id, {'waitlist': waitlist})
    return True, None
