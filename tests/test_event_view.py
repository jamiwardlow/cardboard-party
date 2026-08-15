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
