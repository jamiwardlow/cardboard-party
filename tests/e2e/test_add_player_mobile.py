"""
Regression test for the mobile keyboard-dismiss bug.

When a user taps the "Add player by name" input on a mobile device, the browser
opens the soft keyboard and fires a window resize event. layoutEventFlow() used
to call replaceChildren() on the column containers, which detaches the focused
input from the DOM and causes the browser to immediately dismiss the keyboard.

This test verifies that after a resize event fires while the input is focused,
the input retains focus.
"""
import pytest
from tests.e2e.conftest import TEST_EVENT_ID, make_session_cookie


@pytest.fixture
def mobile_auth_page(playwright, e2e_server, app):
    """A WebKit page emulating iPhone 13, authenticated as the event organizer."""
    iphone = playwright.devices['iPhone 13']
    browser = playwright.webkit.launch()
    context = browser.new_context(**iphone)

    context.add_cookies([{
        'name': 'session',
        'value': make_session_cookie(app),
        'url': e2e_server,
    }])

    page = context.new_page()
    yield page
    browser.close()


def test_add_player_input_keeps_focus_after_resize(mobile_auth_page, e2e_server):
    page = mobile_auth_page
    page.goto(f'{e2e_server}/events/{TEST_EVENT_ID}')

    # Wait for the organizer toolbar to be visible (JS has initialized).
    page.wait_for_function(
        "() => !document.getElementById('player-actions').classList.contains('hidden')",
        timeout=8000,
    )

    # Tap the input to focus it.
    page.locator('#new-player-name').click()

    focused_before = page.evaluate("() => document.activeElement?.id")
    assert focused_before == 'new-player-name', (
        f"Input did not receive focus on click (got: '{focused_before}')"
    )

    # Simulate the soft keyboard opening — on real mobile it shrinks the
    # viewport and fires window resize. We fire that event directly.
    page.evaluate("() => window.dispatchEvent(new Event('resize'))")

    # The resize handler has a 150ms debounce; wait long enough for it to fire.
    page.wait_for_timeout(400)

    focused_after = page.evaluate("() => document.activeElement?.id")
    assert focused_after == 'new-player-name', (
        f"Input lost focus after resize event (keyboard-dismiss bug): got '{focused_after}'"
    )
