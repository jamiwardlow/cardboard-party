"""
Event routes.

Permission model:
  can_manage(event) = is_admin(user) OR user is event owner
  Players can register/unregister themselves and report their own results.
  Anyone can view events and standings.
"""

from flask import Blueprint, request, jsonify, render_template, abort, session
from db import (create_event, get_event, save_event, list_events, delete_event,
                set_player_dropped, set_player_field,
                get_admins, is_admin, add_admin, remove_admin,
                get_user_profile, save_user_profile, list_users,
                record_invite, recent_invite_count, target_invited_since,
                set_invite_optout, is_invite_opted_out)
from swiss import (pair_round, compute_standings, default_num_rounds, BYE_PLAYER_ID,
                   make_bracket, next_bracket_round, CUT_SIZES, DRAW_RESULTS, id_safe_players)
from routes.auth import get_current_user, login_required
from gcp_secrets import get_secret
import discord_api
from storage import upload_avatar, upload_brand_image, delete_object
import datetime
import os
import re
import time
import uuid
from urllib.parse import urlparse

events_bp = Blueprint('events', __name__)

# Per-environment slash-command name (see routes/discord.py); 'cparty' in prod,
# 'cpstaging' on staging. Used in user-facing text and the /discord-bot page.
COMMAND_NAME = os.environ.get('DISCORD_COMMAND_NAME', 'cparty')

# Advanced-creation vocabularies. Kept server-side so the stored values are
# validated against an allow-list rather than trusting whatever the client sends.
TOURNAMENT_TAGS = ['Weekly Play', 'Prerelease', 'Regional Championship Qualifier',
                   'Spotlight Series']
STRUCTURES = ['swiss', 'swiss_top_cut', 'custom']


def _clean_tags(raw) -> list:
    """Keep only recognised tags, preserving the canonical order."""
    chosen = set(raw or [])
    return [t for t in TOURNAMENT_TAGS if t in chosen]

# Plain-text (not rich-text) player-communication fields. Stored verbatim and
# rendered with white-space:pre-wrap + autoescape, so they're safe by default —
# no HTML, no sanitizer needed. Capped to keep documents reasonable.
COMMS_FIELDS = ('rules', 'schedule', 'prizes', 'contact')
_COMMS_MAX = 5000

def _clean_comms(data: dict) -> dict:
    out = {}
    for f in COMMS_FIELDS:
        if f in data:
            out[f] = str(data.get(f) or '')[:_COMMS_MAX]
    return out

REGISTRATION_TYPES = ('open', 'invite_only')

def _self_registration_blocked(event: dict) -> str | None:
    """Why a player can't self-register right now, or None if they can. Covers the
    manual open/closed toggle, invite-only type, and the scheduled date window.
    (Organisers adding players bypass this — it gates self-service only.)"""
    if event.get('registration') != 'open':
        return 'Registration is closed'
    if event.get('registration_type') == 'invite_only':
        return 'This event is by invitation — contact the organiser to be added'
    today = datetime.date.today().isoformat()
    start = event.get('registration_start')
    end   = event.get('registration_end')
    if start and today < start:
        return f'Registration opens on {start}'
    if end and today > end:
        return f'Registration closed on {end}'
    return None

def _entry_code_error(event: dict, data: dict) -> str | None:
    """If the event requires an entry code, check the supplied one. None if OK."""
    code = event.get('entry_code')
    if code and str((data or {}).get('entry_code') or '').strip() != code:
        return 'Incorrect entry code'
    return None


# ── Discord registration (used by the Discord bot, routes/discord.py) ──────────

def _registration_card_status(event: dict):
    """(state, note) for an announced event card — mirrors the self-registration
    gates so the posted card can show open / full / closed. state is
    'open' | 'full' | 'closed'."""
    blocked = _self_registration_blocked(event)
    if blocked:
        return 'closed', blocked
    if event.get('entry_code'):
        return 'closed', 'Entry code required — register on the web'
    cap = event.get('registration_cap', 0)
    active = len([p for p in event['players'] if not p.get('dropped')])
    if cap and active >= cap:
        return 'full', f'Full — {cap} players'
    return 'open', ''

def announce_event_to_channel(event_id: str, channel_id: str, base_url: str):
    """Post an event card (Register button + details link) to a channel and
    remember the message so its status can be kept current. Returns
    (event_name, posted) — posted is False if the event is gone or the post
    failed (e.g. missing channel permission)."""
    e = get_event(event_id)
    if not e:
        return None, False
    base = (base_url or '').rstrip('/')
    state, note = _registration_card_status(e)
    embeds, components = discord_api.event_card(e, f'{base}/events/{event_id}', state, note)
    msg = discord_api.post_message(channel_id, components=components, embeds=embeds)
    if not msg:
        return e.get('name', 'the event'), False
    save_event(event_id, {'discord_announce': {
        'channel_id': channel_id, 'message_id': msg.get('id'), 'base_url': base}})
    return e.get('name', 'the event'), True

def refresh_event_announcement(event: dict) -> None:
    """If this event has an announcement card posted, edit it to reflect the
    current registration status (open / full / closed). Best-effort no-op when
    there's no card. `event` must reflect the current players/registration."""
    ann = event.get('discord_announce') or {}
    if not ann.get('message_id'):
        return
    base = (ann.get('base_url') or '').rstrip('/')
    state, note = _registration_card_status(event)
    embeds, components = discord_api.event_card(
        event, f"{base}/events/{event['id']}", state, note)
    discord_api.edit_message(ann['channel_id'], ann['message_id'],
                             components=components, embeds=embeds)


def discord_registerable_events(limit: int = 25):
    """Events a Discord user can currently self-register for — open, not test,
    not invite-only/closed/expired/full, and with no entry code (codes aren't
    handled in the Discord flow yet). Soonest first; capped for the select menu."""
    out = []
    for e in list_events():
        if e.get('test_mode') or e.get('entry_code'):
            continue
        if _self_registration_blocked(e):
            continue
        cap = e.get('registration_cap', 0)
        active = [p for p in e['players'] if not p.get('dropped')]
        if cap and len(active) >= cap:
            continue
        out.append(e)
    out.sort(key=lambda e: e.get('date', ''))
    return out[:limit]

def _normalize_handle(h: str) -> str:
    """Normalise a Discord handle for comparison: drop a leading @, lower-case,
    and drop a legacy '#1234' discriminator."""
    h = (h or '').strip().lstrip('@').lower()
    return h.split('#', 1)[0] if '#' in h else h

def _find_profile_for_discord(discord_id: str, username: str, display: str = '') -> dict | None:
    """Match a Discord user to an existing account: by a discord_id we've stored
    on the profile before (exact), else by the profile's saved Discord handle
    matching either the interaction's username or display name (people often save
    their display name as their handle). Returns the profile (with google_id) or
    None. Exact ID matches win over handle matches.

    A handle match also lets the caller store the numeric discord_id on the
    account (see register_player_via_discord), so subsequent links are exact and
    immune to the handle being a display name, edited, or a renamed username."""
    candidates = {h for h in (_normalize_handle(username), _normalize_handle(display)) if h}
    by_handle = None
    for u in list_users():
        if discord_id and u.get('discord_id') == discord_id:
            return u
        if candidates and not by_handle and _normalize_handle(u.get('discord')) in candidates:
            by_handle = u
    return by_handle

def register_player_via_discord(event_id: str, discord_id: str, discord_name: str,
                                discord_username: str = ''):
    """Register a Discord user as a player. When their Discord matches an existing
    account (by stored discord_id, or by the profile's Discord handle matching
    their username), the registration is linked to that account and uses its real
    name — otherwise a ghost player is created. Returns ({'player', 'event_name'},
    None) on success or (None, error_message)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    blocked = _self_registration_blocked(e)
    if blocked:
        return None, blocked
    if e.get('entry_code'):
        return None, 'This event needs an entry code — please register on the web.'
    cap = e.get('registration_cap', 0)
    active = [p for p in e['players'] if not p.get('dropped')]
    if cap and len(active) >= cap:
        return None, f'This event is full ({cap} players max).'
    # discord_name is the registrant's Discord display name; pass it as a second
    # handle candidate so an account that saved its display name as the handle
    # still links (and we then lock in the numeric ID below).
    profile = _find_profile_for_discord(discord_id, discord_username, discord_name)
    google_id = profile.get('google_id') if profile else None
    # Already in the event? If active, that's a no-op; if previously dropped,
    # re-activate their existing entry rather than refusing or duplicating them.
    existing = next((p for p in e['players']
                     if p.get('discord_id') == discord_id
                     or (google_id and p.get('google_id') == google_id)), None)
    if existing:
        if not existing.get('dropped'):
            return None, "You're already registered for this event."
        existing['dropped'] = False
        save_event(event_id, {'players': e['players']})
        if google_id and not (profile or {}).get('discord_id'):
            save_user_profile(google_id, {'discord_id': discord_id})
        refresh_event_announcement(e)
        return {'player': existing, 'event_name': e.get('name', 'the event')}, None
    if profile:
        name = (profile.get('name') or discord_name or 'Player').strip()[:80] or 'Player'
        discord_handle = profile.get('discord', '')
    else:
        name = (discord_name or 'Player').strip()[:80] or 'Player'
        discord_handle = ''
    player = {
        'id':         _slugify(name) + '_' + str(len(e['players'])),
        'name':       name,
        'google_id':  google_id,
        'discord_id': discord_id,
        'discord':    discord_handle,
        'dropped':    False,
        'checked_in': False,
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    # Remember the Discord ID on the matched account so future links are exact.
    if google_id and not profile.get('discord_id'):
        save_user_profile(google_id, {'discord_id': discord_id})
    refresh_event_announcement(e)   # may have just hit the cap → show "full"
    return {'player': player, 'event_name': e.get('name', 'the event')}, None


def withdraw_player_via_discord(event_id: str, discord_id: str):
    """Withdraw a Discord-registered player from an event (the toggle counterpart
    to register_player_via_discord). Mirrors the web withdraw: drop if rounds have
    started, else remove the entry. Returns (event_name, None) or (None, error)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    gid = _google_id_for_discord(discord_id)
    player = next((p for p in e['players']
                   if not p.get('dropped') and
                      (p.get('discord_id') == discord_id or (gid and p.get('google_id') == gid))),
                  None)
    if not player:
        return None, "You're not registered for this event."
    unenroll_end = e.get('unenroll_end')
    if unenroll_end and datetime.date.today().isoformat() > unenroll_end:
        return None, 'The unenrollment deadline has passed — contact the organiser.'
    if e['rounds']:
        set_player_dropped(event_id, player['id'], True)
    else:
        e['players'] = [p for p in e['players'] if p['id'] != player['id']]
        save_event(event_id, {'players': e['players']})
    refresh_event_announcement(get_event(event_id))   # a slot may have freed up
    return e.get('name', 'the event'), None


# Anti-spam limits for Discord event invites. Anyone may invite anyone, so these
# keep one person from blasting invites and keep a recipient from being pestered
# about the same event repeatedly.
INVITE_RATE_LIMIT = 10              # max invites one sender may send per window
INVITE_RATE_WINDOW = 60 * 60       # ...within this many seconds (1 hour)
INVITE_DEDUPE_WINDOW = 7 * 24 * 60 * 60   # don't re-invite a target to the same event within 7 days

def invite_player_via_discord(event_id: str, inviter_id: str, target_id: str,
                              inviter_name: str, base_url: str = ''):
    """DM `target_id` an invitation to register for an event, on behalf of
    `inviter_id`. Enforces the anti-spam guards (opt-out, already-registered,
    dedupe, sender rate limit) before sending. Returns (confirmation, None) on
    success or (None, error_message)."""
    if str(target_id) == str(inviter_id):
        return None, f"You can register yourself with `/{COMMAND_NAME} register` — no invite needed."
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    blocked = _self_registration_blocked(e)
    if blocked:
        return None, blocked
    if e.get('entry_code'):
        return None, 'This event needs an entry code, so it cannot be invited to from Discord.'
    cap = e.get('registration_cap', 0)
    active = [p for p in e['players'] if not p.get('dropped')]
    if cap and len(active) >= cap:
        return None, f'This event is full ({cap} players max).'

    # Recipient opted out of invites entirely.
    if is_invite_opted_out(target_id):
        return None, 'That person has chosen not to receive event invites.'

    # Already registered (by Discord ID or a linked account)?
    gid = _google_id_for_discord(target_id)
    if any(p.get('discord_id') == target_id for p in e['players']) or \
       (gid and any(p.get('google_id') == gid for p in e['players'])):
        return None, "They're already registered for this event."

    now = time.time()
    # Don't pester the same person about the same event repeatedly.
    if target_invited_since(target_id, event_id, now - INVITE_DEDUPE_WINDOW):
        return None, "They've already been invited to this event recently."
    # Rate-limit the sender.
    if recent_invite_count(inviter_id, now - INVITE_RATE_WINDOW) >= INVITE_RATE_LIMIT:
        return None, ("You've sent a lot of invites recently — please wait a bit "
                      "before sending more.")

    base = (base_url or '').rstrip('/')
    state, note = _registration_card_status(e)
    delivered = discord_api.dm_event_invite(
        target_id, e, f'{base}/events/{event_id}', inviter_name, state, note)
    if not delivered:
        return None, ("I couldn't DM them — they may not share a server with the bot "
                      "or have DMs from server members turned off.")
    record_invite(inviter_id, target_id, event_id, now)
    return f"Invitation sent for **{e.get('name', 'the event')}**.", None


def _google_id_for_discord(discord_id: str):
    """The Google account (if any) linked to a Discord numeric ID — so we can also
    match players who registered on the web/were added by an organiser but have a
    linked Discord. Returns the google_id or None."""
    prof = _find_profile_for_discord(discord_id, '')   # '' = match by discord_id only
    return prof.get('google_id') if prof else None

def _discord_match_ctx(e: dict, round_idx: int, match_idx: int, discord_id: str, gid=None):
    """Context for a Discord user's match in event `e`, or None if it's not their
    open (unreported, non-bye) match. Shared by the report picker and reporting.
    `gid` is the Google account linked to this Discord user (if known), so a
    web/organiser-added player who's linked their Discord is also matched."""
    rounds = e.get('rounds', [])
    if not (0 <= round_idx < len(rounds)):
        return None
    rnd = rounds[round_idx]
    if not (0 <= match_idx < len(rnd)):
        return None
    m = rnd[match_idx]
    player = next((p for p in e['players']
                   if not p.get('dropped') and
                      (p.get('discord_id') == discord_id or (gid and p.get('google_id') == gid))),
                  None)
    if not player or m.get('is_bye'):
        return None
    if player['id'] not in (m.get('player1_id'), m.get('player2_id')):
        return None
    if m.get('winner_id') or m.get('result') in DRAW_RESULTS:
        return None
    is_p1 = player['id'] == m.get('player1_id')
    opp_id = m['player2_id'] if is_p1 else m['player1_id']
    opp = next((p for p in e['players'] if p['id'] == opp_id), None)
    return {
        'event_id': e['id'], 'event_name': e.get('name', 'Event'),
        'round_idx': round_idx, 'match_idx': match_idx, 'round_num': round_idx + 1,
        'player_id': player['id'], 'opp_id': opp_id,
        'opponent': opp['name'] if opp else 'Opponent', 'is_p1': is_p1,
        'allow_id': bool(e.get('advanced')) and not e.get('intentional_draws_frowned'),
    }

def discord_open_matches(discord_id: str, limit: int = 25):
    """A Discord user's current open matches (latest round of each event they're
    in), for the /cbp report picker."""
    out = []
    gid = _google_id_for_discord(discord_id)
    for e in list_events():
        ridx = len(e.get('rounds', [])) - 1
        if ridx < 0:
            continue
        for midx in range(len(e['rounds'][ridx])):
            ctx = _discord_match_ctx(e, ridx, midx, discord_id, gid)
            if ctx:
                out.append(ctx)
                break
    return out[:limit]

def discord_match_context(event_id: str, round_idx: int, match_idx: int, discord_id: str):
    e = get_event(event_id)
    return _discord_match_ctx(e, round_idx, match_idx, discord_id,
                              _google_id_for_discord(discord_id)) if e else None

# Map a reporter-perspective result code to a stored result + summary.
_DISCORD_RESULT_CODES = {
    'w20': ('win',  2, 0), 'w21': ('win',  2, 1),
    'l02': ('lose', 0, 2), 'l12': ('lose', 1, 2),
    'draw': ('draw', None, None), 'id': ('id', None, None),
}

def report_result_via_discord(event_id, round_idx, match_idx, discord_id, code, base_url=''):
    """Record a result a Discord player reports for their own match. `code` is
    from the reporter's perspective (see _DISCORD_RESULT_CODES). Returns
    (confirmation, None) or (None, error)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    ctx = _discord_match_ctx(e, round_idx, match_idx, discord_id,
                             _google_id_for_discord(discord_id))
    if not ctx:
        return None, "That doesn't look like an open match of yours anymore."
    spec = _DISCORD_RESULT_CODES.get(code)
    if not spec:
        return None, 'Unknown result.'
    kind, mine, theirs = spec
    if kind == 'draw':
        winner_id, result, summary = None, 'draw', 'a draw (1–1)'
    elif kind == 'id':
        if not ctx['allow_id']:
            return None, 'Intentional draws are not allowed for this event.'
        winner_id, result, summary = None, '0-0-3', 'an intentional draw (0–0–3)'
    else:
        winner_id = ctx['player_id'] if kind == 'win' else ctx['opp_id']
        # Stored result is from player1's perspective.
        a, b = (mine, theirs) if ctx['is_p1'] else (theirs, mine)
        result = f'{a}-{b}'
        summary = f"you {'won' if kind == 'win' else 'lost'} {max(mine, theirs)}–{min(mine, theirs)}"
    err = _validate_result(e['rounds'][round_idx][match_idx], winner_id, result)
    if err:
        return None, err
    m = e['rounds'][round_idx][match_idx]
    m['winner_id'] = winner_id
    m['result'] = result
    save_event(event_id, {'rounds': e['rounds']})
    # Mark both players' pairing DMs as reported (mirrors reporting from the DM),
    # so a result entered in the channel/slash still updates their DM message.
    discord_api.mark_dm_pairings_reported(e, [ctx['player_id'], ctx['opp_id']])
    # Let the opponent know it's recorded so they don't report it again.
    discord_api.dm_result_recorded(e, round_idx, match_idx,
                                   exclude_player_id=ctx['player_id'], base_url=base_url)
    return f"Recorded — {summary} vs {ctx['opponent']} ({ctx['event_name']}). GGs!", None


def discord_linkable_events(limit: int = 25):
    """Non-test events an organiser might link to a Discord channel (for the
    /cbp link picker). Most recent first."""
    out = [e for e in list_events() if not e.get('test_mode')]
    out.sort(key=lambda e: e.get('date', ''), reverse=True)
    return out[:limit]

def set_event_discord_channel(event_id: str, channel_id: str):
    """Link an event so its pairings auto-post to this Discord channel. Returns
    the event name, or None if it's gone."""
    e = get_event(event_id)
    if not e:
        return None
    save_event(event_id, {'discord_channel_id': channel_id})
    return e.get('name', 'the event')

def discord_standings_events(limit: int = 25):
    """Non-test events that have started (have standings to show)."""
    out = [e for e in list_events() if e.get('rounds') and not e.get('test_mode')]
    out.sort(key=lambda e: e.get('date', ''), reverse=True)
    return out[:limit]

def discord_standings_text(event_id: str, top: int = 16):
    """Formatted standings for a Discord message, or None if the event is gone."""
    e = get_event(event_id)
    if not e:
        return None
    standings = compute_standings(e['players'], e['rounds'])
    name = e.get('name', 'Event')
    if not standings:
        return f"**{name}** — no standings yet."
    lines = [f"**{name}** — standings"]
    for i, s in enumerate(standings[:top], 1):
        tag = ' _(dropped)_' if s.get('dropped') else ''
        lines.append(f"{i}. {s['name']} — {s['points']} pts{tag}")
    if len(standings) > top:
        lines.append(f"…and {len(standings) - top} more")
    return '\n'.join(lines)


def _normalize_payment_url(raw) -> tuple:
    """Validate/normalize a payment link so it's safe to render as a clickable
    <a href>. Returns (url, error): an empty string for no link, an http(s)
    URL (a missing scheme is assumed https), or (None, msg) if it's not a
    valid web URL — which blocks javascript:/data: and other unsafe schemes.
    """
    url = (raw or '').strip()
    if not url:
        return '', None
    if not urlparse(url).scheme:
        url = 'https://' + url
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None, 'Payment link must be a valid http(s) URL'
    return url, None


# ── Permission helpers ─────────────────────────────────────────────────────────

def _can_manage(event: dict) -> bool:
    """True if the current user is a global admin or the event owner."""
    user = get_current_user()
    if not user:
        return False
    return user['id'] == event.get('owner_id') or is_admin(user['id'])

def _require_manage(event: dict):
    if not _can_manage(event):
        abort(403)

def _find_player_by_google_id(event: dict, google_id: str) -> dict | None:
    return next((p for p in event['players']
                 if p.get('google_id') == google_id), None)

def _find_player_by_guest_token(event: dict, token: str) -> dict | None:
    if not token:
        return None
    return next((p for p in event['players']
                 if p.get('guest_token') and p['guest_token'] == token), None)

def _redact_players(event: dict) -> None:
    """Strip guest self-report tokens before sending an event to clients. The
    token is a bearer secret — anyone holding it can report as that player and
    claim their identity via the magic link — so it must never appear in any
    public event payload. It's only ever returned once, to the joiner.

    Also strip discord_id — it's only used server-side to match a player to the
    Discord user reporting, and there's no need to expose the player↔Discord
    mapping in public payloads."""
    for p in event.get('players', []):
        p.pop('guest_token', None)
        p.pop('discord_id', None)

def _enrich_players_discord(event: dict) -> None:
    """Refresh each linked player's `discord` from their account profile, which is
    the source of truth going forward — so a handle set or changed *after*
    registration shows on the event page, not just the registration-time snapshot.
    Profiles are read once per google_id; guests/ghosts (no google_id) and
    accounts with no saved handle keep their snapshot."""
    cache = {}
    for p in event.get('players', []):
        gid = p.get('google_id')
        if not gid:
            continue
        if gid not in cache:
            cache[gid] = get_user_profile(gid).get('discord', '')
        if cache[gid]:
            p['discord'] = cache[gid]

def _can_report_match(event: dict, match: dict, data: dict) -> bool:
    """Who may report a match result: a manager, the registered Google player
    in the match, or a guest holding that player's self-report token."""
    if _can_manage(event):
        return True
    sides = (match.get('player1_id'), match.get('player2_id'))
    user = get_current_user()
    if user:
        gp = _find_player_by_google_id(event, user['id'])
        if gp and gp['id'] in sides:
            return True
    token = request.headers.get('X-Guest-Token') or (data or {}).get('guest_token')
    gp = _find_player_by_guest_token(event, token)
    return bool(gp and gp['id'] in sides)

def _slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

_RESULT_RE = re.compile(r'^(\d+)-(\d+)$')

def _validate_result(match: dict, winner_id, result) -> str | None:
    """
    Validate a reported result against a match. Returns an error string, or None
    if valid. Enforces that the winner matches the score (result is recorded from
    player1's perspective, i.e. 'p1games-p2games'), so a player can't report a
    score for one player while crediting the win to the other.
    """
    p1, p2 = match.get('player1_id'), match.get('player2_id')
    if result in DRAW_RESULTS:
        return None if winner_id is None else 'A draw cannot have a winner'
    m = _RESULT_RE.match(str(result or ''))
    if not m:
        return 'Invalid result format'
    a, b = int(m.group(1)), int(m.group(2))
    if a == b:
        return 'Use a draw for an equal score'
    expected = p1 if a > b else p2
    if winner_id != expected:
        return 'Winner does not match the score'
    return None

def _now_iso() -> str:
    """Current UTC time as an ISO string (used to stamp round-timer starts)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _new_round_updates(event: dict) -> dict:
    """Fields to set whenever a new round is created: clear the round timer (the
    organiser starts it with the Start-timer button — it no longer auto-starts on
    pairing) and, where delivery is delayed, re-hide pairings/standings until the
    organiser releases them."""
    u = {'round_started_at': ''}
    if event.get('delay_pairings'):
        u['pairings_released'] = False
    if event.get('delay_standings'):
        u['standings_released'] = False
    return u

def _is_bracket_round(rnd: list) -> bool:
    """A round belongs to the single-elimination playoff if its matches are tagged."""
    return bool(rnd) and rnd[0].get('stage') == 'bracket'

def _swiss_complete(event: dict) -> bool:
    """True once every Swiss round has been paired and fully scored (a playoff
    can only start after this)."""
    swiss = [r for r in event['rounds'] if not _is_bracket_round(r)]
    num_rounds = event.get('num_rounds') or default_num_rounds(len(event['players']))
    if len(swiss) < num_rounds:
        return False
    last = swiss[-1] if swiss else []
    return all(m.get('is_bye') or m.get('winner_id') or m.get('result') in DRAW_RESULTS
               for m in last)


# ── Pages ──────────────────────────────────────────────────────────────────────

@events_bp.route('/')
def index():
    return render_template('index.html', user=get_current_user())

@events_bp.route('/about')
def about():
    return render_template('about.html', user=get_current_user())

@events_bp.route('/terms')
def terms():
    return render_template('terms.html', user=get_current_user())

@events_bp.route('/privacy')
def privacy():
    return render_template('privacy.html', user=get_current_user())

@events_bp.route('/discord-bot')
def discord_bot():
    # Invite link for this environment's bot (prod vs staging app), built from the
    # app ID in Secret Manager. Omitted if the bot isn't configured for this env.
    app_id = get_secret('DISCORD_APP_ID')
    invite_url = (f'https://discord.com/oauth2/authorize?client_id={app_id}'
                  '&scope=bot+applications.commands&permissions=18432') if app_id else ''
    # User-install link ("Add to your account"): integration_type=1, commands-only
    # scope (no bot/permissions — it isn't joining a server). Lets someone run the
    # /cparty commands anywhere, even in servers PartyBot isn't in.
    account_url = (f'https://discord.com/oauth2/authorize?client_id={app_id}'
                   '&integration_type=1&scope=applications.commands') if app_id else ''
    return render_template('discord_bot.html', user=get_current_user(),
                           cmd=COMMAND_NAME, invite_url=invite_url, account_url=account_url)

@events_bp.route('/events/<event_id>')
def event_detail(event_id):
    event = get_event(event_id)
    if not event:
        return 'Event not found', 404
    return render_template('event.html', user=get_current_user(), event=event)

@events_bp.route('/admin')
@login_required
def admin_page():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    return render_template('admin.html', user=user, admins=get_admins())

@events_bp.route('/admin/users')
@login_required
def users_page():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)

    # Start from the user directory (email captured at login, profile name/discord).
    directory = {u['google_id']: {
        'google_id': u['google_id'],
        'name':      u.get('name', ''),
        'email':     u.get('email', ''),
        'discord':   u.get('discord', ''),
        'events':    0,
    } for u in list_users()}

    # Enrich/backfill from event registrations: count participation and fill in
    # name/discord for anyone who registered before the directory captured them.
    # Sort ascending by date so later events win for the "latest known" values.
    counts: dict = {}
    for e in sorted(list_events(), key=lambda x: x.get('date', '')):
        for p in e.get('players', []):
            gid = p.get('google_id')
            if not gid:
                continue  # organiser-added ghost player, no account
            counts[gid] = counts.get(gid, 0) + 1
            entry = directory.setdefault(gid, {
                'google_id': gid, 'name': '', 'email': '', 'discord': '', 'events': 0,
            })
            if not entry['name'] and p.get('name'):
                entry['name'] = p['name']
            if not entry['discord'] and p.get('discord'):
                entry['discord'] = p['discord']

    users = list(directory.values())
    for u in users:
        u['events'] = counts.get(u['google_id'], 0)
    users.sort(key=lambda u: (u['name'] or u['email']).lower())
    return render_template('users.html', user=user, users=users)


# ── API: events ────────────────────────────────────────────────────────────────

@events_bp.route('/api/events', methods=['GET'])
def api_list_events():
    # Test-mode events stay out of public discovery — only their owner (and global
    # admins) see them listed, so organisers can rehearse setup without publishing
    # fake events to players.
    user  = get_current_user()
    uid   = user['id'] if user else None
    admin = bool(uid) and is_admin(uid)
    events = []
    for e in list_events():
        if e.get('test_mode') and not admin and e.get('owner_id') != uid:
            continue
        e.pop('entry_code', None)   # secret — not needed for the listing
        # Don't leak delayed pairings here either — hide the latest round from
        # non-managers until it's released.
        if (not _can_manage(e) and e.get('delay_pairings')
                and not e.get('pairings_released', True) and e['rounds']):
            e['rounds'] = e['rounds'][:-1]
            e['pairings_hidden'] = True
        _redact_players(e)
        events.append(e)
    return jsonify(events)

@events_bp.route('/api/events', methods=['POST'])
@login_required
def api_create_event():
    user = get_current_user()
    data = request.json or {}
    if not (data.get('name') or '').strip():
        return jsonify({'error': 'Event name is required'}), 400
    payment_url, err = _normalize_payment_url(data.get('payment_url'))
    if err:
        return jsonify({'error': err}), 400
    event = {
        'name':         data.get('name', 'New Event'),
        'game':         (data.get('game') or '').strip(),     # '' for Simple (no game yet)
        'test_mode':    bool(data.get('test_mode', False)),    # hidden from public discovery
        'tags':         _clean_tags(data.get('tags')),
        'structure':    data.get('structure') if data.get('structure') in STRUCTURES else '',
        # Intended top-cut size (4/8/16) for a Swiss + Top Cut event. This pre-fills
        # the in-event "Cut to Top N" action; the actual cut still executes later.
        'planned_cut_size': data.get('planned_cut_size') if data.get('planned_cut_size') in (4, 8, 16) else 0,
        'requires_decklists': bool(data.get('requires_decklists', False)),
        'round_timer_minutes': data.get('round_timer_minutes') if isinstance(data.get('round_timer_minutes'), int) and data.get('round_timer_minutes') >= 0 else 0,
        'round_started_at': '',   # ISO time the current round's timer started
        # Delayed delivery: hide newly-paired pairings / fresh standings from
        # players until the organiser releases them (prevents stream spoilers).
        'delay_pairings':  bool(data.get('delay_pairings', False)),
        'delay_standings': bool(data.get('delay_standings', False)),
        'pairings_released':  True,
        'standings_released': True,
        # When True, only checked-in players are paired (attendance gate).
        'require_check_in': bool(data.get('require_check_in', False)),
        'brand_text':       str(data.get('brand_text') or '')[:300],
        'brand_image_url':  '',       # set via the brand-image upload endpoint
        'brand_image_object': '',     # GCS object name (for replacing/deleting)
        'entry_code':   str(data.get('entry_code') or '').strip()[:64],  # '' = none required
        'advanced':     bool(data.get('advanced', False)),   # created via the Advanced flow
        # When True, the "0-0-3 Intentional draw" result option is hidden/rejected
        # (Advanced events only). Default False = intentional draws allowed.
        'intentional_draws_frowned': bool(data.get('intentional_draws_frowned', False)),
        'prize_deadline_days': data.get('prize_deadline_days') if isinstance(data.get('prize_deadline_days'), int) and data.get('prize_deadline_days') >= 0 else 0,
        'rules':        str(data.get('rules') or '')[:_COMMS_MAX],
        'schedule':     str(data.get('schedule') or '')[:_COMMS_MAX],
        'prizes':       str(data.get('prizes') or '')[:_COMMS_MAX],
        'contact':      str(data.get('contact') or '')[:_COMMS_MAX],
        'event_type':   data.get('event_type', 'One-day'),
        'format':       data.get('format', 'Limited: Draft'),
        'description':  data.get('description', ''),
        'entry_cost':   data.get('entry_cost', ''),
        'payment_url':  payment_url,
        'date':         data.get('date', str(datetime.date.today())),
        'start_time':   (data.get('start_time') or '').strip()[:5],
        'owner_id':     user['id'],
        'owner_name':   user['name'],
        'players':      [],
        'rounds':       [],
        'num_rounds':   data.get('num_rounds', 0),
        'cut_size':     0,            # 0 = no playoff cut; else 4/8/16
        'cut_seeds':    {},           # player_id -> seed, set when the cut starts
        'status':       'setup',
        'registration': 'open',
        'registration_type':  data.get('registration_type') if data.get('registration_type') in REGISTRATION_TYPES else 'open',
        'registration_start': (data.get('registration_start') or '').strip(),
        'registration_end':   (data.get('registration_end') or '').strip(),
        'unenroll_end':       (data.get('unenroll_end') or '').strip(),
        'registration_cap': data.get('registration_cap', 0),  # 0 = no cap
    }
    eid = create_event(event)
    event['id'] = eid
    return jsonify(event), 201

@events_bp.route('/api/events/<event_id>', methods=['GET'])
def api_get_event(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    e['standings'] = compute_standings(e['players'], e['rounds'])
    # Players who can intentionally draw the final Swiss round and still lock a
    # top-cut spot (empty unless this is the final round of a Swiss + Top Cut event).
    _num_rounds = e.get('num_rounds') or default_num_rounds(len(e['players']))
    e['id_safe_ids'] = list(id_safe_players(e['players'], e['rounds'],
                                            _num_rounds, e.get('planned_cut_size') or 0))
    e['can_manage'] = _can_manage(e)
    owner_profile = get_user_profile(e.get('owner_id', ''))
    # Surface the organizer's discord (if known) so players know how to reach them.
    e['owner_discord'] = owner_profile.get('discord', '')
    # Expose only whether a code is needed; the code itself is a secret the
    # organiser shares out-of-band, so never send it to non-managers.
    e['entry_code_required'] = bool(e.get('entry_code'))
    _enrich_players_discord(e)
    if not e['can_manage']:
        e.pop('entry_code', None)
        # Delayed delivery: hide the latest round's pairings / the standings from
        # players until released. Managers always see everything.
        if e.get('delay_pairings') and not e.get('pairings_released', True) and e['rounds']:
            e['pairings_hidden'] = True
            e['rounds'] = e['rounds'][:-1]
            e['id_safe_ids'] = []
        if e.get('delay_standings') and not e.get('standings_released', True):
            e['standings_hidden'] = True
            e['standings'] = []
    _redact_players(e)
    return jsonify(e)

@events_bp.route('/api/events/<event_id>', methods=['PUT'])
@login_required
def api_update_event(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    data = request.json or {}
    allowed = {'name', 'game', 'test_mode', 'tags', 'structure', 'planned_cut_size',
               'requires_decklists', 'entry_code', 'intentional_draws_frowned',
               'round_timer_minutes', 'delay_pairings', 'delay_standings', 'require_check_in',
               'brand_text',
               'prize_deadline_days', 'rules', 'schedule', 'prizes', 'contact',
               'event_type', 'format', 'description', 'entry_cost',
               'payment_url', 'date', 'start_time', 'num_rounds',
               'status', 'registration', 'registration_cap',
               'registration_type', 'registration_start', 'registration_end', 'unenroll_end'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if 'name' in updates and not str(updates['name']).strip():
        return jsonify({'error': 'Event name is required'}), 400
    if 'test_mode' in updates:
        updates['test_mode'] = bool(updates['test_mode'])
    if 'entry_code' in updates:
        updates['entry_code'] = str(updates['entry_code'] or '').strip()[:64]
    if 'intentional_draws_frowned' in updates:
        updates['intentional_draws_frowned'] = bool(updates['intentional_draws_frowned'])
    if 'registration_type' in updates and updates['registration_type'] not in REGISTRATION_TYPES:
        updates['registration_type'] = 'open'
    if 'requires_decklists' in updates:
        updates['requires_decklists'] = bool(updates['requires_decklists'])
    if 'round_timer_minutes' in updates:
        v = updates['round_timer_minutes']
        updates['round_timer_minutes'] = v if isinstance(v, int) and v >= 0 else 0
    for f in ('delay_pairings', 'delay_standings', 'require_check_in'):
        if f in updates:
            updates[f] = bool(updates[f])
    if 'brand_text' in updates:
        updates['brand_text'] = str(updates['brand_text'] or '')[:300]
    if 'start_time' in updates:
        updates['start_time'] = str(updates['start_time'] or '').strip()[:5]
    if 'prize_deadline_days' in updates:
        v = updates['prize_deadline_days']
        updates['prize_deadline_days'] = v if isinstance(v, int) and v >= 0 else 0
    updates.update(_clean_comms(updates))   # cap the long-text fields
    if 'tags' in updates:
        updates['tags'] = _clean_tags(updates['tags'])
    if 'structure' in updates and updates['structure'] not in STRUCTURES:
        updates['structure'] = ''
    if 'planned_cut_size' in updates and updates['planned_cut_size'] not in (4, 8, 16):
        updates['planned_cut_size'] = 0
    if 'payment_url' in updates:
        updates['payment_url'], err = _normalize_payment_url(updates['payment_url'])
        if err:
            return jsonify({'error': err}), 400
    save_event(event_id, updates)
    e.update(updates)
    _CARD_FIELDS = {'name', 'event_type', 'format', 'date', 'start_time', 'entry_cost', 'description',
                    'registration', 'registration_cap', 'registration_type',
                    'registration_start', 'registration_end', 'entry_code'}
    if e.get('discord_announce') and _CARD_FIELDS & updates.keys():
        refresh_event_announcement(e)
    _redact_players(e)
    return jsonify({**e, **updates})

@events_bp.route('/api/events/<event_id>', methods=['DELETE'])
@login_required
def api_delete_event(event_id):
    """Permanently delete an event. Allowed for the event owner or a global admin."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    delete_event(event_id)
    return jsonify({'ok': True})


# ── API: player registration ───────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/register', methods=['POST'])
@login_required
def api_register(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    blocked = _self_registration_blocked(e)
    if blocked:
        return jsonify({'error': blocked}), 400
    code_err = _entry_code_error(e, request.json or {})
    if code_err:
        return jsonify({'error': code_err}), 400

    # Enforce registration cap
    cap = e.get('registration_cap', 0)
    active = [p for p in e['players'] if not p.get('dropped')]
    if cap and len(active) >= cap:
        return jsonify({'error': f'This event is full ({cap} players max)'}), 400
    user = get_current_user()
    data = request.json or {}
    display_name = data.get('display_name', '').strip() or user['name']
    # The form hides the Discord field when the user already has one on file, so
    # fall back to their saved handle when none is submitted.
    discord      = data.get('discord', '').strip() or get_user_profile(user['id']).get('discord', '')
    # Re-activate a previously dropped entry instead of refusing or duplicating.
    existing = _find_player_by_google_id(e, user['id'])
    if existing:
        if not existing.get('dropped'):
            return jsonify({'error': 'Already registered'}), 400
        set_player_dropped(event_id, existing['id'], False)
        if discord:
            save_user_profile(user['id'], {'discord': discord})
        refresh_event_announcement(get_event(event_id))
        existing['dropped'] = False
        return jsonify(existing), 200
    player = {
        'id':        _slugify(display_name) + '_' + str(len(e['players'])),
        'name':      display_name,
        'google_id': user['id'],
        'discord':   discord,
        'dropped':   False,
        'checked_in': False,
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    refresh_event_announcement(e)   # may have just hit the cap → show "full"
    # Keep the user directory current: capture the discord handle they gave.
    if discord:
        save_user_profile(user['id'], {'discord': discord})
    return jsonify(player), 201

@events_bp.route('/api/events/<event_id>/unregister', methods=['POST'])
@login_required
def api_unregister(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    user = get_current_user()
    player = _find_player_by_google_id(e, user['id'])
    if not player:
        return jsonify({'error': 'Not registered'}), 400
    unenroll_end = e.get('unenroll_end')
    if unenroll_end and datetime.date.today().isoformat() > unenroll_end:
        return jsonify({'error': 'The unenrollment deadline has passed — contact the organiser'}), 400
    if e['rounds']:
        set_player_dropped(event_id, player['id'], True)
    else:
        e['players'] = [p for p in e['players'] if p.get('google_id') != user['id']]
        save_event(event_id, {'players': e['players']})
    refresh_event_announcement(get_event(event_id))   # a slot may have freed up
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/join', methods=['POST'])
def api_join_guest(event_id):
    """Self-join without a Google account. Creates a guest player and returns a
    private self-report token (also usable as a magic link) so they can report
    their own match results. Public — anyone with the event link may join while
    registration is open."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    blocked = _self_registration_blocked(e)
    if blocked:
        return jsonify({'error': blocked}), 400
    code_err = _entry_code_error(e, request.json or {})
    if code_err:
        return jsonify({'error': code_err}), 400
    cap = e.get('registration_cap', 0)
    active = [p for p in e['players'] if not p.get('dropped')]
    if cap and len(active) >= cap:
        return jsonify({'error': f'This event is full ({cap} players max)'}), 400
    data = request.json or {}
    name = data.get('display_name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    token = uuid.uuid4().hex
    player = {
        'id':          _slugify(name) + '_' + str(len(e['players'])),
        'name':        name,
        'google_id':   None,
        'discord':     data.get('discord', '').strip(),
        'dropped':     False,
        'checked_in':  False,
        'guest_token': token,
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    refresh_event_announcement(e)   # may have just hit the cap → show "full"
    echo = {k: v for k, v in player.items() if k != 'guest_token'}
    return jsonify({'player': echo, 'token': token}), 201

@events_bp.route('/api/events/<event_id>/guest', methods=['GET'])
def api_guest_whoami(event_id):
    """Resolve a guest self-report token to its player (for magic-link re-entry
    on another device). Returns only public fields, never the token itself."""
    token = request.args.get('token', '')
    if not token:
        return jsonify({'error': 'Missing token'}), 400
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    p = _find_player_by_guest_token(e, token)
    if not p:
        return jsonify({'error': 'Unknown token'}), 404
    return jsonify({'player_id': p['id'], 'name': p['name']})


# ── API: organiser player management ──────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/players', methods=['POST'])
@login_required
def api_add_player(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    user = get_current_user()
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    # Attach a google_id when adding an existing user: 'self' for the organiser,
    # or a google_id picked from the player search. Linking lets that person see
    # the event on their profile and report their own results.
    google_id = None
    discord   = data.get('discord', '').strip()
    if data.get('self'):
        google_id = user['id']
    elif data.get('google_id'):
        google_id = data['google_id']
        # Trust the directory, not the client, for the linked user's details.
        profile = get_user_profile(google_id)
        name    = profile.get('name') or name
        discord = profile.get('discord', '')
    if google_id and any(p.get('google_id') == google_id for p in e['players']):
        return jsonify({'error': 'That player is already in this event'}), 400
    player = {
        'id':        _slugify(name) + '_' + str(len(e['players'])),
        'name':      name,
        'google_id': google_id,
        'discord':   discord,
        'dropped':   False,
        'checked_in': True,   # organiser added them, so they're present
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    refresh_event_announcement(e)   # may have just hit the cap → show "full"
    return jsonify(player), 201

@events_bp.route('/api/events/<event_id>/player-search', methods=['GET'])
@login_required
def api_player_search(event_id):
    """Manager-only typeahead over the user directory, for linking an existing
    person when adding a player. Excludes users already in the event; returns
    only name + discord + id (no email)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    q = request.args.get('q', '').strip().lower()
    if len(q) < 2:
        return jsonify([])
    in_event = {p.get('google_id') for p in e['players'] if p.get('google_id')}
    matches = []
    for u in list_users():
        gid = u.get('google_id')
        if not gid or gid in in_event:
            continue
        name, discord = u.get('name', ''), u.get('discord', '')
        if q in name.lower() or (discord and q in discord.lower()):
            matches.append({'google_id': gid, 'name': name, 'discord': discord})
            if len(matches) >= 8:
                break
    return jsonify(matches)

@events_bp.route('/api/events/<event_id>/players/<player_id>', methods=['DELETE'])
@login_required
def api_remove_player(event_id, player_id):
    """Remove a player entirely — only before pairing has started, since once a
    round exists the player is referenced by matches (drop them instead)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    if e['rounds']:
        return jsonify({'error': 'Drop the player instead once rounds have started'}), 400
    remaining = [p for p in e['players'] if p['id'] != player_id]
    if len(remaining) == len(e['players']):
        return jsonify({'error': 'Player not found'}), 404
    e['players'] = remaining
    save_event(event_id, {'players': e['players']})
    refresh_event_announcement(e)   # a slot may have freed up
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/players/<player_id>/drop', methods=['POST'])
@login_required
def api_drop_player(event_id, player_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    if not set_player_dropped(event_id, player_id, True):
        return jsonify({'error': 'Player not found'}), 404
    refresh_event_announcement(get_event(event_id))   # a slot may have freed up
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/players/<player_id>/undrop', methods=['POST'])
@login_required
def api_undrop_player(event_id, player_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    if not set_player_dropped(event_id, player_id, False):
        return jsonify({'error': 'Player not found'}), 404
    refresh_event_announcement(get_event(event_id))   # may have re-hit the cap
    return jsonify({'ok': True})

@events_bp.route('/api/events/<event_id>/players/<player_id>/rename', methods=['POST'])
def api_rename_player(event_id, player_id):
    """Change the name shown for a player on this event (e.g. a real name for a
    larger event). Allowed for a manager, or for the player editing their own
    entry (matched by Google account or guest token). Only the display name
    changes — the player's id, matches and standings are unaffected."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    name = str((request.json or {}).get('name') or '').strip()[:80]
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    player = next((p for p in e['players'] if p['id'] == player_id), None)
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    allowed = _can_manage(e)
    if not allowed:
        user = get_current_user()
        if user and player.get('google_id') == user['id']:
            allowed = True
    if not allowed:
        token = request.headers.get('X-Guest-Token') or (request.json or {}).get('guest_token')
        if token and player.get('guest_token') == token:
            allowed = True
    if not allowed:
        abort(403)
    player['name'] = name
    save_event(event_id, {'players': e['players']})
    return jsonify({'ok': True, 'name': name})

@events_bp.route('/api/events/<event_id>/players/<player_id>/checkin', methods=['POST'])
@login_required
def api_check_in(event_id, player_id):
    """Organiser marks a player checked in (or not) for attendance gating."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    value = bool((request.json or {}).get('checked_in', True))
    player = set_player_field(event_id, player_id, 'checked_in', value)
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    return jsonify(player)


# ── API: pairing ───────────────────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/pair', methods=['POST'])
@login_required
def api_pair_round(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)

    # If a playoff bracket is underway, "pair" advances it to the next round by
    # pairing the winners. Every match needs a winner first (single elimination
    # has no draws — a drawn match must be resolved to a decisive result).
    if e['rounds'] and _is_bracket_round(e['rounds'][-1]):
        last = e['rounds'][-1]
        if len(last) <= 1:
            return jsonify({'error': 'The playoff is already complete'}), 400
        if any(m.get('winner_id') is None for m in last):
            return jsonify({'error': 'Every playoff match needs a winner before advancing '
                                     '(resolve any draws first)'}), 400
        new_round = next_bracket_round(last)
        e['rounds'].append(new_round)
        save_event(event_id, {'rounds': e['rounds'], **_new_round_updates(e)})
        round_num = len(e['rounds'])
        discord_api.announce_round(e, round_num)
        discord_api.dm_round_pairings(e, round_num, request.host_url)
        return jsonify({'round_num': round_num, 'pairings': new_round})

    if e['rounds'] and e.get('event_type') != 'League':
        last = e['rounds'][-1]
        unfinished = [m for m in last
                      if not m.get('is_bye')
                      and m.get('winner_id') is None
                      and m.get('result') not in DRAW_RESULTS]
        if unfinished:
            return jsonify({'error': 'Previous round has unrecorded results'}), 400
    num_rounds = e['num_rounds'] or default_num_rounds(len(e['players']))
    if len(e['rounds']) >= num_rounds:
        return jsonify({'error': 'All rounds already paired'}), 400
    # When check-in is required, only checked-in players are paired.
    players_to_pair = e['players']
    if e.get('require_check_in'):
        players_to_pair = [p for p in e['players'] if p.get('checked_in')]
        if len([p for p in players_to_pair if not p.get('dropped')]) < 2:
            return jsonify({'error': 'Need at least 2 checked-in players to pair'}), 400
    new_round = pair_round(players_to_pair, e['rounds'])
    e['rounds'].append(new_round)
    updates = {'rounds': e['rounds'], 'status': 'active', 'registration': 'closed',
               **_new_round_updates(e)}
    save_event(event_id, updates)
    e['registration'] = 'closed'    # so the announcement card now shows closed
    refresh_event_announcement(e)

    round_num  = len(e['rounds'])
    discord_api.announce_round(e, round_num)
    discord_api.dm_round_pairings(e, round_num, request.host_url)

    return jsonify({'round_num': round_num, 'pairings': new_round})


@events_bp.route('/api/events/<event_id>/cut', methods=['POST'])
@login_required
def api_cut_to_top(event_id):
    """Start a single-elimination playoff for the top `cut_size` players."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    if e['rounds'] and _is_bracket_round(e['rounds'][-1]):
        return jsonify({'error': 'A playoff bracket has already started'}), 400
    if not _swiss_complete(e):
        return jsonify({'error': 'Finish all rounds before cutting to a playoff'}), 400

    cut_size = (request.json or {}).get('cut_size')
    if cut_size not in CUT_SIZES:
        return jsonify({'error': 'Cut size must be 4, 8, or 16'}), 400
    active = [p for p in e['players'] if not p.get('dropped')]
    if cut_size > len(active):
        return jsonify({'error': f'Only {len(active)} active players — '
                                 f'not enough for a top {cut_size}'}), 400

    standings = compute_standings(e['players'], e['rounds'])
    new_round, seeds = make_bracket(standings, cut_size)
    e['rounds'].append(new_round)
    save_event(event_id, {'rounds': e['rounds'], 'cut_size': cut_size, 'cut_seeds': seeds,
                          **_new_round_updates(e)})

    round_num = len(e['rounds'])
    discord_api.announce_round(e, round_num)
    discord_api.dm_round_pairings(e, round_num, request.host_url)
    return jsonify({'round_num': round_num, 'cut_size': cut_size, 'pairings': new_round})


@events_bp.route('/api/events/<event_id>/timer', methods=['POST'])
@login_required
def api_restart_timer(event_id):
    """Restart the round timer from now (organiser action)."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    now = _now_iso()
    save_event(event_id, {'round_started_at': now})
    e['round_started_at'] = now
    discord_api.update_round_pairings(e)   # show the live countdown on the pairings card
    return jsonify({'round_started_at': now})


@events_bp.route('/api/events/<event_id>/release', methods=['POST'])
@login_required
def api_release_delivery(event_id):
    """Reveal delayed pairings or standings to players."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    what = (request.json or {}).get('what')
    field = {'pairings': 'pairings_released', 'standings': 'standings_released'}.get(what)
    if not field:
        return jsonify({'error': 'Specify what to release: pairings or standings'}), 400
    save_event(event_id, {field: True})
    return jsonify({'ok': True, field: True})


# ── API: edit pairings ─────────────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/pairings', methods=['PUT'])
@login_required
def api_edit_pairings(event_id, round_num):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    idx = round_num - 1
    if idx < 0 or idx >= len(e['rounds']):
        return jsonify({'error': 'Round not found'}), 404
    if _is_bracket_round(e['rounds'][idx]):
        return jsonify({'error': 'Playoff pairings are set by the bracket and cannot be edited'}), 400
    has_results = any(
        m.get('winner_id') is not None or m.get('result') in DRAW_RESULTS
        for m in e['rounds'][idx] if not m.get('is_bye')
    )
    if has_results:
        return jsonify({'error': 'Cannot edit pairings after results have been entered'}), 400
    new_pairings = request.json or []
    valid_ids = {p['id'] for p in e['players']} | {BYE_PLAYER_ID}
    names = {p['id']: p['name'] for p in e['players']}
    seen: set = set()
    for match in new_pairings:
        for key in ('player1_id', 'player2_id'):
            if match.get(key) not in valid_ids:
                return jsonify({'error': f"Unknown player: {match.get(key)}"}), 400
        p1, p2 = match.get('player1_id'), match.get('player2_id')
        if match.get('is_bye'):
            # A bye is exactly one real player sitting out (vs the bye marker).
            reals = [x for x in (p1, p2) if x != BYE_PLAYER_ID]
            if len(reals) != 1:
                return jsonify({'error': 'A bye must include exactly one player'}), 400
        elif p1 == p2:
            return jsonify({'error': 'A player cannot be paired against themselves'}), 400
        for pid in (p1, p2):
            if pid == BYE_PLAYER_ID:
                continue
            if pid in seen:
                return jsonify({'error': f"{names.get(pid, pid)} is assigned to more than one match"}), 400
            seen.add(pid)

    # Every player who was in this round must still be assigned (to a match or a
    # bye) exactly once — multiple byes are fine, but no one may be left out.
    original = {pid for m in e['rounds'][idx]
                for pid in (m.get('player1_id'), m.get('player2_id'))
                if pid and pid != BYE_PLAYER_ID}
    missing = original - seen
    if missing:
        who = ', '.join(sorted(names.get(p, p) for p in missing))
        return jsonify({'error': f"{who} {'is' if len(missing) == 1 else 'are'} not in any match"}), 400
    if seen - original:
        return jsonify({'error': 'Pairings can only include players already in this round'}), 400

    e['rounds'][idx] = new_pairings
    save_event(event_id, {'rounds': e['rounds']})
    return jsonify({'round_num': round_num, 'pairings': new_pairings})


@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/repair', methods=['POST'])
@login_required
def api_repair_round(event_id, round_num):
    """Regenerate the latest round's pairings from the current active players.

    Used to correct a round that was paired against a stale player list (e.g.
    a drop hadn't taken effect yet). Only the latest round can be re-paired —
    re-pairing an earlier round would invalidate the history later rounds were
    built on. Any results already recorded in the round are discarded (the
    client confirms first).
    """
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    idx = round_num - 1
    if idx != len(e['rounds']) - 1:
        return jsonify({'error': 'Only the latest round can be re-paired'}), 400
    if _is_bracket_round(e['rounds'][idx]):
        return jsonify({'error': 'Playoff rounds cannot be re-paired'}), 400
    new_round = pair_round(e['players'], e['rounds'][:idx])
    e['rounds'][idx] = new_round
    save_event(event_id, {'rounds': e['rounds'], **_new_round_updates(e)})

    discord_api.announce_round(e, round_num)
    discord_api.dm_round_pairings(e, round_num, request.host_url)

    return jsonify({'round_num': round_num, 'pairings': new_round})


# ── API: results ───────────────────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/results', methods=['POST'])
def api_record_result(event_id, round_num):
    # No @login_required: guests report their own matches via a self-report token.
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    idx = round_num - 1
    if idx < 0 or idx >= len(e['rounds']):
        return jsonify({'error': 'Round not found'}), 404
    data        = request.json or {}
    match_index = data.get('match_index')
    winner_id   = data.get('winner_id')
    result      = data.get('result')
    rnd = e['rounds'][idx]
    if match_index is None or match_index < 0 or match_index >= len(rnd):
        return jsonify({'error': 'Invalid match_index'}), 400
    match = rnd[match_index]
    if not _can_report_match(e, match, data):
        return jsonify({'error': 'You can only report results for your own matches'}), 403
    if match.get('is_bye'):
        return jsonify({'error': 'Cannot record a result for a bye'}), 400
    if result == '0-0-3' and e.get('intentional_draws_frowned'):
        return jsonify({'error': 'Intentional draws are not allowed for this event'}), 400
    err = _validate_result(match, winner_id, result)
    if err:
        return jsonify({'error': err}), 400
    match['winner_id'] = winner_id
    match['result']    = result
    save_event(event_id, {'rounds': e['rounds']})
    # Notify the other player(s) via Discord so they don't report it again. The
    # reporter (a player) is excluded; an organiser reporting excludes no one.
    reporter_pid = None
    user = get_current_user()
    if user:
        gp = _find_player_by_google_id(e, user['id'])
        if gp:
            reporter_pid = gp['id']
    if reporter_pid is None:
        token = request.headers.get('X-Guest-Token') or data.get('guest_token')
        gp = _find_player_by_guest_token(e, token)
        if gp:
            reporter_pid = gp['id']
    discord_api.dm_result_recorded(e, idx, match_index,
                                   exclude_player_id=reporter_pid, base_url=request.host_url)
    return jsonify(match)


# ── API: admin management ──────────────────────────────────────────────────────

@events_bp.route('/api/admins', methods=['GET'])
@login_required
def api_list_admins():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    return jsonify(get_admins())

@events_bp.route('/api/admins', methods=['POST'])
@login_required
def api_add_admin():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    data  = request.json or {}
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({'error': 'Email required'}), 400

    # Look up their Google ID by fetching their profile via Google's API.
    # This only works if they've signed into Cardboard Party at least once,
    # since we store user info in the session but not in a users collection yet.
    # For now, store by email and resolve the ID on next sign-in.
    add_admin(google_id=f'pending:{email}', email=email, name=email)
    return jsonify({'ok': True, 'email': email}), 201

@events_bp.route('/api/admins/<path:admin_id>', methods=['DELETE'])
@login_required
def api_remove_admin(admin_id):
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    if admin_id == user['id']:
        return jsonify({'error': "You can't remove yourself"}), 400
    remove_admin(admin_id)
    return jsonify({'ok': True})


# ── Player profiles ────────────────────────────────────────────────────────────

@events_bp.route('/players/<google_id>')
def player_profile(google_id):
    all_events = list_events()
    profile = None
    event_history = []

    for e in sorted(all_events, key=lambda x: x.get('date', ''), reverse=True):
        player = next((p for p in e.get('players', [])
                       if p.get('google_id') == google_id), None)
        if not player:
            continue
        if not profile:
            profile = {'name': player['name'], 'discord': player.get('discord', '')}

        standings = compute_standings(e['players'], e['rounds'])
        rank = next((i + 1 for i, s in enumerate(standings)
                     if s['id'] == player['id']), None)

        wins = losses = draws = 0
        for rnd in e.get('rounds', []):
            for m in rnd:
                if m.get('is_bye'):
                    if m.get('player1_id') == player['id']:
                        wins += 1
                    continue
                if player['id'] not in (m.get('player1_id'), m.get('player2_id')):
                    continue
                if not m.get('result'):
                    continue
                if m.get('result') in DRAW_RESULTS:
                    draws += 1
                elif m.get('winner_id') == player['id']:
                    wins += 1
                else:
                    losses += 1

        standing = next((s for s in standings if s['id'] == player['id']), {})
        event_history.append({
            'id': e['id'], 'name': e['name'], 'date': e.get('date', ''),
            'rank': rank, 'total': len(standings),
            'wins': wins, 'losses': losses, 'draws': draws,
            'omw': standing.get('omw'),
            'gw':  standing.get('gw'),
            'ogw': standing.get('ogw'),
        })

    saved = get_user_profile(google_id)
    if not profile:
        # No event history yet. Build a profile from the saved users/<id> doc if
        # one exists (the person has signed in or been registered), so anyone can
        # view it. Fall back to the current user's session name for their own
        # brand-new profile.
        cur = get_current_user()
        if saved:
            profile = {'name': saved.get('name', ''),
                       'discord': saved.get('discord', '')}
        elif cur and cur['id'] == google_id:
            profile = {'name': cur.get('name', ''), 'discord': ''}
        else:
            return 'Player not found', 404

    # Merge with saved user profile (display name / discord edits)
    if saved.get('name'):
        profile['name'] = saved['name']
    if saved.get('discord'):
        profile['discord'] = saved['discord']
    # Effective avatar: custom upload if set, else the Google picture.
    profile['picture'] = saved.get('avatar_url') or saved.get('google_picture') or ''
    profile['about'] = saved.get('about', '')
    profile['pronouns'] = saved.get('pronouns', '')
    profile['pronunciation'] = saved.get('pronunciation', '')

    return render_template('player.html',
        user=get_current_user(),
        profile=profile,
        google_id=google_id,
        event_history=event_history,
        total_wins=sum(h['wins'] for h in event_history),
        total_losses=sum(h['losses'] for h in event_history),
        total_draws=sum(h['draws'] for h in event_history),
    )


# ── Result editing (organiser only) ───────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/results/<int:match_index>', methods=['PUT'])
@login_required
def api_edit_result(event_id, round_num, match_index):
    """Organiser overwrites an already-recorded result."""
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)

    idx = round_num - 1
    if idx < 0 or idx >= len(e['rounds']):
        return jsonify({'error': 'Round not found'}), 404
    rnd = e['rounds'][idx]
    if match_index < 0 or match_index >= len(rnd):
        return jsonify({'error': 'Invalid match_index'}), 400

    data      = request.json or {}
    winner_id = data.get('winner_id')
    result    = data.get('result')

    if rnd[match_index].get('is_bye'):
        return jsonify({'error': 'Cannot edit a bye result'}), 400
    if result == '0-0-3' and e.get('intentional_draws_frowned'):
        return jsonify({'error': 'Intentional draws are not allowed for this event'}), 400
    err = _validate_result(rnd[match_index], winner_id, result)
    if err:
        return jsonify({'error': err}), 400

    rnd[match_index]['winner_id'] = winner_id
    rnd[match_index]['result']    = result
    save_event(event_id, {'rounds': e['rounds']})
    return jsonify(rnd[match_index])


# ── User profile editing ───────────────────────────────────────────────────────

@events_bp.route('/api/profile', methods=['GET'])
@login_required
def api_get_profile():
    user = get_current_user()
    profile = get_user_profile(user['id'])
    return jsonify({
        'name':    profile.get('name', user['name']),
        'discord': profile.get('discord', ''),
        'pronouns':      profile.get('pronouns', ''),
        'pronunciation': profile.get('pronunciation', ''),
        'about':   profile.get('about', ''),
    })

@events_bp.route('/api/profile', methods=['PUT'])
@login_required
def api_update_profile():
    user = get_current_user()
    data = request.json or {}
    updates = {}
    # Display name is not editable — it comes strictly from Google (refreshed on
    # login), so any 'name' in the request is ignored.
    if 'discord' in data:
        updates['discord'] = data['discord'].strip()
    if 'pronouns' in data:
        updates['pronouns'] = data['pronouns'].strip()[:40]
    if 'pronunciation' in data:
        updates['pronunciation'] = data['pronunciation'].strip()[:80]
    if 'about' in data:
        updates['about'] = data['about'].strip()[:1000]
    if not updates:
        return jsonify({'error': 'Nothing to update'}), 400
    save_user_profile(user['id'], updates)
    return jsonify({'ok': True, **updates})

@events_bp.route('/api/users/<google_id>/discord', methods=['PUT'])
@login_required
def api_admin_set_discord(google_id):
    """Global admins can set any user's Discord handle (e.g. to help a player
    fix or add a missing one). Limited to discord — names come from Google and
    everything else is the user's own to edit."""
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    discord = (request.json or {}).get('discord', '').strip()
    save_user_profile(google_id, {'discord': discord})
    return jsonify({'ok': True, 'discord': discord})


# ── Profile avatar (custom upload, overrides the Google picture) ──────────────

_MAX_AVATAR_BYTES = 6 * 1024 * 1024  # 6 MB before resizing

@events_bp.route('/api/profile/avatar', methods=['POST'])
@login_required
def api_upload_avatar():
    user = get_current_user()
    file = request.files.get('avatar')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400
    raw = file.read(_MAX_AVATAR_BYTES + 1)
    if not raw:
        return jsonify({'error': 'The file is empty'}), 400
    if len(raw) > _MAX_AVATAR_BYTES:
        return jsonify({'error': 'Image too large (max 6 MB)'}), 400
    try:
        url, obj = upload_avatar(user['id'], raw)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    profile = get_user_profile(user['id'])
    old_obj = profile.get('avatar_object')
    save_user_profile(user['id'], {'avatar_url': url, 'avatar_object': obj})
    if old_obj and old_obj != obj:
        delete_object(old_obj)
    session['user']['picture'] = url  # reflect in the nav this session
    return jsonify({'avatar_url': url})

@events_bp.route('/api/events/<event_id>/brand-image', methods=['POST'])
@login_required
def api_upload_brand_image(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    file = request.files.get('image')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400
    raw = file.read(_MAX_AVATAR_BYTES + 1)
    if not raw:
        return jsonify({'error': 'The file is empty'}), 400
    if len(raw) > _MAX_AVATAR_BYTES:
        return jsonify({'error': 'Image too large (max 6 MB)'}), 400
    try:
        url, obj = upload_brand_image(event_id, raw)
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400
    old_obj = e.get('brand_image_object')
    save_event(event_id, {'brand_image_url': url, 'brand_image_object': obj})
    if old_obj and old_obj != obj:
        delete_object(old_obj)
    return jsonify({'brand_image_url': url})

@events_bp.route('/api/events/<event_id>/brand-image', methods=['DELETE'])
@login_required
def api_delete_brand_image(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    old_obj = e.get('brand_image_object')
    save_event(event_id, {'brand_image_url': '', 'brand_image_object': ''})
    if old_obj:
        delete_object(old_obj)
    return jsonify({'ok': True})

@events_bp.route('/api/profile/avatar', methods=['DELETE'])
@login_required
def api_delete_avatar():
    user = get_current_user()
    profile = get_user_profile(user['id'])
    old_obj = profile.get('avatar_object')
    save_user_profile(user['id'], {'avatar_url': '', 'avatar_object': ''})
    if old_obj:
        delete_object(old_obj)
    google_pic = profile.get('google_picture', '')
    session['user']['picture'] = google_pic  # revert nav to Google picture
    return jsonify({'avatar_url': google_pic})
