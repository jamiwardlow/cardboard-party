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
from gcp_secrets import get_secret

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


# Print-legality markers a player can append to a line, e.g. "4 Lightning Bolt [Proxy]".
_TAG_RE = re.compile(r'\[([^\]]+)\]\s*$')

def _norm_tag(s: str) -> str:
    """Canonicalise a bracket marker to 'proxy' | 'gold' | 'ce' | 'ie', or '' if it's
    not a recognised print-legality tag (then the bracket is dropped from the name)."""
    t = re.sub(r'[^a-z]', '', s.lower())
    if t in ('proxy', 'proxies', 'prx'):                              return 'proxy'
    if t in ('goldborder', 'gold', 'gb'):                            return 'gold'
    if t in ('ce', 'collectorsedition', 'collectoredition'):         return 'ce'
    if t in ('ie', 'internationaledition'):                          return 'ie'
    return ''


def _front_face(name: str) -> str:
    """Front-face name of a split/DFC/adventure card ('Fire // Ice' → 'Fire'). The
    Scryfall collection endpoint matches the front face but rejects the combined name."""
    return name.split('//')[0].strip() if '//' in name else name


def parse_decklist(text: str):
    """Return (maindeck, sideboard) as lists of (count, name, tag), where tag is a
    print-legality marker ('proxy'/'gold'/'ce'/'ie') or '' for a normal printing.
    Recognises section labels (Deck / Maindeck / --Maindeck-- / Sideboard / SB). If
    no sideboard label is present, a blank line after the maindeck starts the
    sideboard (MTGO-style export)."""
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
        rest, tag = m.group(2), ''
        tm = _TAG_RE.search(rest)
        if tm:                                     # pull a trailing [Proxy]/[CE]/… marker
            raw = tm.group(1).strip()
            tag = _norm_tag(raw) or raw.lower()    # canonical tag, or the raw text if unknown
            rest = rest[:tm.start()].rstrip()
        name = _clean_name(rest)
        if name:
            (main if section == 'main' else side).append((int(m.group(1)), name, tag))
    return main, side


def _scryfall_cards(names):
    """(by_name, not_found, reached). by_name maps a lowercased card/face name to
    {'name', 'type_line', 'legalities'}; not_found lists names Scryfall didn't know.
    `reached` is False on a network/API failure (so it's never read as bad names)."""
    by_name = {}
    # Query split/DFC/adventure cards by their front face ('A // B' → 'A'); the
    # collection endpoint matches that but rejects the combined name. The returned
    # card still carries its full name + face names, which we index below.
    queries = list({_front_face(n) for n in names})
    for i in range(0, len(queries), 75):               # collection endpoint caps at 75
        batch = queries[i:i + 75]
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
        time.sleep(0.1)                                # be polite to Scryfall
    # A name resolved if its full form or its front face was indexed.
    not_found = [n for n in names
                 if n.lower() not in by_name and _front_face(n).lower() not in by_name]
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
_MOX_ENDPOINT = 'https://api2.moxfield.com/v3/decks/all/{id}'


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
    """Fetch a public Moxfield deck and return (decklist_text, deck_name, error). The
    text is a snapshot — once saved it's the authoritative copy, unaffected by later
    edits on Moxfield.

    Access is via the MoxKey User-Agent Moxfield issued us — a SECRET credential
    (like a password) kept in Secret Manager, only ever sent server→Moxfield, never
    to Scryfall or the client. One request per import (Moxfield caps us at 1/sec)."""
    m = _MOX_ID_RE.search(url or '')
    if not m:
        return '', '', "That doesn't look like a Moxfield deck URL."
    ua = get_secret('MOXFIELD_USER_AGENT')
    if not ua:
        return '', '', 'Moxfield import isn’t configured right now — paste your list instead.'
    try:
        r = requests.get(_MOX_ENDPOINT.format(id=m.group(1)),
                         headers={'User-Agent': ua, 'Accept': 'application/json'}, timeout=10)
    except requests.RequestException:
        return '', '', "Couldn't reach Moxfield — paste your list instead."
    if r.status_code == 404:
        return '', '', 'Moxfield deck not found — is it set to public?'
    if not r.ok:
        return '', '', "Couldn't import from Moxfield — paste your list instead."
    try:
        data = r.json()
    except ValueError:
        return '', '', "Couldn't read that Moxfield deck — paste your list instead."
    text = _moxfield_to_text(data)
    if not text:
        return '', '', 'That Moxfield deck looks empty — paste your list instead.'
    return text, str(data.get('name') or '').strip()[:120], ''


# Print-legality tags → the event policy flag that permits them, and display words.
_TAG_ALLOW = {'proxy': 'allow_proxies', 'gold': 'allow_gold_border',
              'ce': 'allow_ce', 'ie': 'allow_ie'}
_TAG_MARK  = {'proxy': 'a proxy', 'gold': 'gold-border', 'ce': 'CE', 'ie': 'IE'}
_TAG_WORD  = {'proxy': 'proxies', 'gold': 'gold-border cards',
              'ce': 'Collector’s Edition (CE) cards', 'ie': 'International Edition (IE) cards'}


_CANON_TAGS = ('proxy', 'gold', 'ce', 'ie')


def _check_print_legality(cards, policy, add):
    """Flag any card tagged with a printing the event doesn't allow (recognized but
    not permitted, or an unrecognized tag entirely), plus a proxy overage. `cards`
    is the combined (count, name, tag) list; `add(severity, msg)`."""
    for _, n, t in cards:
        if not t:
            continue
        if t in _CANON_TAGS:
            if not policy.get(_TAG_ALLOW[t], False):
                add('error', f'"{n}" is marked {_TAG_MARK[t]}, but {_TAG_WORD[t]} '
                             f'aren’t allowed at this event.')
        else:
            add('error', f'"{n}" is marked [{t}], which isn’t an allowed printing '
                         f'for this event.')
    if policy.get('allow_proxies') and policy.get('proxy_policy') == 'limited':
        lim = policy.get('proxy_limit') or 0
        pc = sum(c for c, _, t in cards if t == 'proxy')
        if lim and pc > lim:
            add('error', f'{pc} proxies in the list — this event allows up to {lim}.')


def validate_decklist(text: str, fmt: str = 'none', policy: dict = None) -> dict:
    """Validate a decklist for a given format and the event's print-legality policy.
    Returns a structured summary (safe to store and show): maindeck/sideboard/proxy
    counts, severity-tagged issues, and a status (none/valid/errors/warnings/
    unchecked). For a recognised format it checks card legality (Scryfall), the
    60-card maindeck minimum, the 15-card sideboard maximum, and the 4-copy limit
    (basic lands exempt). Print-legality tag checks (proxy/gold/CE/IE allowed, proxy
    limit) run for every format. 'none' = no card-database validation."""
    fmt = fmt if fmt in VALIDATION_FORMATS else 'none'
    policy = policy or {}
    main, side = parse_decklist(text)
    main_count = sum(c for c, _, _ in main)
    side_count = sum(c for c, _, _ in side)
    proxy_count = sum(c for c, _, t in main + side if t == 'proxy')
    result = {'format': fmt, 'maindeck_count': main_count, 'sideboard_count': side_count,
              'proxy_count': proxy_count, 'issues': [], 'status': 'valid', 'ok': True}

    issues = []
    def add(sev, msg):
        issues.append({'severity': sev, 'message': msg})

    # Print-legality tag checks apply regardless of validation format.
    _check_print_legality(main + side, policy, add)

    if fmt == 'none':
        result['issues'] = issues
        if issues:
            result['status'] = 'errors' if any(i['severity'] == 'error' for i in issues) else 'warnings'
            result['ok'] = False
        else:
            result['status'] = 'none'
        return result

    by_name, not_found, reached = _scryfall_cards(list({n for _, n, _ in main + side}))
    if not reached:
        result.update(status='unchecked', ok=False, issues=issues,
                      note="Couldn't reach the card database, so the list wasn't validated.")
        return result

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
    for c, n, _ in main + side:
        totals[n.lower()] = totals.get(n.lower(), 0) + c
    for n in {n for _, n, _ in main + side}:
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
