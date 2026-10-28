from unittest.mock import patch

from result_actions import record_result, edit_result, delete_result


def _event(**kw):
    m = {'player1_id': 'a', 'player2_id': 'b', 'winner_id': None, 'result': None, 'is_bye': False}
    bye = {'player1_id': 'a', 'player2_id': '__bye__', 'winner_id': 'a', 'result': '2-0-0', 'is_bye': True}
    return {'id': 'e1', 'best_of': 3, 'rounds': [[m, bye]], **kw}


def _run(fn, *a, **kw):
    with patch('result_actions.save_event') as save, patch('result_actions.discord_api') as api:
        return fn(*a, **kw), save, api


def test_record_saves_and_notifies():
    e = _event()
    (m, err), save, api = _run(record_result, e, 0, 0, 'a', '2-0')
    assert err is None and m['winner_id'] == 'a'
    save.assert_called_once()
    api.notify_result.assert_called_once()


def test_rejects_bad_index_bye_bad_score_and_frowned_id():
    e = _event(intentional_draws_frowned=True)
    for args, msg in [((0, 9, 'a', '2-0'), 'Invalid match_index'),
                      ((0, 1, 'a', '2-0'), 'bye'),
                      ((0, 0, 'b', '2-0'), 'Winner'),
                      ((0, 0, None, '0-0-3'), 'Intentional')]:
        (m, err), save, api = _run(record_result, e, *args)
        assert m is None and msg in err
        save.assert_not_called()
        api.notify_result.assert_not_called()


def test_edit_notifies_players():
    e = _event()
    (m, err), _, api = _run(edit_result, e, 0, 0, 'b', '0-2')
    assert err is None and m['winner_id'] == 'b'
    api.notify_result.assert_called_once()


def test_delete_clears_and_returns_previous():
    e = _event()
    _run(record_result, e, 0, 0, 'a', '2-0')
    (was, err), save, _ = _run(delete_result, e, 0, 0)
    assert err is None and was['result'] == '2-0'
    assert e['rounds'][0][0]['winner_id'] is None
    assert _run(delete_result, e, 0, 0)[0][1] == 'No result to delete'


def test_delete_rejects_bye_and_bad_index():
    e = _event()
    assert _run(delete_result, e, 0, 1)[0][1] == 'Cannot delete a bye result'
    assert _run(delete_result, e, 0, 9)[0][1] == 'Invalid match_index'
