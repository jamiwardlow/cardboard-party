"""
Transport-free organiser player-management mutations: add, remove, pin a
fixed table, set a draft seat, shuffle seats.

Unlike event_actions.py (which loads its own event and persists), these take
the already-loaded event dict, mutate it in place, and return (result, None)
on success or (None, error_str) on failure. Every call site already has the
event loaded (permission checks require it), so a second Firestore read would
be redundant. Persistence and side effects (save_event, refresh_event_announcement,
DMs, logging) stay the caller's responsibility.
"""
import random
import uuid

from event_state import _is_full, _now_iso, auto_check_in, make_player_entry
from routes.event_fields import _MAX_TABLE


def add_player(event: dict, *, name: str, google_id=None, discord: str = '',
                email: str = '') -> tuple:
    """Add a player, or route onto the waitlist if the event is full.
    Returns (player_dict, None), ({'waitlisted': True, 'name': ...}, None),
    or (None, error_str). Caller resolves google_id -> trusted name/discord/
    email from the user directory before calling."""
    if google_id and any(p.get('google_id') == google_id for p in event['players']):
        return None, 'That player is already in this event'
    if _is_full(event):
        waitlist = event.get('waitlist') or []
        if google_id and any(w.get('google_id') == google_id and w.get('status') == 'waitlisted'
                             for w in waitlist):
            return None, 'That player is already on the waitlist'
        record = {
            'id':        uuid.uuid4().hex,
            'google_id': google_id,
            'name':      name[:80],
            'email':     email,
            'discord':   discord,
            'status':    'waitlisted',
            'joined_at': _now_iso(),
            'added_by_organizer': True,
        }
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
        if not (1 <= n <= _MAX_TABLE):
            return None, f'Table must be between 1 and {_MAX_TABLE}'
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
