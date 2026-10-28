"""
Transport-free round starting — the one place that owns the completion check,
player selection, tables, timer and delivery order for pair / cut / repair.
Handlers do auth and jsonify; each function returns `(result, None)` or
`(None, error_str)` and has already saved the event and delivered pairings.
"""
import random

import discord_api
from db import save_event
from event_announcements import refresh_event_announcement
from event_state import _now_iso, _is_bracket_round, _swiss_complete
from swiss import (pair_round, pair_draft_r1, pair_draft_r2, compute_standings,
                   default_num_rounds, make_bracket, next_bracket_round,
                   assign_tables, CUT_SIZES, DRAW_RESULTS)


def _auto_starts_timer(event: dict) -> bool:
    """Simple events always auto-start a configured round timer; advanced events opt in."""
    return bool(event.get('round_timer_minutes')
                and (event.get('auto_start_timer') or not event.get('advanced')))


def _new_round_updates(event: dict) -> dict:
    """Fields to set whenever a new round is created: reset the round timer and,
    where delivery is delayed, re-hide pairings/standings until the organiser
    releases them.

    Advanced events wait for the organiser's Start-timer button unless they opt in
    via `auto_start_timer`; simple events always auto-start. The timer then starts the moment pairings go live: now
    if they post immediately, or on release if delayed (see api_release_delivery),
    so the clock never runs while players can't yet see their pairings."""
    u = {'round_started_at': ''}
    if event.get('delay_pairings'):
        u['pairings_released'] = False
    if event.get('delay_standings'):
        u['standings_released'] = False
    if _auto_starts_timer(event) and not event.get('delay_pairings'):
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


def _commit(event, updates, round_num, base_url, *, announce=False):
    """Save, fold into the in-memory event (so delivery sees an auto-started timer
    and the released flags), then deliver — in that order."""
    save_event(event['id'], updates)
    event.update(updates)
    if announce:
        refresh_event_announcement(event)
    _deliver_pairings(event, round_num, base_url)


def _seat_unseated(event, players_to_pair):
    """Fill seat gaps for players who joined before seats were assigned at registration."""
    active = [p for p in players_to_pair if not p.get('dropped')]
    unseated = [p for p in active if not isinstance(p.get('seat'), int)]
    random.shuffle(unseated)
    used = {p['seat'] for p in active if isinstance(p.get('seat'), int)}
    seat = 1
    for p in unseated:
        while seat in used:
            seat += 1
        for ep in event['players']:
            if ep['id'] == p['id']:
                ep['seat'] = seat
                break
        p['seat'] = seat
        used.add(seat)
        seat += 1


def pair_next_round(event: dict, base_url: str):
    """Pair the next Swiss/draft round, or advance a running playoff bracket."""
    rounds = event['rounds']
    if rounds and _is_bracket_round(rounds[-1]):
        # Single elimination has no draws — every match needs a decisive winner.
        last = rounds[-1]
        if len(last) <= 1:
            return None, 'The playoff is already complete'
        if any(m.get('winner_id') is None for m in last):
            return None, ('Every playoff match needs a winner before advancing '
                          '(resolve any draws first)')
        new_round = next_bracket_round(last)
        assign_tables(new_round, event['players'], event)
        rounds.append(new_round)
        _commit(event, {'rounds': rounds, **_new_round_updates(event)}, len(rounds), base_url)
        return {'round_num': len(rounds), 'pairings': new_round}, None

    if rounds and event.get('event_type') != 'League':
        if any(not m.get('is_bye') and m.get('winner_id') is None
               and m.get('result') not in DRAW_RESULTS for m in rounds[-1]):
            return None, 'Previous round has unrecorded results'
    num_rounds = event['num_rounds'] or default_num_rounds(len(event['players']))
    if len(rounds) >= num_rounds:
        return None, 'All rounds already paired'
    # When check-in is required, only checked-in players are paired.
    players_to_pair = event['players']
    if event.get('require_check_in'):
        players_to_pair = [p for p in event['players'] if p.get('checked_in')]
        if len([p for p in players_to_pair if not p.get('dropped')]) < 2:
            return None, 'Need at least 2 checked-in players to pair'
    best_of = event.get('best_of', 3)
    is_draft = (event.get('format') or '').lower() == 'draft'
    is_draft_r1 = is_draft and not rounds
    if is_draft_r1:
        _seat_unseated(event, players_to_pair)
        new_round = pair_draft_r1(players_to_pair, bracket=event.get('structure') == 'single_elim',
                                  best_of=best_of)
    elif is_draft and len(rounds) == 1:
        new_round = pair_draft_r2(players_to_pair, rounds, best_of=best_of)
    else:
        new_round = pair_round(players_to_pair, rounds, best_of=best_of)

    assign_tables(new_round, event['players'], event)
    rounds.append(new_round)
    updates = {'rounds': rounds, 'status': 'active', 'registration': 'closed',
               **_new_round_updates(event)}
    if is_draft_r1:
        updates['players'] = event['players']   # persist seat assignments
    _commit(event, updates, len(rounds), base_url, announce=True)
    return {'round_num': len(rounds), 'pairings': new_round}, None


def cut_to_top(event: dict, cut_size, base_url: str):
    """Start a single-elimination playoff for the top `cut_size` players."""
    rounds = event['rounds']
    if rounds and _is_bracket_round(rounds[-1]):
        return None, 'A playoff bracket has already started'
    if not _swiss_complete(event):
        return None, 'Finish all rounds before cutting to a playoff'
    if event.get('structure') == 'swiss':
        return None, 'This event is set to Swiss only — no playoff cut'
    if cut_size not in CUT_SIZES:
        return None, 'Cut size must be 4, 8, or 16'
    active = [p for p in event['players'] if not p.get('dropped')]
    if cut_size > len(active):
        return None, f'Only {len(active)} active players — not enough for a top {cut_size}'

    new_round, seeds = make_bracket(compute_standings(event['players'], rounds), cut_size)
    assign_tables(new_round, event['players'], event)
    rounds.append(new_round)
    _commit(event, {'rounds': rounds, 'cut_size': cut_size, 'cut_seeds': seeds,
                    **_new_round_updates(event)}, len(rounds), base_url)
    return {'round_num': len(rounds), 'cut_size': cut_size, 'pairings': new_round}, None


def repair_round(event: dict, round_num: int, base_url: str):
    """Re-pair the latest round from the current active players, discarding its results."""
    rounds = event['rounds']
    idx = round_num - 1
    if idx != len(rounds) - 1:
        return None, 'Only the latest round can be re-paired'
    if _is_bracket_round(rounds[idx]):
        return None, 'Playoff rounds cannot be re-paired'
    # shuffle=True so a re-pair yields a *different* valid pairing (the initial
    # pair is deterministic, so re-pairing it unchanged would reproduce it exactly).
    new_round = pair_round(event['players'], rounds[:idx], shuffle=True,
                           best_of=event.get('best_of', 3))
    assign_tables(new_round, event['players'], event)
    rounds[idx] = new_round
    _commit(event, {'rounds': rounds, **_new_round_updates(event)}, round_num, base_url)
    return {'round_num': round_num, 'pairings': new_round}, None
