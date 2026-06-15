"""
Outbound Discord REST calls for the bot (channel posts). HTTP-only — pairs with
the HTTP-Interactions endpoint in routes/discord.py; no gateway connection.
"""

import threading

import requests

from gcp_secrets import get_secret
from discord_notify import _round_label, fmt_time

API = 'https://discord.com/api/v10'

# Brand palette (mirrors static/css/style.css). Discord buttons can't take a
# custom hex (only a fixed style enum), but an embed's left-border colour can —
# so the brand colour lives on the embed stripe, with buttons leaning on blurple
# (≈ our purple) and green from the available styles.
BRAND_PURPLE = 0xA78BFA   # --accent
BRAND_PINK = 0xF472B6     # --pink
BRAND_GREEN = 0x4ADE80    # --green

# Button styles: 1 primary (blurple), 2 secondary (grey), 3 success (green),
# 4 danger (red), 5 link.
STYLE_BLURPLE = 1
STYLE_GREY = 2
STYLE_GREEN = 3
STYLE_RED = 4
STYLE_LINK = 5


def _embed(description: str, color: int = BRAND_PURPLE, title: str = None, url: str = None):
    """A branded embed — the coloured left stripe is where our purple/pink/green
    actually shows up in Discord."""
    e = {'description': (description or '')[:4096], 'color': color}
    if title:
        e['title'] = title[:256]
    if url:
        e['url'] = url
    return e


def post_message(channel_id: str, content: str = None, components=None, embeds=None):
    """Post a message to a channel as the bot. Best-effort (never raises). Returns
    the created message dict (so callers can keep its id) on success, else None."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and channel_id):
        return None
    payload = {'allowed_mentions': {'parse': []}}
    if content:
        payload['content'] = content[:2000]
    if components:
        payload['components'] = components
    if embeds:
        payload['embeds'] = embeds
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


def edit_message(channel_id: str, message_id: str, content: str = None,
                 components=None, embeds=None) -> bool:
    """Edit a message the bot posted (used to keep an event card's status current).
    Best-effort (never raises)."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and channel_id and message_id):
        return False
    payload = {'content': content or '', 'components': components or [],
               'embeds': embeds or [], 'allowed_mentions': {'parse': []}}
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


# Stripe colour reflects registration status: open = green (go), full = pink,
# closed = purple.
_CARD_COLORS = {'open': BRAND_GREEN, 'full': BRAND_PINK, 'closed': BRAND_PURPLE}


def event_card(event: dict, event_url: str, state: str, note: str = ''):
    """Build (embeds, components) for an event 'card' — a branded embed plus a
    Register button (for players who don't know the slash commands) and a link to
    the web page. The button is disabled and the text reflects status when
    registration is full or closed. `state` is 'open' | 'full' | 'closed'."""
    headline = {'open': 'registration open', 'full': 'registration full',
                'closed': 'registration closed'}.get(state, '')
    bits = [f"_{headline}_"] if headline else []
    meta = ' · '.join(x for x in (event.get('event_type'), event.get('date'),
                                  fmt_time(event.get('start_time')),
                                  event.get('format')) if x)
    if meta:
        bits.append(meta)
    if event.get('entry_cost'):
        bits.append(f"Entry: {event['entry_cost']}")
    if event.get('description'):
        bits.append(event['description'][:300])
    if state != 'open' and note:
        bits.append(f'_{note}._')
    embed = _embed('\n'.join(bits), color=_CARD_COLORS.get(state, BRAND_PURPLE),
                   title=event.get('name', 'Event'), url=event_url)
    register_btn = {'type': 2, 'style': STYLE_BLURPLE, 'label': 'Register',
                    'custom_id': f"cbp_reg_btn:{event['id']}"}
    if state != 'open':
        register_btn['disabled'] = True
    components = [{'type': 1, 'components': [
        register_btn,
        {'type': 2, 'style': STYLE_LINK, 'label': 'View details', 'url': event_url},
    ]}]
    return [embed], components


def dm_event_invite(target_id: str, event: dict, event_url: str, inviter_name: str,
                    state: str = 'open', note: str = '') -> bool:
    """DM `target_id` an invitation to register for an event — the same card the
    /cparty announce flow posts (Register button + details link), prefixed with
    who invited them, plus a button to opt out of future invites. Best-effort;
    returns True only if the DM was actually delivered."""
    embeds, components = event_card(event, event_url, state, note)
    components.append({'type': 1, 'components': [
        {'type': 2, 'style': STYLE_GREY, 'label': "Don't invite me", 'custom_id': 'cbp_invite_optout'}]})
    content = f"🃏 **{inviter_name}** invited you to register for an event on Cardboard Party:"
    return dm_user(target_id, content, components, embeds)


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
    embed = _embed("\n".join(lines), color=BRAND_PURPLE,
                   title=f"{event.get('name', 'Event')} — {_round_label(round_num, rnd)} pairings")
    components = [{'type': 1, 'components': [
        {'type': 2, 'style': STYLE_BLURPLE, 'label': 'Report my result', 'custom_id': 'cbp_report_btn'}]}]
    post_message(channel_id, embeds=[embed], components=components)


def dm_user(discord_id: str, content: str = None, components=None, embeds=None) -> bool:
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
        return bool(post_message(r.json().get('id'), content, components, embeds))
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
                {'type': 2, 'style': STYLE_LINK, 'label': 'View on the web', 'url': event_url}]}]
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
        embed = _embed(f"Your match vs **{opp['name'] if opp else '?'}** was reported "
                       f"as **{outcome}**. GGs!",
                       title=f"{ename} — {label}")
        dm_user(did, embeds=[embed], components=link)


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
    link_btn = ({'type': 2, 'style': STYLE_LINK, 'label': 'View on the web', 'url': event_url}
                if event_url else None)

    def report_row():
        row = [{'type': 2, 'style': STYLE_BLURPLE, 'label': 'Report my result', 'custom_id': 'cbp_report_btn'}]
        if link_btn:
            row.append(link_btn)
        return [{'type': 1, 'components': row}]

    for m in rnd:
        if m.get('is_bye'):
            p = players.get(m.get('player1_id'))
            did = _player_discord_id(p) if p and not p.get('dropped') else None
            if did:
                embed = _embed("You have a **bye** this round (auto-win).",
                               color=BRAND_GREEN, title=f"{ename} — {label}")
                dm_user(did, embeds=[embed],
                        components=([{'type': 1, 'components': [link_btn]}] if link_btn else None))
            continue
        for pid, oid in ((m.get('player1_id'), m.get('player2_id')),
                         (m.get('player2_id'), m.get('player1_id'))):
            p, opp = players.get(pid), players.get(oid)
            if not p or p.get('dropped'):
                continue
            did = _player_discord_id(p)
            if not did:
                continue
            embed = _embed(f"You're paired against **{opp['name'] if opp else '?'}**. "
                           f"Report your result when you're done:",
                           title=f"{ename} — {label}")
            dm_user(did, embeds=[embed], components=report_row())
