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

EPHEMERAL = 1 << 6           # message flag: only the invoking user sees it


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
    if itype in (MESSAGE_COMPONENT, MODAL_SUBMIT):
        return _reply('That action is not available yet.')
    abort(400)


def _handle_command(body):
    name = (body.get('data') or {}).get('name')
    if name == 'cbp':
        # Phase 0 placeholder — confirms the pipeline works end to end.
        # Later phases route subcommands (register / report / standings).
        return _reply('🃏 Cardboard Party is connected! More commands coming soon.')
    return _reply('Unknown command.')
