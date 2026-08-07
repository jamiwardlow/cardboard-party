"""
api_delete_result lets an organiser clear a recorded result so the match
becomes editable again (unlocked in the edit-pairings modal).
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
        {'player1_id': 'p1', 'player2_id': '__bye__', 'winner_id': None, 'result': None, 'is_bye': True},
    ]])


def _delete(client, match_index):
    with patch('routes.events.get_event', return_value=_event_with_round()) as mock_get, \
         patch('routes.events.save_event') as mock_save:
        resp = client.delete(f'/api/events/evt1/rounds/1/results/{match_index}')
    return resp, mock_get, mock_save


def test_clears_a_resulted_match(auth_client):
    resp, _, mock_save = _delete(auth_client, 0)
    assert resp.status_code == 200
    saved_match = mock_save.call_args[0][1]['rounds'][0][0]
    assert saved_match['winner_id'] is None
    assert saved_match['result'] is None


def test_rejects_a_match_with_no_result(auth_client):
    resp, _, mock_save = _delete(auth_client, 1)
    assert resp.status_code == 400
    assert 'No result' in resp.get_json()['error']
    mock_save.assert_not_called()


def test_rejects_a_bye(auth_client):
    resp, _, mock_save = _delete(auth_client, 2)
    assert resp.status_code == 400
    assert 'bye' in resp.get_json()['error']
    mock_save.assert_not_called()


def test_requires_sign_in(client):
    resp, _, mock_save = _delete(client, 0)
    assert resp.status_code == 302
    mock_save.assert_not_called()
