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
                   url_for, jsonify, current_app, render_template)
from db import (get_admins, add_admin, remove_admin,
                get_user_profile, save_user_profile, list_users)
from gcp_secrets import get_secret

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

GOOGLE_AUTH_URL    = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL   = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'

# CLIENT_ID is public (sent to the browser) and lives in app.yaml env_variables.
# CLIENT_SECRET comes from Secret Manager in production, or an env var locally.
CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '')
CLIENT_SECRET = get_secret('GOOGLE_CLIENT_SECRET')

# Discord OAuth2 (login). Client id = the app id (public, also used by the bot);
# the OAuth2 client secret is separate from the bot token and lives in Secret
# Manager as DISCORD_CLIENT_SECRET. Login is offered only when both are present.
DISCORD_AUTH_URL  = 'https://discord.com/oauth2/authorize'
DISCORD_TOKEN_URL = 'https://discord.com/api/oauth2/token'
DISCORD_USER_URL  = 'https://discord.com/api/users/@me'
DISCORD_CLIENT_ID     = get_secret('DISCORD_APP_ID')
DISCORD_CLIENT_SECRET = get_secret('DISCORD_CLIENT_SECRET')

def discord_login_enabled() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)


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


def _begin_oauth(next_target: str) -> str:
    """Common start for any provider: mint+store a CSRF state nonce and stash a
    safe post-login destination. Returns the state to send to the provider."""
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    session['oauth_next'] = next_target if _is_safe_redirect(next_target) else None
    return state


@auth_bp.route('/login')
def login():
    """Provider chooser. Everything links here; it offers Google (and Discord, when
    configured), preserving the post-login `next` destination."""
    return render_template('login.html',
                           next=request.args.get('next', ''),
                           discord_enabled=discord_login_enabled())


@auth_bp.route('/login/google')
def login_google():
    state = _begin_oauth(request.args.get('next', ''))
    params = {
        'client_id':     CLIENT_ID,
        'redirect_uri':  url_for('auth.callback', _external=True),
        'response_type': 'code',
        'scope':         'openid email profile',
        'access_type':   'online',
        'state':         state,
    }
    return redirect(f'{GOOGLE_AUTH_URL}?{urlencode(params)}')


@auth_bp.route('/login/discord')
def login_discord():
    if not discord_login_enabled():
        return 'Discord login is not configured.', 503
    state = _begin_oauth(request.args.get('next', ''))
    params = {
        'client_id':     DISCORD_CLIENT_ID,
        'redirect_uri':  url_for('auth.discord_callback', _external=True),
        'response_type': 'code',
        'scope':         'identify email',
        'state':         state,
    }
    return redirect(f'{DISCORD_AUTH_URL}?{urlencode(params)}')


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

    # Capture email + Google picture into the user directory, and refresh the
    # display name from Google on every login — Google is the source of truth
    # for display names (they're not editable in-app).
    profile = get_user_profile(session['user']['id'])
    google_pic = session['user'].get('picture', '')
    updates = {
        'email':          session['user']['email'],
        'google_picture': google_pic,
        'name':           session['user']['name'],
    }
    save_user_profile(session['user']['id'], updates)

    # The nav shows session['user']['picture']; use the custom avatar if set.
    session['user']['picture'] = profile.get('avatar_url') or google_pic

    next_url = session.pop('oauth_next', None) or url_for('events.index')
    return redirect(next_url)


def _resolve_account_for_discord(discord_id: str, email: str):
    """Find an existing account to link a Discord login into: by a numeric Discord
    ID already stored on a profile (e.g. from prior bot use), else by a verified
    matching email. Returns the account id (the users-doc id, which is a Google
    sub for Google accounts) or None to create a fresh Discord account."""
    did = str(discord_id)
    email_l = (email or '').strip().lower()
    by_email = None
    for u in list_users():
        if u.get('discord_id') == did:
            return u['google_id']            # list_users sets google_id = doc id
        if email_l and not by_email and (u.get('email') or '').strip().lower() == email_l:
            by_email = u['google_id']
    return by_email


@auth_bp.route('/discord/callback')
def discord_callback():
    if not discord_login_enabled():
        return 'Discord login is not configured.', 503
    state = request.args.get('state')
    if not state or state != session.pop('oauth_state', None):
        return 'Login failed: invalid state.', 400
    code = request.args.get('code')
    if not code:
        return 'Login failed: no code received.', 400

    token_data = requests.post(DISCORD_TOKEN_URL, data={
        'client_id':     DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type':    'authorization_code',
        'code':          code,
        'redirect_uri':  url_for('auth.discord_callback', _external=True),
    }, headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=10).json()
    access_token = token_data.get('access_token')
    if not access_token:
        return 'Login failed: could not get access token.', 400

    du = requests.get(DISCORD_USER_URL,
                      headers={'Authorization': f'Bearer {access_token}'}, timeout=10).json()
    discord_id = du.get('id')
    if not discord_id:
        return 'Login failed: could not read Discord profile.', 400
    username = du.get('username') or ''
    display  = du.get('global_name') or username or 'Player'
    email    = du.get('email') if du.get('verified') else None
    avatar   = (f"https://cdn.discordapp.com/avatars/{discord_id}/{du['avatar']}.png"
                if du.get('avatar') else '')

    # Link into an existing account if we recognise them, else make a new one.
    account_id = _resolve_account_for_discord(discord_id, email)
    if account_id is None:
        account_id = f'discord:{discord_id}'
    saved = get_user_profile(account_id)

    updates = {'discord_id': str(discord_id)}
    if not (saved.get('discord') or '').strip():
        updates['discord'] = username           # don't clobber a chosen handle
    if avatar:
        updates['discord_picture'] = avatar
    if email and not saved.get('email'):
        updates['email'] = email
    if not (saved.get('name') or '').strip():
        updates['name'] = display               # keep an existing (e.g. Google) name
    save_user_profile(account_id, updates)

    session['user'] = {
        'id':      account_id,
        'email':   saved.get('email') or email,
        'name':    saved.get('name') or display,
        'picture': '',
    }
    if session['user']['email']:
        _resolve_pending_admin(session['user'])
    saved = get_user_profile(account_id)
    session['user']['picture'] = (saved.get('avatar_url') or saved.get('google_picture')
                                  or saved.get('discord_picture') or '')

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
    # Whether they already have a Discord handle on file, so the registration
    # form can skip asking for it again.
    profile = get_user_profile(user['id'])
    return jsonify({'signed_in': True, 'user': user,
                    'has_discord': bool((profile.get('discord') or '').strip())})


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
