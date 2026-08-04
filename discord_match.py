"""
Discord match layer — open-match lookup and result reporting for Discord interactions.
No Flask context: pure functions over plain dicts.
"""
import discord_api
from db import get_event, save_event, add_event_log, list_events
from event_state import _validate_result, _now_iso
from swiss import DRAW_RESULTS
from discord_identity import resolve_discord_identity, normalize_handle


def _log_discord(event_id: str, actor_name: str, action: str, detail: str = ''):
    add_event_log(event_id, {'at': _now_iso(), 'action': action, 'detail': detail,
                             'actor_id': '', 'actor_name': actor_name or 'A Discord user'})


def _discord_match_ctx(e: dict, round_idx: int, match_idx: int, discord_id: str,
                       gid=None, handles=None):
    """Context for a Discord user's match in event `e`, or None if it's not their
    open (unreported, non-bye) match. Shared by the report picker and reporting.
    `gid` is the Google account linked to this Discord user (if known) and
    `handles` the verified handle candidates, so a web/organiser-added player who's
    linked their Discord OR just has a matching handle on file is also matched."""
    rounds = e.get('rounds', [])
    if not (0 <= round_idx < len(rounds)):
        return None
    rnd = rounds[round_idx]
    if not (0 <= match_idx < len(rnd)):
        return None
    m = rnd[match_idx]
    player = next((p for p in e['players']
                   if not p.get('dropped') and
                      (p.get('discord_id') == discord_id
                       or (gid and p.get('google_id') == gid)
                       or (handles and normalize_handle(p.get('discord')) in handles))),
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
        'best_of': e.get('best_of', 3),
    }


def discord_open_matches(discord_id: str, username: str = '', display: str = '', limit: int = 25):
    """A Discord user's current open matches (latest round of each event they're
    in), for the /cbp report picker."""
    out = []
    gid, handles = resolve_discord_identity(discord_id, username, display)
    for e in list_events():
        ridx = len(e.get('rounds', [])) - 1
        if ridx < 0:
            continue
        for midx in range(len(e['rounds'][ridx])):
            ctx = _discord_match_ctx(e, ridx, midx, discord_id, gid, handles)
            if ctx:
                out.append(ctx)
                break
    return out[:limit]


def discord_match_context(event_id: str, round_idx: int, match_idx: int, discord_id: str,
                          username: str = '', display: str = ''):
    e = get_event(event_id)
    if not e:
        return None
    gid, handles = resolve_discord_identity(discord_id, username, display)
    return _discord_match_ctx(e, round_idx, match_idx, discord_id, gid, handles)


# Map a reporter-perspective result code to a stored result + summary.
# Tuples are (kind, my_games, their_games) or (kind, my_games, their_games, draws).
_DISCORD_RESULT_CODES = {
    'w20':  ('win',  2, 0),    'w21':  ('win',  2, 1),
    'l02':  ('lose', 0, 2),    'l12':  ('lose', 1, 2),
    'w10':  ('win',  1, 0),    'l01':  ('lose', 0, 1),
    'w101': ('win',  1, 0, 1), 'l011': ('lose', 0, 1, 1),
    'draw': ('draw', None, None), 'id': ('id', None, None),
}
_BO3_CODES = frozenset({'w20', 'w21', 'l02', 'l12', 'draw', 'id', 'w101', 'l011'})
_BO1_CODES = frozenset({'w10', 'l01', 'draw'})


def report_result_via_discord(event_id, round_idx, match_idx, discord_id, code, base_url='',
                              username='', display=''):
    """Record a result a Discord player reports for their own match. `code` is
    from the reporter's perspective (see _DISCORD_RESULT_CODES). Returns
    (confirmation, None) or (None, error)."""
    e = get_event(event_id)
    if not e:
        return None, 'That event no longer exists.'
    gid, handles = resolve_discord_identity(discord_id, username, display)
    ctx = _discord_match_ctx(e, round_idx, match_idx, discord_id, gid, handles)
    if not ctx:
        return None, "That doesn't look like an open match of yours anymore."
    spec = _DISCORD_RESULT_CODES.get(code)
    if not spec:
        return None, 'Unknown result.'
    best_of = e.get('best_of', 3)
    valid_codes = _BO1_CODES if best_of == 1 else _BO3_CODES
    if code not in valid_codes:
        return None, 'That result is not valid for this event format.'
    kind, mine, theirs, *rest = spec
    draws = rest[0] if rest else 0
    if kind == 'draw':
        winner_id, result, summary = None, 'draw', 'a draw (1–1)'
    elif kind == 'id':
        if not ctx['allow_id']:
            return None, 'Intentional draws are not allowed for this event.'
        winner_id, result, summary = None, '0-0-3', 'an intentional draw (0–0–3)'
    else:
        winner_id = ctx['player_id'] if kind == 'win' else ctx['opp_id']
        a, b = (mine, theirs) if ctx['is_p1'] else (theirs, mine)
        result = f'{a}-{b}-{draws}' if draws else f'{a}-{b}'
        hi, lo = max(mine, theirs), min(mine, theirs)
        score_str = f'{hi}–{lo}–{draws}' if draws else f'{hi}–{lo}'
        summary = f"you {'won' if kind == 'win' else 'lost'} {score_str}"
    err = _validate_result(e['rounds'][round_idx][match_idx], winner_id, result, best_of)
    if err:
        return None, err
    m = e['rounds'][round_idx][match_idx]
    m['winner_id'] = winner_id
    m['result'] = result
    save_event(event_id, {'rounds': e['rounds']})
    discord_api.notify_result(e, round_idx, match_idx, base_url,
                              exclude_player_id=ctx['player_id'])
    names = {p['id']: p['name'] for p in e['players']}
    reporter = names.get(ctx['player_id'], display or 'A player')
    _log_discord(event_id, reporter, 'result',
                 f"reported round {round_idx + 1} vs {ctx['opponent']} ({result}) via Discord")
    return f"Recorded — {summary} vs {ctx['opponent']} ({ctx['event_name']}). GGs!", None
