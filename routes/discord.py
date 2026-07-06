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

import os
from flask import Blueprint, request, jsonify, abort
from gcp_secrets import get_secret

discord_bp = Blueprint('discord', __name__)

# Slash-command name, per environment so staging and prod (same code) don't
# collide in a shared server. Prod defaults to 'cparty'; staging.yaml overrides
# it to 'cpstaging'. Used by the router, all user-facing text, and the help page.
COMMAND_NAME = os.environ.get('DISCORD_COMMAND_NAME', 'cparty')

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
MODAL = 9                    # pop up a text-input form (collects the optional note)

EPHEMERAL = 1 << 6           # message flag: only the invoking user sees it

# Component types
ACTION_ROW = 1
BUTTON = 2
STRING_SELECT = 3
TEXT_INPUT = 4               # a modal's text field
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


def _message_modal(custom_id: str, title: str, placeholder: str):
    """Pop up a single-field form asking for an optional message to include with an
    announcement/invitation. `custom_id` carries the event (and target) so the
    submit handler knows what to post. Max length leaves room for our own prefix."""
    return jsonify({'type': MODAL, 'data': {
        'custom_id': custom_id,
        'title': title[:45],
        'components': [{'type': ACTION_ROW, 'components': [{
            'type': TEXT_INPUT,
            'custom_id': 'message',
            'label': 'Message (optional)',
            'style': 2,                 # paragraph (multi-line)
            'required': False,
            'max_length': 1800,
            'placeholder': placeholder[:100],
        }]}]}})


def _modal_text(body, field: str = 'message') -> str:
    """Pull a submitted text-input value out of a MODAL_SUBMIT body."""
    for row in (body.get('data') or {}).get('components') or []:
        for c in row.get('components') or []:
            if c.get('custom_id') == field:
                return (c.get('value') or '').strip()
    return ''


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
        return _handle_modal(body)
    abort(400)


def _interaction_user(body):
    """(discord_id, display_name) of whoever triggered the interaction."""
    member = body.get('member') or {}
    user = member.get('user') or body.get('user') or {}
    name = member.get('nick') or user.get('global_name') or user.get('username') or 'Player'
    return user.get('id'), name

def _interaction_username(body):
    """The triggering user's unique Discord username (the @handle), used to match
    them to an existing account at registration."""
    member = body.get('member') or {}
    user = member.get('user') or body.get('user') or {}
    return user.get('username') or ''


def _handle_command(body):
    data = body.get('data') or {}
    if data.get('name') != COMMAND_NAME:
        return _reply('Unknown command.')
    sub = ((data.get('options') or [{}])[0]).get('name')
    if sub == 'register':
        return _register_picker()
    if sub == 'drop':
        return _drop_picker(body)
    if sub == 'report':
        return _report_menu(body)
    if sub == 'standings':
        return _standings_menu()
    if sub == 'link':
        return _link_menu(body)
    if sub == 'announce':
        return _announce_menu(body)
    if sub == 'invite':
        return _invite_menu(body)
    if sub == 'help':
        return _help()
    return _reply(f'🃏 Cardboard Party is connected! Try `/{COMMAND_NAME} register`, '
                  f'`/{COMMAND_NAME} report`, or `/{COMMAND_NAME} standings`.')


def _help():
    """An ephemeral list of what the bot can do."""
    c = COMMAND_NAME
    lines = [
        '🃏 **Cardboard Party — bot commands**',
        '',
        f'`/{c} register` — Register yourself for an event.',
        f'`/{c} drop` — Drop yourself from an event you registered for.',
        f'`/{c} report` — Report the result of your match.',
        f'`/{c} standings` — Show an event\'s standings.',
        f'`/{c} invite` — DM someone an invitation to register for an event.',
        f'`/{c} link` — *(organizer)* Post an event\'s pairings in this channel each round.',
        f'`/{c} announce` — *(organizer)* Post an event here with a one-tap Register button.',
        '',
        'Each round the bot also DMs you your pairing with a button to report your result.',
    ]
    return _reply('\n'.join(lines))


def _announce_menu(body):
    """Pick one of your events to post in the current channel with a Register button
    (or Join Waitlist when the event is full)."""
    from routes.events import discord_registerable_events
    events = discord_registerable_events(owner_discord_id=_interaction_user(body)[0], include_full=True)
    if not events:
        return _reply('You don’t own any events open for sign-ups to announce.')
    options = [{'label': (e.get('name') or 'Event')[:100], 'value': e['id']} for e in events]
    select = {'type': STRING_SELECT, 'custom_id': 'cbp_announce_select',
              'placeholder': 'Which event do you want to post here?', 'options': options}
    return jsonify({'type': CHANNEL_MESSAGE, 'data': {
        'flags': EPHEMERAL,
        'content': 'Post an event here so players can register with one tap:',
        'components': [{'type': ACTION_ROW, 'components': [select]}]}})


def _invite_menu(body):
    """/cparty invite user:@someone — pick an event, then DM them an invitation.
    The @user is a native Discord user option, so we get their numeric ID (the DM
    is deliverable only because they're a server member the bot shares a guild
    with)."""
    data = body.get('data') or {}
    sub = (data.get('options') or [{}])[0]
    target_id = next((o.get('value') for o in (sub.get('options') or [])
                      if o.get('name') == 'user'), None)
    if not target_id:
        return _reply('Please choose someone to invite.')
    from routes.events import discord_registerable_events
    events = discord_registerable_events(owner_discord_id=_interaction_user(body)[0], include_full=True)
    if not events:
        return _reply('You don’t own any events open for sign-ups to invite anyone to.')
    options = [{'label': (e.get('name') or 'Event')[:100], 'value': e['id']} for e in events]
    select = {'type': STRING_SELECT, 'custom_id': f'cbp_invite_select:{target_id}',
              'placeholder': 'Which event do you want to invite them to?', 'options': options}
    return jsonify({'type': CHANNEL_MESSAGE, 'data': {
        'flags': EPHEMERAL,
        'content': f'Invite <@{target_id}> to which event?',
        'allowed_mentions': {'parse': []},
        'components': [{'type': ACTION_ROW, 'components': [select]}]}})


def _invite_action_row(eid, registered):
    """Action row for a DM invitation card: a Register/Withdraw toggle (reflecting
    whether they're currently registered) plus a View-details link."""
    toggle = ({'type': BUTTON, 'style': 4, 'label': 'Withdraw', 'custom_id': f'cbp_wd_btn:{eid}'}
              if registered else
              {'type': BUTTON, 'style': 1, 'label': 'Register', 'custom_id': f'cbp_reg_btn:{eid}'})
    view = {'type': BUTTON, 'style': 5, 'label': 'View details',
            'url': f"{request.host_url.rstrip('/')}/events/{eid}"}
    return [{'type': ACTION_ROW, 'components': [toggle, view]}]


def _waitlist_action_row(eid, on_waitlist):
    """Action row for a full event's card/DM: a Join/Leave Waitlist toggle plus a
    View-details link."""
    toggle = ({'type': BUTTON, 'style': 4, 'label': 'Leave Waitlist', 'custom_id': f'cbp_wl_leave_btn:{eid}'}
              if on_waitlist else
              {'type': BUTTON, 'style': 3, 'label': 'Join Waitlist', 'custom_id': f'cbp_waitlist_btn:{eid}'})
    view = {'type': BUTTON, 'style': 5, 'label': 'View details',
            'url': f"{request.host_url.rstrip('/')}/events/{eid}"}
    return [{'type': ACTION_ROW, 'components': [toggle, view]}]


def _link_menu(body):
    """Pick one of your events whose pairings should auto-post in this channel."""
    from routes.events import discord_linkable_events
    events = discord_linkable_events(owner_discord_id=_interaction_user(body)[0])
    if not events:
        return _reply('You don’t own any events to link yet.')
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


def _drop_picker(body):
    """Reply with an ephemeral select menu of events the user is registered for, so
    they can drop themselves — works for ghost players who registered via a button
    without an account (matched by their Discord ID/handle)."""
    from routes.events import discord_droppable_events
    discord_id, display = _interaction_user(body)
    events = discord_droppable_events(discord_id, _interaction_username(body), display)
    if not events:
        return _reply("You're not registered for any events right now.")
    options = [{'label': (e.get('name') or 'Event')[:100], 'value': e['id']} for e in events]
    select = {'type': STRING_SELECT, 'custom_id': 'cbp_drop_select',
              'placeholder': 'Choose an event to drop from', 'options': options}
    return jsonify({'type': CHANNEL_MESSAGE, 'data': {
        'flags': EPHEMERAL, 'content': 'Which event do you want to drop from?',
        'components': [{'type': ACTION_ROW, 'components': [select]}]}})


def _report_menu(body, in_place=False):
    """Entry for /cparty report: 0 matches → notice; 1 → result buttons; many → picker.

    When `in_place` (the button was tapped on the player's own pairing DM), edit
    that message in place instead of posting a new one — so its 'Report my result'
    button becomes the result buttons, and ultimately the disabled 'Result
    reported' confirmation. Channel posts and the slash command spawn a fresh
    ephemeral message instead (the channel button is shared by the whole round)."""
    from routes.events import discord_open_matches
    discord_id, display = _interaction_user(body)
    matches = discord_open_matches(discord_id, _interaction_username(body), display)
    if not matches:
        return _reply('You have no matches to report right now.')
    if len(matches) == 1:
        data = _report_buttons(matches[0])
    else:
        options = [{'label': f"{m['event_name']} — vs {m['opponent']}"[:100],
                    'value': f"{m['event_id']}:{m['round_idx']}:{m['match_idx']}"} for m in matches]
        select = {'type': STRING_SELECT, 'custom_id': 'cbp_report_select',
                  'placeholder': 'Which match?', 'options': options}
        data = {'flags': EPHEMERAL, 'content': 'Which match do you want to report?',
                'components': [{'type': ACTION_ROW, 'components': [select]}]}
    if in_place:
        # Editing an existing (non-ephemeral) DM message: drop the ephemeral flag.
        data = {k: v for k, v in data.items() if k != 'flags'}
        return jsonify({'type': UPDATE_MESSAGE, 'data': data})
    return jsonify({'type': CHANNEL_MESSAGE, 'data': data})


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
        eid = values[0]
        result, err = register_player_via_discord(
            eid, discord_id, name, _interaction_username(body))
        if err:
            return _update(f'⚠️ {err}')
        return _update(f"✅ Registered for **{result['event_name']}** as "
                       f"**{result['player']['name']}**. Report results here with `/{COMMAND_NAME} report`.",
                       f"{request.host_url.rstrip('/')}/events/{eid}")

    if custom_id == 'cbp_drop_select':
        from routes.events import withdraw_player_via_discord
        val = ((body.get('data') or {}).get('values') or [''])[0]
        if not val:
            return _update('No event selected.')
        ename, err = withdraw_player_via_discord(
            val, discord_id, _interaction_username(body), name)
        if err:
            return _update(f'⚠️ {err}')
        return _update(f"✅ You've dropped from **{ename}**. Changed your mind? "
                       f"Register again with `/{COMMAND_NAME} register`.",
                       f"{request.host_url.rstrip('/')}/events/{val}")

    if custom_id == 'cbp_report_select':
        from routes.events import discord_match_context
        val = ((body.get('data') or {}).get('values') or [''])[0]
        try:
            eid, ri, mi = val.split(':'); ri, mi = int(ri), int(mi)
        except ValueError:
            return _update('Sorry, that selection was invalid.')
        ctx = discord_match_context(eid, ri, mi, discord_id, _interaction_username(body), name)
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
        # Event chosen → ask for an optional note, then post on modal submit.
        val = ((body.get('data') or {}).get('values') or [''])[0]
        if not val:
            return _update('No event selected.')
        return _message_modal(f'cbp_announce_modal:{val}', 'Announce event',
                              'Optional — posted above the event card.')

    if custom_id.startswith('cbp_reg_btn:'):
        # One-tap Register button (on a channel announce card or a DM invitation).
        from routes.events import register_player_via_discord
        eid = custom_id.split(':', 1)[1]
        result, err = register_player_via_discord(
            eid, discord_id, name, _interaction_username(body))
        if err:
            return _reply(f'⚠️ {err}')
        confirm = (f"✅ Registered for **{result['event_name']}** as "
                   f"**{result['player']['name']}**. Report results here with `/{COMMAND_NAME} report`.")
        # On a DM invitation, flip the card's Register button to Withdraw in place.
        # A shared announce card in a channel just gets an ephemeral confirmation.
        if body.get('message') and not body.get('guild_id'):
            return jsonify({'type': UPDATE_MESSAGE, 'data': {
                'content': confirm, 'components': _invite_action_row(eid, registered=True)}})
        return _reply(confirm)

    if custom_id.startswith('cbp_wd_btn:'):
        # Withdraw button on a DM invitation → drop/remove, then toggle back to Register.
        from routes.events import withdraw_player_via_discord
        eid = custom_id.split(':', 1)[1]
        ename, err = withdraw_player_via_discord(eid, discord_id, _interaction_username(body), name)
        if err:
            return _reply(f'⚠️ {err}')
        return jsonify({'type': UPDATE_MESSAGE, 'data': {
            'content': f"You've withdrawn from **{ename}**. Changed your mind? Tap Register.",
            'components': _invite_action_row(eid, registered=False)}})

    if custom_id.startswith('cbp_waitlist_btn:'):
        # Join Waitlist button (on a full event's announce card or invitation DM).
        from routes.events import waitlist_player_via_discord
        eid = custom_id.split(':', 1)[1]
        result, err = waitlist_player_via_discord(eid, discord_id, name, _interaction_username(body))
        if err:
            return _reply(f'⚠️ {err}')
        confirm = (f"✅ You're on the waitlist for **{result['event_name']}** "
                   f"(position {result['position']}). We'll let the organiser promote you "
                   "if a spot opens.")
        # On a DM card, flip Join → Leave Waitlist in place; a shared channel card
        # just gets an ephemeral confirmation.
        if body.get('message') and not body.get('guild_id'):
            return jsonify({'type': UPDATE_MESSAGE, 'data': {
                'content': confirm, 'components': _waitlist_action_row(eid, on_waitlist=True)}})
        return _reply(confirm)

    if custom_id.startswith('cbp_wl_leave_btn:'):
        # Leave Waitlist toggle on a DM card → remove, then flip back to Join Waitlist.
        from routes.events import waitlist_leave_via_discord
        eid = custom_id.split(':', 1)[1]
        ename, err = waitlist_leave_via_discord(eid, discord_id, _interaction_username(body), name)
        if err:
            return _reply(f'⚠️ {err}')
        return jsonify({'type': UPDATE_MESSAGE, 'data': {
            'content': f"You've left the waitlist for **{ename}**. Changed your mind? Tap Join Waitlist.",
            'components': _waitlist_action_row(eid, on_waitlist=False)}})

    if custom_id.startswith('cbp_invite_select:'):
        # Event chosen for an invite → ask for an optional note, then DM on submit.
        target_id = custom_id.split(':', 1)[1]
        values = (body.get('data') or {}).get('values') or []
        if not values:
            return _update('No event selected.')
        return _message_modal(f'cbp_invite_modal:{target_id}:{values[0]}', 'Invite to event',
                              'Optional — a personal note added to your invitation.')

    if custom_id == 'cbp_invite_optout':
        # "Don't invite me" button on an invitation DM.
        from db import set_invite_optout
        set_invite_optout(discord_id, True)
        return _reply("Got it — you won't receive event invites from Cardboard Party anymore.")

    if custom_id.startswith('cbp_report_btn:'):
        # "Report my result" on a pairing DM — the button carries its own match
        # (cbp_report_btn:<eid>:<ri>:<mi>), so report that exact pairing directly
        # instead of offering a picker. Tapped on the player's own DM (a message,
        # no guild), so edit it in place into the result buttons.
        from routes.events import discord_match_context
        try:
            _, eid, ri, mi = custom_id.split(':'); ri, mi = int(ri), int(mi)
        except ValueError:
            return _update('Sorry, that button was invalid.')
        ctx = discord_match_context(eid, ri, mi, discord_id, _interaction_username(body), name)
        if not ctx:
            return _update("That doesn't look like an open match of yours anymore.")
        data = _report_buttons(ctx)
        if bool(body.get('message')) and not body.get('guild_id'):
            data = {k: v for k, v in data.items() if k != 'flags'}   # editing a non-ephemeral DM
            return jsonify({'type': UPDATE_MESSAGE, 'data': data})
        return jsonify({'type': CHANNEL_MESSAGE, 'data': data})

    if custom_id == 'cbp_report_btn':
        # "Report my result" → the report flow for the clicker. If it was tapped on
        # the player's own pairing DM (a message, no guild), update that message in
        # place so its button becomes the result entry (and then "Result reported");
        # the shared channel-post button stays a fresh ephemeral.
        in_place = bool(body.get('message')) and not body.get('guild_id')
        return _report_menu(body, in_place=in_place)

    if custom_id.startswith('cbp_rep:'):
        from routes.events import report_result_via_discord
        try:
            _, eid, ri, mi, code = custom_id.split(':'); ri, mi = int(ri), int(mi)
        except ValueError:
            return _update('Sorry, that button was invalid.')
        msg, err = report_result_via_discord(eid, ri, mi, discord_id, code, request.host_url,
                                             _interaction_username(body), name)
        if err:
            return _update(f'⚠️ {err}')
        return _reported(f'✅ {msg}', f"{request.host_url.rstrip('/')}/events/{eid}")

    return _reply('That action is not available yet.')


def _handle_modal(body):
    """Handle a submitted message form for announce/invite — post/DM with the note."""
    custom_id = (body.get('data') or {}).get('custom_id') or ''
    discord_id, name = _interaction_user(body)
    message = _modal_text(body)

    if custom_id.startswith('cbp_announce_modal:'):
        from routes.events import announce_event_to_channel
        eid = custom_id.split(':', 1)[1]
        channel_id = body.get('channel_id') or (body.get('channel') or {}).get('id')
        ename, posted = announce_event_to_channel(eid, channel_id, request.host_url, message)
        if ename is None:
            return _reply('That event no longer exists.')
        if not posted:
            return _reply("I couldn't post here — check that I have permission to send "
                          "messages in this channel.")
        return _reply(f"✅ Posted **{ename}** in this channel with a Register button. "
                      "It'll update automatically when registration fills or closes.")

    if custom_id.startswith('cbp_invite_modal:'):
        from routes.events import invite_player_via_discord
        _, target_id, eid = custom_id.split(':', 2)
        msg, err = invite_player_via_discord(
            eid, discord_id, target_id, name, request.host_url, message)
        return _reply(f'⚠️ {err}' if err else f'✅ {msg}')

    return _reply('That action is not available yet.')


def _update(content: str, event_url: str = ''):
    """Edit the (ephemeral) message a component is on, clearing its controls. When
    `event_url` is given, keep a single 'View details' link button so the player
    can jump to the event page from the confirmation."""
    components = ([{'type': ACTION_ROW, 'components': [
        {'type': BUTTON, 'style': 5, 'label': 'View details', 'url': event_url}]}]
        if event_url else [])
    return jsonify({'type': UPDATE_MESSAGE, 'data': {'content': content, 'components': components}})


def _reported(content: str, event_url: str = ''):
    """Edit the result-entry message after a successful report: swap the buttons
    for a greyed-out, disabled 'Result reported' button so it's clear the result is
    in and can't be entered again, keeping the 'View on the web' link alongside it."""
    row = [{'type': BUTTON, 'style': 2, 'label': 'Result reported',
            'custom_id': 'cbp_reported_noop', 'disabled': True}]
    if event_url:
        row.append({'type': BUTTON, 'style': 5, 'label': 'View on the web', 'url': event_url})
    return jsonify({'type': UPDATE_MESSAGE, 'data': {
        'content': content,
        'components': [{'type': ACTION_ROW, 'components': row}]}})
