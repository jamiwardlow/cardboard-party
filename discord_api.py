"""
Outbound Discord REST calls for the bot (channel posts). HTTP-only — pairs with
the HTTP-Interactions endpoint in routes/discord.py; no gateway connection.
"""

import datetime
import threading
import time

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


_widget_cache = {}            # guild_id -> (expires_at, data|None)
_WIDGET_TTL = datetime.timedelta(minutes=5)


def get_widget_info(guild_id: str):
    """Public server-widget data (server name, online count, instant-invite link)
    for a custom 'join our Discord' card. No auth needed, but the server must have
    'Enable Server Widget' turned on. Best-effort and cached for a few minutes:
    returns None (cached too, to avoid hammering on an outage) if the widget is off
    or Discord is unreachable, so callers can fall back to a static card."""
    now = datetime.datetime.now(datetime.timezone.utc)
    cached = _widget_cache.get(guild_id)
    if cached and cached[0] > now:
        return cached[1]
    data = None
    try:
        r = requests.get(f'https://discord.com/api/guilds/{guild_id}/widget.json',
                         timeout=3)
        r.raise_for_status()
        w = r.json()
        data = {
            'name':   w.get('name'),
            'online': w.get('presence_count'),
            'invite': w.get('instant_invite'),
        }
    except Exception:
        data = None
    _widget_cache[guild_id] = (now + _WIDGET_TTL, data)
    return data


def _embed(description: str, color: int = BRAND_PURPLE, title: str = None, url: str = None):
    """A branded embed — the coloured left stripe is where our purple/pink/green
    actually shows up in Discord."""
    e = {'description': (description or '')[:4096], 'color': color}
    if title:
        e['title'] = title[:256]
    if url:
        e['url'] = url
    return e


def post_message(channel_id: str, content: str = None, components=None, embeds=None,
                 allowed_mentions=None):
    """Post a message to a channel as the bot. Best-effort (never raises). Returns
    the created message dict (so callers can keep its id) on success, else None.
    `allowed_mentions` defaults to none (no pings); pass e.g. {'roles': [id]} to
    intentionally ping a role."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and channel_id):
        return None
    payload = {'allowed_mentions': allowed_mentions or {'parse': []}}
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


def get_user(discord_id: str):
    """Fetch a user object (username, global_name, …) by numeric ID, or None.
    Best-effort — used to backfill the @handle of players who registered via the
    bot before we captured it."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and discord_id):
        return None
    try:
        r = requests.get(f'{API}/users/{discord_id}',
                         headers={'Authorization': f'Bot {token}'}, timeout=5)
        return r.json() if r.ok else None
    except Exception as e:
        print(f'discord get_user error: {e}')
        return None


# ── Server / channel discovery (for the web channel picker) ──────────────────────

def list_bot_guilds():
    """Servers the bot is a member of — [{id, name}]. Best-effort ([] on failure)."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not token:
        return []
    try:
        r = requests.get(f'{API}/users/@me/guilds',
                         headers={'Authorization': f'Bot {token}'}, timeout=8)
        if not r.ok:
            print(f'discord list_bot_guilds {r.status_code}: {r.text[:200]}')
            return []
        return [{'id': g['id'], 'name': g.get('name', 'server')} for g in r.json()]
    except Exception as e:
        print(f'discord list_bot_guilds error: {e}')
        return []

_ADMINISTRATOR = 0x8   # Discord permission bit

def _bot_get(path: str):
    """GET a Discord API path with the bot token; parsed JSON or None."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not token:
        return None
    try:
        r = requests.get(f'{API}{path}', headers={'Authorization': f'Bot {token}'}, timeout=8)
        return r.json() if r.ok else None
    except Exception as e:
        print(f'discord GET {path} error: {e}')
        return None

def is_user_guild_admin(guild_id: str, user_id: str) -> bool:
    """True if `user_id` owns `guild_id` or holds a role with the ADMINISTRATOR
    permission there. Uses the bot token (no user OAuth needed). Best-effort."""
    if not (guild_id and user_id):
        return False
    guild = _bot_get(f'/guilds/{guild_id}')
    if guild and str(guild.get('owner_id')) == str(user_id):
        return True
    member = _bot_get(f'/guilds/{guild_id}/members/{user_id}')
    if not member:                        # not a member (404) or error
        return False
    roles = _bot_get(f'/guilds/{guild_id}/roles') or []
    admin_role_ids = {r['id'] for r in roles if int(r.get('permissions', 0)) & _ADMINISTRATOR}
    return any(rid in admin_role_ids for rid in member.get('roles', []))

def list_guild_text_channels(guild_id: str):
    """Text/announcement channels in a guild — [{id, name}], in Discord's order.
    Best-effort ([] on failure)."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and guild_id):
        return []
    try:
        r = requests.get(f'{API}/guilds/{guild_id}/channels',
                         headers={'Authorization': f'Bot {token}'}, timeout=8)
        if not r.ok:
            print(f'discord list_guild_text_channels {r.status_code}: {r.text[:200]}')
            return []
        # Channel types: 0 = text, 5 = announcement. Others (voice, category, forum…)
        # can't take a normal message, so they're excluded.
        chans = [c for c in r.json() if c.get('type') in (0, 5)]
        chans.sort(key=lambda c: (c.get('position', 0), c.get('name', '')))
        return [{'id': c['id'], 'name': c.get('name', 'channel')} for c in chans]
    except Exception as e:
        print(f'discord list_guild_text_channels error: {e}')
        return []


# ── Event roles (assign a Discord role to an event's members) ────────────────────

def guild_id_for_channel(channel_id: str):
    """The guild a linked channel belongs to (GET /channels/{id}), or None."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and channel_id):
        return None
    try:
        r = requests.get(f'{API}/channels/{channel_id}',
                         headers={'Authorization': f'Bot {token}'}, timeout=5)
        return r.json().get('guild_id') if r.ok else None
    except Exception as e:
        print(f'discord guild_id_for_channel error: {e}')
        return None


def create_guild_role(guild_id: str, name: str):
    """Create a mentionable role. Returns the role id, '' when the bot lacks the
    Manage Roles permission / hierarchy (403), or None on other failure."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and guild_id):
        return None
    try:
        r = requests.post(f'{API}/guilds/{guild_id}/roles',
                          headers={'Authorization': f'Bot {token}'},
                          json={'name': (name or 'Event players')[:100], 'mentionable': True},
                          timeout=8)
        if r.status_code == 403:
            return ''
        if not r.ok:
            print(f'discord create_role {r.status_code}: {r.text[:200]}')
            return None
        return r.json().get('id')
    except Exception as e:
        print(f'discord create_role error: {e}')
        return None


def add_member_role(guild_id: str, user_id: str, role_id: str) -> str:
    """Assign a role to a guild member. Returns 'ok' | 'forbidden' (Manage Roles /
    hierarchy) | 'missing' (member not in the guild) | 'error'."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and guild_id and user_id and role_id):
        return 'error'
    try:
        r = requests.put(f'{API}/guilds/{guild_id}/members/{user_id}/roles/{role_id}',
                         headers={'Authorization': f'Bot {token}'}, timeout=8)
        if r.status_code in (200, 204):
            return 'ok'
        if r.status_code == 403:
            return 'forbidden'
        if r.status_code == 404:
            return 'missing'
        print(f'discord add_member_role {r.status_code}: {r.text[:200]}')
        return 'error'
    except Exception as e:
        print(f'discord add_member_role error: {e}')
        return 'error'


def delete_guild_role(guild_id: str, role_id: str) -> bool:
    """Delete an event role (best-effort; treats an already-gone role as success)."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and guild_id and role_id):
        return False
    try:
        r = requests.delete(f'{API}/guilds/{guild_id}/roles/{role_id}',
                            headers={'Authorization': f'Bot {token}'}, timeout=8)
        return r.ok or r.status_code == 404
    except Exception as e:
        print(f'discord delete_role error: {e}')
        return False


def sync_event_role(event: dict, role_id: str):
    """Assign the event role to every non-dropped player with a linked Discord ID,
    then post a summary to the linked channel. Background thread, best-effort."""
    threading.Thread(target=_sync_event_role, args=(event, role_id), daemon=True).start()


def _sync_event_role(event, role_id):
    guild_id = event.get('discord_guild_id')
    channel_id = event.get('discord_channel_id')
    if not (guild_id and role_id):
        return
    assigned = forbidden = missing = 0
    for p in event.get('players', []):
        if p.get('dropped'):
            continue
        did = _player_discord_id(p)
        if not did:
            continue
        status = add_member_role(guild_id, did, role_id)
        if status == 'ok':
            assigned += 1
        elif status == 'forbidden':
            forbidden += 1
        elif status == 'missing':
            missing += 1
        time.sleep(0.15)                               # gentle on the rate limit
    mention = f'<@&{role_id}>'
    if forbidden:
        msg = (f"⚠️ Couldn't fully assign {mention} — I need the **Manage Roles** "
               f"permission and my role positioned above it.")
    else:
        no_link = sum(1 for p in event.get('players', [])
                      if not p.get('dropped') and not _player_discord_id(p))
        parts = [f"Added **{assigned}** member{'' if assigned == 1 else 's'} to {mention}."]
        if no_link:
            parts.append(f"{no_link} have no linked Discord — add them manually.")
        if missing:
            parts.append(f"{missing} aren't in this server yet.")
        msg = ' '.join(parts)
    if channel_id:
        post_message(channel_id, content=msg)         # parse:[] → renders the role, no ping


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
    """Build (embeds, components) for an event 'card' — a branded embed plus an
    action button (Register, or Join Waitlist when full) and a link to the web
    page. When full the card invites players onto the waitlist; when closed the
    button is disabled. `state` is 'open' | 'full' | 'closed'."""
    headline = {'open': 'registration open', 'full': 'waitlist open',
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
    if state == 'full':
        action_btn = {'type': 2, 'style': STYLE_GREEN, 'label': 'Join Waitlist',
                      'custom_id': f"cbp_waitlist_btn:{event['id']}"}
    else:
        action_btn = {'type': 2, 'style': STYLE_BLURPLE, 'label': 'Register',
                      'custom_id': f"cbp_reg_btn:{event['id']}"}
        if state != 'open':
            action_btn['disabled'] = True
    components = [{'type': 1, 'components': [
        action_btn,
        {'type': 2, 'style': STYLE_LINK, 'label': 'View details', 'url': event_url},
    ]}]
    return [embed], components


def dm_event_invite(target_id: str, event: dict, event_url: str, inviter_name: str,
                    state: str = 'open', note: str = '', message: str = '') -> bool:
    """DM `target_id` an invitation to register for an event — the same card the
    /cparty announce flow posts (Register button + details link), prefixed with
    who invited them, plus a button to opt out of future invites. `message` is an
    optional personal note from the inviter. Best-effort; returns True only if the
    DM was actually delivered."""
    embeds, components = event_card(event, event_url, state, note)
    components.append({'type': 1, 'components': [
        {'type': 2, 'style': STYLE_GREY, 'label': "Don't invite me", 'custom_id': 'cbp_invite_optout'}]})
    verb = 'join the waitlist for' if state == 'full' else 'register for'
    content = f"🃏 **{inviter_name}** invited you to {verb} an event on Cardboard Party:"
    if message:
        # Quote the personal note so it reads as coming from the inviter (>>> spans
        # the rest of the message, so multi-line notes stay in the quote).
        content += f"\n\n>>> {message}"
    return dm_user(target_id, content, components, embeds)


def dm_registration_confirmation(discord_id: str, event: dict, event_url: str):
    """DM a player confirming their registration for an event. Best-effort."""
    meta = ' · '.join(x for x in (event.get('event_type'), event.get('date'),
                                   fmt_time(event.get('start_time')),
                                   event.get('format')) if x)
    desc = '✅ You\'re registered!' + (f'\n{meta}' if meta else '')
    embed = _embed(desc, color=BRAND_GREEN, title=event.get('name', 'Event'), url=event_url)
    components = [{'type': 1, 'components': [
        {'type': 2, 'style': STYLE_LINK, 'label': 'View event', 'url': event_url},
    ]}]
    dm_user(discord_id, embeds=[embed], components=components)


def _round_end_unix(event: dict):
    """Unix timestamp when the current round's timer ends, or None if no timer is
    configured/started. Used for Discord's live <t:…:R> relative time."""
    started = event.get('round_started_at')
    mins = event.get('round_timer_minutes') or 0
    if not started or not mins:
        return None
    try:
        return int(datetime.datetime.fromisoformat(started).timestamp()) + mins * 60
    except (ValueError, TypeError):
        return None


def _pairings_components(link_btn=None):
    """The channel pairings post's action row: Report + optional 'View on the web'."""
    row = [{'type': 2, 'style': STYLE_BLURPLE, 'label': 'Report my result', 'custom_id': 'cbp_report_btn'}]
    if link_btn:
        row.append(link_btn)
    return [{'type': 1, 'components': row}]


def _table_name(event: dict, m: dict) -> str:
    """'Table 5' (or 'Table 5 · Feature Match') for a seated match, else '' — when
    tables are off, the match is unassigned, or it's a bye."""
    if not event.get('tables_enabled') or m.get('is_bye'):
        return ''
    t = m.get('table')
    if not t:
        return ''
    label = (event.get('table_labels') or {}).get(str(t))
    return f"Table {t}" + (f" · {label}" if label else "")


def _pairings_embed(event: dict, round_num: int, end_unix=None):
    rnd = event['rounds'][round_num - 1]
    names = {p['id']: p['name'] for p in event.get('players', [])}
    lines = []
    for m in rnd:
        if m.get('is_bye'):
            lines.append(f"• {names.get(m.get('player1_id'), '?')} — *bye*")
        else:
            tag = _table_name(event, m)
            prefix = f"**{tag}** — " if tag else ""
            lines.append(f"• {prefix}{names.get(m.get('player1_id'), '?')} vs "
                         f"{names.get(m.get('player2_id'), '?')}")
    if end_unix:
        # Discord renders <t:…:R> as a live, client-updating relative time.
        lines.append(f"\n⏱ **Round ends** <t:{end_unix}:R>")
    return _embed("\n".join(lines), color=BRAND_PURPLE,
                  title=f"{event.get('name', 'Event')} — {_round_label(round_num, rnd)} pairings")


def announce_round(event: dict, round_num: int, base_url: str = ''):
    """Post a round's pairings to the event's linked Discord channel (if any),
    with a button players tap to report their own result and a 'View on the web'
    link. Remembers the message so the timer can be added to it later. Best-effort.
    Includes the live countdown when the timer's already running (auto-start);
    otherwise there's no timer line until the organiser starts it (see
    update_round_pairings)."""
    channel_id = event.get('discord_channel_id')
    rounds = event.get('rounds', [])
    if not channel_id or not (1 <= round_num <= len(rounds)):
        return
    msg = post_message(channel_id, embeds=[_pairings_embed(event, round_num, _round_end_unix(event))],
                       components=_pairings_components(_link_btn(base_url, event['id'])))
    if msg and event.get('id'):
        from db import save_event
        save_event(event['id'], {'discord_pairings': {
            'channel_id': channel_id, 'message_id': msg.get('id'), 'round_num': round_num}})


def update_round_pairings(event: dict, base_url: str = ''):
    """Re-render the latest round's pairings post to show the live timer countdown
    (called when the organiser starts or restarts the round timer). Best-effort
    no-op if there's no stored pairings message for the current round."""
    p = event.get('discord_pairings') or {}
    rounds = event.get('rounds', [])
    if not p.get('message_id') or p.get('round_num') != len(rounds):
        return
    embed = _pairings_embed(event, p['round_num'], _round_end_unix(event))
    edit_message(p['channel_id'], p['message_id'], embeds=[embed],
                 components=_pairings_components(_link_btn(base_url, event['id'])))


def dm_user(discord_id: str, content: str = None, components=None, embeds=None):
    """Send a direct message to a Discord user (opening the DM channel first).
    Returns the created message dict (with its id + channel_id, so callers can
    edit it later) on success, else None. Best-effort — fails quietly if the user
    shares no server with the bot or has DMs disabled."""
    token = get_secret('DISCORD_BOT_TOKEN')
    if not (token and discord_id):
        return None
    try:
        r = requests.post(f'{API}/users/@me/channels',
                          headers={'Authorization': f'Bot {token}'},
                          json={'recipient_id': str(discord_id)}, timeout=5)
        if not r.ok:
            print(f'discord dm open {r.status_code}: {r.text[:200]}')
            return None
        return post_message(r.json().get('id'), content, components, embeds)
    except Exception as e:
        print(f'discord dm_user error: {e}')
        return None


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


def _result_dm_embed(event: dict, m: dict, pid: str, opp_name: str, round_label: str):
    """The result card sent/shown to player `pid`: event title + outcome message."""
    winner, result = m.get('winner_id'), m.get('result')
    if winner is None and result in _DRAW_RESULTS:
        outcome = 'a draw'
    elif winner == pid:
        outcome = 'a win for you'
    else:
        outcome = f"a win for {opp_name}"
    return _embed(f"Your match vs **{opp_name}** was reported as **{outcome}**. GGs!",
                  title=f"{event.get('name', 'Event')} — {round_label}")


def notify_result(event: dict, round_idx: int, match_idx: int, base_url: str = '',
                  exclude_player_id: str = None):
    """After a result is recorded, update the players over Discord: turn each
    player's stored pairing DM for this round into a result card (outcome + event
    link + a disabled 'Result reported' button), or send a fresh card if they have
    no pairing DM. `exclude_player_id` skips the reporter when their own message is
    already handled (e.g. a Discord report's interaction response). Background
    thread, best-effort."""
    threading.Thread(target=_notify_result,
                     args=(event, round_idx, match_idx, base_url, exclude_player_id),
                     daemon=True).start()


def _notify_result(event, round_idx, match_idx, base_url, exclude_player_id=None):
    rounds = event.get('rounds', [])
    if not (0 <= round_idx < len(rounds)):
        return
    rnd = rounds[round_idx]
    if not (0 <= match_idx < len(rnd)) or rnd[match_idx].get('is_bye'):
        return
    m = rnd[match_idx]
    players = {p['id']: p for p in event.get('players', [])}
    label = _round_label(round_idx + 1, rnd)
    link_btn = _link_btn(base_url, event['id'])
    info = event.get('discord_pairing_dms') or {}
    dms = (info.get('dms') or {}) if info.get('round_num') == round_idx + 1 else {}
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
        embed = _result_dm_embed(event, m, pid, opp['name'] if opp else '?', label)
        dm = dms.get(pid)
        if dm and dm.get('message_id'):
            # Transform the existing pairing DM into the result card in place.
            edit_message(dm['channel_id'], dm['message_id'], embeds=[embed],
                         components=_reported_components(link_btn))
        else:
            # No pairing DM on file — send a fresh card with just the link.
            dm_user(did, embeds=[embed],
                    components=([{'type': 1, 'components': [link_btn]}] if link_btn else None))


def dm_round_pairings(event: dict, round_num: int, base_url: str = ''):
    """DM each player (who has a linked Discord) their pairing for this round,
    with a 'Report my result' button and a link to the event page. Runs in a
    background thread so DMing a large field doesn't block the pairing response."""
    threading.Thread(target=_dm_round_pairings,
                     args=(event, round_num, base_url), daemon=True).start()


def _link_btn(base_url: str, event_id: str):
    """The 'View on the web' link button, or None when there's no base URL."""
    url = f"{base_url.rstrip('/')}/events/{event_id}" if base_url else ''
    return ({'type': 2, 'style': STYLE_LINK, 'label': 'View on the web', 'url': url}
            if url else None)


def _report_components(link_btn, report_custom_id='cbp_report_btn'):
    """The action row shown on an unreported pairing DM: Report + optional link.
    `report_custom_id` carries the specific match (cbp_report_btn:<eid>:<ri>:<mi>)
    so the DM's button reports that exact pairing rather than offering a picker."""
    row = [{'type': 2, 'style': STYLE_BLURPLE, 'label': 'Report my result', 'custom_id': report_custom_id}]
    if link_btn:
        row.append(link_btn)
    return [{'type': 1, 'components': row}]


def _report_btn_id(event: dict, round_num: int, match_idx: int) -> str:
    """Match-specific custom_id for a pairing DM's Report button."""
    return f"cbp_report_btn:{event['id']}:{round_num - 1}:{match_idx}"


def _reported_components(link_btn=None):
    """The disabled 'Result reported' row shown once a result is in, optionally
    with a 'View on the web' link button alongside."""
    row = [{'type': 2, 'style': STYLE_GREY, 'label': 'Result reported',
            'custom_id': 'cbp_reported_noop', 'disabled': True}]
    if link_btn:
        row.append(link_btn)
    return [{'type': 1, 'components': row}]


def _pairing_dm_embed(event: dict, m: dict, opp_name: str, round_label: str):
    """The pairing-DM body, including the table assignment when one is set."""
    tag = _table_name(event, m)
    seat = f" at **{tag}**" if tag else ""
    return _embed(f"You're paired against **{opp_name}**{seat}. "
                  f"Report your result when you're done:",
                  title=f"{event.get('name', 'Event')} — {round_label}")


def _dm_round_pairings(event: dict, round_num: int, base_url: str):
    rounds = event.get('rounds', [])
    if not (1 <= round_num <= len(rounds)):
        return
    rnd = rounds[round_num - 1]
    players = {p['id']: p for p in event.get('players', [])}
    label = _round_label(round_num, rnd)
    ename = event.get('name', 'Event')
    link_btn = _link_btn(base_url, event['id'])

    dms = {}   # player_id -> {channel_id, message_id}, so a report elsewhere can
               # mark this DM "reported" too (see mark_dm_pairing_reported).
    for midx, m in enumerate(rnd):
        if m.get('is_bye'):
            p = players.get(m.get('player1_id'))
            did = _player_discord_id(p) if p and not p.get('dropped') else None
            if did:
                embed = _embed("You have a **bye** this round (auto-win). GGs!",
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
            embed = _pairing_dm_embed(event, m, opp['name'] if opp else '?', label)
            msg = dm_user(did, embeds=[embed],
                          components=_report_components(link_btn, _report_btn_id(event, round_num, midx)))
            if msg and msg.get('id'):
                dms[pid] = {'channel_id': msg.get('channel_id'), 'message_id': msg['id']}
    if dms and event.get('id'):
        from db import save_event
        save_event(event['id'], {'discord_pairing_dms': {'round_num': round_num, 'dms': dms}})


def update_pairing_dm_for_match(event: dict, round_num: int, match_idx: int, base_url: str = ''):
    """Re-edit the two players' pairing DMs for one match so they show the current
    table (preserving the match's reported state). Threaded + best-effort; no-op if
    we have no stored DMs for this round or it's a bye. Called when an organiser
    changes a match's table after pairings have been DM'd."""
    info = event.get('discord_pairing_dms') or {}
    if info.get('round_num') != round_num:
        return
    dms = info.get('dms') or {}
    rounds = event.get('rounds', [])
    if not (1 <= round_num <= len(rounds)):
        return
    rnd = rounds[round_num - 1]
    if not (0 <= match_idx < len(rnd)) or rnd[match_idx].get('is_bye'):
        return
    threading.Thread(target=lambda: _edit_match_dms(event, round_num, match_idx, dms, base_url),
                     daemon=True).start()


def _edit_match_dms(event: dict, round_num: int, match_idx: int, dms: dict, base_url: str):
    rnd = event['rounds'][round_num - 1]
    m = rnd[match_idx]
    players = {p['id']: p for p in event.get('players', [])}
    label = _round_label(round_num, rnd)
    link_btn = _link_btn(base_url, event['id'])
    # A reported match keeps its disabled 'Result reported' row; otherwise the
    # Report row — so editing the embed (for the new table) doesn't revert it.
    comps = (_reported_components(link_btn) if m.get('result')
             else _report_components(link_btn, _report_btn_id(event, round_num, match_idx)))
    for pid, oid in ((m.get('player1_id'), m.get('player2_id')),
                     (m.get('player2_id'), m.get('player1_id'))):
        dm = dms.get(pid)
        if not dm or not dm.get('message_id'):
            continue
        opp = players.get(oid)
        embed = _pairing_dm_embed(event, m, opp['name'] if opp else '?', label)
        edit_message(dm['channel_id'], dm['message_id'], embeds=[embed], components=comps)


def dm_pairing_changed(event: dict, round_num: int, changed_pids, base_url: str = ''):
    """A pairing edit changed these players' opponent and/or table: ping each with a
    fresh DM showing their new pairing, supersede their now-stale pairing DM, and
    re-track the new message. Background thread, best-effort."""
    threading.Thread(target=_dm_pairing_changed,
                     args=(event, round_num, list(changed_pids), base_url), daemon=True).start()


def _dm_pairing_changed(event, round_num, changed_pids, base_url):
    rounds = event.get('rounds', [])
    if not (1 <= round_num <= len(rounds)):
        return
    rnd = rounds[round_num - 1]
    players = {p['id']: p for p in event.get('players', [])}
    label = _round_label(round_num, rnd)
    ename = event.get('name', 'Event')
    link_btn = _link_btn(base_url, event['id'])
    info = event.get('discord_pairing_dms') or {}
    old_dms = (info.get('dms') or {}) if info.get('round_num') == round_num else {}
    # Each real player's current match (bye markers excluded; bye recipient = p1).
    match_of = {}
    for m in rnd:
        match_of[m.get('player1_id')] = m
        if not m.get('is_bye'):
            match_of[m.get('player2_id')] = m
    new_dms = dict(old_dms)
    for pid in changed_pids:
        p = players.get(pid)
        m = match_of.get(pid)
        if not p or p.get('dropped') or not m:
            continue
        did = _player_discord_id(p)
        if not did:
            continue
        # Supersede the stale DM so its old opponent/table isn't acted on.
        old = old_dms.get(pid)
        if old and old.get('message_id'):
            edit_message(old['channel_id'], old['message_id'], components=[],
                         embeds=[_embed("Your pairing for this round changed — see the newer message below.",
                                        title=f"{ename} — {label}")])
        if m.get('is_bye'):
            embed = _embed("Heads up — your pairing changed. You now have a **bye** this round "
                           "(auto-win). GGs!", color=BRAND_GREEN, title=f"{ename} — {label}")
            msg = dm_user(did, embeds=[embed],
                          components=([{'type': 1, 'components': [link_btn]}] if link_btn else None))
        else:
            oid = m['player2_id'] if m.get('player1_id') == pid else m['player1_id']
            opp = players.get(oid)
            tag = _table_name(event, m)
            seat = f" at **{tag}**" if tag else ""
            embed = _embed(f"Heads up — your pairing changed. You're now paired against "
                           f"**{opp['name'] if opp else '?'}**{seat}. Report your result when you're done:",
                           title=f"{ename} — {label}")
            msg = dm_user(did, embeds=[embed],
                          components=_report_components(link_btn, _report_btn_id(event, round_num, rnd.index(m))))
        if msg and msg.get('id'):
            new_dms[pid] = {'channel_id': msg.get('channel_id'), 'message_id': msg['id']}
    if event.get('id'):
        from db import save_event
        save_event(event['id'], {'discord_pairing_dms': {'round_num': round_num, 'dms': new_dms}})


def mark_dm_pairing_reported(event: dict, player_id: str):
    """Edit a player's pairing DM for the current round to show their result is in
    — swapping the 'Report my result' button for a disabled 'Result reported', the
    same end state as reporting from the DM. Best-effort no-op if we have no stored
    DM message for them this round."""
    info = event.get('discord_pairing_dms') or {}
    if info.get('round_num') != len(event.get('rounds', [])):
        return
    dm = (info.get('dms') or {}).get(player_id)
    if not dm or not dm.get('message_id'):
        return
    edit_message(dm['channel_id'], dm['message_id'], components=_reported_components())


def mark_dm_pairings_reported(event: dict, player_ids):
    """Background, best-effort: mark each player's pairing DM as reported, so it
    doesn't block the 3s interaction response with extra Discord edits."""
    threading.Thread(
        target=lambda: [mark_dm_pairing_reported(event, pid) for pid in player_ids],
        daemon=True).start()
