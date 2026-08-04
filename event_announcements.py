"""
Event announcement layer — posting and refreshing Discord event cards.
No Flask context: pure functions over plain dicts.
"""
import discord_api
from db import get_event, save_event
from event_state import _self_registration_blocked


def registration_card_status(event: dict):
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


def announce_event_to_channel(event_id: str, channel_id: str, base_url: str, message: str = '',
                              mention_role_id: str = None):
    """Post an event card (Register button + details link) to a channel and
    remember the message so its status can be kept current. `message` is an
    optional organiser note posted above the card; `mention_role_id` pings that
    role above the card. Returns (event_name, posted) — posted is False if the
    event is gone or the post failed (e.g. missing channel permission)."""
    e = get_event(event_id)
    if not e:
        return None, False
    base = (base_url or '').rstrip('/')
    state, note = registration_card_status(e)
    embeds, components = discord_api.event_card(e, f'{base}/events/{event_id}', state, note)
    content = message or ''
    allowed = None
    if mention_role_id:
        content = (f'<@&{mention_role_id}> ' + content).rstrip()
        allowed = {'roles': [str(mention_role_id)]}
    msg = discord_api.post_message(channel_id, content=(content or None),
                                   components=components, embeds=embeds, allowed_mentions=allowed)
    if not msg:
        return e.get('name', 'the event'), False
    save_event(event_id, {'discord_announce': {
        'channel_id': channel_id, 'message_id': msg.get('id'), 'base_url': base,
        'message': content}})
    return e.get('name', 'the event'), True


def refresh_event_announcement(event: dict) -> None:
    """If this event has an announcement card posted, edit it to reflect the
    current registration status (open / full / closed). Best-effort no-op when
    there's no card. `event` must reflect the current players/registration."""
    ann = event.get('discord_announce') or {}
    if not ann.get('message_id'):
        return
    base = (ann.get('base_url') or '').rstrip('/')
    state, note = registration_card_status(event)
    embeds, components = discord_api.event_card(
        event, f"{base}/events/{event['id']}", state, note)
    discord_api.edit_message(ann['channel_id'], ann['message_id'],
                             content=(ann.get('message') or None),
                             components=components, embeds=embeds)
