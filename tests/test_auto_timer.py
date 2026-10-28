from round_actions import _new_round_updates


def test_simple_event_autostarts_timer():
    assert _new_round_updates({'round_timer_minutes': 50})['round_started_at']


def test_advanced_needs_opt_in_and_delay_defers():
    assert not _new_round_updates({'advanced': True, 'round_timer_minutes': 50})['round_started_at']
    assert _new_round_updates({'advanced': True, 'auto_start_timer': True, 'round_timer_minutes': 50})['round_started_at']
    assert not _new_round_updates({'round_timer_minutes': 50, 'delay_pairings': True})['round_started_at']
    assert not _new_round_updates({})['round_started_at']
