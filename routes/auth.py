"""
Google OAuth2 authentication routes.

Flow:
  /auth/login  → redirects to Google consent screen
  /auth/callback → exchanges code for token, stores user in session
  /auth/logout → clears session
"""

import os
import secrets
import requests
from urllib.parse import urlencode, urljoin, urlparse
from flask import (Blueprint, redirect, request, session,
                   url_for, jsonify, current_app)
from db import (get_admins, add_admin, remove_admin,
                get_user_profile, save_user_profile)
from gcp_secrets import get_secret

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

GOOGLE_AUTH_URL    = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL   = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'

# CLIENT_ID is public (sent to the browser) and lives in app.yaml env_variables.
# CLIENT_SECRET comes from Secret Manager in production, or an env var locally.
CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '')
CLIENT_SECRET = get_secret('GOOGLE_CLIENT_SECRET')


def get_current_user() -> dict | None:
    """Return the signed-in user dict from the session, or None."""
    return session.get('user')


def _is_safe_redirect(target: str) -> bool:
    """True only for redirects back to this same host (blocks open redirects)."""
    if not target:
        return False
    test = urlparse(urljoin(request.host_url, target))
    ref  = urlparse(request.host_url)
    return test.scheme in ('http', 'https') and test.netloc == ref.netloc


def login_required(f):
    """Decorator: redirect to /auth/login if not signed in."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not get_current_user():
            return redirect(url_for('auth.login', next=request.url))
        return f(*args, **kwargs)
    return decorated


@auth_bp.route('/login')
def login():
    redirect_uri = url_for('auth.callback', _external=True)

    # CSRF protection: random state echoed back by Google and verified below.
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state

    # Stash the post-login destination in the session (Google doesn't echo it),
    # but only if it's a safe same-host target.
    next_target = request.args.get('next', '')
    if _is_safe_redirect(next_target):
        session['oauth_next'] = next_target
    else:
        session.pop('oauth_next', None)

    params = {
        'client_id':     CLIENT_ID,
        'redirect_uri':  redirect_uri,
        'response_type': 'code',
        'scope':         'openid email profile',
        'access_type':   'online',
        'state':         state,
    }
    return redirect(f'{GOOGLE_AUTH_URL}?{urlencode(params)}')


@auth_bp.route('/callback')
def callback():
    state = request.args.get('state')
    if not state or state != session.pop('oauth_state', None):
        return 'Login failed: invalid state.', 400

    code = request.args.get('code')
    if not code:
        return 'Login failed: no code received.', 400

    redirect_uri = url_for('auth.callback', _external=True)
    token_resp = requests.post(GOOGLE_TOKEN_URL, data={
        'code':          code,
        'client_id':     CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'redirect_uri':  redirect_uri,
        'grant_type':    'authorization_code',
    })
    token_data = token_resp.json()
    access_token = token_data.get('access_token')
    if not access_token:
        return 'Login failed: could not get access token.', 400

    user_resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={'Authorization': f'Bearer {access_token}'}
    )
    user_info = user_resp.json()

    session['user'] = {
        'id':      user_info.get('sub'),
        'email':   user_info.get('email'),
        'name':    user_info.get('name'),
        'picture': user_info.get('picture'),
    }

    # Resolve any pending admin entry added by email before first sign-in
    _resolve_pending_admin(session['user'])

    # Capture email into the user directory. Only set the display name if the
    # user hasn't already customized one, so we don't clobber their choice.
    profile = get_user_profile(session['user']['id'])
    updates = {'email': session['user']['email']}
    if not profile.get('name'):
        updates['name'] = session['user']['name']
    save_user_profile(session['user']['id'], updates)

    next_url = session.pop('oauth_next', None) or url_for('events.index')
    return redirect(next_url)


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect('/')


@auth_bp.route('/me')
def me():
    user = get_current_user()
    if not user:
        return jsonify({'signed_in': False})
    return jsonify({'signed_in': True, 'user': user})


def _resolve_pending_admin(user: dict):
    """
    When a user signs in, check if they were added as an admin by email
    (before their first sign-in). If so, replace the pending entry with
    their real Google ID.
    """
    admins = get_admins()
    pending_key = f"pending:{user['email'].lower()}"
    entry = next((a for a in admins if a['id'] == pending_key), None)
    if entry:
        remove_admin(pending_key)
        add_admin(user['id'], user['email'], user['name'])
