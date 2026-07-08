import os
import time
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit
from flask import Flask, request, redirect
from routes.auth import auth_bp
from routes.events import events_bp
from routes.discord import discord_bp
from gcp_secrets import get_secret
from discord_notify import fmt_time

app = Flask(__name__)
app.jinja_env.filters['time12'] = fmt_time
# Production key comes from Secret Manager (FLASK_SECRET_KEY); local dev falls
# back to a static insecure key so sessions still work without GCP access.
app.secret_key = get_secret('FLASK_SECRET_KEY') or 'dev-only-insecure-key'

# Harden the session cookie. Secure is prod-only so local HTTP login still works;
# SameSite=Lax also blunts CSRF against the cookie-authenticated /api endpoints,
# while still allowing the top-level OAuth callback navigation to send the cookie.
_is_prod = os.environ.get('FLASK_ENV') == 'production'
app.config.update(
    SESSION_COOKIE_SECURE=_is_prod,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Keep people signed in across browser/app restarts. Without this, Flask sends a
    # non-persistent session cookie (no Max-Age), which mobile Safari/Chrome routinely
    # evict on backgrounding or tab recycling — silently logging users out. Login marks
    # the session permanent (see routes/auth.py), so this lifetime applies to it; with
    # SESSION_REFRESH_EACH_REQUEST (Flask default True) the 30-day window slides forward
    # on each visit, so active users effectively stay signed in.
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

app.register_blueprint(auth_bp)
app.register_blueprint(events_bp)
app.register_blueprint(discord_bp)

# Canonical host (e.g. "cardboardparty.gg"). When set, every page is served
# under this host: requests to any other host (the appspot URL, www.*, etc.)
# are 301-redirected so all URLs show the custom domain. Unset (local dev, or
# before the custom domain is mapped + DNS/SSL live) → no redirect, so deploying
# this is a safe no-op until CANONICAL_HOST is added to app.yaml.
CANONICAL_HOST = os.environ.get('CANONICAL_HOST', '').strip()

@app.route('/_ah/warmup')
def _warmup():
    """App Engine sends this to a new instance before routing live traffic to it
    (with inbound_services: warmup). Reaching this route means the WSGI app and
    all imports are loaded, so the instance is warm — which keeps the first real
    request (e.g. a Discord interaction) from paying the ~3.7s cold-start that
    exceeds Discord's 3s response limit."""
    return '', 200

@app.before_request
def _redirect_to_canonical_host():
    if not CANONICAL_HOST or request.method not in ('GET', 'HEAD'):
        return None
    if request.path.startswith('/_ah/'):   # App Engine internal (warmup, etc.)
        return None
    if request.host == CANONICAL_HOST:
        return None
    parts = urlsplit(request.url)._replace(netloc=CANONICAL_HOST, scheme='https')
    return redirect(urlunsplit(parts), code=301)

@app.after_request
def _no_store_dynamic(resp):
    """Stop browsers caching dynamic pages and the JSON API. Without explicit
    directives these responses carry no cache headers, and Safari in particular
    heuristically caches fetch() GETs — serving stale event data (e.g. a finished
    playoff that still looks unfinished, so the champion banner never shows).
    Static assets are served by App Engine's /static handler (not Flask) and keep
    their own cache-busted caching, so they're unaffected."""
    resp.headers['Cache-Control'] = 'no-store'
    return resp

# Cache-busting token for static assets (CSS/JS). App Engine serves /static with
# a 10-minute default cache, so without this a CSS change can take 10 min to show
# even on a hard refresh. GAE_VERSION changes on every deploy; locally it's unset,
# so fall back to the process start time so a restart picks up edits.
_ASSET_VERSION = os.environ.get('GAE_VERSION') or str(int(time.time()))

# HTTP-referrer-restricted Google Maps key. Fetched from Secret Manager (secret name:
# MAPS_API_KEY) so it stays out of source control. Empty when unset → Maps features
# degrade gracefully to a plain text field + a View-on-Google-Maps link.
MAPS_API_KEY = get_secret('MAPS_API_KEY')

@app.context_processor
def inject_globals():
    from routes.events import has_public_decklists
    return {'asset_v': _ASSET_VERSION, 'maps_api_key': MAPS_API_KEY,
            'has_public_decklists': has_public_decklists()}

if __name__ == '__main__':
    app.run(debug=True, port=8080)
