"""
Semantic query functions over the event collection.

Each function encapsulates a call to db.list_events() plus any filter predicate,
giving callers a named, testable interface and a single place to swap in a
server-side Firestore query if needed.

Callers should import from here rather than calling list_events() directly with
inline filters.
"""
from db import list_events
from event_state import _self_registration_blocked, _is_full, _event_complete


# ── Predicates ────────────────────────────────────────────────────────────────

def is_decklist_public(event: dict) -> bool:
    """True when this event's decklists are publicly visible:
    requires_decklists is set, and either the event is complete or decklists have
    been explicitly released (but not still closed without a release)."""
    if not event.get('requires_decklists'):
        return False
    closed = bool(event.get('closed_decklists'))
    released = bool(event.get('decklists_released'))
    if closed and not released:
        return False
    if not closed and not _event_complete(event):
        return False
    return True


# ── Query functions ───────────────────────────────────────────────────────────

def public_decklist_events() -> list[dict]:
    """Events whose decklists are publicly visible."""
    return [e for e in list_events() if is_decklist_public(e)]


def events_for_player(google_id: str) -> list[dict]:
    """All events a Google account has participated in, newest first."""
    return sorted(
        [e for e in list_events()
         if any(p.get('google_id') == google_id for p in e.get('players', []))],
        key=lambda e: e.get('date', ''),
        reverse=True,
    )


def registerable_for_discord(owner_gid: str = None, include_full: bool = False,
                              limit: int = 25) -> list[dict]:
    """Events open for Discord self-registration.

    `owner_gid` (Google ID) restricts to events owned by that organiser.
    Full events are excluded unless `include_full=True` (used for announce/invite
    pickers where a waitlist button is still valid). Soonest first.
    """
    out = []
    for e in list_events():
        if owner_gid and e.get('owner_id') != owner_gid:
            continue
        if e.get('test_mode') or e.get('entry_code'):
            continue
        if _self_registration_blocked(e):
            continue
        if not include_full and _is_full(e):
            continue
        out.append(e)
    out.sort(key=lambda e: e.get('date', ''))
    return out[:limit]


def linkable_for_discord(owner_gid: str = None, limit: int = 25) -> list[dict]:
    """Non-test events an organiser can link to a Discord channel; newest first.

    `owner_gid` restricts to events owned by that organiser.
    """
    out = [e for e in list_events()
           if not e.get('test_mode')
           and (not owner_gid or e.get('owner_id') == owner_gid)]
    out.sort(key=lambda e: e.get('date', ''), reverse=True)
    return out[:limit]


def with_standings(limit: int = 25) -> list[dict]:
    """Non-test events that have at least one round (i.e. have standings); newest first."""
    out = [e for e in list_events() if e.get('rounds') and not e.get('test_mode')]
    out.sort(key=lambda e: e.get('date', ''), reverse=True)
    return out[:limit]
