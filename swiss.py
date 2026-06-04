"""
Swiss pairing algorithm for Cardboard Party.

Given a list of players with their match history, generates pairings for the
next round following Swiss rules:
  - Players are sorted by points (descending)
  - Players with equal points are paired against each other where possible
  - No player faces the same opponent twice
  - If there's an odd number of active players, the lowest-ranked player
    who hasn't yet received a bye gets a bye (counts as a win)
"""

import math
from typing import Optional


BYE_PLAYER_ID = '__bye__'


def pair_round(players: list[dict], rounds: list[list[dict]]) -> list[dict]:
    """
    Generate pairings for the next round.

    Args:
        players: list of player dicts, each with at least:
                 { 'id': str, 'name': str, 'dropped': bool }
        rounds:  list of previous rounds, each a list of match dicts:
                 { 'player1_id': str, 'player2_id': str,
                   'winner_id': str | None,   # None = not yet played / draw
                   'is_bye': bool }

    Returns:
        list of match dicts for the new round (winner_id and result left empty)
    """
    active = [p for p in players if not p.get('dropped', False)]

    points   = _compute_points(active, rounds)
    bye_hist = _bye_history(rounds)
    opp_hist = _opponent_history(rounds)

    # Sort by points desc, then by name for determinism
    active.sort(key=lambda p: (-points[p['id']], p['name']))

    # Handle bye for odd player count
    bye_player_id: Optional[str] = None
    if len(active) % 2 == 1:
        bye_player_id = _choose_bye(active, points, bye_hist)
        active = [p for p in active if p['id'] != bye_player_id]

    pairings = _pair(active, points, opp_hist)

    if bye_player_id:
        pairings.append({
            'player1_id': bye_player_id,
            'player2_id': BYE_PLAYER_ID,
            'winner_id':  bye_player_id,
            'result':     '2-0-0',
            'is_bye':     True,
        })

    return pairings


def _pair(players: list[dict], points: dict, opp_hist: dict) -> list[dict]:
    """
    Greedy Swiss pairing: iterate down the sorted list, pair each unpaired
    player with the highest-ranked unpaired player they haven't faced.
    Falls back to allowing repeat matches if necessary (shouldn't happen in
    short tournaments).
    """
    unpaired = list(players)
    pairings  = []

    while len(unpaired) >= 2:
        p1 = unpaired.pop(0)
        paired = False

        for i, p2 in enumerate(unpaired):
            if p2['id'] not in opp_hist.get(p1['id'], set()):
                unpaired.pop(i)
                pairings.append(_make_pairing(p1, p2))
                paired = True
                break

        if not paired:
            # Fallback: pair with next player regardless of history
            p2 = unpaired.pop(0)
            pairings.append(_make_pairing(p1, p2))

    return pairings


def _make_pairing(p1: dict, p2: dict) -> dict:
    return {
        'player1_id': p1['id'],
        'player2_id': p2['id'],
        'winner_id':  None,
        'result':     None,
        'is_bye':     False,
    }


def _choose_bye(players: list[dict], points: dict, bye_hist: set) -> str:
    """
    Pick the bye recipient: lowest-ranked player who hasn't had a bye.
    If everyone has had a bye, pick the lowest-ranked overall.
    """
    eligible = [p for p in players if p['id'] not in bye_hist]
    pool = eligible if eligible else players
    # lowest-ranked = last in points-sorted list
    pool_sorted = sorted(pool, key=lambda p: (points[p['id']], p['name']))
    return pool_sorted[0]['id']


def _compute_points(players: list[dict], rounds: list[list[dict]]) -> dict:
    pts = {p['id']: 0 for p in players}
    for rnd in rounds:
        for match in rnd:
            if match.get('is_bye'):
                pts[match['player1_id']] = pts.get(match['player1_id'], 0) + 3
            elif match.get('winner_id'):
                pts[match['winner_id']] = pts.get(match['winner_id'], 0) + 3
            elif match.get('result') == 'draw':
                pts[match['player1_id']] = pts.get(match['player1_id'], 0) + 1
                pts[match['player2_id']] = pts.get(match['player2_id'], 0) + 1
    return pts


def _bye_history(rounds: list[list[dict]]) -> set:
    """Returns the set of player IDs who have already received a bye."""
    had_bye = set()
    for rnd in rounds:
        for match in rnd:
            if match.get('is_bye'):
                had_bye.add(match['player1_id'])
    return had_bye


def _opponent_history(rounds: list[list[dict]]) -> dict:
    """Returns {player_id: set(opponent_ids)} across all previous rounds."""
    hist: dict = {}
    for rnd in rounds:
        for match in rnd:
            p1, p2 = match['player1_id'], match['player2_id']
            if p2 == BYE_PLAYER_ID:
                continue
            hist.setdefault(p1, set()).add(p2)
            hist.setdefault(p2, set()).add(p1)
    return hist


def compute_standings(players: list[dict], rounds: list[list[dict]]) -> list[dict]:
    """
    Returns player standings sorted by:
      1. Match points (win=3, draw=1, loss/bye=0 for opponent)
      2. Opponent match win % (OMW)
      3. Game win % (GW)
      4. Opponent game win % (OGW)

    Each entry: { id, name, points, omw, gw, ogw }
    """
    active = [p for p in players if not p.get('dropped', False)]
    points = _compute_points(active, rounds)

    game_wins   = {p['id']: 0 for p in active}
    game_losses = {p['id']: 0 for p in active}

    for rnd in rounds:
        for match in rnd:
            if match.get('is_bye') or not match.get('result'):
                continue
            try:
                w, l, d = map(int, match['result'].split('-'))
            except (ValueError, AttributeError):
                continue
            p1, p2 = match['player1_id'], match['player2_id']
            game_wins[p1]   = game_wins.get(p1, 0)   + w
            game_losses[p1] = game_losses.get(p1, 0)  + l
            game_wins[p2]   = game_wins.get(p2, 0)    + l
            game_losses[p2] = game_losses.get(p2, 0)  + w

    opp_hist = _opponent_history(rounds)

    # A player has "played" once they have a recorded result or a bye; until
    # then their tiebreakers are meaningless and shown as '—' (None).
    played = {p['id']: 0 for p in active}
    for rnd in rounds:
        for match in rnd:
            if match.get('is_bye'):
                played[match['player1_id']] = played.get(match['player1_id'], 0) + 1
            elif match.get('result'):
                for key in ('player1_id', 'player2_id'):
                    played[match[key]] = played.get(match[key], 0) + 1

    def gw_pct(pid):
        w = game_wins.get(pid, 0)
        total = w + game_losses.get(pid, 0)
        return max(w / total, 1/3) if total else 1/3

    def omw_pct(pid):
        opps = opp_hist.get(pid, set())
        if not opps:
            return 1/3
        return sum(max(points.get(o, 0) / max(len(rounds) * 3, 1), 1/3) for o in opps) / len(opps)

    def ogw_pct(pid):
        opps = opp_hist.get(pid, set())
        if not opps:
            return 1/3
        return sum(gw_pct(o) for o in opps) / len(opps)

    standings = []
    for p in active:
        pid = p['id']
        has_played = played.get(pid, 0) > 0
        standings.append({
            'id':   pid,
            'name': p['name'],
            'points': points.get(pid, 0),
            'omw':  round(omw_pct(pid), 4) if has_played else None,
            'gw':   round(gw_pct(pid), 4) if has_played else None,
            'ogw':  round(ogw_pct(pid), 4) if has_played else None,
        })

    standings.sort(key=lambda s: (-s['points'], -(s['omw'] or 0),
                                  -(s['gw'] or 0), -(s['ogw'] or 0)))
    return standings


def default_num_rounds(num_players: int) -> int:
    """Standard Swiss round count: ceil(log2(players))."""
    if num_players < 2:
        return 1
    return math.ceil(math.log2(num_players))
