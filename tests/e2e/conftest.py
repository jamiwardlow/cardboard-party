import socket
import threading
import os
import pytest
from unittest.mock import patch

os.environ.pop('GOOGLE_CLOUD_PROJECT', None)
os.environ.setdefault('GOOGLE_CLIENT_ID', 'test-client-id')

TEST_EVENT_ID = 'e2e-test-event-001'
TEST_USER = {
    'id': 'test_uid',
    'name': 'Test Organizer',
    'email': 'test@example.com',
    'picture': '',
}


def _minimal_event(**overrides):
    base = {
        'id': TEST_EVENT_ID,
        'name': 'E2E Test Event',
        'owner_id': 'test_uid',
        'players': [],
        'rounds': [],
        'status': 'upcoming',
        'registration': 'open',
        'co_organizer_ids': [],
        'format': 'Standard',
    }
    base.update(overrides)
    return base


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='module')
def app():
    from main import app as flask_app
    flask_app.config.update({'TESTING': True, 'SECRET_KEY': 'test-secret-key'})
    yield flask_app


@pytest.fixture(scope='module')
def e2e_server(app):
    from werkzeug.serving import make_server

    event = _minimal_event()

    def fake_get_event(eid):
        return dict(event) if eid == TEST_EVENT_ID else None

    def fake_get_user_profile(uid):
        if uid == 'test_uid':
            return {'name': 'Test Organizer', 'discord': '', 'email': 'test@example.com'}
        return {}

    patches = [
        patch('routes.events.get_event', side_effect=fake_get_event),
        patch('routes.events.get_user_profile', side_effect=fake_get_user_profile),
        patch('routes.events.save_event', return_value=None),
        patch('routes.events.add_event_log', return_value=None),
    ]
    for p in patches:
        p.start()

    port = _free_port()
    server = make_server('127.0.0.1', port, app)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    yield f'http://127.0.0.1:{port}'

    for p in patches:
        p.stop()
    server.shutdown()


def make_session_cookie(app):
    """Return a signed Flask session cookie value for the test user."""
    from flask.sessions import SecureCookieSessionInterface
    interface = SecureCookieSessionInterface()
    serializer = interface.get_signing_serializer(app)
    return serializer.dumps({'user': TEST_USER})
