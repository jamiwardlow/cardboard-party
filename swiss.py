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
import random
from typing import Optional


BYE_PLAYER_ID = '__bye__'

# Result values that count as a drawn match (1 match point each, no game wins):
# 'draw' = an ordinary 1–1 draw; '0-0-3' = an intentional draw (Advanced events).
DRAW_RESULTS = ('draw', '0-0-3')


def pair_round(players: list[dict], rounds: list[list[dict]], shuffle: bool = False,
               best_of: int = 3) -> list[dict]:
    """
    Generate pairings for the next round.

    Args:
        players: list of player dicts, each with at least:
                 { 'id': str, 'name': str, 'dropped': bool }
        rounds:  list of previous rounds, each a list of match dicts:
                 { 'player1_id': str, 'player2_id': str,
                   'winner_id': str | None,   # None = not yet played / draw
                   'is_bye': bool }
        best_of: 1 or 3 — controls the bye result score (1-0-0 vs 2-0-0)

    Returns:
        list of match dicts for the new round (winner_id and result left empty)
    """
    active = [p for p in players if not p.get('dropped', False)]

    points   = _compute_points(active, rounds)
    bye_hist = _bye_history(rounds)
    opp_hist = _opponent_history(rounds)

    # Sort by points desc. Normally tie-break by name for a deterministic, stable
    # pairing; on a re-pair, shuffle first so equal-point players get a *different*
    # valid pairing (sort is stable, so the random order survives within a bracket).
    if shuffle:
        random.shuffle(active)
        active.sort(key=lambda p: -points[p['id']])
    else:
        active.sort(key=lambda p: (-points[p['id']], p['name']))

    # Handle bye for odd player count
    bye_player_id: Optional[str] = None
    if len(active) % 2 == 1:
        bye_player_id = _choose_bye(active, points, bye_hist)
        active = [p for p in active if p['id'] != bye_player_id]

    pairings = _pair(active, points, opp_hist)

    if bye_player_id:
        wins_needed = best_of // 2 + 1
        pairings.append({
            'player1_id': bye_player_id,
            'player2_id': BYE_PLAYER_ID,
            'winner_id':  bye_player_id,
            'result':     f'{wins_needed}-0-0',
            'is_bye':     True,
            'table':      None,   # byes are never seated at a table
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
        'table':      None,   # filled in by assign_tables() when tables are enabled
    }


def assign_tables(matches: list[dict], players: list[dict], settings: dict) -> list[dict]:
    """Set each match's 'table' in place from the event's table settings.

    `settings` carries tables_enabled / table_start / table_end / tables_excluded /
    table_labels (the event doc works directly). Rules:
      - Byes never get a table (None).
      - A player with a positive int 'fixed_table' keeps that exact table every
        round; it's removed from the auto pool so nothing else lands on it.
      - Remaining matches fill the range [start..end] in pairing order (which
        already tracks standings), skipping reserved (excluded) and labeled tables
        (labeled tables are placed manually) and any fixed tables.
      - If tables are off, the range is unset/invalid, or the pool runs out, the
        affected matches get table=None (the UI shows "Table TBD").
    """
    start = settings.get('table_start') or 1
    end = settings.get('table_end') or 0
    if not settings.get('tables_enabled') or not end or end < start:
        for m in matches:
            m['table'] = None
        return matches

    excluded = {int(n) for n in (settings.get('tables_excluded') or [])}
    labeled = {int(k) for k in (settings.get('table_labels') or {})}
    fixed = {p['id']: p['fixed_table'] for p in players
             if isinstance(p.get('fixed_table'), int) and p['fixed_table'] > 0}
    claimed = set(fixed.values())

    pool = [n for n in range(start, end + 1)
            if n not in excluded and n not in labeled and n not in claimed]
    nxt = iter(pool)
    for m in matches:
        if m.get('is_bye'):
            m['table'] = None
            continue
        # Honour a fixed seat. If BOTH players are fixed to different tables, the
        # lower-numbered table wins (venue priority) — the organiser is alerted to
        # the clash in the UI and can override it manually.
        ft1, ft2 = fixed.get(m.get('player1_id')), fixed.get(m.get('player2_id'))
        ft = min(ft1, ft2) if (ft1 and ft2) else (ft1 or ft2)
        m['table'] = ft if ft else next(nxt, None)
    return matches


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
            elif match.get('result') in DRAW_RESULTS:
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
    # Dropped players remain in the standings (ranked by their record at drop
    # time). Computing over every player also keeps opponents' tiebreakers
    # accurate — a dropped opponent's points/games still count toward OMW/OGW.
    ranked = list(players)
    points = _compute_points(ranked, rounds)

    game_wins   = {p['id']: 0 for p in ranked}
    game_losses = {p['id']: 0 for p in ranked}

    for rnd in rounds:
        for match in rnd:
            result = match.get('result')
            # Decisive results are stored as a two-part 'p1games-p2games'
            # score (e.g. '2-1'). Byes and draws (ordinary or intentional) carry
            # no per-game split, so they don't contribute to game win %.
            if match.get('is_bye') or not result or result in DRAW_RESULTS:
                continue
            parts = str(result).split('-')
            try:
                w, l = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                continue
            p1, p2 = match['player1_id'], match['player2_id']
            game_wins[p1]   = game_wins.get(p1, 0)   + w
            game_losses[p1] = game_losses.get(p1, 0)  + l
            game_wins[p2]   = game_wins.get(p2, 0)    + l
            game_losses[p2] = game_losses.get(p2, 0)  + w

    opp_hist = _opponent_history(rounds)

    # A player has "played" once they have a recorded result or a bye; until
    # then their tiebreakers are meaningless and shown as '—' (None).
    played = {p['id']: 0 for p in ranked}
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
    for p in ranked:
        pid = p['id']
        has_played = played.get(pid, 0) > 0
        standings.append({
            'id':   pid,
            'name': p['name'],
            'points': points.get(pid, 0),
            'omw':  round(omw_pct(pid), 4) if has_played else None,
            'gw':   round(gw_pct(pid), 4) if has_played else None,
            'ogw':  round(ogw_pct(pid), 4) if has_played else None,
            'dropped': bool(p.get('dropped', False)),
        })

    standings.sort(key=lambda s: (-s['points'], -(s['omw'] or 0),
                                  -(s['gw'] or 0), -(s['ogw'] or 0)))
    return standings


def player_match_record(player_id: str, rounds: list[list[dict]], *, count_byes: bool = False) -> tuple:
    """Returns (wins, losses, draws) for a player across all rounds.

    Byes are excluded by default. Pass count_byes=True to count a bye as a win,
    which is appropriate for player-facing win/loss display.
    """
    wins = losses = draws = 0
    for rnd in rounds:
        for match in rnd:
            if match.get('is_bye'):
                if count_byes and match.get('player1_id') == player_id:
                    wins += 1
                continue
            if player_id not in (match['player1_id'], match['player2_id']):
                continue
            result = match.get('result')
            if not result:
                continue
            if result in DRAW_RESULTS or match.get('winner_id') is None:
                draws += 1
            elif match.get('winner_id') == player_id:
                wins += 1
            else:
                losses += 1
    return wins, losses, draws


def default_num_rounds(num_players: int) -> int:
    """Standard Swiss round count: ceil(log2(players))."""
    if num_players < 2:
        return 1
    return math.ceil(math.log2(num_players))


def id_safe_players(players: list[dict], rounds: list[list[dict]],
                    num_rounds: int, cut_size: int) -> set:
    """Player ids who can intentionally draw their current match and still be
    GUARANTEED a Top-`cut_size` finish. Only applies during the final Swiss round
    of an event with a top cut; returns an empty set otherwise.

    A player is "safe" only if fewer than cut_size *other* players can possibly
    finish with at least the player's post-draw points — so at most cut_size-1
    can sit at or above them, a locked top-cut slot. This is deliberately
    conservative: pairing constraints (rivals play each other, so they can't all
    win) and same-point tiebreakers can only help the player, never hurt, so a
    "safe" answer never misleads.
    """
    if not cut_size or not rounds:
        return set()
    swiss = [r for r in rounds if not (r and r[0].get('stage') == 'bracket')]
    if not swiss or len(swiss) != num_rounds:   # only the final Swiss round
        return set()
    active = [p for p in players if not p.get('dropped')]
    if len(active) <= cut_size:
        return {p['id'] for p in active}         # whole field makes the cut

    last = swiss[-1]
    points = _compute_points(active, swiss[:-1])  # standing before this round
    opponent = {}
    for m in last:
        if m.get('is_bye'):
            continue
        opponent[m['player1_id']] = m['player2_id']
        opponent[m['player2_id']] = m['player1_id']

    safe = set()
    for p in active:
        pid = p['id']
        # Needs a still-open (no result, non-bye) match this round to draw.
        match = next((m for m in last
                      if not m.get('is_bye')
                      and pid in (m.get('player1_id'), m.get('player2_id'))), None)
        if not match or match.get('winner_id') or match.get('result') in DRAW_RESULTS:
            continue
        p_final = points.get(pid, 0) + 1          # a draw adds 1 point
        opp_id  = opponent.get(pid)
        ahead = 0
        for q in active:
            if q['id'] == pid:
                continue
            # P's opponent must also draw (a draw is mutual) → +1; anyone else
            # could win their match → +3.
            gain = 1 if q['id'] == opp_id else 3
            if points.get(q['id'], 0) + gain >= p_final:
                ahead += 1
        if ahead < cut_size:
            safe.add(pid)
    return safe


# ── Draft pod pairing ───────────────────────────────────────────────────────────

def pair_draft_r2(players: list[dict], rounds: list[list[dict]],
                  best_of: int = 3) -> list[dict]:
    """Round 2 pairing for a Draft event.

    Odd-seated round-1 pairs compete within their group: winner(1v5) plays
    winner(3v7), loser(1v5) plays loser(3v7). Even-seated groups do the same
    (winner(2v6) vs winner(4v8), etc.). Any players not covered by a complete
    group pairing (e.g. from drops or non-standard pod sizes) fall through to
    standard Swiss.
    """
    active = [p for p in players if not p.get('dropped')]
    active_ids = {p['id'] for p in active}
    seat_of = {p['id']: p.get('seat', 0) for p in players}
    player_by_id = {p['id']: p for p in players}
    r1 = rounds[0]
    points = _compute_points(active, rounds)
    opp_hist = _opponent_history(rounds)
    bye_hist = _bye_history(rounds)

    # Build sorted list of round-1 non-bye match results
    r1_summaries = []
    for match in r1:
        if match.get('is_bye'):
            continue
        p1, p2 = match['player1_id'], match['player2_id']
        s1, s2 = seat_of.get(p1, 0), seat_of.get(p2, 0)
        lower = min(s1, s2)
        winner = match.get('winner_id')
        # For draws or unresolved matches, treat p1 as winner (arbitrary but consistent)
        w = winner if winner in (p1, p2) else p1
        l = p2 if w == p1 else p1
        r1_summaries.append({'lower_seat': lower, 'winner': w, 'loser': l})
    r1_summaries.sort(key=lambda m: m['lower_seat'])

    odd_g = [m for m in r1_summaries if m['lower_seat'] % 2 == 1]
    even_g = [m for m in r1_summaries if m['lower_seat'] % 2 == 0]

    pairings: list[dict] = []
    covered: set = set()

    for group in (odd_g, even_g):
        for i in range(0, len(group) - 1, 2):
            m1, m2 = group[i], group[i + 1]
            ids = [m1['winner'], m1['loser'], m2['winner'], m2['loser']]
            if not all(pid in active_ids for pid in ids):
                continue  # a player dropped; leave them for the fallback
            pairings.append(_make_pairing(player_by_id[m1['winner']], player_by_id[m2['winner']]))
            pairings.append(_make_pairing(player_by_id[m1['loser']], player_by_id[m2['loser']]))
            covered.update(ids)

    remaining = [p for p in active if p['id'] not in covered]
    if remaining:
        if len(remaining) % 2 == 1:
            bye_id = _choose_bye(remaining, points, bye_hist)
            remaining = [p for p in remaining if p['id'] != bye_id]
            wins_needed = best_of // 2 + 1
            pairings.append({
                'player1_id': bye_id, 'player2_id': BYE_PLAYER_ID,
                'winner_id': bye_id, 'result': f'{wins_needed}-0-0',
                'is_bye': True, 'table': None,
            })
        remaining.sort(key=lambda p: (-points[p['id']], p['name']))
        pairings.extend(_pair(remaining, points, opp_hist))

    return pairings


def pair_draft_r1(players: list[dict], bracket: bool = False,
                  best_of: int = 3) -> list[dict]:
    """First-round pairing for a Draft event.

    Players must already have a 'seat' integer (1..n).  Pairs seat k vs seat
    (half + k), giving top-half-of-pod vs bottom-half: 1v5, 2v6, 3v7, 4v8 for
    an 8-player pod.  If bracket is True, matches are tagged stage='bracket' for
    single-elimination advancement.  An odd player at the end gets a bye.
    """
    active = sorted([p for p in players if not p.get('dropped')],
                    key=lambda p: p.get('seat', 0))
    n = len(active)
    half = n // 2
    pairings = []
    for i in range(half):
        m = _make_pairing(active[i], active[i + half])
        if bracket:
            m['stage'] = 'bracket'
        pairings.append(m)
    if n % 2 == 1:
        bye_p = active[-1]
        wins_needed = best_of // 2 + 1
        pairings.append({
            'player1_id': bye_p['id'],
            'player2_id': BYE_PLAYER_ID,
            'winner_id':  bye_p['id'],
            'result':     f'{wins_needed}-0-0',
            'is_bye':     True,
            'table':      None,
        })
    return pairings


# ── Single-elimination playoff bracket ──────────────────────────────────────────
#
# After Swiss, the organiser may "cut to top N" (4, 8, or 16): the top N players
# by standings are seeded into a single-elimination bracket. Bracket matches are
# ordinary match dicts tagged with stage == 'bracket' and appended to `rounds`,
# so the rest of the app treats them like any other round.

CUT_SIZES = (4, 8, 16)


def _bracket_match(p1_id: str, p2_id: str) -> dict:
    return {'player1_id': p1_id, 'player2_id': p2_id,
            'winner_id': None, 'result': None, 'is_bye': False,
            'stage': 'bracket', 'table': None}


def bracket_seed_order(n: int) -> list[int]:
    """Standard single-elimination seeding slots for a bracket of size n.

    Returns seed numbers in slot order so that top seeds only meet late and
    each round's adjacent winners feed the next round. e.g. n=8 ->
    [1, 8, 4, 5, 2, 7, 3, 6], giving first-round matches (1,8) (4,5) (2,7) (3,6).
    """
    order = [1]
    while len(order) < n:
        size = len(order) * 2
        order = [x for s in order for x in (s, size + 1 - s)]
    return order


def make_bracket(standings: list[dict], cut_size: int) -> tuple[list[dict], dict]:
    """Build the first bracket round for the top `cut_size` players.

    Args:
        standings: output of compute_standings (sorted best-first; carries a
                   'dropped' flag). Dropped players are skipped.
        cut_size:  one of CUT_SIZES.

    Returns (matches, seeds) where seeds maps player_id -> seed number (1-based).
    """
    seeded = [s for s in standings if not s.get('dropped')][:cut_size]
    seeds = {s['id']: i + 1 for i, s in enumerate(seeded)}
    by_seed = {seed: pid for pid, seed in seeds.items()}
    order = bracket_seed_order(cut_size)
    matches = [_bracket_match(by_seed[order[i]], by_seed[order[i + 1]])
               for i in range(0, len(order), 2)]
    return matches, seeds


def next_bracket_round(prev_round: list[dict]) -> list[dict]:
    """Pair the winners of a completed bracket round, preserving bracket order.

    Every match in prev_round must already have a winner_id (draws must be
    resolved first — single elimination needs someone to advance).
    """
    winners = [m['winner_id'] for m in prev_round]
    return [_bracket_match(winners[i], winners[i + 1])
            for i in range(0, len(winners), 2)]
