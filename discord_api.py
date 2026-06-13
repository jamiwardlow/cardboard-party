"""
Outbound Discord REST calls for the bot (channel posts). HTTP-only — pairs with
the HTTP-Interactions endpoint in routes/discord.py; no gateway connection.
"""

import threading

import requests

from gcp_secrets import get_secret
from discord_notify import _round_label

API = 'https://discord.com/api/v10'


def post_message(channel_id: str, content: str, components=None):
    """Post a message to a channel as the bot. Best-effort (never raises). Returns
    the created message dict (so callers can keep its id) on success, else None."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and channel_id):
        return None
    payload = {'content': content[:2000], 'allowed_mentions': {'parse': []}}
    if components:
        payload['components'] = components
    try:
        r = requests.post(f'{API}/channels/{channel_id}/messages',
                          headers={'Authorization': f'Bot {token}'},
                          json=payload, timeout=5)
        if not r.ok:
            print(f'discord post_message {r.status_code}: {r.text[:200]}')
            return None
        return r.json()
    except Exception as e:
        print(f'discord post_message error: {e}')
        return None


def edit_message(channel_id: str, message_id: str, content: str, components=None) -> bool:
    """Edit a message the bot posted (used to keep an event card's status current).
    Best-effort (never raises)."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and channel_id and message_id):
        return False
    payload = {'content': content[:2000], 'components': components or [],
               'allowed_mentions': {'parse': []}}
    try:
        r = requests.patch(f'{API}/channels/{channel_id}/messages/{message_id}',
                           headers={'Authorization': f'Bot {token}'},
                           json=payload, timeout=5)
        if not r.ok:
            print(f'discord edit_message {r.status_code}: {r.text[:200]}')
        return r.ok
    except Exception as e:
        print(f'discord edit_message error: {e}')
        return False


def event_card(event: dict, event_url: str, state: str, note: str = ''):
    """Build (content, components) for an event 'card' — a Register button (for
    players who don't know the slash commands) plus a link to the web page. The
    button is disabled and the text reflects status when registration is full or
    closed. `state` is 'open' | 'full' | 'closed'."""
    headline = {'open': 'registration open', 'full': 'registration full',
                'closed': 'registration closed'}.get(state, '')
    title = f"**{event.get('name', 'Event')}**"
    bits = [f"{title} — {headline}" if headline else title]
    meta = ' · '.join(x for x in (event.get('event_type'), event.get('date'),
                                  event.get('format')) if x)
    if meta:
        bits.append(meta)
    if event.get('entry_cost'):
        bits.append(f"Entry: {event['entry_cost']}")
    if event.get('description'):
        bits.append(event['description'][:300])
    if state != 'open' and note:
        bits.append(f'_{note}._')
    register_btn = {'type': 2, 'style': 3, 'label': 'Register',
                    'custom_id': f"cbp_reg_btn:{event['id']}"}
    if state != 'open':
        register_btn['disabled'] = True
    components = [{'type': 1, 'components': [
        register_btn,
        {'type': 2, 'style': 5, 'label': 'View details', 'url': event_url},
    ]}]
    return '\n'.join(bits), components


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


def dm_user(discord_id: str, content: str, components=None) -> bool:
    """Send a direct message to a Discord user (opening the DM channel first).
    Best-effort — fails quietly if the user shares no server with the bot or has
    DMs disabled."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and discord_id):
        return False
    try:
        r = requests.post(f'{API}/users/@me/channels',
                          headers={'Authorization': f'Bot {token}'},
                          json={'recipient_id': str(discord_id)}, timeout=5)
        if not r.ok:
            print(f'discord dm open {r.status_code}: {r.text[:200]}')
            return False
        return bool(post_message(r.json().get('id'), content, components))
    except Exception as e:
        print(f'discord dm_user error: {e}')
        return False


_DRAW_RESULTS = ('draw', '0-0-3')


def _player_discord_id(p):
    """A player's Discord ID — from the entry (bot-registered players), else from
    their linked account's profile (web/organiser-added players who've linked a
    Discord). Returns None if we have no numeric Discord ID for them."""
    if p.get('discord_id'):
        return p['discord_id']
    gid = p.get('google_id')
    if gid:
        from db import get_user_profile
        return get_user_profile(gid).get('discord_id')
    return None


def dm_result_recorded(event: dict, round_idx: int, match_idx: int,
                       exclude_player_id: str = None, base_url: str = ''):
    """DM the players in a match — except `exclude_player_id` (whoever just
    reported) — that the result is in, so they don't try to report it again.
    Background thread, best-effort."""
    threading.Thread(target=_dm_result_recorded,
                     args=(event, round_idx, match_idx, exclude_player_id, base_url),
                     daemon=True).start()


def _dm_result_recorded(event, round_idx, match_idx, exclude_player_id, base_url):
    rounds = event.get('rounds', [])
    if not (0 <= round_idx < len(rounds)):
        return
    rnd = rounds[round_idx]
    if not (0 <= match_idx < len(rnd)):
        return
    m = rnd[match_idx]
    if m.get('is_bye'):
        return
    players = {p['id']: p for p in event.get('players', [])}
    label = _round_label(round_idx + 1, rnd)
    ename = event.get('name', 'Event')
    event_url = f"{base_url.rstrip('/')}/events/{event['id']}" if base_url else ''
    link = ([{'type': 1, 'components': [
                {'type': 2, 'style': 5, 'label': 'View on the web', 'url': event_url}]}]
            if event_url else None)
    winner, result = m.get('winner_id'), m.get('result')
    for pid, oid in ((m.get('player1_id'), m.get('player2_id')),
                     (m.get('player2_id'), m.get('player1_id'))):
        if pid == exclude_player_id:
            continue
        p, opp = players.get(pid), players.get(oid)
        if not p or p.get('dropped'):
            continue
        did = _player_discord_id(p)
        if not did:
            continue
        if winner is None and result in _DRAW_RESULTS:
            outcome = 'a draw'
        elif winner == pid:
            outcome = 'a win for you'
        else:
            outcome = f"a win for {opp['name'] if opp else 'your opponent'}"
        dm_user(did,
                f"**{ename} — {label}**\nYour match vs **{opp['name'] if opp else '?'}** "
                f"was reported as **{outcome}** — no need to report it again.",
                link)


def dm_round_pairings(event: dict, round_num: int, base_url: str = ''):
    """DM each player (who has a linked Discord) their pairing for this round,
    with a 'Report my result' button and a link to the event page. Runs in a
    background thread so DMing a large field doesn't block the pairing response."""
    threading.Thread(target=_dm_round_pairings,
                     args=(event, round_num, base_url), daemon=True).start()


def _dm_round_pairings(event: dict, round_num: int, base_url: str):
    rounds = event.get('rounds', [])
    if not (1 <= round_num <= len(rounds)):
        return
    rnd = rounds[round_num - 1]
    players = {p['id']: p for p in event.get('players', [])}
    label = _round_label(round_num, rnd)
    ename = event.get('name', 'Event')
    event_url = f"{base_url.rstrip('/')}/events/{event['id']}" if base_url else ''
    link_btn = ({'type': 2, 'style': 5, 'label': 'View on the web', 'url': event_url}
                if event_url else None)

    def report_row():
        row = [{'type': 2, 'style': 1, 'label': 'Report my result', 'custom_id': 'cbp_report_btn'}]
        if link_btn:
            row.append(link_btn)
        return [{'type': 1, 'components': row}]

    for m in rnd:
        if m.get('is_bye'):
            p = players.get(m.get('player1_id'))
            did = _player_discord_id(p) if p and not p.get('dropped') else None
            if did:
                dm_user(did,
                        f"**{ename} — {label}**\nYou have a **bye** this round (auto-win).",
                        ([{'type': 1, 'components': [link_btn]}] if link_btn else None))
            continue
        for pid, oid in ((m.get('player1_id'), m.get('player2_id')),
                         (m.get('player2_id'), m.get('player1_id'))):
            p, opp = players.get(pid), players.get(oid)
            if not p or p.get('dropped'):
                continue
            did = _player_discord_id(p)
            if not did:
                continue
            dm_user(did,
                    f"**{ename} — {label}**\nYou're paired against "
                    f"**{opp['name'] if opp else '?'}**. Report your result when you're done:",
                    report_row())
