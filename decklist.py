"""
Decklist parsing + validation (MVP slice 2).

Parses a pasted constructed decklist into maindeck/sideboard, validates card
names against Scryfall, and checks the 60-card maindeck minimum. `parse_decklist`
is network-free; `validate_decklist` calls Scryfall (bulk name check + fuzzy
"did you mean" suggestions).
"""

import re
import time
import requests

SCRYFALL = 'https://api.scryfall.com'
_HEADERS = {'User-Agent': 'CardboardParty/1.0 (+https://cardboardparty.gg)',
            'Accept': 'application/json'}
MAINDECK_MIN = 60

_MAIN_HEADERS = {'deck', 'maindeck', 'main', 'mainboard'}
_SIDE_HEADERS = {'sideboard', 'sb', 'sideboardcards'}
# "4 Lightning Bolt", "4x Lightning Bolt", "10 Forest"
_LINE_RE = re.compile(r'^(\d+)\s*[xX]?\s+(.+)$')


def _norm_header(line: str) -> str:
    return re.sub(r'[^a-z]', '', line.lower())


def _clean_name(name: str) -> str:
    name = re.sub(r'\s*\*[^*]*\*\s*$', '', name)            # trailing *F* foil markers
    name = re.sub(r'\s*\([^)]*\)\s*[\w-]*\s*$', '', name)   # trailing (SET) 123 collector info
    return name.strip()


def parse_decklist(text: str):
    """Return (maindeck, sideboard) as lists of (count, name). Recognises section
    labels (Deck / Maindeck / --Maindeck-- / Sideboard / SB). If no sideboard
    label is present, a blank line after the maindeck starts the sideboard
    (MTGO-style export)."""
    stripped = [l.strip() for l in (text or '').splitlines()]
    has_side_label = any(_norm_header(l) in _SIDE_HEADERS
                         for l in stripped if l and not _LINE_RE.match(l))
    main, side, section = [], [], 'main'
    for l in stripped:
        if not l:
            if not has_side_label and section == 'main' and main:
                section = 'side'
            continue
        m = _LINE_RE.match(l)
        if not m:                                  # a label or stray line
            h = _norm_header(l)
            if h in _SIDE_HEADERS:
                section = 'side'
            elif h in _MAIN_HEADERS:
                section = 'main'
            continue
        name = _clean_name(m.group(2))
        if name:
            (main if section == 'main' else side).append((int(m.group(1)), name))
    return main, side


def _scryfall_unknown(names):
    """(unknown_names, reached). `reached` is False if we couldn't reach Scryfall,
    so a network failure is never reported as bad card names."""
    unknown = []
    for i in range(0, len(names), 75):                 # collection endpoint caps at 75
        batch = names[i:i + 75]
        try:
            r = requests.post(f'{SCRYFALL}/cards/collection', headers=_HEADERS,
                              json={'identifiers': [{'name': n} for n in batch]}, timeout=10)
            if not r.ok:
                return [], False
            for nf in r.json().get('not_found', []):
                if nf.get('name'):
                    unknown.append(nf['name'])
        except requests.RequestException:
            return [], False
        time.sleep(0.1)                                # be polite to Scryfall
    return unknown, True


def _scryfall_suggest(name: str) -> str:
    try:
        r = requests.get(f'{SCRYFALL}/cards/named', headers=_HEADERS,
                         params={'fuzzy': name}, timeout=10)
        if r.ok:
            return r.json().get('name', '')
    except requests.RequestException:
        pass
    return ''


_MOX_ID_RE = re.compile(r'moxfield\.com/decks/([A-Za-z0-9_-]+)')
_MOX_ENDPOINTS = ('https://api2.moxfield.com/v3/decks/all/{id}',
                  'https://api.moxfield.com/v2/decks/all/{id}')


def _moxfield_to_text(data: dict) -> str:
    """Convert a Moxfield deck JSON into our decklist text (maindeck + sideboard).
    Handles the v3 `boards.<board>.cards` shape and the older v2 name-keyed shape."""
    def lines(v3_board, v2_board):
        boards = data.get('boards') or {}
        cards = ((boards.get(v3_board) or {}).get('cards')) or {}
        if cards:
            return [f"{e.get('quantity', 1)} {(e.get('card') or {}).get('name', '')}".strip()
                    for e in cards.values() if (e.get('card') or {}).get('name')]
        flat = data.get(v2_board)
        if isinstance(flat, dict):
            return [f"{v.get('quantity', 1)} {k}" for k, v in flat.items() if k]
        return []
    main, side = lines('mainboard', 'mainboard'), lines('sideboard', 'sideboard')
    out = list(main)
    if side:
        out += ['', 'Sideboard'] + side
    return '\n'.join(out).strip()


def import_moxfield(url: str):
    """Fetch a public Moxfield deck and return (decklist_text, error). The text is
    a snapshot — once saved it's the authoritative copy, unaffected by later edits
    on Moxfield. Moxfield may block automated access; that returns a clear error."""
    m = _MOX_ID_RE.search(url or '')
    if not m:
        return '', "That doesn't look like a Moxfield deck URL."
    deck_id = m.group(1)
    last_err = "Couldn't import from Moxfield — paste your list instead."
    for ep in _MOX_ENDPOINTS:
        try:
            r = requests.get(ep.format(id=deck_id), headers=_HEADERS, timeout=10)
        except requests.RequestException:
            continue
        if r.status_code == 404:
            return '', 'Moxfield deck not found — is it set to public?'
        if not r.ok:
            last_err = ("Couldn't import from Moxfield (it may be blocking automated "
                        "access) — paste your list instead.")
            continue
        try:
            text = _moxfield_to_text(r.json())
        except ValueError:
            continue
        if text:
            return text, ''
    return '', last_err


def validate_decklist(text: str) -> dict:
    """Validate a pasted decklist: 60-card maindeck minimum + card-name check with
    suggestions. Returns a summary safe to store and show to players/organisers."""
    main, side = parse_decklist(text)
    main_count = sum(c for c, _ in main)
    side_count = sum(c for c, _ in side)
    names = list({n for _, n in main + side})
    unknown, reached = _scryfall_unknown(names)

    issues, unknown_out = [], []
    if main_count < MAINDECK_MIN:
        issues.append(f'Maindeck has {main_count} card{"" if main_count == 1 else "s"}; '
                      f'a constructed deck needs at least {MAINDECK_MIN}.')
    for n in unknown[:15]:                             # cap suggestion lookups
        sug = _scryfall_suggest(n)
        unknown_out.append({'name': n, 'suggestion': sug})
        issues.append(f'Unrecognized card: "{n}"' + (f' — did you mean "{sug}"?' if sug else '.'))

    result = {
        'maindeck_count': main_count, 'sideboard_count': side_count,
        'unknown': unknown_out, 'issues': issues,
        'card_check': reached, 'ok': reached and not issues,
    }
    if not reached:
        result['note'] = "Couldn't reach the card database, so card names weren't checked."
    return result
