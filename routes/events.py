"""
Event routes.

Permission model:
  can_manage(event) = is_admin(user) OR user is event owner
  Players can register/unregister themselves and report their own results.
  Anyone can view events and standings.
"""

from flask import Blueprint, request, jsonify, render_template, abort, session
from db import (create_event, get_event, save_event, list_events, delete_event,
                set_player_dropped,
                get_admins, is_admin, add_admin, remove_admin,
                get_user_profile, save_user_profile, list_users,
                get_config, save_config)
from swiss import (pair_round, compute_standings, default_num_rounds, BYE_PLAYER_ID,
                   make_bracket, next_bracket_round, CUT_SIZES)
from routes.auth import get_current_user, login_required
from discord_notify import post_round, post_test, is_valid_webhook
from storage import upload_avatar, delete_object
import datetime
import re
import uuid
from urllib.parse import urlparse

events_bp = Blueprint('events', __name__)

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
    public event payload. It's only ever returned once, to the joiner."""
    for p in event.get('players', []):
        p.pop('guest_token', None)

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
    if result == 'draw':
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

def _resolve_event_webhook(event: dict) -> str:
    """Resolve an event's notification setting to an actual webhook URL ('' = none)."""
    mode = event.get('notify_mode', 'none')
    if mode == 'community':
        return get_config().get('discord_webhook', '')
    if mode == 'saved':
        owner = get_user_profile(event.get('owner_id', ''))
        wid = event.get('notify_webhook_id')
        wh = next((w for w in owner.get('webhooks', []) if w.get('id') == wid), None)
        return wh['url'] if wh else ''
    return ''

def _mask_webhook(url: str) -> str:
    """Identify a webhook without revealing its token."""
    return (url[:38] + '…' + url[-4:]) if len(url) > 46 else url

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
    return all(m.get('is_bye') or m.get('winner_id') or m.get('result') == 'draw'
               for m in last)


# ── Pages ──────────────────────────────────────────────────────────────────────

@events_bp.route('/')
def index():
    return render_template('index.html', user=get_current_user())

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
        'notify_mode':      'none',   # none | community | saved
        'notify_webhook_id': '',      # which saved webhook (when mode == saved)
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
    e['can_manage'] = _can_manage(e)
    owner_profile = get_user_profile(e.get('owner_id', ''))
    # Surface the organizer's discord (if known) so players know how to reach them.
    e['owner_discord'] = owner_profile.get('discord', '')
    # Whether the "community channel" option is available (admin has set one).
    e['community_webhook_set'] = bool(get_config().get('discord_webhook'))
    # Managers pick the notification destination from the owner's saved webhooks
    # (labels only — never expose the URLs, and only to people who can manage).
    if e['can_manage']:
        e['owner_webhooks'] = [{'id': w['id'], 'label': w.get('label', '')}
                               for w in owner_profile.get('webhooks', [])]
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
               'requires_decklists', 'prize_deadline_days', 'rules', 'schedule', 'prizes', 'contact',
               'event_type', 'format', 'description', 'entry_cost',
               'payment_url', 'date', 'num_rounds',
               'status', 'registration', 'registration_cap',
               'registration_type', 'registration_start', 'registration_end', 'unenroll_end',
               'notify_mode', 'notify_webhook_id'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if 'name' in updates and not str(updates['name']).strip():
        return jsonify({'error': 'Event name is required'}), 400
    if 'test_mode' in updates:
        updates['test_mode'] = bool(updates['test_mode'])
    if 'registration_type' in updates and updates['registration_type'] not in REGISTRATION_TYPES:
        updates['registration_type'] = 'open'
    if 'requires_decklists' in updates:
        updates['requires_decklists'] = bool(updates['requires_decklists'])
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
    if updates.get('notify_mode') not in (None, 'none', 'community', 'saved'):
        return jsonify({'error': 'Invalid notify_mode'}), 400
    if 'payment_url' in updates:
        updates['payment_url'], err = _normalize_payment_url(updates['payment_url'])
        if err:
            return jsonify({'error': err}), 400
    save_event(event_id, updates)
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

    # Enforce registration cap
    cap = e.get('registration_cap', 0)
    active = [p for p in e['players'] if not p.get('dropped')]
    if cap and len(active) >= cap:
        return jsonify({'error': f'This event is full ({cap} players max)'}), 400
    user = get_current_user()
    data = request.json or {}
    display_name = data.get('display_name', '').strip() or user['name']
    discord      = data.get('discord', '').strip()
    if any(p.get('google_id') == user['id'] for p in e['players']):
        return jsonify({'error': 'Already registered'}), 400
    player = {
        'id':        _slugify(display_name) + '_' + str(len(e['players'])),
        'name':      display_name,
        'google_id': user['id'],
        'discord':   discord,
        'dropped':   False,
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
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
        'guest_token': token,
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
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
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
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
    return jsonify({'ok': True})


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
        save_event(event_id, {'rounds': e['rounds']})
        round_num = len(e['rounds'])
        standings = compute_standings(e['players'], e['rounds'])
        webhook   = _resolve_event_webhook(e)
        if webhook:
            post_round(webhook, e, round_num, new_round, standings)
        return jsonify({'round_num': round_num, 'pairings': new_round})

    if e['rounds'] and e.get('event_type') != 'League':
        last = e['rounds'][-1]
        unfinished = [m for m in last
                      if not m.get('is_bye')
                      and m.get('winner_id') is None
                      and m.get('result') != 'draw']
        if unfinished:
            return jsonify({'error': 'Previous round has unrecorded results'}), 400
    num_rounds = e['num_rounds'] or default_num_rounds(len(e['players']))
    if len(e['rounds']) >= num_rounds:
        return jsonify({'error': 'All rounds already paired'}), 400
    new_round = pair_round(e['players'], e['rounds'])
    e['rounds'].append(new_round)
    updates = {'rounds': e['rounds'], 'status': 'active', 'registration': 'closed'}
    save_event(event_id, updates)

    round_num  = len(e['rounds'])
    standings  = compute_standings(e['players'], e['rounds'])
    webhook    = _resolve_event_webhook(e)
    if webhook:
        post_round(webhook, e, round_num, new_round, standings)

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
    save_event(event_id, {'rounds': e['rounds'], 'cut_size': cut_size, 'cut_seeds': seeds})

    round_num = len(e['rounds'])
    webhook   = _resolve_event_webhook(e)
    if webhook:
        post_round(webhook, e, round_num, new_round,
                   compute_standings(e['players'], e['rounds']))
    return jsonify({'round_num': round_num, 'cut_size': cut_size, 'pairings': new_round})


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
        m.get('winner_id') is not None or m.get('result') == 'draw'
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
        if not match.get('is_bye') and p1 == p2:
            return jsonify({'error': 'A player cannot be paired against themselves'}), 400
        for pid in (p1, p2):
            if pid == BYE_PLAYER_ID:
                continue
            if pid in seen:
                return jsonify({'error': f"{names.get(pid, pid)} is assigned to more than one match"}), 400
            seen.add(pid)
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
    save_event(event_id, {'rounds': e['rounds']})

    standings = compute_standings(e['players'], e['rounds'])
    webhook   = _resolve_event_webhook(e)
    if webhook:
        post_round(webhook, e, round_num, new_round, standings)

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
    err = _validate_result(match, winner_id, result)
    if err:
        return jsonify({'error': err}), 400
    match['winner_id'] = winner_id
    match['result']    = result
    save_event(event_id, {'rounds': e['rounds']})
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
                if m.get('result') == 'draw':
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
        # No event history yet — still let the user view/edit their own profile.
        cur = get_current_user()
        if cur and cur['id'] == google_id:
            profile = {'name': saved.get('name') or cur.get('name', ''),
                       'discord': saved.get('discord', '')}
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


# ── Admin settings (Discord webhook etc.) ─────────────────────────────────────

@events_bp.route('/admin/settings')
@login_required
def settings_page():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    config = get_config()
    return render_template('settings.html', user=user, config=config)

@events_bp.route('/api/settings', methods=['PUT'])
@login_required
def api_update_settings():
    user = get_current_user()
    if not is_admin(user['id']):
        abort(403)
    data = request.json or {}
    updates = {}
    if 'discord_webhook' in data:
        wh = (data.get('discord_webhook') or '').strip()
        if wh and not is_valid_webhook(wh):
            return jsonify({'error': 'That does not look like a Discord webhook URL'}), 400
        updates['discord_webhook'] = wh  # '' clears it
    save_config(updates)
    return jsonify({'ok': True})


# ── Personal Discord webhooks (per organiser) ─────────────────────────────────
# Saved on the user's profile as webhooks: [{id, label, url}]. Any signed-in user
# can manage their own; events reference them by id via notify_webhook_id.

@events_bp.route('/webhooks')
@login_required
def webhooks_page():
    return render_template('webhooks.html', user=get_current_user())

@events_bp.route('/api/webhooks', methods=['GET'])
@login_required
def api_list_webhooks():
    user  = get_current_user()
    hooks = get_user_profile(user['id']).get('webhooks', [])
    # Never return full URLs to the client.
    return jsonify([{'id': w['id'], 'label': w.get('label', ''),
                     'masked': _mask_webhook(w.get('url', ''))} for w in hooks])

@events_bp.route('/api/webhooks', methods=['POST'])
@login_required
def api_add_webhook():
    user  = get_current_user()
    data  = request.json or {}
    label = (data.get('label') or '').strip()
    url   = (data.get('url') or '').strip()
    if not label:
        return jsonify({'error': 'A label is required'}), 400
    if not is_valid_webhook(url):
        return jsonify({'error': 'That does not look like a Discord webhook URL'}), 400
    profile = get_user_profile(user['id'])
    hooks   = profile.get('webhooks', [])
    if len(hooks) >= 25:
        return jsonify({'error': 'You have reached the saved-webhook limit'}), 400
    hook = {'id': uuid.uuid4().hex[:12], 'label': label, 'url': url}
    hooks.append(hook)
    save_user_profile(user['id'], {'webhooks': hooks})
    return jsonify({'id': hook['id'], 'label': label, 'masked': _mask_webhook(url)}), 201

@events_bp.route('/api/webhooks/<webhook_id>', methods=['DELETE'])
@login_required
def api_delete_webhook(webhook_id):
    user  = get_current_user()
    hooks = [w for w in get_user_profile(user['id']).get('webhooks', [])
             if w.get('id') != webhook_id]
    save_user_profile(user['id'], {'webhooks': hooks})
    return jsonify({'ok': True})

@events_bp.route('/api/webhooks/<webhook_id>/test', methods=['POST'])
@login_required
def api_test_webhook(webhook_id):
    user = get_current_user()
    wh = next((w for w in get_user_profile(user['id']).get('webhooks', [])
               if w.get('id') == webhook_id), None)
    if not wh:
        return jsonify({'error': 'Webhook not found'}), 404
    if post_test(wh['url']):
        return jsonify({'ok': True})
    return jsonify({'error': 'Discord did not accept the test message'}), 502
