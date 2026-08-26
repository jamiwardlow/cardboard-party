"""
api_edit_pairings wires apply_pairing_edit into the route: a validation error
becomes a 400 without saving, and a valid edit gets saved and DMed. Detailed
validation/locking/diff behavior is covered directly against apply_pairing_edit
in tests/test_swiss_apply_pairing_edit.py.
"""

from unittest.mock import patch
from tests.conftest import minimal_event


def _player(pid, name):
    return {'id': pid, 'name': name, 'google_id': None, 'dropped': False}


def _event_with_round():
    return minimal_event(players=[
        _player('p1', 'Alice'), _player('p2', 'Bob'),
        _player('p3', 'Carol'), _player('p4', 'Dave'),
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


def test_saves_a_valid_edit_and_returns_the_new_round(auth_client):
    resp, _, mock_save = _put(auth_client, [
        {'player1_id': 'p3', 'player2_id': 'p4', 'is_bye': False},
        {'player1_id': 'p1', 'player2_id': 'p2', 'is_bye': False},
    ])
    assert resp.status_code == 200
    saved_round = mock_save.call_args[0][1]['rounds'][0]
    assert len(saved_round) == 2
    assert resp.get_json()['round_num'] == 1
