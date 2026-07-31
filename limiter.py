"""
Rate-limiting setup (flask-limiter 4.x).

Using in-process memory storage — simple, no extra infrastructure, and
sufficient for this app's scale. The trade-off: each App Engine instance
tracks its own counters independently, so the effective global limit is
num_instances × per-instance limit. With App Engine Standard's max of 20
instances, a per-instance limit of 20/minute means at most ~400 attempts/
minute can get through globally — still meaningful protection against targeted
floods and credential stuffing.

Upgrade path: swap storage_uri for "redis://<host>" or
"memcached://<host>" to share counters across instances.
"""

from flask import request
from flask_limiter import Limiter


def _client_ip() -> str:
    """Real client IP behind App Engine's load-balancer proxy.
    The LB prepends the original client address to X-Forwarded-For and that
    header cannot be spoofed by the client in App Engine Standard (the LB
    rewrites it). Falls back to remote_addr for local dev."""
    xff = request.headers.get('X-Forwarded-For', '')
    return xff.split(',')[0].strip() if xff else (request.remote_addr or '127.0.0.1')


limiter = Limiter(
    key_func=_client_ip,
    storage_uri='memory://',
    default_limits=[],          # no blanket global limit — apply per-endpoint
)
