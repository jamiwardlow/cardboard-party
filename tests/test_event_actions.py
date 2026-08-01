"""
Tests for event_actions.py — the transport-free registration mutation layer.

All tests call functions directly (no Flask client, no Firestore).
db calls are mocked at the event_actions module level.
"""
from unittest.mock import patch, MagicMock, call
import pytest
from tests.conftest import minimal_event


def make_player(name='Alice', google_id='gid_alice', discord_id=None,
                dropped=False, **kw):
    p = {
        'id': f'{name.lower()}_abc123',
        'name': name,
        'google_id': google_id,
        'discord': name.lower(),
        'dropped': dropped,
        'checked_in': False,
    }
    if discord_id:
        p['discord_id'] = discord_id
    p.update(kw)
    return p


def _patch(event, save=None, drop=None):
    """Context manager that patches all db calls in event_actions."""
    return (
        patch('event_actions.get_event', return_value=event),
        patch('event_actions.save_event', save or MagicMock()),
        patch('event_actions.set_player_dropped', drop or MagicMock()),
    )


# ── register_player ───────────────────────────────────────────────────────────

class TestRegisterPlayer:

    def _call(self, event, save=None, **kw):
        from event_actions import register_player
        with patch('event_actions.get_event', return_value=event), \
             patch('event_actions.save_event', save or MagicMock()):
            return register_player('evt1', name='Bob', google_id='gid_bob', **kw)

    def test_returns_error_when_event_not_found(self):
        from event_actions import register_player
        with patch('event_actions.get_event', return_value=None):
            player, err = register_player('evt1', name='Bob', google_id='gid_bob')
        assert player is None
        assert 'no longer exists' in err

    def test_blocks_when_registration_closed(self):
        evt = minimal_event(registration='closed')
        player, err = self._call(evt)
        assert player is None
        assert 'closed' in err.lower()

    def test_blocks_when_entry_code_wrong(self):
        evt = minimal_event(entry_code='SECRET')
        player, err = self._call(evt, entry_code='WRONG')
        assert player is None
        assert 'entry code' in err.lower()

    def test_passes_with_correct_entry_code(self):
        evt = minimal_event(entry_code='SECRET')
        player, err = self._call(evt, entry_code='SECRET')
        assert err is None
        assert player is not None

    def test_blocks_when_event_full(self):
        players = [make_player(name=f'P{i}', google_id=f'g{i}') for i in range(4)]
        evt = minimal_event(players=players, registration_cap=4)
        player, err = self._call(evt)
        assert player is None
        assert 'full' in err.lower()

    def test_blocks_when_already_registered(self):
        existing = make_player(google_id='gid_bob')
        evt = minimal_event(players=[existing])
        from event_actions import register_player
        with patch('event_actions.get_event', return_value=evt), \
             patch('event_actions.save_event', MagicMock()):
            player, err = register_player('evt1', name='Bob', google_id='gid_bob')
        assert player is None
        assert 'already registered' in err.lower()

    def test_creates_new_player_entry(self):
        evt = minimal_event()
        saved = {}

        def capture(eid, fields):
            saved.update(fields)

        player, err = self._call(evt, save=capture)
        assert err is None
        assert player is not None
        assert player['name'] == 'Bob'
        assert player['google_id'] == 'gid_bob'
        assert player['dropped'] is False
        assert 'players' in saved
        assert saved['players'][0]['id'] == player['id']

    def test_reactivates_dropped_player_by_google_id(self):
        dropped = make_player(name='Bob', google_id='gid_bob', dropped=True)
        evt = minimal_event(players=[dropped])
        saved = {}

        def capture(eid, fields):
            saved.update(fields)

        player, err = self._call(evt, save=capture)
        assert err is None
        assert player['dropped'] is False
        assert len(saved['players']) == 1

    def test_reactivates_dropped_player_by_discord_id(self):
        dropped = make_player(name='Bob', google_id='gid_bob',
                              discord_id='did_bob', dropped=True)
        evt = minimal_event(players=[dropped])
        saved = {}

        def capture(eid, fields):
            saved.update(fields)

        from event_actions import register_player
        with patch('event_actions.get_event', return_value=evt), \
             patch('event_actions.save_event', capture):
            player, err = register_player(
                'evt1', name='Bob', google_id='gid_bob', discord_id='did_bob',
            )
        assert err is None
        assert player['dropped'] is False

    def test_sets_discord_handle_on_reactivation_when_missing(self):
        dropped = make_player(name='Bob', google_id='gid_bob', dropped=True, discord='')
        evt = minimal_event(players=[dropped])
        from event_actions import register_player
        with patch('event_actions.get_event', return_value=evt), \
             patch('event_actions.save_event', MagicMock()):
            player, err = register_player(
                'evt1', name='Bob', google_id='gid_bob', discord='newhandle',
            )
        assert err is None
        assert player['discord'] == 'newhandle'

    def test_assigns_draft_seat_for_draft_events(self):
        evt = minimal_event(format='Draft')
        player, err = self._call(evt)
        assert err is None
        assert player.get('seat') == 1

    def test_checked_in_defaults_to_false(self):
        evt = minimal_event()
        player, err = self._call(evt)
        assert err is None
        assert player['checked_in'] is False


# ── unregister_player ─────────────────────────────────────────────────────────

class TestUnregisterPlayer:

    def _call(self, event, save=None, drop=None, **kw):
        from event_actions import unregister_player
        with patch('event_actions.get_event', return_value=event), \
             patch('event_actions.save_event', save or MagicMock()), \
             patch('event_actions.set_player_dropped', drop or MagicMock()):
            return unregister_player('evt1', **kw)

    def test_returns_error_when_event_not_found(self):
        from event_actions import unregister_player
        with patch('event_actions.get_event', return_value=None):
            result, err = unregister_player('evt1', google_id='gid_bob')
        assert result is None
        assert 'no longer exists' in err

    def test_returns_error_when_not_registered(self):
        evt = minimal_event()
        result, err = self._call(evt, google_id='gid_bob')
        assert result is None
        assert 'not registered' in err.lower()

    def test_blocks_when_self_service_drop_disabled(self):
        player = make_player(google_id='gid_bob')
        evt = minimal_event(players=[player], self_service_drop_enabled=False)
        result, err = self._call(evt, google_id='gid_bob')
        assert result is None
        assert 'disabled' in err.lower()

    def test_blocks_past_unenroll_deadline(self):
        player = make_player(google_id='gid_bob')
        evt = minimal_event(players=[player], unenroll_end='2020-01-01')
        result, err = self._call(evt, google_id='gid_bob')
        assert result is None
        assert 'deadline' in err.lower()

    def test_removes_player_before_rounds(self):
        player = make_player(google_id='gid_bob')
        evt = minimal_event(players=[player], rounds=[])
        saved = {}

        def capture(eid, fields):
            saved.update(fields)

        result, err = self._call(evt, save=capture, google_id='gid_bob')
        assert err is None
        assert result is True
        assert not any(p['google_id'] == 'gid_bob' for p in saved['players'])

    def test_drops_player_after_rounds_start(self):
        player = make_player(google_id='gid_bob')
        round_data = [{'player1_id': player['id'], 'player2_id': '__bye__',
                       'winner_id': None, 'result': None, 'is_bye': True}]
        evt = minimal_event(players=[player], rounds=[round_data])
        drop_mock = MagicMock()
        result, err = self._call(evt, drop=drop_mock, google_id='gid_bob')
        assert err is None
        drop_mock.assert_called_once_with('evt1', player['id'], True)

    def test_lookup_by_player_id(self):
        player = make_player(google_id='gid_bob')
        evt = minimal_event(players=[player], rounds=[])
        saved = {}

        def capture(eid, fields):
            saved.update(fields)

        result, err = self._call(evt, save=capture, player_id=player['id'])
        assert err is None
        assert result is True


# ── join_waitlist ─────────────────────────────────────────────────────────────

class TestJoinWaitlist:

    def _call(self, event, save=None, **kw):
        from event_actions import join_waitlist
        with patch('event_actions.get_event', return_value=event), \
             patch('event_actions.save_event', save or MagicMock()):
            return join_waitlist('evt1', name='Bob', google_id='gid_bob', **kw)

    def _full_event(self, extra_waitlist=None):
        players = [make_player(name=f'P{i}', google_id=f'g{i}') for i in range(4)]
        return minimal_event(players=players, registration_cap=4,
                             waitlist=extra_waitlist or [])

    def test_blocks_when_not_full(self):
        evt = minimal_event(registration_cap=8)
        result, err = self._call(evt)
        assert result is None
        assert 'open spots' in err.lower()

    def test_blocks_when_registration_closed(self):
        evt = self._full_event()
        evt['registration'] = 'closed'
        result, err = self._call(evt)
        assert result is None

    def test_blocks_when_already_registered(self):
        players = [make_player(name=f'P{i}', google_id=f'g{i}') for i in range(3)]
        players.append(make_player(name='Bob', google_id='gid_bob'))
        evt = minimal_event(players=players, registration_cap=4)
        # make it full by capping at 4
        result, err = self._call(evt)
        assert result is None
        assert 'already registered' in err.lower()

    def test_blocks_when_already_waitlisted(self):
        existing_wl = [{'id': 'w1', 'google_id': 'gid_bob', 'status': 'waitlisted'}]
        evt = self._full_event(extra_waitlist=existing_wl)
        result, err = self._call(evt)
        assert result is None
        assert 'already on the waitlist' in err.lower()

    def test_adds_to_waitlist_and_returns_position(self):
        evt = self._full_event()
        saved = {}

        def capture(eid, fields):
            saved.update(fields)

        result, err = self._call(evt, save=capture)
        assert err is None
        assert result['position'] == 1
        assert saved['waitlist'][0]['google_id'] == 'gid_bob'
        assert saved['waitlist'][0]['status'] == 'waitlisted'

    def test_extra_fields_merged_into_record(self):
        evt = self._full_event()
        saved = {}

        def capture(eid, fields):
            saved.update(fields)

        from event_actions import join_waitlist
        with patch('event_actions.get_event', return_value=evt), \
             patch('event_actions.save_event', capture):
            result, err = join_waitlist(
                'evt1', name='Bob', google_id='gid_bob', discord_id='did_bob',
                extra_fields={'added_by_organizer': True},
            )
        assert err is None
        rec = saved['waitlist'][0]
        assert rec['discord_id'] == 'did_bob'
        assert rec['added_by_organizer'] is True


# ── leave_waitlist ────────────────────────────────────────────────────────────

class TestLeaveWaitlist:

    def _call(self, event, save=None, **kw):
        from event_actions import leave_waitlist
        with patch('event_actions.get_event', return_value=event), \
             patch('event_actions.save_event', save or MagicMock()):
            return leave_waitlist('evt1', **kw)

    def test_returns_error_when_event_not_found(self):
        from event_actions import leave_waitlist
        with patch('event_actions.get_event', return_value=None):
            result, err = leave_waitlist('evt1', google_id='gid_bob')
        assert result is None
        assert 'no longer exists' in err

    def test_returns_error_when_not_on_waitlist(self):
        evt = minimal_event()
        result, err = self._call(evt, google_id='gid_bob')
        assert result is None
        assert 'not on the waitlist' in err.lower()

    def test_sets_status_removed_by_self(self):
        wl = [{'id': 'w1', 'google_id': 'gid_bob', 'name': 'Bob', 'status': 'waitlisted'}]
        evt = minimal_event(waitlist=wl)
        saved = {}

        def capture(eid, fields):
            saved.update(fields)

        result, err = self._call(evt, save=capture, google_id='gid_bob')
        assert err is None
        assert result is True
        assert saved['waitlist'][0]['status'] == 'removed_by_self'

    def test_lookup_by_discord_id(self):
        wl = [{'id': 'w1', 'discord_id': 'did_bob', 'google_id': None,
               'name': 'Bob', 'status': 'waitlisted'}]
        evt = minimal_event(waitlist=wl)
        result, err = self._call(evt, discord_id='did_bob')
        assert err is None
