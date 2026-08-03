"""Tests for swiss.py pairing logic."""

import pytest
from swiss import pair_draft_r1, pair_draft_r2, pair_round, BYE_PLAYER_ID
from event_state import _validate_result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _players(n):
    """Return n players with seats 1..n."""
    return [{'id': f'p{i}', 'name': f'Player {i}', 'dropped': False, 'seat': i}
            for i in range(1, n + 1)]


def _win(p1_id, p2_id):
    return {'player1_id': p1_id, 'player2_id': p2_id,
            'winner_id': p1_id, 'result': '2-0', 'is_bye': False}


def _ids(pairings):
    """Sorted frozensets of player id pairs for order-independent comparison."""
    return {frozenset([m['player1_id'], m['player2_id']]) for m in pairings
            if not m.get('is_bye')}


# ── pair_draft_r2: standard 8-player pod ──────────────────────────────────────

def _r1_8player(players, winners=(1, 2, 3, 4)):
    """Build round 1 for an 8-player pod; `winners` lists the seat that won each match."""
    p = {pl['seat']: pl['id'] for pl in players}
    matches = []
    for seat, opp_seat in [(1, 5), (2, 6), (3, 7), (4, 8)]:
        winner_seat = seat if seat in winners else opp_seat
        loser_seat = opp_seat if winner_seat == seat else seat
        matches.append(_win(p[winner_seat], p[loser_seat]))
    return matches


def test_r2_winners_paired_by_seat_group():
    """winner(1v5) plays winner(3v7); winner(2v6) plays winner(4v8)."""
    players = _players(8)
    p = {pl['seat']: pl['id'] for pl in players}
    r1 = _r1_8player(players, winners={1, 2, 3, 4})  # low seats all win
    r2 = pair_draft_r2(players, [r1])
    pairs = _ids(r2)
    # Odd group: winner(1v5)=p1 vs winner(3v7)=p3; loser(1v5)=p5 vs loser(3v7)=p7
    assert frozenset([p[1], p[3]]) in pairs
    assert frozenset([p[5], p[7]]) in pairs
    # Even group: winner(2v6)=p2 vs winner(4v8)=p4; loser(2v6)=p6 vs loser(4v8)=p8
    assert frozenset([p[2], p[4]]) in pairs
    assert frozenset([p[6], p[8]]) in pairs
    assert len(r2) == 4


def test_r2_high_seats_win():
    """When high-numbered seats won r1, winners are still paired within their seat group."""
    players = _players(8)
    p = {pl['seat']: pl['id'] for pl in players}
    r1 = _r1_8player(players, winners={5, 6, 7, 8})  # high seats win
    r2 = pair_draft_r2(players, [r1])
    pairs = _ids(r2)
    assert frozenset([p[5], p[7]]) in pairs  # odd group winners
    assert frozenset([p[1], p[3]]) in pairs  # odd group losers
    assert frozenset([p[6], p[8]]) in pairs  # even group winners
    assert frozenset([p[2], p[4]]) in pairs  # even group losers


def test_r2_mixed_winners():
    """Mixed winner seats still honour the odd/even seat-group rule."""
    players = _players(8)
    p = {pl['seat']: pl['id'] for pl in players}
    # seat 5 beats seat 1; seat 2 beats seat 6; seat 3 beats seat 7; seat 8 beats seat 4
    r1 = _r1_8player(players, winners={5, 2, 3, 8})
    r2 = pair_draft_r2(players, [r1])
    pairs = _ids(r2)
    # Odd group: winner(1v5)=p5 vs winner(3v7)=p3; loser(1v5)=p1 vs loser(3v7)=p7
    assert frozenset([p[5], p[3]]) in pairs
    assert frozenset([p[1], p[7]]) in pairs
    # Even group: winner(2v6)=p2 vs winner(4v8)=p8; loser(2v6)=p6 vs loser(4v8)=p4
    assert frozenset([p[2], p[8]]) in pairs
    assert frozenset([p[6], p[4]]) in pairs


def test_r2_no_rematch_from_r1():
    """No round-2 pairing should repeat a round-1 match."""
    players = _players(8)
    r1 = _r1_8player(players)
    r2 = pair_draft_r2(players, [r1])
    r1_pairs = _ids(r1)
    r2_pairs = _ids(r2)
    assert r1_pairs.isdisjoint(r2_pairs)


# ── pair_draft_r2: non-standard pod sizes fall back to Swiss ──────────────────

def test_r2_4player_falls_back_to_swiss():
    """A 4-player pod has only one match per group; falls back to standard Swiss."""
    players = _players(4)
    p = {pl['seat']: pl['id'] for pl in players}
    r1 = [_win(p[1], p[3]), _win(p[2], p[4])]
    r2 = pair_draft_r2(players, [r1])
    pairs = _ids(r2)
    # Standard Swiss would pair the two winners and the two losers
    assert frozenset([p[1], p[2]]) in pairs
    assert frozenset([p[3], p[4]]) in pairs


# ── pair_draft_r2: dropped player falls back gracefully ───────────────────────

def test_r2_dropped_player_handled():
    """If a player who played in r1 drops before r2, their group falls to Swiss."""
    players = _players(8)
    # p7 (seat 7, loser of 3v7) drops
    players[6]['dropped'] = True  # seat 7 = index 6
    p = {pl['seat']: pl['id'] for pl in players if not pl['dropped']}
    r1 = _r1_8player([pl for pl in players], winners={1, 2, 3, 4})
    r2 = pair_draft_r2(players, [r1])
    # With p7 dropped, the odd group (1v5 and 3v7) can't fully pair; those 3 active
    # players fall to standard Swiss. Only the even group pair by seats.
    # Regardless, we should get valid pairings covering all 7 active players.
    active_ids = {p['id'] for p in players if not p.get('dropped')}
    paired_ids = {m['player1_id'] for m in r2} | {m['player2_id'] for m in r2
                                                    if not m.get('is_bye')}
    bye_ids = {m['player1_id'] for m in r2 if m.get('is_bye')}
    assert (paired_ids | bye_ids) >= active_ids


# ── Round 3: standard Swiss already handles it ────────────────────────────────

def _build_r2_pairings(players, r1, r2_winners):
    """Build round 2 pairings where `r2_winners` is a set of player IDs that win."""
    r2 = pair_draft_r2(players, [r1])
    results = []
    for m in r2:
        if m.get('is_bye'):
            results.append(m)
            continue
        p1, p2 = m['player1_id'], m['player2_id']
        winner = p1 if p1 in r2_winners else p2
        results.append({**m, 'winner_id': winner, 'result': '2-0'})
    return results


def test_r3_2_0_players_paired():
    """The two 2-0 players are paired with each other in round 3."""
    players = _players(8)
    p = {pl['seat']: pl['id'] for pl in players}
    r1 = _r1_8player(players, winners={1, 2, 3, 4})
    # Round 2: seat 1 and seat 3 both win (they're the odd-group r2 match)
    r2_with_results = _build_r2_pairings(players, r1, r2_winners={p[1], p[2], p[5], p[6]})
    r3 = pair_round(players, [r1, r2_with_results])
    pairs = _ids(r3)
    # p1 and p2 are 2-0; they should be paired
    assert frozenset([p[1], p[2]]) in pairs


def test_r3_0_2_players_paired():
    """The two 0-2 players are paired with each other in round 3."""
    players = _players(8)
    p = {pl['seat']: pl['id'] for pl in players}
    r1 = _r1_8player(players, winners={1, 2, 3, 4})
    r2_with_results = _build_r2_pairings(players, r1, r2_winners={p[1], p[2], p[5], p[6]})
    r3 = pair_round(players, [r1, r2_with_results])
    pairs = _ids(r3)
    # p7 and p8 are 0-2 (lost r1 and r2)
    assert frozenset([p[7], p[8]]) in pairs


def test_r3_1_1_players_avoid_rematches():
    """The four 1-1 players are paired with someone they haven't faced."""
    players = _players(8)
    p = {pl['seat']: pl['id'] for pl in players}
    r1 = _r1_8player(players, winners={1, 2, 3, 4})
    r2_with_results = _build_r2_pairings(players, r1, r2_winners={p[1], p[2], p[5], p[6]})
    r3 = pair_round(players, [r1, r2_with_results])

    # Build the full opponent history across r1 and r2
    from swiss import _opponent_history
    opp_hist = _opponent_history([r1, r2_with_results])

    for m in r3:
        if m.get('is_bye'):
            continue
        p1, p2 = m['player1_id'], m['player2_id']
        assert p2 not in opp_hist.get(p1, set()), \
            f"{p1} and {p2} already played each other"


# ── _validate_result: best_of enforcement ─────────────────────────────────────

def _match(p1='p1', p2='p2'):
    return {'player1_id': p1, 'player2_id': p2}


def test_bo3_valid_2_0():
    assert _validate_result(_match(), 'p1', '2-0', best_of=3) is None

def test_bo3_valid_2_1():
    assert _validate_result(_match(), 'p1', '2-1', best_of=3) is None

def test_bo3_valid_0_2_p2_wins():
    assert _validate_result(_match(), 'p2', '0-2', best_of=3) is None

def test_bo3_rejects_1_0():
    assert _validate_result(_match(), 'p1', '1-0', best_of=3) is not None

def test_bo3_rejects_5_3():
    assert _validate_result(_match(), 'p1', '5-3', best_of=3) is not None

def test_bo3_draw_accepted():
    assert _validate_result(_match(), None, 'draw', best_of=3) is None

def test_bo1_valid_1_0():
    assert _validate_result(_match(), 'p1', '1-0', best_of=1) is None

def test_bo1_valid_0_1_p2_wins():
    assert _validate_result(_match(), 'p2', '0-1', best_of=1) is None

def test_bo1_rejects_2_0():
    assert _validate_result(_match(), 'p1', '2-0', best_of=1) is not None

def test_bo1_rejects_2_1():
    assert _validate_result(_match(), 'p1', '2-1', best_of=1) is not None

def test_bo1_draw_accepted():
    assert _validate_result(_match(), None, 'draw', best_of=1) is None

def test_validate_result_winner_mismatch_rejected():
    assert _validate_result(_match(), 'p2', '2-0', best_of=3) is not None

def test_bo3_valid_1_0_1_p1_wins():
    assert _validate_result(_match(), 'p1', '1-0-1', best_of=3) is None

def test_bo3_valid_0_1_1_p2_wins():
    assert _validate_result(_match(), 'p2', '0-1-1', best_of=3) is None

def test_bo3_1_0_1_winner_mismatch_rejected():
    assert _validate_result(_match(), 'p2', '1-0-1', best_of=3) is not None

def test_bo1_rejects_1_0_1():
    assert _validate_result(_match(), 'p1', '1-0-1', best_of=1) is not None

def test_bo3_rejects_score_exceeding_best_of():
    assert _validate_result(_match(), 'p1', '1-0-3', best_of=3) is not None


# ── pair_round: rematch fallback ──────────────────────────────────────────────

def test_pair_round_allows_rematch_when_no_new_opponents():
    """After everyone has played everyone, pairing falls back to a rematch."""
    players = [{'id': f'p{i}', 'name': f'Player {i}', 'dropped': False}
               for i in range(1, 5)]
    # Three rounds exhaust all six pairings among four players.
    r1 = [_win('p1', 'p2'), _win('p3', 'p4')]
    r2 = [_win('p1', 'p3'), _win('p2', 'p4')]
    r3 = [_win('p1', 'p4'), _win('p2', 'p3')]
    r4 = pair_round(players, [r1, r2, r3])

    from swiss import _opponent_history
    prior = _opponent_history([r1, r2, r3])
    rematches = [
        m for m in r4 if not m.get('is_bye')
        and m['player2_id'] in prior.get(m['player1_id'], set())
    ]
    assert rematches, 'expected at least one rematch once the field is exhausted'
    assert len(_ids(r4)) == 2


# ── Bye result scores by best_of ──────────────────────────────────────────────

def test_bye_result_bo3():
    players = [{'id': 'a', 'name': 'A', 'dropped': False},
               {'id': 'b', 'name': 'B', 'dropped': False},
               {'id': 'c', 'name': 'C', 'dropped': False}]
    rnd = pair_round(players, [], best_of=3)
    bye = next(m for m in rnd if m.get('is_bye'))
    assert bye['result'] == '2-0-0'

def test_bye_result_bo1():
    players = [{'id': 'a', 'name': 'A', 'dropped': False},
               {'id': 'b', 'name': 'B', 'dropped': False},
               {'id': 'c', 'name': 'C', 'dropped': False}]
    rnd = pair_round(players, [], best_of=1)
    bye = next(m for m in rnd if m.get('is_bye'))
    assert bye['result'] == '1-0-0'

def test_draft_r1_bye_result_bo1():
    players = [{'id': f'p{i}', 'name': f'P{i}', 'dropped': False, 'seat': i}
               for i in range(1, 4)]
    rnd = pair_draft_r1(players, best_of=1)
    bye = next(m for m in rnd if m.get('is_bye'))
    assert bye['result'] == '1-0-0'
