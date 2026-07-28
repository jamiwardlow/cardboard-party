import os
import sys

# Strip GCP project so get_secret() never calls Secret Manager during tests.
os.environ.pop('GOOGLE_CLOUD_PROJECT', None)
os.environ.setdefault('GOOGLE_CLIENT_ID', 'test-client-id')

import pytest


@pytest.fixture()
def app():
    from main import app as flask_app
    flask_app.config.update({'TESTING': True, 'SECRET_KEY': 'test-secret-key'})
    yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def auth_client(app):
    """Test client with a pre-seeded signed-in session (user is event owner)."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['user'] = {
            'id': 'test_uid',
            'name': 'Test User',
            'email': 'test@example.com',
            'picture': '',
        }
    return c


def minimal_event(**overrides) -> dict:
    """Return a minimal valid event dict, optionally overriding fields."""
    base = {
        'id': 'evt1',
        'name': 'Test Event',
        'owner_id': 'test_uid',
        'players': [],
        'rounds': [],
        'status': 'upcoming',
        'registration': 'open',
        'co_organizer_ids': [],
    }
    base.update(overrides)
    return base
