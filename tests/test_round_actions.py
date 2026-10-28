from unittest.mock import patch
from round_actions import pair_next_round, repair_round
from tests.conftest import minimal_event


def _players(n):
    return [{'id': f'p{i}', 'name': f'P{i}', 'google_id': None, 'dropped': False} for i in range(n)]


def _run(fn, *args):
    with patch('round_actions.save_event') as save, \
         patch('round_actions.refresh_event_announcement'), \
         patch('round_actions.discord_api') as d:
        return fn(*args, 'http://x'), save, d


def test_pair_saves_before_delivering_with_auto_timer():
    e = minimal_event(players=_players(4), num_rounds=2, round_timer_minutes=50)
    (res, err), save, d = _run(pair_next_round, e)
    assert err is None and res['round_num'] == 1
    assert save.call_args[0][1]['round_started_at']
    d.announce_round.assert_called_once()


def test_pair_blocked_by_unrecorded_results():
    e = minimal_event(players=_players(4), num_rounds=2)
    _run(pair_next_round, e)
    (res, err), save, _ = _run(pair_next_round, e)
    assert res is None and 'unrecorded' in err
    save.assert_not_called()


def test_delay_pairings_withholds_delivery():
    e = minimal_event(players=_players(4), num_rounds=2, delay_pairings=True)
    (_, err), _, d = _run(pair_next_round, e)
    assert err is None
    d.announce_round.assert_not_called()
    assert e['pairings_released'] is False


def test_repair_only_latest_round():
    e = minimal_event(players=_players(4), num_rounds=3)
    _run(pair_next_round, e)
    (res, err), _, _ = _run(repair_round, e, 2)
    assert res is None and 'latest' in err
