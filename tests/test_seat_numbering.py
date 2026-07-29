"""
Tests for seat numbering features on Draft events:
  - Waitlist promotion assigns a seat
  - api_set_seat rejects duplicate seat numbers
  - api_shuffle_seats randomises seats 1..N across active players
"""

from unittest.mock import patch
from tests.conftest import minimal_event


# ── Waitlist promotion ────────────────────────────────────────────────────────

def _fake_promote(event_id, wid, build_player, promoter, now):
    """promote_waitlist_entry stub that calls build_player and returns ok."""
    rec = {'id': wid, 'name': 'Bob', 'google_id': None, 'discord': '', 'discord_id': None}
    player = build_player(rec, 1)
    return ('ok', player)


def test_promote_waitlist_assigns_seat_on_draft(auth_client):
    evt = minimal_event(
        format='Draft',
        players=[{'id': 'p1', 'name': 'Alice', 'dropped': False, 'seat': 1}],
        waitlist=[{'id': 'w1', 'name': 'Bob', 'status': 'waitlisted'}],
    )
    with patch('routes.events.get_event', return_value=evt), \
         patch('routes.events.promote_waitlist_entry', side_effect=_fake_promote), \
         patch('routes.events.refresh_event_announcement'), \
         patch('routes.events._log_action'):
        resp = auth_client.post('/api/events/evt1/waitlist/w1/promote')

    assert resp.status_code == 200
    assert resp.get_json()['player']['seat'] == 2


def test_promote_waitlist_no_seat_on_non_draft(auth_client):
    evt = minimal_event(
        format='Standard',
        players=[],
        waitlist=[{'id': 'w1', 'name': 'Bob', 'status': 'waitlisted'}],
    )
    with patch('routes.events.get_event', return_value=evt), \
         patch('routes.events.promote_waitlist_entry', side_effect=_fake_promote), \
         patch('routes.events.refresh_event_announcement'), \
         patch('routes.events._log_action'):
        resp = auth_client.post('/api/events/evt1/waitlist/w1/promote')

    assert resp.status_code == 200
    assert 'seat' not in resp.get_json()['player']


# ── api_set_seat uniqueness ───────────────────────────────────────────────────

def test_set_seat_rejects_duplicate(auth_client):
    evt = minimal_event(players=[
        {'id': 'p1', 'name': 'Alice', 'dropped': False, 'seat': 3},
        {'id': 'p2', 'name': 'Bob',   'dropped': False},
    ])
    with patch('routes.events.get_event', return_value=evt), \
         patch('routes.events.save_event'):
        resp = auth_client.post('/api/events/evt1/players/p2/seat', json={'seat': 3})

    assert resp.status_code == 409
    assert 'Alice' in resp.get_json()['error']


def test_set_seat_allows_reassigning_own_seat(auth_client):
    """Setting a player's seat to their current value should succeed."""
    evt = minimal_event(players=[
        {'id': 'p1', 'name': 'Alice', 'dropped': False, 'seat': 3},
    ])
    with patch('routes.events.get_event', return_value=evt), \
         patch('routes.events.save_event'):
        resp = auth_client.post('/api/events/evt1/players/p1/seat', json={'seat': 3})

    assert resp.status_code == 200


def test_set_seat_allows_free_seat(auth_client):
    evt = minimal_event(players=[
        {'id': 'p1', 'name': 'Alice', 'dropped': False, 'seat': 1},
        {'id': 'p2', 'name': 'Bob',   'dropped': False},
    ])
    with patch('routes.events.get_event', return_value=evt), \
         patch('routes.events.save_event'):
        resp = auth_client.post('/api/events/evt1/players/p2/seat', json={'seat': 2})

    assert resp.status_code == 200
    assert resp.get_json()['seat'] == 2


# ── api_shuffle_seats ─────────────────────────────────────────────────────────

def test_shuffle_seats_rejected_for_non_draft(auth_client):
    evt = minimal_event(
        format='Standard',
        players=[{'id': 'p1', 'name': 'Alice', 'dropped': False}],
    )
    with patch('routes.events.get_event', return_value=evt):
        resp = auth_client.post('/api/events/evt1/seats/shuffle')

    assert resp.status_code == 400


def test_shuffle_seats_assigns_unique_1_to_n(auth_client):
    evt = minimal_event(
        format='Draft',
        players=[
            {'id': 'p1', 'name': 'Alice',   'dropped': False, 'seat': 1},
            {'id': 'p2', 'name': 'Bob',     'dropped': False, 'seat': 2},
            {'id': 'p3', 'name': 'Charlie', 'dropped': False, 'seat': 3},
        ],
    )
    with patch('routes.events.get_event', return_value=evt), \
         patch('routes.events.save_event'):
        resp = auth_client.post('/api/events/evt1/seats/shuffle')

    assert resp.status_code == 200
    seats = sorted(p['seat'] for p in resp.get_json()['players'])
    assert seats == [1, 2, 3]


def test_shuffle_seats_blocked_after_round_1(auth_client):
    evt = minimal_event(
        format='Draft',
        rounds=[[{'player1_id': 'p1', 'player2_id': 'p2', 'winner_id': None, 'is_bye': False}]],
        players=[
            {'id': 'p1', 'name': 'Alice', 'dropped': False, 'seat': 1},
            {'id': 'p2', 'name': 'Bob',   'dropped': False, 'seat': 2},
        ],
    )
    with patch('routes.events.get_event', return_value=evt):
        resp = auth_client.post('/api/events/evt1/seats/shuffle')

    assert resp.status_code == 400


def test_shuffle_seats_excludes_dropped_players(auth_client):
    evt = minimal_event(
        format='Draft',
        players=[
            {'id': 'p1', 'name': 'Alice',   'dropped': False, 'seat': 1},
            {'id': 'p2', 'name': 'Bob',     'dropped': True,  'seat': 99},
            {'id': 'p3', 'name': 'Charlie', 'dropped': False, 'seat': 3},
        ],
    )
    with patch('routes.events.get_event', return_value=evt), \
         patch('routes.events.save_event'):
        resp = auth_client.post('/api/events/evt1/seats/shuffle')

    assert resp.status_code == 200
    players = resp.get_json()['players']
    active_seats = sorted(p['seat'] for p in players if not p.get('dropped'))
    assert active_seats == [1, 2]
