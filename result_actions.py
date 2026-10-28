"""
Transport-free result recording — the one place that owns bounds, bye and
intentional-draw rules, score validation, persistence and the Discord result
card. Callers (web routes, Discord) supply who/where and write their own log
line. Each function returns `(match, None)` or `(None, error_str)`.
"""
import discord_api
from db import save_event
from event_state import _validate_result
from swiss import DRAW_RESULTS


def get_match(event: dict, round_idx: int, match_idx) -> dict | None:
    rounds = event.get('rounds', [])
    if not (0 <= round_idx < len(rounds)):
        return None
    rnd = rounds[round_idx]
    if not isinstance(match_idx, int) or not (0 <= match_idx < len(rnd)):
        return None
    return rnd[match_idx]


def _write(event, match, winner_id, result):
    match['winner_id'] = winner_id
    match['result'] = result
    save_event(event['id'], {'rounds': event['rounds']})
    return match


def _checked(event, round_idx, match_idx, winner_id, result, verb):
    match = get_match(event, round_idx, match_idx)
    if match is None:
        return None, 'Invalid match_index'
    if match.get('is_bye'):
        return None, f'Cannot {verb} a result for a bye'
    if result == '0-0-3' and event.get('intentional_draws_frowned'):
        return None, 'Intentional draws are not allowed for this event'
    err = _validate_result(match, winner_id, result, event.get('best_of', 3))
    return (None, err) if err else (match, None)


def record_result(event, round_idx, match_idx, winner_id, result, base_url='',
                  exclude_player_id=None):
    """Validate, store and announce a result. `exclude_player_id` skips that
    player's Discord card (a Discord reporter's own reply already covers it)."""
    match, err = _checked(event, round_idx, match_idx, winner_id, result, 'record')
    if err:
        return None, err
    _write(event, match, winner_id, result)
    discord_api.notify_result(event, round_idx, match_idx, base_url,
                              exclude_player_id=exclude_player_id)
    return match, None


def edit_result(event, round_idx, match_idx, winner_id, result, base_url=''):
    """Organiser overwrite. Same rules as record; players' cards are refreshed too."""
    match, err = _checked(event, round_idx, match_idx, winner_id, result, 'edit')
    if err:
        return None, err
    _write(event, match, winner_id, result)
    discord_api.notify_result(event, round_idx, match_idx, base_url)
    return match, None


def delete_result(event, round_idx, match_idx):
    """Clear a recorded result. Returns the match as it was (copy) so the caller can log it.
    No Discord update: a cleared match has no outcome to show."""
    match = get_match(event, round_idx, match_idx)
    if match is None:
        return None, 'Invalid match_index'
    if match.get('is_bye'):
        return None, 'Cannot delete a bye result'
    if match.get('winner_id') is None and match.get('result') not in DRAW_RESULTS:
        return None, 'No result to delete'
    was = dict(match)
    _write(event, match, None, None)
    return was, None
