"""
Discord bot via HTTP Interactions (no gateway/persistent connection needed).

Discord POSTs every slash command / button / modal interaction to
`/discord/interactions` as an Ed25519-signed request; we verify the signature
with the app's public key, answer the PING handshake, and route commands.
Outbound actions (DMs, channel posts) go through Discord's REST API — see
discord_api.py (added in later phases).

Secrets (per environment, in Secret Manager): DISCORD_PUBLIC_KEY (verify
requests), DISCORD_BOT_TOKEN (REST calls), DISCORD_APP_ID (REST/command setup).
"""

from flask import Blueprint, request, jsonify, abort
from gcp_secrets import get_secret

discord_bp = Blueprint('discord', __name__)

# Interaction types (incoming)
PING = 1
APPLICATION_COMMAND = 2
MESSAGE_COMPONENT = 3
MODAL_SUBMIT = 5

# Response types (outgoing)
PONG = 1
CHANNEL_MESSAGE = 4          # reply with a message
DEFERRED_MESSAGE = 5         # "thinking…", edit later (for work that may exceed 3s)
UPDATE_MESSAGE = 7           # edit the message a component is attached to

EPHEMERAL = 1 << 6           # message flag: only the invoking user sees it

# Component types
ACTION_ROW = 1
BUTTON = 2
STRING_SELECT = 3
# Button styles: 1 primary, 2 secondary, 3 success (green), 4 danger (red)


def _verify_signature(req) -> bool:
    """Verify the Ed25519 signature Discord attaches to every interaction POST."""
    public_key = get_secret('DISCORD_PUBLIC_KEY')
    signature = req.headers.get('X-Signature-Ed25519', '')
    timestamp = req.headers.get('X-Signature-Timestamp', '')
    if not (public_key and signature and timestamp):
        return False
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError
    try:
        VerifyKey(bytes.fromhex(public_key)).verify(
            timestamp.encode() + req.data, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


def _reply(content: str, ephemeral: bool = True):
    """A simple text reply to an interaction."""
    data = {'content': content}
    if ephemeral:
        data['flags'] = EPHEMERAL
    return jsonify({'type': CHANNEL_MESSAGE, 'data': data})


@discord_bp.route('/discord/interactions', methods=['POST'])
def interactions():
    if not _verify_signature(request):
        abort(401)
    body = request.get_json(silent=True) or {}
    itype = body.get('type')

    if itype == PING:                       # Discord's endpoint-verification handshake
        return jsonify({'type': PONG})
    if itype == APPLICATION_COMMAND:
        return _handle_command(body)
    if itype == MESSAGE_COMPONENT:
        return _handle_component(body)
    if itype == MODAL_SUBMIT:
        return _reply('That action is not available yet.')
    abort(400)


def _interaction_user(body):
    """(discord_id, display_name) of whoever triggered the interaction."""
    member = body.get('member') or {}
    user = member.get('user') or body.get('user') or {}
    name = member.get('nick') or user.get('global_name') or user.get('username') or 'Player'
    return user.get('id'), name


def _handle_command(body):
    data = body.get('data') or {}
    if data.get('name') != 'cbp':
        return _reply('Unknown command.')
    sub = ((data.get('options') or [{}])[0]).get('name')
    if sub == 'register':
        return _register_picker()
    if sub == 'report':
        return _report_menu(body)
    if sub == 'standings':
        return _standings_menu()
    if sub == 'link':
        return _link_menu()
    if sub == 'announce':
        return _announce_menu()
    return _reply('🃏 Cardboard Party is connected! Try `/cbp register`, `/cbp report`, or `/cbp standings`.')


def _announce_menu():
    """Pick an event to post in the current channel with a one-click Register button."""
    from routes.events import discord_registerable_events
    events = discord_registerable_events()
    if not events:
        return _reply('There are no events open for registration to announce.')
    options = [{'label': (e.get('name') or 'Event')[:100], 'value': e['id']} for e in events]
    select = {'type': STRING_SELECT, 'custom_id': 'cbp_announce_select',
              'placeholder': 'Which event do you want to post here?', 'options': options}
    return jsonify({'type': CHANNEL_MESSAGE, 'data': {
        'flags': EPHEMERAL,
        'content': 'Post an event here so players can register with one tap:',
        'components': [{'type': ACTION_ROW, 'components': [select]}]}})


def _link_menu():
    """Pick an event whose pairings should auto-post in the current channel."""
    from routes.events import discord_linkable_events
    events = discord_linkable_events()
    if not events:
        return _reply('No events to link yet.')
    options = [{'label': (e.get('name') or 'Event')[:100], 'value': e['id']} for e in events]
    select = {'type': STRING_SELECT, 'custom_id': 'cbp_link_select',
              'placeholder': 'Which event posts pairings here?', 'options': options}
    return jsonify({'type': CHANNEL_MESSAGE, 'data': {
        'flags': EPHEMERAL,
        'content': 'Link an event to **this channel** — its pairings will post here each round:',
        'components': [{'type': ACTION_ROW, 'components': [select]}]}})


def _standings_menu():
    """Show standings; pick an event first if more than one has started."""
    from routes.events import discord_standings_events, discord_standings_text
    events = discord_standings_events()
    if not events:
        return _reply('No events have standings yet.')
    if len(events) == 1:
        return _reply(discord_standings_text(events[0]['id']))
    options = [{'label': (e.get('name') or 'Event')[:100], 'value': e['id']} for e in events]
    select = {'type': STRING_SELECT, 'custom_id': 'cbp_standings_select',
              'placeholder': 'Which event?', 'options': options}
    return jsonify({'type': CHANNEL_MESSAGE, 'data': {
        'flags': EPHEMERAL, 'content': 'Standings for which event?',
        'components': [{'type': ACTION_ROW, 'components': [select]}]}})


def _register_picker():
    """Reply with an ephemeral select menu of events open for registration."""
    from routes.events import discord_registerable_events
    events = discord_registerable_events()
    if not events:
        return _reply('There are no events open for registration right now.')
    options = []
    for e in events:
        opt = {'label': (e.get('name') or 'Event')[:100], 'value': e['id']}
        desc = ' · '.join(x for x in (e.get('date', ''), e.get('format', '')) if x)[:100]
        if desc:
            opt['description'] = desc
        options.append(opt)
    select = {'type': STRING_SELECT, 'custom_id': 'cbp_register_select',
              'placeholder': 'Choose an event to register for', 'options': options}
    return jsonify({'type': CHANNEL_MESSAGE, 'data': {
        'flags': EPHEMERAL, 'content': 'Which event do you want to register for?',
        'components': [{'type': ACTION_ROW, 'components': [select]}]}})


def _report_menu(body):
    """Entry for /cbp report: 0 matches → notice; 1 → result buttons; many → picker."""
    from routes.events import discord_open_matches
    discord_id, _ = _interaction_user(body)
    matches = discord_open_matches(discord_id)
    if not matches:
        return _reply('You have no matches to report right now.')
    if len(matches) == 1:
        return jsonify({'type': CHANNEL_MESSAGE, 'data': _report_buttons(matches[0])})
    options = [{'label': f"{m['event_name']} — vs {m['opponent']}"[:100],
                'value': f"{m['event_id']}:{m['round_idx']}:{m['match_idx']}"} for m in matches]
    select = {'type': STRING_SELECT, 'custom_id': 'cbp_report_select',
              'placeholder': 'Which match?', 'options': options}
    return jsonify({'type': CHANNEL_MESSAGE, 'data': {
        'flags': EPHEMERAL, 'content': 'Which match do you want to report?',
        'components': [{'type': ACTION_ROW, 'components': [select]}]}})


def _report_buttons(ctx):
    """Result-entry buttons for one match (from the reporter's perspective)."""
    def cid(code):
        return f"cbp_rep:{ctx['event_id']}:{ctx['round_idx']}:{ctx['match_idx']}:{code}"
    rows = [{'type': ACTION_ROW, 'components': [
        {'type': BUTTON, 'style': 3, 'label': 'Win 2–0',  'custom_id': cid('w20')},
        {'type': BUTTON, 'style': 3, 'label': 'Win 2–1',  'custom_id': cid('w21')},
        {'type': BUTTON, 'style': 4, 'label': 'Lose 1–2', 'custom_id': cid('l12')},
        {'type': BUTTON, 'style': 4, 'label': 'Lose 0–2', 'custom_id': cid('l02')},
        {'type': BUTTON, 'style': 2, 'label': 'Draw',     'custom_id': cid('draw')},
    ]}]
    if ctx.get('allow_id'):
        rows.append({'type': ACTION_ROW, 'components': [
            {'type': BUTTON, 'style': 2, 'label': 'Intentional draw (0–0–3)', 'custom_id': cid('id')}]})
    return {'flags': EPHEMERAL,
            'content': f"Report your Round {ctx['round_num']} match vs "
                       f"**{ctx['opponent']}** ({ctx['event_name']}):",
            'components': rows}


def _handle_component(body):
    custom_id = (body.get('data') or {}).get('custom_id') or ''
    discord_id, name = _interaction_user(body)

    if custom_id == 'cbp_register_select':
        from routes.events import register_player_via_discord
        values = (body.get('data') or {}).get('values') or []
        if not values:
            return _update('No event selected.')
        result, err = register_player_via_discord(values[0], discord_id, name)
        if err:
            return _update(f'⚠️ {err}')
        return _update(f"✅ Registered for **{result['event_name']}** as "
                       f"**{result['player']['name']}**. Report results here with `/cbp report`.")

    if custom_id == 'cbp_report_select':
        from routes.events import discord_match_context
        val = ((body.get('data') or {}).get('values') or [''])[0]
        try:
            eid, ri, mi = val.split(':'); ri, mi = int(ri), int(mi)
        except ValueError:
            return _update('Sorry, that selection was invalid.')
        ctx = discord_match_context(eid, ri, mi, discord_id)
        if not ctx:
            return _update("That doesn't look like an open match of yours anymore.")
        return jsonify({'type': UPDATE_MESSAGE, 'data': _report_buttons(ctx)})

    if custom_id == 'cbp_standings_select':
        from routes.events import discord_standings_text
        val = ((body.get('data') or {}).get('values') or [''])[0]
        return _update(discord_standings_text(val) or 'That event no longer exists.')

    if custom_id == 'cbp_link_select':
        from routes.events import set_event_discord_channel
        val = ((body.get('data') or {}).get('values') or [''])[0]
        channel_id = body.get('channel_id') or (body.get('channel') or {}).get('id')
        name = set_event_discord_channel(val, channel_id)
        if not name:
            return _update('That event no longer exists.')
        return _update(f"✅ **{name}** pairings will post in this channel each round.")

    if custom_id == 'cbp_announce_select':
        import discord_api
        from routes.events import get_event
        val = ((body.get('data') or {}).get('values') or [''])[0]
        channel_id = body.get('channel_id') or (body.get('channel') or {}).get('id')
        event = get_event(val)
        if not event:
            return _update('That event no longer exists.')
        event_url = request.host_url.rstrip('/') + '/events/' + val
        ok = discord_api.announce_event(event, channel_id, event_url)
        if not ok:
            return _update("I couldn't post here — check that I have permission to send "
                           "messages in this channel.")
        return _update(f"✅ Posted **{event.get('name', 'the event')}** in this channel "
                       "with a Register button.")

    if custom_id.startswith('cbp_reg_btn:'):
        # One-tap Register button on an announcement post (no slash command needed).
        from routes.events import register_player_via_discord
        eid = custom_id.split(':', 1)[1]
        result, err = register_player_via_discord(eid, discord_id, name)
        if err:
            return _reply(f'⚠️ {err}')
        return _reply(f"✅ Registered for **{result['event_name']}** as "
                      f"**{result['player']['name']}**. Report results here with `/cbp report`.")

    if custom_id == 'cbp_report_btn':
        # "Report my result" on a pairings post → the report flow for the clicker.
        return _report_menu(body)

    if custom_id.startswith('cbp_rep:'):
        from routes.events import report_result_via_discord
        try:
            _, eid, ri, mi, code = custom_id.split(':'); ri, mi = int(ri), int(mi)
        except ValueError:
            return _update('Sorry, that button was invalid.')
        msg, err = report_result_via_discord(eid, ri, mi, discord_id, code)
        return _update(f'⚠️ {err}' if err else f'✅ {msg}')

    return _reply('That action is not available yet.')


def _update(content: str):
    """Edit the (ephemeral) message a component is on, clearing its controls."""
    return jsonify({'type': UPDATE_MESSAGE, 'data': {'content': content, 'components': []}})
