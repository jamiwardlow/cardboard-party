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
STRING_SELECT = 3


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
    return _reply('🃏 Cardboard Party is connected! Try `/cbp register`.')


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


def _handle_component(body):
    custom_id = (body.get('data') or {}).get('custom_id')
    if custom_id == 'cbp_register_select':
        from routes.events import register_player_via_discord
        values = (body.get('data') or {}).get('values') or []
        if not values:
            return _update('No event selected.')
        discord_id, name = _interaction_user(body)
        result, err = register_player_via_discord(values[0], discord_id, name)
        if err:
            return _update(f'⚠️ {err}')
        return _update(f"✅ Registered for **{result['event_name']}** as "
                       f"**{result['player']['name']}**. You'll be able to report "
                       f"results here with `/cbp report` soon.")
    return _reply('That action is not available yet.')


def _update(content: str):
    """Edit the (ephemeral) message a component is on, clearing its controls."""
    return jsonify({'type': UPDATE_MESSAGE, 'data': {'content': content, 'components': []}})
