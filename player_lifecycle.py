"""
Transport-free player lifecycle — register, unregister, join/leave waitlist,
plus organiser add/remove/pin-table/seat/shuffle. Owns the full check, the
waitlist fallback and the waitlist-entry shape.

Two call styles, both returning (result, None) or (None, error_str):
  * self-service (register_player, unregister_player, join/leave_waitlist) load
    and persist the event by id;
  * organiser (add_player, remove_player, set_fixed_table, set_seat,
    shuffle_seats) take the caller's already-loaded event, mutate it in place and
    leave persistence to the caller.
Transport-specific side effects (DMs, logging, refresh_event_announcement) are the
caller's responsibility.
"""
import datetime
import random
import uuid

from db import get_event, save_event, set_player_dropped
from event_state import MAX_TABLE
from event_state import (
    entry_code_ok,
    is_full, now_iso, self_registration_blocked,
    auto_check_in, make_player_entry,
)


def _waitlist_record(name, *, google_id=None, email='', discord='', **extra) -> dict:
    return {
        'id':        uuid.uuid4().hex,
        'google_id': google_id,
        'name':      name[:80],
        'email':     email,
        'discord':   discord,
        'status':    'waitlisted',
        'joined_at': now_iso(),
        **extra,
    }


def register_player(event_id: str, *, name: str, google_id=None, discord: str = '',
                    discord_id=None, checked_in: bool = False,
                    entry_code: str = '') -> tuple:
    """Validate and register a player. Re-activates dropped entries in place.
    Returns (player_dict, None) or (None, error_str)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    blocked = self_registration_blocked(e)
    if blocked:
        return None, blocked
    if not entry_code_ok(e, entry_code):
        return None, 'Incorrect entry code'
    if is_full(e):
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
    blocked = self_registration_blocked(e)
    if blocked:
        return None, blocked
    if not entry_code_ok(e, entry_code):
        return None, 'Incorrect entry code'
    if not is_full(e):
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
    record = _waitlist_record(name, google_id=google_id, email=email, discord=discord)
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


# ── Organiser mutations (take a loaded event; caller saves) ──


def add_player(event: dict, *, name: str, google_id=None, discord: str = '',
                email: str = '') -> tuple:
    """Add a player, or route onto the waitlist if the event is full.
    Returns (player_dict, None), ({'waitlisted': True, 'name': ...}, None),
    or (None, error_str). Caller resolves google_id -> trusted name/discord/
    email from the user directory before calling."""
    if google_id and any(p.get('google_id') == google_id for p in event['players']):
        return None, 'That player is already in this event'
    if is_full(event):
        waitlist = event.get('waitlist') or []
        if google_id and any(w.get('google_id') == google_id and w.get('status') == 'waitlisted'
                             for w in waitlist):
            return None, 'That player is already on the waitlist'
        record = _waitlist_record(name, google_id=google_id, email=email, discord=discord,
                                  added_by_organizer=True)
        waitlist.append(record)
        event['waitlist'] = waitlist
        return {'waitlisted': True, 'name': record['name']}, None
    player = make_player_entry(
        name, event,
        google_id=google_id, discord=discord, checked_in=auto_check_in(event),
    )
    event['players'].append(player)
    return player, None


def remove_player(event: dict, player_id: str) -> tuple:
    """Remove a player entirely — only before pairing has started, since once
    a round exists the player is referenced by matches (drop them instead).
    Returns (removed_player, None) or (None, error_str)."""
    if event['rounds']:
        return None, 'Drop the player instead once rounds have started'
    removed = next((p for p in event['players'] if p['id'] == player_id), None)
    if removed is None:
        return None, 'Player not found'
    event['players'] = [p for p in event['players'] if p['id'] != player_id]
    return removed, None


def set_fixed_table(event: dict, player_id: str, raw_table) -> tuple:
    """Pin or clear a player's fixed seat. Pass None/''/0/'0' to clear.
    Returns (player_dict, None) or (None, error_str); player_dict['fixed_table']
    reflects the new value, absent if cleared."""
    player = next((p for p in event['players'] if p['id'] == player_id), None)
    if not player:
        return None, 'Player not found'
    table = None
    if raw_table not in (None, '', 0, '0'):
        try:
            n = int(raw_table)
        except (TypeError, ValueError):
            return None, 'Table must be a number'
        if not (1 <= n <= MAX_TABLE):
            return None, f'Table must be between 1 and {MAX_TABLE}'
        table = n
    if table is None:
        player.pop('fixed_table', None)
    else:
        player['fixed_table'] = table
    return player, None


def set_seat(event: dict, player_id: str, raw_seat) -> tuple:
    """Set or clear a player's draft pod seat number. Pass None/''/0/'0' to
    clear. Returns (player_dict, None) or (None, error_str)."""
    player = next((p for p in event['players'] if p['id'] == player_id), None)
    if not player:
        return None, 'Player not found'
    seat = None
    if raw_seat not in (None, '', 0, '0'):
        try:
            n = int(raw_seat)
        except (TypeError, ValueError):
            return None, 'Seat must be a number'
        if not (1 <= n <= 256):
            return None, 'Seat must be between 1 and 256'
        seat = n
    if seat is None:
        player.pop('seat', None)
    else:
        conflict = next((p for p in event['players']
                          if p.get('seat') == seat and p['id'] != player_id), None)
        if conflict:
            return None, f"Seat {seat} is already assigned to {conflict['name']}"
        player['seat'] = seat
    return player, None


def shuffle_seats(event: dict) -> tuple:
    """Randomly reassign seat numbers 1..N to all active (non-dropped) draft
    players. Returns (players, None) or (None, error_str)."""
    if (event.get('format') or '').lower() != 'draft':
        return None, 'Seat shuffling is only available for Draft events'
    if event.get('rounds'):
        return None, 'Seat numbers cannot be changed after Round 1 is paired'
    active = [p for p in event['players'] if not p.get('dropped')]
    seats = list(range(1, len(active) + 1))
    random.shuffle(seats)
    for player, seat in zip(active, seats):
        player['seat'] = seat
    return event['players'], None
