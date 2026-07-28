"""
Tests for event_queries.py — semantic query functions over the event collection.

All tests call query functions directly (no Flask, no Firestore) by patching
event_queries.list_events at the module level.
"""
from unittest.mock import patch
from tests.conftest import minimal_event


def _call(fn_name, events, **kw):
    import event_queries
    fn = getattr(event_queries, fn_name)
    with patch('event_queries.list_events', return_value=events):
        return fn(**kw)


# ── is_decklist_public predicate ─────────────────────────────────────────────

class TestIsDecklistPublic:

    def test_false_when_requires_decklists_not_set(self):
        from event_queries import is_decklist_public
        e = minimal_event()
        assert not is_decklist_public(e)

    def test_false_when_closed_and_not_released(self):
        from event_queries import is_decklist_public
        e = minimal_event(requires_decklists=True, closed_decklists=True, decklists_released=False)
        assert not is_decklist_public(e)

    def test_true_when_closed_and_released(self):
        from event_queries import is_decklist_public
        e = minimal_event(requires_decklists=True, closed_decklists=True, decklists_released=True)
        assert is_decklist_public(e)

    def test_false_when_not_closed_and_event_incomplete(self):
        from event_queries import is_decklist_public
        e = minimal_event(requires_decklists=True, closed_decklists=False)
        # no rounds → not complete
        assert not is_decklist_public(e)

    def test_true_when_not_closed_and_event_status_finished(self):
        from event_queries import is_decklist_public
        e = minimal_event(requires_decklists=True, closed_decklists=False, status='finished',
                          rounds=[[]])
        assert is_decklist_public(e)


# ── public_decklist_events ────────────────────────────────────────────────────

class TestPublicDecklistEvents:

    def test_returns_only_public_events(self):
        public = minimal_event(id='pub', requires_decklists=True,
                               closed_decklists=True, decklists_released=True)
        private = minimal_event(id='priv')
        result = _call('public_decklist_events', [public, private])
        assert [e['id'] for e in result] == ['pub']

    def test_returns_empty_when_none_public(self):
        e = minimal_event()
        result = _call('public_decklist_events', [e])
        assert result == []


# ── events_for_player ─────────────────────────────────────────────────────────

class TestEventsForPlayer:

    def _player(self, gid):
        return {'id': 'p1', 'name': 'Alice', 'google_id': gid, 'dropped': False}

    def test_returns_events_with_matching_player(self):
        e = minimal_event(players=[self._player('gid_alice')])
        result = _call('events_for_player', [e], google_id='gid_alice')
        assert len(result) == 1

    def test_excludes_events_player_is_not_in(self):
        e = minimal_event(players=[self._player('gid_bob')])
        result = _call('events_for_player', [e], google_id='gid_alice')
        assert result == []

    def test_results_are_newest_first(self):
        e1 = minimal_event(id='e1', date='2026-01-01', players=[self._player('gid_alice')])
        e2 = minimal_event(id='e2', date='2026-06-01', players=[self._player('gid_alice')])
        result = _call('events_for_player', [e1, e2], google_id='gid_alice')
        assert [e['id'] for e in result] == ['e2', 'e1']


# ── registerable_for_discord ──────────────────────────────────────────────────

class TestRegisterableForDiscord:

    def test_returns_open_events(self):
        e = minimal_event()
        result = _call('registerable_for_discord', [e])
        assert len(result) == 1

    def test_excludes_test_mode(self):
        e = minimal_event(test_mode=True)
        assert _call('registerable_for_discord', [e]) == []

    def test_excludes_closed_registration(self):
        e = minimal_event(registration='closed')
        assert _call('registerable_for_discord', [e]) == []

    def test_excludes_entry_code(self):
        e = minimal_event(entry_code='SECRET')
        assert _call('registerable_for_discord', [e]) == []

    def test_excludes_full_by_default(self):
        players = [{'id': f'p{i}', 'name': f'P{i}', 'google_id': f'g{i}', 'dropped': False}
                   for i in range(4)]
        e = minimal_event(players=players, registration_cap=4)
        assert _call('registerable_for_discord', [e]) == []

    def test_includes_full_when_requested(self):
        players = [{'id': f'p{i}', 'name': f'P{i}', 'google_id': f'g{i}', 'dropped': False}
                   for i in range(4)]
        e = minimal_event(players=players, registration_cap=4)
        assert len(_call('registerable_for_discord', [e], include_full=True)) == 1

    def test_filters_by_owner_gid(self):
        owned = minimal_event(id='owned', owner_id='gid_alice')
        other = minimal_event(id='other', owner_id='gid_bob')
        result = _call('registerable_for_discord', [owned, other], owner_gid='gid_alice')
        assert [e['id'] for e in result] == ['owned']

    def test_respects_limit(self):
        events = [minimal_event(id=f'e{i}', date=f'2026-0{i+1}-01') for i in range(3)]
        result = _call('registerable_for_discord', events, limit=2)
        assert len(result) == 2


# ── linkable_for_discord ──────────────────────────────────────────────────────

class TestLinkableForDiscord:

    def test_excludes_test_mode(self):
        e = minimal_event(test_mode=True)
        assert _call('linkable_for_discord', [e]) == []

    def test_includes_non_test_events(self):
        e = minimal_event()
        assert len(_call('linkable_for_discord', [e])) == 1

    def test_filters_by_owner_gid(self):
        owned = minimal_event(id='owned', owner_id='gid_alice')
        other = minimal_event(id='other', owner_id='gid_bob')
        result = _call('linkable_for_discord', [owned, other], owner_gid='gid_alice')
        assert [e['id'] for e in result] == ['owned']

    def test_results_newest_first(self):
        e1 = minimal_event(id='e1', date='2026-01-01')
        e2 = minimal_event(id='e2', date='2026-06-01')
        result = _call('linkable_for_discord', [e1, e2])
        assert [e['id'] for e in result] == ['e2', 'e1']


# ── with_standings ────────────────────────────────────────────────────────────

class TestWithStandings:

    def test_excludes_events_with_no_rounds(self):
        e = minimal_event(rounds=[])
        assert _call('with_standings', [e]) == []

    def test_excludes_test_mode(self):
        e = minimal_event(rounds=[[]], test_mode=True)
        assert _call('with_standings', [e]) == []

    def test_includes_events_with_rounds(self):
        e = minimal_event(rounds=[[]])
        assert len(_call('with_standings', [e])) == 1

    def test_results_newest_first(self):
        e1 = minimal_event(id='e1', date='2026-01-01', rounds=[[]])
        e2 = minimal_event(id='e2', date='2026-06-01', rounds=[[]])
        result = _call('with_standings', [e1, e2])
        assert [e['id'] for e in result] == ['e2', 'e1']
