"""
Event routes.

Permission model:
  can_manage(event) = is_admin(user) OR user is event owner
  Players can register/unregister themselves and report their own results.
  Anyone can view events and standings.
"""

from flask import Blueprint, request, jsonify, render_template, abort
from db import (create_event, get_event, save_event, list_events,
                get_admins, is_admin, add_admin, remove_admin,
                get_user_profile, save_user_profile, list_users,
                get_config, save_config)
from swiss import pair_round, compute_standings, default_num_rounds
from routes.auth import get_current_user, login_required
from discord_notify import post_round
import datetime
import re

events_bp = Blueprint('events', __name__)


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
    return jsonify(list_events())

@events_bp.route('/api/events', methods=['POST'])
@login_required
def api_create_event():
    user = get_current_user()
    data = request.json or {}
    event = {
        'name':         data.get('name', 'New Event'),
        'event_type':   data.get('event_type', 'One-day'),
        'format':       data.get('format', 'Limited: Draft'),
        'date':         data.get('date', str(datetime.date.today())),
        'owner_id':     user['id'],
        'owner_name':   user['name'],
        'players':      [],
        'rounds':       [],
        'num_rounds':   data.get('num_rounds', 0),
        'status':       'setup',
        'registration': 'open',
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
    e['can_manage'] = _can_manage(e)
    return jsonify(e)

@events_bp.route('/api/events/<event_id>', methods=['PUT'])
@login_required
def api_update_event(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    data = request.json or {}
    allowed = {'name', 'event_type', 'format', 'date', 'num_rounds',
               'status', 'registration', 'registration_cap'}
    updates = {k: v for k, v in data.items() if k in allowed}
    save_event(event_id, updates)
    return jsonify({**e, **updates})


# ── API: player registration ───────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/register', methods=['POST'])
@login_required
def api_register(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    if e.get('registration') != 'open':
        return jsonify({'error': 'Registration is closed'}), 400

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
    if e['rounds']:
        player['dropped'] = True
    else:
        e['players'] = [p for p in e['players'] if p.get('google_id') != user['id']]
    save_event(event_id, {'players': e['players']})
    return jsonify({'ok': True})


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
    # If 'self' flag is passed, attach the organiser's google_id so they
    # get a profile link and can report their own results.
    google_id = user['id'] if data.get('self') else None
    player = {
        'id':        _slugify(name) + '_' + str(len(e['players'])),
        'name':      name,
        'google_id': google_id,
        'discord':   data.get('discord', ''),
        'dropped':   False,
    }
    e['players'].append(player)
    save_event(event_id, {'players': e['players']})
    return jsonify(player), 201

@events_bp.route('/api/events/<event_id>/players/<player_id>/drop', methods=['POST'])
@login_required
def api_drop_player(event_id, player_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
    player = next((p for p in e['players'] if p['id'] == player_id), None)
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    player['dropped'] = True
    save_event(event_id, {'players': e['players']})
    return jsonify({'ok': True})


# ── API: pairing ───────────────────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/pair', methods=['POST'])
@login_required
def api_pair_round(event_id):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    _require_manage(e)
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
    config     = get_config()
    webhook    = config.get('discord_webhook', '')
    if webhook:
        post_round(webhook, e, round_num, new_round, standings)

    return jsonify({'round_num': round_num, 'pairings': new_round})


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
    has_results = any(
        m.get('winner_id') is not None or m.get('result') == 'draw'
        for m in e['rounds'][idx] if not m.get('is_bye')
    )
    if has_results:
        return jsonify({'error': 'Cannot edit pairings after results have been entered'}), 400
    new_pairings = request.json or []
    valid_ids = {p['id'] for p in e['players']} | {'__bye__'}
    for match in new_pairings:
        for key in ('player1_id', 'player2_id'):
            if match.get(key) not in valid_ids:
                return jsonify({'error': f"Unknown player: {match.get(key)}"}), 400
    e['rounds'][idx] = new_pairings
    save_event(event_id, {'rounds': e['rounds']})
    return jsonify({'round_num': round_num, 'pairings': new_pairings})


# ── API: results ───────────────────────────────────────────────────────────────

@events_bp.route('/api/events/<event_id>/rounds/<int:round_num>/results', methods=['POST'])
@login_required
def api_record_result(event_id, round_num):
    e = get_event(event_id)
    if not e:
        return jsonify({'error': 'Not found'}), 404
    user = get_current_user()
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
    if not _can_manage(e):
        player = _find_player_by_google_id(e, user['id'])
        if not player:
            return jsonify({'error': 'Not registered for this event'}), 403
        if player['id'] not in (match.get('player1_id'), match.get('player2_id')):
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

    if not profile:
        return 'Player not found', 404

    # Merge with saved user profile (display name / discord edits)
    saved = get_user_profile(google_id)
    if saved.get('name'):
        profile['name'] = saved['name']
    if saved.get('discord'):
        profile['discord'] = saved['discord']

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
    })

@events_bp.route('/api/profile', methods=['PUT'])
@login_required
def api_update_profile():
    user = get_current_user()
    data = request.json or {}
    updates = {}
    if 'name' in data:
        updates['name']    = data['name'].strip()
    if 'discord' in data:
        updates['discord'] = data['discord'].strip()
    if not updates:
        return jsonify({'error': 'Nothing to update'}), 400
    save_user_profile(user['id'], updates)
    return jsonify({'ok': True, **updates})


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
    data    = request.json or {}
    allowed = {'discord_webhook'}
    updates = {k: v for k, v in data.items() if k in allowed}
    save_config(updates)
    return jsonify({'ok': True})
