"""
Tests for discord_identity.py — the fallback-handle security fix.

Covers: display name/nickname is spoofable (anyone can set theirs to any string,
no uniqueness) and must NOT be usable to match an account; verified username is
unique and safe to match on; a successful handle match locks discord_id onto the
account so future lookups are exact.
"""
from unittest.mock import patch, MagicMock
from discord_identity import find_profile_for_discord, resolve_discord_identity


def make_user(google_id='gid_alice', discord='alice', discord_id=None):
    u = {'google_id': google_id, 'discord': discord}
    if discord_id:
        u['discord_id'] = discord_id
    return u


class TestFindProfileForDiscord:

    def test_matches_by_verified_username(self):
        alice = make_user(discord='alice')
        with patch('discord_identity.list_users', return_value=[alice]), \
             patch('discord_identity.save_user_profile', MagicMock()):
            prof = find_profile_for_discord('999', 'alice', 'someone else')
        assert prof['google_id'] == 'gid_alice'

    def test_does_not_match_by_display_name(self):
        """An attacker who renames their display name to a victim's saved handle
        must NOT be matched to the victim's account — display names aren't unique
        and cost nothing to set, unlike the verified username."""
        alice = make_user(discord='alice')
        with patch('discord_identity.list_users', return_value=[alice]), \
             patch('discord_identity.save_user_profile', MagicMock()) as save:
            prof = find_profile_for_discord('999', 'attacker_handle', 'alice')
        assert prof is None
        save.assert_not_called()

    def test_locks_discord_id_on_first_handle_match(self):
        alice = make_user(discord='alice')
        with patch('discord_identity.list_users', return_value=[alice]), \
             patch('discord_identity.save_user_profile', MagicMock()) as save:
            prof = find_profile_for_discord('999', 'alice')
        save.assert_called_once_with('gid_alice', {'discord_id': '999'})
        assert prof['discord_id'] == '999'

    def test_exact_id_match_wins_and_does_not_resave(self):
        alice = make_user(discord='alice', discord_id='999')
        with patch('discord_identity.list_users', return_value=[alice]), \
             patch('discord_identity.save_user_profile', MagicMock()) as save:
            prof = find_profile_for_discord('999', 'someone_new')
        assert prof['google_id'] == 'gid_alice'
        save.assert_not_called()


class TestResolveDiscordIdentity:

    def test_handles_set_excludes_display_name(self):
        with patch('discord_identity.list_users', return_value=[]):
            gid, handles = resolve_discord_identity('999', 'alice', 'Totally Different Display')
        assert handles == {'alice'}
