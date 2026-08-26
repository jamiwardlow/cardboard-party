"""
Tests for player_actions.py — organiser player-management mutations.

All tests call functions directly against a plain dict (no Flask, no mocks).
"""
from tests.conftest import minimal_event
from player_actions import add_player, remove_player, set_fixed_table, set_seat, shuffle_seats


def _player(pid, name, **kw):
    p = {'id': pid, 'name': name, 'google_id': None, 'discord': '', 'dropped': False}
    p.update(kw)
    return p


# ── add_player ──────────────────────────────────────────────────────────────

def test_add_player_appends_when_room():
    e = minimal_event(players=[])
    player, err = add_player(e, name='Alice')
    assert err is None
    assert player['name'] == 'Alice'
    assert e['players'] == [player]


def test_add_player_rejects_duplicate_google_id():
    e = minimal_event(players=[_player('p1', 'Alice', google_id='gid1')])
    result, err = add_player(e, name='Alice again', google_id='gid1')
    assert result is None
    assert 'already in this event' in err


def test_add_player_waitlists_when_full():
    e = minimal_event(registration_cap=1, players=[_player('p1', 'Alice')])
    result, err = add_player(e, name='Bob')
    assert err is None
    assert result == {'waitlisted': True, 'name': 'Bob'}
    assert e['waitlist'][0]['name'] == 'Bob'
    assert len(e['players']) == 1


def test_add_player_rejects_duplicate_waitlist_join():
    e = minimal_event(registration_cap=1, players=[_player('p1', 'Alice')],
                       waitlist=[{'google_id': 'gid_bob', 'status': 'waitlisted'}])
    result, err = add_player(e, name='Bob', google_id='gid_bob')
    assert result is None
    assert 'already on the waitlist' in err


# ── remove_player ───────────────────────────────────────────────────────────

def test_remove_player_deletes_before_rounds_start():
    e = minimal_event(players=[_player('p1', 'Alice'), _player('p2', 'Bob')])
    removed, err = remove_player(e, 'p1')
    assert err is None
    assert removed['name'] == 'Alice'
    assert [p['id'] for p in e['players']] == ['p2']


def test_remove_player_blocked_once_rounds_exist():
    e = minimal_event(players=[_player('p1', 'Alice')], rounds=[[{}]])
    result, err = remove_player(e, 'p1')
    assert result is None
    assert 'Drop the player instead' in err


def test_remove_player_not_found():
    e = minimal_event(players=[_player('p1', 'Alice')])
    result, err = remove_player(e, 'nope')
    assert result is None
    assert err == 'Player not found'


# ── set_fixed_table ──────────────────────────────────────────────────────────

def test_set_fixed_table_sets_and_clears():
    e = minimal_event(players=[_player('p1', 'Alice')])
    player, err = set_fixed_table(e, 'p1', 5)
    assert err is None
    assert player['fixed_table'] == 5

    player, err = set_fixed_table(e, 'p1', None)
    assert err is None
    assert 'fixed_table' not in player


def test_set_fixed_table_rejects_out_of_range():
    e = minimal_event(players=[_player('p1', 'Alice')])
    result, err = set_fixed_table(e, 'p1', 'abc')
    assert result is None
    assert 'must be a number' in err

    result, err = set_fixed_table(e, 'p1', 100000)
    assert result is None
    assert 'must be between' in err


# ── set_seat ─────────────────────────────────────────────────────────────────

def test_set_seat_rejects_conflict():
    e = minimal_event(players=[_player('p1', 'Alice', seat=3), _player('p2', 'Bob')])
    result, err = set_seat(e, 'p2', 3)
    assert result is None
    assert 'Alice' in err


def test_set_seat_allows_own_current_seat():
    e = minimal_event(players=[_player('p1', 'Alice', seat=3)])
    player, err = set_seat(e, 'p1', 3)
    assert err is None
    assert player['seat'] == 3


# ── shuffle_seats ────────────────────────────────────────────────────────────

def test_shuffle_seats_requires_draft_format():
    e = minimal_event(format='Standard', players=[_player('p1', 'Alice')])
    result, err = shuffle_seats(e)
    assert result is None
    assert 'Draft' in err


def test_shuffle_seats_blocked_after_round_1():
    e = minimal_event(format='Draft', players=[_player('p1', 'Alice')], rounds=[[{}]])
    result, err = shuffle_seats(e)
    assert result is None
    assert 'Round 1' in err


def test_shuffle_seats_excludes_dropped():
    e = minimal_event(format='Draft', players=[
        _player('p1', 'Alice'), _player('p2', 'Bob', dropped=True),
    ])
    players, err = shuffle_seats(e)
    assert err is None
    assert players[0]['seat'] == 1
    assert 'seat' not in players[1]
