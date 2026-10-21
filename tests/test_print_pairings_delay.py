from unittest.mock import patch

from tests.conftest import minimal_event

RND = [[{'player1_id': 'a', 'player2_id': 'b', 'winner_id': None}]]


def _ev(**kw):
    return minimal_event(players=[{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}],
                         rounds=RND + RND, delay_pairings=True, pairings_released=False, **kw)


def _get(c, n):
    with patch('routes.events.get_event', return_value=_ev()), \
         patch('event_queries.list_events', return_value=[]):
        return c.get(f'/events/evt1/rounds/{n}/pairings/print')


def test_held_round_404_for_public(client):
    assert _get(client, 2).status_code == 404


def test_earlier_round_still_public(client):
    assert _get(client, 1).status_code == 200


def test_manager_sees_held_round(auth_client):
    assert _get(auth_client, 2).status_code == 200
