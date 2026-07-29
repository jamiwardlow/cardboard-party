"""
Shared event-state helpers — pure predicates and utilities over the event dict.
Imported by both routes/events.py and discord_actions.py, so they live here to
avoid a circular import between those two modules.
"""
import datetime
import re

from swiss import DRAW_RESULTS, default_num_rounds


def _now_iso() -> str:
    """Current UTC time as an ISO string (used to stamp round-timer starts)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def _active_count(event: dict) -> int:
    """Number of active (non-dropped) participants — what counts against the cap."""
    return len([p for p in event.get('players', []) if not p.get('dropped')])


def _is_full(event: dict) -> bool:
    """True when a cap is set and active participants have reached it."""
    cap = event.get('registration_cap', 0)
    return bool(cap) and _active_count(event) >= cap


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


def _assign_draft_seat(player: dict, event: dict) -> None:
    """Stamp the next available seat number onto player if this is a Draft event."""
    if (event.get('format') or '').lower() != 'draft':
        return
    used = {p['seat'] for p in event['players'] if isinstance(p.get('seat'), int)}
    seat = 1
    while seat in used:
        seat += 1
    player['seat'] = seat


def _is_bracket_round(rnd: list) -> bool:
    """A round belongs to the single-elimination playoff if its matches are tagged."""
    return bool(rnd) and rnd[0].get('stage') == 'bracket'


def _swiss_complete(event: dict) -> bool:
    """True once every Swiss round has been paired and fully scored."""
    swiss = [r for r in event['rounds'] if not _is_bracket_round(r)]
    num_rounds = event.get('num_rounds') or default_num_rounds(len(event['players']))
    if len(swiss) < num_rounds:
        return False
    last = swiss[-1] if swiss else []
    return all(m.get('is_bye') or m.get('winner_id') or m.get('result') in DRAW_RESULTS
               for m in last)


def _event_complete(event: dict) -> bool:
    """True once the event is finished — a decided playoff final, an explicit
    'finished' status, or all Swiss rounds paired and fully scored."""
    rounds = event.get('rounds') or []
    if not rounds:
        return False
    last = rounds[-1]
    if _is_bracket_round(last):
        return len(last) == 1 and bool(last[0].get('winner_id'))
    if event.get('status') == 'finished':
        return True
    return _swiss_complete(event)


_RESULT_RE = re.compile(r'^(\d+)-(\d+)$')


def _validate_result(match: dict, winner_id, result, best_of: int = 3) -> str | None:
    """Validate a reported result against a match. Returns an error string, or None
    if valid. Enforces that the winner matches the score (result is recorded from
    player1's perspective, i.e. 'p1games-p2games'), so a player can't report a
    score for one player while crediting the win to the other."""
    p1, p2 = match.get('player1_id'), match.get('player2_id')
    if result in DRAW_RESULTS:
        return None if winner_id is None else 'A draw cannot have a winner'
    m = _RESULT_RE.match(str(result or ''))
    if not m:
        return 'Invalid result format'
    a, b = int(m.group(1)), int(m.group(2))
    if a == b:
        return 'Use a draw for an equal score'
    wins_needed = best_of // 2 + 1  # 2 for BO3, 1 for BO1
    if max(a, b) != wins_needed:
        return f'Invalid score for best-of-{best_of}'
    expected = p1 if a > b else p2
    if winner_id != expected:
        return 'Winner does not match the score'
    return None
