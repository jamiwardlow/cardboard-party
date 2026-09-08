"""An explicit 'finished' status force-completes an event even when its last
round is an abandoned, unresolved playoff bracket round."""

from event_state import _event_complete


def _bracket_event(status=None):
    return {
        'status': status,
        'rounds': [[
            {'player1_id': 'p1', 'player2_id': 'p2', 'winner_id': None,
             'result': None, 'is_bye': False, 'stage': 'bracket', 'table': 1},
            {'player1_id': 'p3', 'player2_id': 'p4', 'winner_id': None,
             'result': None, 'is_bye': False, 'stage': 'bracket', 'table': 2},
        ]],
    }


def test_unresolved_bracket_round_is_not_complete_by_default():
    assert _event_complete(_bracket_event()) is False


def test_finished_status_overrides_an_unresolved_bracket_round():
    assert _event_complete(_bracket_event(status='finished')) is True
