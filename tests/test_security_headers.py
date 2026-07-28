"""
Security header tests.

Every HTTP response must include defensive headers that prevent clickjacking
(X-Frame-Options), MIME-type confusion (X-Content-Type-Options), and
referrer leakage (Referrer-Policy).

The /_ah/warmup endpoint is used because it is the simplest route in the app
and requires no session or database access.
"""


def test_response_includes_x_frame_options(client):
    resp = client.get('/_ah/warmup')
    assert resp.headers.get('X-Frame-Options') == 'DENY'


def test_response_includes_x_content_type_options(client):
    resp = client.get('/_ah/warmup')
    assert resp.headers.get('X-Content-Type-Options') == 'nosniff'


def test_response_includes_referrer_policy(client):
    resp = client.get('/_ah/warmup')
    assert resp.headers.get('Referrer-Policy') == 'strict-origin-when-cross-origin'


def test_cache_control_is_no_store(client):
    resp = client.get('/_ah/warmup')
    assert resp.headers.get('Cache-Control') == 'no-store'
