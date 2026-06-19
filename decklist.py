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
SIDEBOARD_MAX = 15
COPY_LIMIT = 4

# Deck-validation formats (the values double as Scryfall `legalities` keys).
# 'none' = no automatic validation. Designed to add legacy/commander/etc. later.
VALIDATION_FORMATS = {'none': 'No automatic validation', 'premodern': 'Premodern'}

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


def _scryfall_cards(names):
    """(by_name, not_found, reached). by_name maps a lowercased card/face name to
    {'name', 'type_line', 'legalities'}; not_found lists names Scryfall didn't know.
    `reached` is False on a network/API failure (so it's never read as bad names)."""
    by_name, not_found = {}, []
    for i in range(0, len(names), 75):                 # collection endpoint caps at 75
        batch = names[i:i + 75]
        try:
            r = requests.post(f'{SCRYFALL}/cards/collection', headers=_HEADERS,
                              json={'identifiers': [{'name': n} for n in batch]}, timeout=10)
            if not r.ok:
                return {}, [], False
            j = r.json()
        except (requests.RequestException, ValueError):
            return {}, [], False
        for c in j.get('data', []):
            entry = {'name': c.get('name', ''), 'type_line': c.get('type_line', ''),
                     'legalities': c.get('legalities', {})}
            by_name[entry['name'].lower()] = entry
            for face in (c.get('card_faces') or []):   # DFC / split: index each face name
                if face.get('name'):
                    by_name[face['name'].lower()] = entry
        for nf in j.get('not_found', []):
            if nf.get('name'):
                not_found.append(nf['name'])
        time.sleep(0.1)                                # be polite to Scryfall
    return by_name, not_found, True


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


def validate_decklist(text: str, fmt: str = 'none') -> dict:
    """Validate a decklist for a given format. Returns a structured summary (safe to
    store and show): maindeck/sideboard counts, severity-tagged issues, and a status
    (none/valid/errors/unchecked). For a recognised format it checks card legality
    (Scryfall), the 60-card maindeck minimum, the 15-card sideboard maximum, and the
    4-copy limit (basic lands exempt). 'none' = no automatic validation."""
    fmt = fmt if fmt in VALIDATION_FORMATS else 'none'
    main, side = parse_decklist(text)
    main_count = sum(c for c, _ in main)
    side_count = sum(c for c, _ in side)
    result = {'format': fmt, 'maindeck_count': main_count, 'sideboard_count': side_count,
              'issues': [], 'status': 'valid', 'ok': True}
    if fmt == 'none':
        result['status'] = 'none'
        return result

    by_name, not_found, reached = _scryfall_cards(list({n for _, n in main + side}))
    if not reached:
        result.update(status='unchecked', ok=False,
                      note="Couldn't reach the card database, so the list wasn't validated.")
        return result

    issues = []
    def add(sev, msg):
        issues.append({'severity': sev, 'message': msg})

    label = VALIDATION_FORMATS[fmt]
    if main_count < MAINDECK_MIN:
        add('error', f'Maindeck has {main_count} card{"" if main_count == 1 else "s"}; '
                     f'needs at least {MAINDECK_MIN}.')
    if side_count > SIDEBOARD_MAX:
        add('error', f'Sideboard has {side_count} cards; the maximum is {SIDEBOARD_MAX}.')
    for n in not_found[:15]:
        sug = _scryfall_suggest(n)
        add('error', f'Unrecognized card: "{n}"' + (f' — did you mean "{sug}"?' if sug else '.'))

    totals = {}
    for c, n in main + side:
        totals[n.lower()] = totals.get(n.lower(), 0) + c
    for n in {n for _, n in main + side}:
        card = by_name.get(n.lower())
        if not card:
            continue                                   # already flagged as unrecognized
        if 'basic' not in card.get('type_line', '').lower() and totals[n.lower()] > COPY_LIMIT:
            add('error', f'{totals[n.lower()]} copies of "{card["name"]}" — the limit is {COPY_LIMIT}.')
        legal = (card.get('legalities') or {}).get(fmt)
        if legal == 'banned':
            add('error', f'"{card["name"]}" is banned in {label}.')
        elif legal == 'not_legal':
            add('error', f'"{card["name"]}" is not legal in {label}.')

    has_error = any(i['severity'] == 'error' for i in issues)
    result['issues'] = issues
    result['status'] = 'errors' if has_error else ('warnings' if issues else 'valid')
    result['ok'] = not issues
    return result
