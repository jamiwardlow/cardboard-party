from unittest.mock import patch

from event_view import build_event_view
from tests.conftest import minimal_event


def test_owner_name_set_from_profile():
    event = minimal_event(owner_id='owner_gid')
    with patch('event_view.get_user_profile', return_value={'name': 'Alice', 'discord': 'alice#123'}):
        view = build_event_view(event, current_user=None)
    assert view['owner_name'] == 'Alice'
    assert view['owner_discord'] == 'alice#123'


def test_owner_name_falls_back_to_owner_id_when_no_profile():
    event = minimal_event(owner_id='owner_gid')
    with patch('event_view.get_user_profile', return_value={}):
        view = build_event_view(event, current_user=None)
    assert view['owner_name'] == 'owner_gid'


def test_is_event_staff_true_for_owner():
    event = minimal_event(owner_id='owner_gid')
    with patch('event_view.get_user_profile', return_value={}), \
         patch('event_view.is_admin', return_value=False):
        view = build_event_view(event, current_user={'id': 'owner_gid'})
    assert view['is_event_staff'] is True


def test_is_event_staff_true_for_co_organizer():
    event = minimal_event(owner_id='owner_gid', co_organizer_ids=['co_gid'])
    with patch('event_view.get_user_profile', return_value={}), \
         patch('event_view.is_admin', return_value=False):
        view = build_event_view(event, current_user={'id': 'co_gid'})
    assert view['is_event_staff'] is True


def test_is_event_staff_false_for_admin_who_is_not_owner_or_co_organizer():
    event = minimal_event(owner_id='owner_gid')
    with patch('event_view.get_user_profile', return_value={}), \
         patch('event_view.is_admin', return_value=True):
        view = build_event_view(event, current_user={'id': 'admin_gid'})
    assert view['can_manage'] is True
    assert view['is_event_staff'] is False


def test_is_event_staff_false_for_logged_out_viewer():
    event = minimal_event(owner_id='owner_gid')
    with patch('event_view.get_user_profile', return_value={}):
        view = build_event_view(event, current_user=None)
    assert view['is_event_staff'] is False
