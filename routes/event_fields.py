"""
Event field validation and normalisation.

clean_event_fields(raw, partial=False) is the single entry point:
  partial=False (create) — returns a full dict with defaults for every field.
  partial=True  (update) — returns only the keys present in raw, cleaned.

Returns (cleaned, errors) where errors maps field name → error message string.
"""

import datetime
from urllib.parse import urlparse
from decklist import VALIDATION_FORMATS

TOURNAMENT_TAGS = ['Weekly Play', 'Prerelease', 'Regional Championship Qualifier',
                   'Spotlight Series']
STRUCTURES = ['swiss', 'swiss_top_cut', 'single_elim', 'custom']
REGISTRATION_TYPES = ('open', 'invite_only')
PROXY_POLICIES = ('unlimited', 'limited', 'custom')
COMMS_FIELDS = ('rules', 'schedule', 'prizes', 'contact')
_COMMS_MAX = 5000
_MAX_TABLE = 999


def _clean_tags(raw) -> list:
    chosen = set(raw or [])
    return [t for t in TOURNAMENT_TAGS if t in chosen]


def _clean_comms(data: dict) -> dict:
    out = {}
    for f in COMMS_FIELDS:
        if f in data:
            out[f] = str(data.get(f) or '')[:_COMMS_MAX]
    return out


def _clean_table_list(raw) -> list:
    """Sorted, de-duplicated, bounded list of table numbers (reserved/unavailable)."""
    out = set()
    for v in (raw or []):
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= _MAX_TABLE:
            out.add(n)
    return sorted(out)


def _clean_table_labels(raw) -> dict:
    """{str(table_number): label} map, numeric keys validated, labels trimmed/capped."""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                continue
            label = str(v or '').strip()[:40]
            if 1 <= n <= _MAX_TABLE and label:
                out[str(n)] = label
    return out


def _bounded_table(v, lo):
    """An int table number within [lo, _MAX_TABLE], else the floor `lo`."""
    return v if isinstance(v, int) and lo <= v <= _MAX_TABLE else lo


def _coord(v, lo: float, hi: float):
    """A float latitude/longitude within [lo, hi], else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def _normalize_payment_url(raw) -> tuple:
    """Validate/normalize a payment link so it's safe to render as a clickable
    <a href>. Returns (url, error): an empty string for no link, an http(s)
    URL (a missing scheme is assumed https), or (None, msg) if it's not a
    valid web URL — which blocks javascript:/data: and other unsafe schemes.
    """
    url = (raw or '').strip()
    if not url:
        return '', None
    if not urlparse(url).scheme:
        url = 'https://' + url
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None, 'Payment link must be a valid http(s) URL'
    return url, None


def clean_event_fields(raw: dict, partial: bool = False) -> tuple[dict, dict]:
    """
    Validate and normalise event fields from a client payload.

    partial=False (create): returns a full dict with defaults for every field.
    partial=True  (update): returns only the keys present in raw, cleaned.

    Returns (cleaned, errors) where errors maps field name → error message.
    """
    errors = {}
    c = {}

    def _has(key):
        return not partial or key in raw

    def _get(key, default=None):
        return raw.get(key, default)

    # ── Name ──────────────────────────────────────────────────────────────────
    if _has('name'):
        name = str(_get('name', 'New Event') or '').strip()
        if not name:
            errors['name'] = 'Event name is required'
        c['name'] = name

    # ── Simple scalar fields ───────────────────────────────────────────────────
    if _has('game'):
        c['game'] = str(_get('game') or '').strip()

    if _has('event_type'):
        c['event_type'] = _get('event_type', 'One-day')

    if _has('format'):
        c['format'] = _get('format', 'Limited: Draft')

    if _has('date'):
        c['date'] = _get('date', str(datetime.date.today()))

    if _has('start_time'):
        c['start_time'] = str(_get('start_time') or '').strip()[:5]

    if _has('location'):
        c['location'] = str(_get('location') or '').strip()[:200]

    if _has('lat'):
        c['lat'] = _coord(_get('lat'), -90, 90)

    if _has('lng'):
        c['lng'] = _coord(_get('lng'), -180, 180)

    if _has('place_id'):
        c['place_id'] = str(_get('place_id') or '').strip()[:300]

    if _has('num_rounds'):
        c['num_rounds'] = _get('num_rounds', 0)

    if _has('registration_cap'):
        c['registration_cap'] = _get('registration_cap', 0)

    # ── Boolean flags ──────────────────────────────────────────────────────────
    for field, default in (
        ('test_mode', False),
        ('advanced', False),
        ('intentional_draws_frowned', False),
        ('requires_decklists', False),
        ('decklists_required', False),
        ('closed_decklists', False),
        ('allow_proxies', False),
        ('allow_gold_border', False),
        ('allow_ce', False),
        ('allow_ie', False),
        ('tables_enabled', False),
        ('auto_start_timer', False),
        ('delay_pairings', False),
        ('delay_standings', False),
        ('require_check_in', False),
    ):
        if _has(field):
            c[field] = bool(_get(field, default))

    if _has('self_service_drop_enabled'):
        c['self_service_drop_enabled'] = bool(_get('self_service_drop_enabled', True))

    # ── Non-negative integer fields ────────────────────────────────────────────
    for field in ('round_timer_minutes', 'prize_deadline_days'):
        if _has(field):
            v = _get(field, 0)
            c[field] = v if isinstance(v, int) and v >= 0 else 0

    # ── Validated enum fields ──────────────────────────────────────────────────
    if _has('structure'):
        c['structure'] = _get('structure') if _get('structure') in STRUCTURES else ''

    if _has('planned_cut_size'):
        c['planned_cut_size'] = _get('planned_cut_size') if _get('planned_cut_size') in (4, 8, 16) else 0

    if _has('registration_type'):
        c['registration_type'] = _get('registration_type') if _get('registration_type') in REGISTRATION_TYPES else 'open'

    if _has('validation_format'):
        c['validation_format'] = _get('validation_format') if _get('validation_format') in VALIDATION_FORMATS else 'none'

    if _has('proxy_policy'):
        c['proxy_policy'] = _get('proxy_policy') if _get('proxy_policy') in PROXY_POLICIES else 'unlimited'

    # ── Proxy limit ────────────────────────────────────────────────────────────
    if _has('proxy_limit'):
        v = _get('proxy_limit', 0)
        c['proxy_limit'] = v if isinstance(v, int) and v >= 0 else 0

    # ── Capped string fields ───────────────────────────────────────────────────
    if _has('decklist_visibility_note'):
        c['decklist_visibility_note'] = str(_get('decklist_visibility_note') or '')[:500]

    if _has('proxy_note'):
        c['proxy_note'] = str(_get('proxy_note') or '').strip()[:500]

    if _has('brand_text'):
        c['brand_text'] = str(_get('brand_text') or '')[:300]

    if _has('entry_code'):
        c['entry_code'] = str(_get('entry_code') or '').strip()[:64]

    for field in ('description', 'entry_cost', 'drop_policy_text', 'refund_policy_text',
                  'rules', 'schedule', 'prizes', 'contact'):
        if _has(field):
            c[field] = str(_get(field) or '')[:_COMMS_MAX]

    # ── Date/time string fields ────────────────────────────────────────────────
    for field in ('decklist_deadline', 'registration_start', 'registration_end',
                  'unenroll_end', 'refund_window_end'):
        if _has(field):
            c[field] = str(_get(field) or '').strip()

    # ── Table assignments ──────────────────────────────────────────────────────
    if _has('table_start'):
        c['table_start'] = _bounded_table(_get('table_start'), 1)

    if _has('table_end'):
        c['table_end'] = _bounded_table(_get('table_end'), 0)

    if _has('tables_excluded'):
        c['tables_excluded'] = _clean_table_list(_get('tables_excluded'))

    if _has('table_labels'):
        c['table_labels'] = _clean_table_labels(_get('table_labels'))

    # ── Tags ───────────────────────────────────────────────────────────────────
    if _has('tags'):
        c['tags'] = _clean_tags(_get('tags'))

    # ── Payment URL (only field that produces a validation error) ─────────────
    if _has('payment_url'):
        url, err = _normalize_payment_url(_get('payment_url'))
        if err:
            errors['payment_url'] = err
        else:
            c['payment_url'] = url

    # ── Pass-through state fields (update only — never defaulted at create) ────
    if partial:
        for field in ('status', 'registration'):
            if field in raw:
                c[field] = raw[field]

    return c, errors
