"""
Ad-hoc mobile-viewport preview: spins up the app in-process (same pattern as
tests/e2e/conftest.py), opens it in a WebKit/iPhone-13 context, and screenshots
whatever path you pass. Not a test — a dev tool for iterating on mobile CSS.

Usage:
    python3 scripts/mobile_preview.py /events/e2e-test-event-001 out.png
    python3 scripts/mobile_preview.py /                            out.png
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.e2e.conftest import app as _app_fixture, e2e_server as _server_fixture  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def _resolve_fixture(gen_fixture, *args):
    gen = gen_fixture.__wrapped__(*args)
    value = next(gen)
    return value, gen


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else '/'
    out = sys.argv[2] if len(sys.argv) > 2 else '/tmp/mobile_preview.png'

    app, app_gen = _resolve_fixture(_app_fixture)
    server_url, server_gen = _resolve_fixture(_server_fixture, app)

    try:
        with sync_playwright() as p:
            iphone = p.devices['iPhone 13']
            browser = p.webkit.launch()
            context = browser.new_context(**iphone)
            page = context.new_page()
            page.goto(f'{server_url}{path}')
            page.wait_for_timeout(500)
            page.screenshot(path=out, full_page=True)
            browser.close()
        print(f'Saved {out} ({iphone["viewport"]["width"]}x{iphone["viewport"]["height"]} @ {iphone["device_scale_factor"]}x)')
    finally:
        next(server_gen, None)
        next(app_gen, None)


if __name__ == '__main__':
    main()
