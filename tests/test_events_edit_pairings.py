"""
api_edit_pairings locks only matches that already have a result, instead of
blocking the whole round, and allows adding a pairing for a player who wasn't
already in the round (late arrivals mid-round).
"""

from unittest.mock import patch
from tests.conftest import minimal_event


def _player(pid, name):
    return {'id': pid, 'name': name, 'google_id': None, 'dropped': False}


def _event_with_round():
    return minimal_event(players=[
        _player('p1', 'Alice'), _player('p2', 'Bob'),
        _player('p3', 'Carol'), _player('p4', 'Dave'),
        _player('p5', 'Eve'), _player('p6', 'Frank'),
    ], rounds=[[
        {'player1_id': 'p1', 'player2_id': 'p2', 'winner_id': 'p1', 'result': '2-0-0', 'is_bye': False, 'table': 1},
        {'player1_id': 'p3', 'player2_id': 'p4', 'winner_id': None, 'result': None, 'is_bye': False, 'table': 2},
    ]])


def _put(auth_client, payload):
    with patch('routes.events.get_event', return_value=_event_with_round()) as mock_get, \
         patch('routes.events.save_event') as mock_save, \
         patch('routes.events.discord_api.dm_pairing_changed'):
        resp = auth_client.put('/api/events/evt1/rounds/1/pairings', json=payload)
    return resp, mock_get, mock_save


def test_rejects_repairing_a_resulted_match(auth_client):
    resp, _, mock_save = _put(auth_client, [
        {'player1_id': 'p1', 'player2_id': 'p3', 'is_bye': False},
        {'player1_id': 'p2', 'player2_id': 'p4', 'is_bye': False},
    ])
    assert resp.status_code == 400
    assert 'Alice vs Bob' in resp.get_json()['error']
    mock_save.assert_not_called()


def test_rearranges_other_matches_while_preserving_the_resulted_one(auth_client):
    resp, _, mock_save = _put(auth_client, [
        {'player1_id': 'p3', 'player2_id': 'p4', 'is_bye': False},   # unchanged
        {'player1_id': 'p1', 'player2_id': 'p2', 'is_bye': False},   # resulted pair, reordered position
    ])
    assert resp.status_code == 200
    saved_round = mock_save.call_args[0][1]['rounds'][0]
    resulted = next(m for m in saved_round if {m['player1_id'], m['player2_id']} == {'p1', 'p2'})
    assert resulted['winner_id'] == 'p1'
    assert resulted['result'] == '2-0-0'


def test_adds_a_pairing_for_players_not_already_in_the_round(auth_client):
    resp, _, mock_save = _put(auth_client, [
        {'player1_id': 'p1', 'player2_id': 'p2', 'is_bye': False},
        {'player1_id': 'p3', 'player2_id': 'p4', 'is_bye': False},
        {'player1_id': 'p5', 'player2_id': 'p6', 'is_bye': False},
    ])
    assert resp.status_code == 200
    saved_round = mock_save.call_args[0][1]['rounds'][0]
    assert len(saved_round) == 3
    assert any({m['player1_id'], m['player2_id']} == {'p5', 'p6'} for m in saved_round)
