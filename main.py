import os
import time
from flask import Flask
from routes.auth import auth_bp
from routes.events import events_bp
from gcp_secrets import get_secret

app = Flask(__name__)
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
)

app.register_blueprint(auth_bp)
app.register_blueprint(events_bp)

# Cache-busting token for static assets (CSS/JS). App Engine serves /static with
# a 10-minute default cache, so without this a CSS change can take 10 min to show
# even on a hard refresh. GAE_VERSION changes on every deploy; locally it's unset,
# so fall back to the process start time so a restart picks up edits.
_ASSET_VERSION = os.environ.get('GAE_VERSION') or str(int(time.time()))

@app.context_processor
def inject_asset_version():
    return {'asset_v': _ASSET_VERSION}

if __name__ == '__main__':
    app.run(debug=True, port=8080)
