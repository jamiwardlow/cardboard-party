"""
Event field validation tests.

'description' and 'entry_cost' must be capped at _COMMS_MAX (5000) characters
on both event creation and event update, matching the cap already applied to
rules / schedule / prizes / contact.
"""

from unittest.mock import patch
from tests.conftest import minimal_event

_OVER = 'x' * 6000   # 6000 chars > _COMMS_MAX (5000)
_MAX = 5000


# ── Create event ─────────────────────────────────────────────────────────────

def test_create_event_caps_description(auth_client):
    with patch('routes.events.create_event', return_value='evt1'):
        resp = auth_client.post('/api/events', json={
            'name': 'Test Event',
            'description': _OVER,
        })
    assert resp.status_code == 201
    assert len(resp.get_json()['description']) == _MAX


def test_create_event_caps_entry_cost(auth_client):
    with patch('routes.events.create_event', return_value='evt1'):
        resp = auth_client.post('/api/events', json={
            'name': 'Test Event',
            'entry_cost': _OVER,
        })
    assert resp.status_code == 201
    assert len(resp.get_json()['entry_cost']) == _MAX


# ── Update event ──────────────────────────────────────────────────────────────

def test_update_event_caps_description(auth_client):
    evt = minimal_event()
    with patch('routes.events.get_event', return_value=evt), \
         patch('routes.events.save_event') as mock_save, \
         patch('routes.events.is_admin', return_value=False):
        resp = auth_client.put('/api/events/evt1', json={'description': _OVER})
    assert resp.status_code == 200
    saved = mock_save.call_args[0][1]   # second positional arg to save_event
    assert len(saved['description']) == _MAX


def test_update_event_caps_entry_cost(auth_client):
    evt = minimal_event()
    with patch('routes.events.get_event', return_value=evt), \
         patch('routes.events.save_event') as mock_save, \
         patch('routes.events.is_admin', return_value=False):
        resp = auth_client.put('/api/events/evt1', json={'entry_cost': _OVER})
    assert resp.status_code == 200
    saved = mock_save.call_args[0][1]
    assert len(saved['entry_cost']) == _MAX
