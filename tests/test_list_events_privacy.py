from unittest.mock import patch

from tests.conftest import minimal_event


def test_listing_hides_organizer_ids(client):
    ev = minimal_event(owner_id='o1', co_organizer_ids=['pending:x@y.com', 'g2'])
    with patch('routes.events.list_events', return_value=[ev]):
        e = client.get('/api/events').get_json()[0]
    assert 'owner_id' not in e and 'co_organizer_ids' not in e
    assert e['is_organizer'] is False


def test_listing_flags_own_events(auth_client):
    ev = minimal_event(owner_id='test_uid')
    with patch('routes.events.list_events', return_value=[ev]), \
         patch('routes.events.is_admin', return_value=False):
        assert auth_client.get('/api/events').get_json()[0]['is_organizer'] is True
