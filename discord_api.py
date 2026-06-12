"""
Outbound Discord REST calls for the bot (channel posts). HTTP-only — pairs with
the HTTP-Interactions endpoint in routes/discord.py; no gateway connection.
"""

import requests

from gcp_secrets import get_secret
from discord_notify import _round_label

API = 'https://discord.com/api/v10'


def post_message(channel_id: str, content: str, components=None) -> bool:
    """Post a message to a channel as the bot. Best-effort (never raises)."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and channel_id):
        return False
    payload = {'content': content[:2000], 'allowed_mentions': {'parse': []}}
    if components:
        payload['components'] = components
    try:
        r = requests.post(f'{API}/channels/{channel_id}/messages',
                          headers={'Authorization': f'Bot {token}'},
                          json=payload, timeout=5)
        if not r.ok:
            print(f'discord post_message {r.status_code}: {r.text[:200]}')
        return r.ok
    except Exception as e:
        print(f'discord post_message error: {e}')
        return False


def announce_event(event: dict, channel_id: str, event_url: str) -> bool:
    """Post an event 'card' to a channel with a one-click Register button (for
    players who don't know the slash commands) and a link to the web page."""
    bits = [f"**{event.get('name', 'Event')}** — registration open"]
    meta = ' · '.join(x for x in (event.get('event_type'), event.get('date'),
                                  event.get('format')) if x)
    if meta:
        bits.append(meta)
    if event.get('entry_cost'):
        bits.append(f"Entry: {event['entry_cost']}")
    if event.get('description'):
        bits.append(event['description'][:300])
    bits.append('Tap **Register** to join right here — no account or link needed.')
    components = [{'type': 1, 'components': [
        {'type': 2, 'style': 3, 'label': 'Register', 'custom_id': f"cbp_reg_btn:{event['id']}"},
        {'type': 2, 'style': 5, 'label': 'View details', 'url': event_url},
    ]}]
    return post_message(channel_id, '\n'.join(bits), components)


def announce_round(event: dict, round_num: int):
    """Post a round's pairings to the event's linked Discord channel (if any),
    with a button players tap to report their own result. Best-effort."""
    channel_id = event.get('discord_channel_id')
    rounds = event.get('rounds', [])
    if not channel_id or not (1 <= round_num <= len(rounds)):
        return
    rnd = rounds[round_num - 1]
    names = {p['id']: p['name'] for p in event.get('players', [])}
    lines = []
    for m in rnd:
        if m.get('is_bye'):
            lines.append(f"• {names.get(m.get('player1_id'), '?')} — *bye*")
        else:
            lines.append(f"• {names.get(m.get('player1_id'), '?')} vs "
                         f"{names.get(m.get('player2_id'), '?')}")
    content = (f"**{event.get('name', 'Event')} — {_round_label(round_num, rnd)} pairings**\n"
               + "\n".join(lines))
    components = [{'type': 1, 'components': [
        {'type': 2, 'style': 1, 'label': 'Report my result', 'custom_id': 'cbp_report_btn'}]}]
    post_message(channel_id, content, components)
