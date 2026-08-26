"""
apply_pairing_edit locks only matches that already have a result, instead of
blocking the whole round, and allows adding a pairing for a player who wasn't
already in the round (late arrivals mid-round).
"""

from swiss import apply_pairing_edit
from tests.conftest import minimal_event


def _player(pid, name):
    return {'id': pid, 'name': name, 'google_id': None, 'dropped': False}


PLAYERS = [
    _player('p1', 'Alice'), _player('p2', 'Bob'),
    _player('p3', 'Carol'), _player('p4', 'Dave'),
    _player('p5', 'Eve'), _player('p6', 'Frank'),
]

CURRENT_ROUND = [
    {'player1_id': 'p1', 'player2_id': 'p2', 'winner_id': 'p1', 'result': '2-0-0', 'is_bye': False, 'table': 1},
    {'player1_id': 'p3', 'player2_id': 'p4', 'winner_id': None, 'result': None, 'is_bye': False, 'table': 2},
]


def _event():
    return minimal_event(players=PLAYERS)


def test_rejects_repairing_a_resulted_match():
    new_round, changed, err = apply_pairing_edit(CURRENT_ROUND, [
        {'player1_id': 'p1', 'player2_id': 'p3', 'is_bye': False},
        {'player1_id': 'p2', 'player2_id': 'p4', 'is_bye': False},
    ], PLAYERS, _event())
    assert new_round is None
    assert changed is None
    assert 'Alice vs Bob' in err


def test_rearranges_other_matches_while_preserving_the_resulted_one():
    new_round, changed, err = apply_pairing_edit(CURRENT_ROUND, [
        {'player1_id': 'p3', 'player2_id': 'p4', 'is_bye': False},   # unchanged
        {'player1_id': 'p1', 'player2_id': 'p2', 'is_bye': False},   # resulted pair, reordered position
    ], PLAYERS, _event())
    assert err is None
    resulted = next(m for m in new_round if {m['player1_id'], m['player2_id']} == {'p1', 'p2'})
    assert resulted['winner_id'] == 'p1'
    assert resulted['result'] == '2-0-0'


def test_adds_a_pairing_for_players_not_already_in_the_round():
    new_round, changed, err = apply_pairing_edit(CURRENT_ROUND, [
        {'player1_id': 'p1', 'player2_id': 'p2', 'is_bye': False},
        {'player1_id': 'p3', 'player2_id': 'p4', 'is_bye': False},
        {'player1_id': 'p5', 'player2_id': 'p6', 'is_bye': False},
    ], PLAYERS, _event())
    assert err is None
    assert len(new_round) == 3
    assert any({m['player1_id'], m['player2_id']} == {'p5', 'p6'} for m in new_round)
    assert {'p5', 'p6'} <= set(changed)
