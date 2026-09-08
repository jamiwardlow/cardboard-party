"""
api_cut_to_top refuses to start a playoff bracket on events explicitly
declared 'swiss' (Swiss only) at creation, even though Swiss is complete.
"""

from unittest.mock import patch
from tests.conftest import minimal_event


def _player(pid, name):
    return {'id': pid, 'name': name, 'google_id': None, 'dropped': False}


def _completed_swiss_event(**overrides):
    return minimal_event(
        num_rounds=1,
        players=[_player('p1', 'Alice'), _player('p2', 'Bob'),
                 _player('p3', 'Carol'), _player('p4', 'Dave')],
        rounds=[[
            {'player1_id': 'p1', 'player2_id': 'p2', 'winner_id': 'p1', 'result': '2-0-0', 'is_bye': False, 'table': 1},
            {'player1_id': 'p3', 'player2_id': 'p4', 'winner_id': 'p3', 'result': '2-0-0', 'is_bye': False, 'table': 2},
        ]],
        **overrides,
    )


def _cut(auth_client, event):
    with patch('routes.events.get_event', return_value=event), \
         patch('routes.events.save_event') as mock_save, \
         patch('routes.events.discord_api.announce_round'), \
         patch('routes.events.discord_api.dm_round_pairings'):
        resp = auth_client.post('/api/events/evt1/cut', json={'cut_size': 4})
    return resp, mock_save


def test_rejects_cut_on_declared_swiss_only_event(auth_client):
    resp, mock_save = _cut(auth_client, _completed_swiss_event(structure='swiss'))
    assert resp.status_code == 400
    assert 'Swiss only' in resp.get_json()['error']
    mock_save.assert_not_called()


def test_allows_cut_when_structure_unset(auth_client):
    resp, mock_save = _cut(auth_client, _completed_swiss_event())
    assert resp.status_code == 200
    mock_save.assert_called_once()
