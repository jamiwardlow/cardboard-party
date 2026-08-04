"""
Tests for discord_match.py — Discord match-reporting layer.

All tests call functions directly (no Flask test client, no Firestore).
db calls are mocked at the discord_match module level.
"""
from unittest.mock import patch, MagicMock
from tests.conftest import minimal_event


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
        from discord_match import report_result_via_discord
        with patch('discord_match.get_event', return_value=event), \
             patch('discord_identity.list_users', return_value=[]), \
             patch('discord_match.save_event', MagicMock()), \
             patch('discord_match.add_event_log', MagicMock()), \
             patch('discord_match.discord_api'):
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
