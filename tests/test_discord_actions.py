"""
Tests for discord_actions.py — the Discord mutation layer.

All tests call functions directly (no Flask test client, no Firestore).
db calls are mocked at the discord_actions module level so the patch targets
match what the functions actually import.
"""
from unittest.mock import patch, MagicMock, call
import pytest
from tests.conftest import minimal_event


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_player(name='Alice', discord_id='111', google_id='gid_alice', dropped=False, **kw):
    return {
        'id': f'{name.lower()}_abc123',
        'name': name,
        'google_id': google_id,
        'discord_id': discord_id,
        'discord': name.lower(),
        'dropped': dropped,
        'checked_in': False,
        **kw,
    }


def no_profile(*args, **kw):
    """Stub for list_users — returns empty list so no account matches."""
    return []


# ── register_player_via_discord ───────────────────────────────────────────────

class TestRegisterViaDiscord:

    def _call(self, event, list_users=None, save_event=None, save_profile=None,
              discord_id='999', discord_name='Bob', username='bob', host_url='http://test/'):
        from discord_actions import register_player_via_discord
        _save = save_event or MagicMock()
        with patch('discord_actions.get_event', return_value=event), \
             patch('event_actions.get_event', return_value=event), \
             patch('event_actions.save_event', _save), \
             patch('discord_actions.list_users', return_value=list_users or []), \
             patch('discord_actions.save_event', _save), \
             patch('discord_actions.save_user_profile', save_profile or MagicMock()), \
             patch('discord_actions.refresh_event_announcement'), \
             patch('discord_actions.add_event_log'), \
             patch('discord_actions.discord_api'):
            return register_player_via_discord(
                'evt1', discord_id, discord_name, username, host_url=host_url)

    def test_blocks_when_event_not_found(self):
        from discord_actions import register_player_via_discord
        with patch('discord_actions.get_event', return_value=None):
            result, err = register_player_via_discord('evt1', '999', 'Bob', 'bob')
        assert result is None
        assert 'no longer exists' in err

    def test_blocks_when_registration_closed(self):
        evt = minimal_event(registration='closed')
        result, err = self._call(evt)
        assert result is None
        assert 'closed' in err.lower()

    def test_blocks_when_entry_code_required(self):
        evt = minimal_event(entry_code='SECRET')
        result, err = self._call(evt)
        assert result is None
        assert 'entry code' in err.lower()

    def test_blocks_when_event_full(self):
        players = [make_player(name=f'P{i}', discord_id=str(i), google_id=f'g{i}')
                   for i in range(8)]
        evt = minimal_event(players=players, registration_cap=8)
        result, err = self._call(evt)
        assert result is None
        assert 'full' in err.lower()

    def test_blocks_when_already_registered(self):
        existing = make_player(discord_id='999')
        evt = minimal_event(players=[existing])
        result, err = self._call(evt, discord_id='999')
        assert result is None
        assert 'already registered' in err.lower()

    def test_creates_ghost_player_when_no_account(self):
        evt = minimal_event()
        saved = {}

        def capture_save(eid, fields):
            saved.update(fields)

        result, err = self._call(evt, save_event=capture_save, discord_id='999',
                                 discord_name='Bob', username='bobhandle')
        assert err is None
        assert result is not None
        assert result['event_name'] == 'Test Event'
        # Player was added to the event
        assert 'players' in saved
        new_player = saved['players'][0]
        assert new_player['discord_id'] == '999'
        assert new_player['name'] == 'Bob'
        assert new_player['dropped'] is False

    def test_links_existing_account_by_discord_id(self):
        """When list_users returns a profile with the same discord_id, the
        registration uses that profile's name and google_id."""
        existing_profile = {
            'google_id': 'gid_existing',
            'name': 'Real Name',
            'discord': 'realhandle',
            'discord_id': '999',
        }
        evt = minimal_event()
        saved = {}

        def capture_save(eid, fields):
            saved.update(fields)

        result, err = self._call(evt, list_users=[existing_profile],
                                 save_event=capture_save, discord_id='999')
        assert err is None
        player = saved['players'][0]
        assert player['google_id'] == 'gid_existing'
        assert player['name'] == 'Real Name'

    def test_reactivates_dropped_player(self):
        """A previously-dropped player gets reactivated rather than duplicated."""
        dropped = make_player(discord_id='999', dropped=True)
        evt = minimal_event(players=[dropped])
        saved = {}

        def capture_save(eid, fields):
            saved.update(fields)

        result, err = self._call(evt, discord_id='999', save_event=capture_save)
        assert err is None
        assert saved['players'][0]['dropped'] is False
        assert len(saved['players']) == 1   # not duplicated


# ── withdraw_player_via_discord ───────────────────────────────────────────────

class TestWithdrawViaDiscord:

    def _call(self, event, list_users=None, discord_id='111', username='alice', display='Alice'):
        from discord_actions import withdraw_player_via_discord
        with patch('discord_actions.get_event', return_value=event), \
             patch('event_actions.get_event', return_value=event), \
             patch('event_actions.save_event', MagicMock()), \
             patch('event_actions.set_player_dropped', MagicMock()), \
             patch('discord_actions.list_users', return_value=list_users or []), \
             patch('discord_actions.save_event', MagicMock()), \
             patch('discord_actions.set_player_dropped', MagicMock()), \
             patch('discord_actions.refresh_event_announcement'), \
             patch('discord_actions.add_event_log', MagicMock()):
            return withdraw_player_via_discord('evt1', discord_id, username, display)

    def test_error_when_not_registered(self):
        evt = minimal_event()
        name, err = self._call(evt)
        assert name is None
        assert 'not registered' in err.lower()

    def test_removes_player_before_rounds_start(self):
        player = make_player(discord_id='111')
        evt = minimal_event(players=[player], rounds=[])
        save_mock = MagicMock()
        from discord_actions import withdraw_player_via_discord
        with patch('discord_actions.get_event', return_value=evt), \
             patch('event_actions.get_event', return_value=evt), \
             patch('event_actions.save_event', save_mock), \
             patch('event_actions.set_player_dropped', MagicMock()), \
             patch('discord_actions.list_users', return_value=[]), \
             patch('discord_actions.save_event', MagicMock()), \
             patch('discord_actions.refresh_event_announcement'), \
             patch('discord_actions.add_event_log', MagicMock()):
            name, err = withdraw_player_via_discord('evt1', '111', 'alice', 'Alice')
        assert err is None
        # Players list was saved empty (player removed)
        saved_players = save_mock.call_args[0][1]['players']
        assert not any(p['id'] == player['id'] for p in saved_players)

    def test_drops_player_after_rounds_start(self):
        player = make_player(discord_id='111')
        round_data = [{'player1_id': player['id'], 'player2_id': 'other', 'winner_id': None, 'result': None, 'is_bye': False}]
        evt = minimal_event(players=[player], rounds=[round_data])
        drop_mock = MagicMock()
        from discord_actions import withdraw_player_via_discord
        with patch('discord_actions.get_event', return_value=evt), \
             patch('event_actions.get_event', return_value=evt), \
             patch('event_actions.set_player_dropped', drop_mock), \
             patch('event_actions.save_event', MagicMock()), \
             patch('discord_actions.list_users', return_value=[]), \
             patch('discord_actions.save_event', MagicMock()), \
             patch('discord_actions.refresh_event_announcement'), \
             patch('discord_actions.add_event_log', MagicMock()):
            name, err = withdraw_player_via_discord('evt1', '111', 'alice', 'Alice')
        assert err is None
        drop_mock.assert_called_once_with('evt1', player['id'], True)

    def test_blocks_when_self_service_drop_disabled(self):
        player = make_player(discord_id='111')
        evt = minimal_event(players=[player], self_service_drop_enabled=False)
        name, err = self._call(evt)
        assert name is None
        assert 'disabled' in err.lower()


# ── waitlist_player_via_discord ───────────────────────────────────────────────

class TestWaitlistViaDiscord:

    def _call(self, event, list_users=None, discord_id='999', discord_name='Bob', username='bob'):
        from discord_actions import waitlist_player_via_discord
        with patch('discord_actions.get_event', return_value=event), \
             patch('event_actions.get_event', return_value=event), \
             patch('event_actions.save_event', MagicMock()), \
             patch('discord_actions.list_users', return_value=list_users or []), \
             patch('discord_actions.save_event', MagicMock()), \
             patch('discord_actions.add_event_log', MagicMock()):
            return waitlist_player_via_discord('evt1', discord_id, discord_name, username)

    def test_blocks_when_not_full(self):
        evt = minimal_event(registration_cap=8)   # 0 players, cap 8 → not full
        result, err = self._call(evt)
        assert result is None
        assert 'open spots' in err.lower()

    def test_blocks_when_registration_closed(self):
        players = [make_player(name=f'P{i}', discord_id=str(i), google_id=f'g{i}')
                   for i in range(8)]
        evt = minimal_event(players=players, registration_cap=8, registration='closed')
        result, err = self._call(evt)
        assert result is None

    def test_blocks_when_already_registered(self):
        existing = make_player(discord_id='999')
        players = [existing] + [make_player(name=f'P{i}', discord_id=str(i), google_id=f'g{i}')
                                for i in range(1, 8)]
        evt = minimal_event(players=players, registration_cap=8)
        result, err = self._call(evt, discord_id='999')
        assert result is None
        assert 'already registered' in err.lower()

    def test_blocks_when_already_waitlisted(self):
        players = [make_player(name=f'P{i}', discord_id=str(i), google_id=f'g{i}')
                   for i in range(8)]
        waitlist = [{'id': 'w1', 'discord_id': '999', 'status': 'waitlisted'}]
        evt = minimal_event(players=players, registration_cap=8, waitlist=waitlist)
        result, err = self._call(evt, discord_id='999')
        assert result is None
        assert 'already on the waitlist' in err.lower()

    def test_adds_to_waitlist_when_full_and_returns_position(self):
        players = [make_player(name=f'P{i}', discord_id=str(i), google_id=f'g{i}')
                   for i in range(8)]
        evt = minimal_event(players=players, registration_cap=8)
        saved = {}

        def capture_save(eid, fields):
            saved.update(fields)

        from discord_actions import waitlist_player_via_discord
        with patch('discord_actions.get_event', return_value=evt), \
             patch('event_actions.get_event', return_value=evt), \
             patch('event_actions.save_event', capture_save), \
             patch('discord_actions.list_users', return_value=[]), \
             patch('discord_actions.save_event', MagicMock()), \
             patch('discord_actions.add_event_log', MagicMock()):
            result, err = waitlist_player_via_discord('evt1', '999', 'Bob', 'bob')

        assert err is None
        assert result['position'] == 1
        assert saved['waitlist'][0]['discord_id'] == '999'
        assert saved['waitlist'][0]['status'] == 'waitlisted'


# ── report_result_via_discord ─────────────────────────────────────────────────

class TestReportResultViaDiscord:

    def _make_event_with_match(self, player_id='alice_abc123', winner_id=None):
        player = make_player(name='Alice', discord_id='111', google_id='gid_alice')
        player['id'] = player_id
        opp = make_player(name='Bob', discord_id='222', google_id='gid_bob')
        opp['id'] = 'bob_abc456'
        match = {
            'player1_id': player_id,
            'player2_id': 'bob_abc456',
            'winner_id': winner_id,
            'result': None,
            'is_bye': False,
        }
        return minimal_event(players=[player, opp], rounds=[[match]])

    def _call(self, event, discord_id='111', code='w20', **kw):
        from discord_actions import report_result_via_discord
        with patch('discord_actions.get_event', return_value=event), \
             patch('discord_actions.list_users', return_value=[]), \
             patch('discord_actions.save_event', MagicMock()), \
             patch('discord_actions.add_event_log', MagicMock()), \
             patch('discord_actions.discord_api'):
            return report_result_via_discord('evt1', 0, 0, discord_id, code, **kw)

    def test_records_a_win(self):
        evt = self._make_event_with_match()
        result, err = self._call(evt, discord_id='111', code='w21')
        assert err is None
        assert 'won' in result.lower()

    def test_records_a_loss(self):
        evt = self._make_event_with_match()
        result, err = self._call(evt, discord_id='111', code='l02')
        assert err is None
        assert 'lost' in result.lower()

    def test_records_a_draw(self):
        evt = self._make_event_with_match()
        result, err = self._call(evt, discord_id='111', code='draw')
        assert err is None
        assert 'draw' in result.lower()

    def test_error_when_match_already_reported(self):
        evt = self._make_event_with_match(winner_id='alice_abc123')
        result, err = self._call(evt, discord_id='111', code='w20')
        assert result is None
        assert 'open match' in err.lower()

    def test_error_when_not_participant(self):
        evt = self._make_event_with_match()
        result, err = self._call(evt, discord_id='333', code='w20')  # unknown user
        assert result is None


# ── discord_registerable_events ───────────────────────────────────────────────

class TestDiscordRegisterableEvents:

    def _call(self, events, **kw):
        from discord_actions import discord_registerable_events
        with patch('event_queries.list_events', return_value=events), \
             patch('discord_actions.list_users', return_value=[]):
            return discord_registerable_events(**kw)

    def test_returns_open_events(self):
        evt = minimal_event(date='2026-08-01')
        result = self._call([evt])
        assert len(result) == 1

    def test_excludes_test_mode_events(self):
        evt = minimal_event(test_mode=True)
        result = self._call([evt])
        assert result == []

    def test_excludes_closed_registration(self):
        evt = minimal_event(registration='closed')
        result = self._call([evt])
        assert result == []

    def test_excludes_full_events_by_default(self):
        players = [make_player(name=f'P{i}', discord_id=str(i), google_id=f'g{i}')
                   for i in range(4)]
        evt = minimal_event(players=players, registration_cap=4)
        result = self._call([evt])
        assert result == []

    def test_includes_full_events_when_requested(self):
        players = [make_player(name=f'P{i}', discord_id=str(i), google_id=f'g{i}')
                   for i in range(4)]
        evt = minimal_event(players=players, registration_cap=4)
        result = self._call([evt], include_full=True)
        assert len(result) == 1

    def test_excludes_events_with_entry_code(self):
        evt = minimal_event(entry_code='SECRET')
        result = self._call([evt])
        assert result == []


# ── discord_standings_text ────────────────────────────────────────────────────

class TestDiscordStandingsText:

    def test_returns_none_for_missing_event(self):
        from discord_actions import discord_standings_text
        with patch('discord_actions.get_event', return_value=None):
            assert discord_standings_text('gone') is None

    def test_returns_no_standings_message_for_empty_rounds(self):
        from discord_actions import discord_standings_text
        evt = minimal_event(rounds=[])
        with patch('discord_actions.get_event', return_value=evt):
            result = discord_standings_text('evt1')
        assert 'no standings' in result.lower()

    def test_formats_standings_with_player_points(self):
        from discord_actions import discord_standings_text
        player = make_player(name='Alice')
        bye_match = {
            'player1_id': player['id'], 'player2_id': '__bye__',
            'winner_id': player['id'], 'result': '2-0-0', 'is_bye': True,
        }
        evt = minimal_event(players=[player], rounds=[[bye_match]])
        with patch('discord_actions.get_event', return_value=evt):
            result = discord_standings_text('evt1')
        assert 'Alice' in result
        assert 'pts' in result
