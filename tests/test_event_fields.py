"""
Unit tests for routes.event_fields.clean_event_fields.

These test the pure-function layer directly — no Flask client, no DB.
"""

import pytest
from routes.event_fields import clean_event_fields, TOURNAMENT_TAGS, STRUCTURES, BEST_OF_OPTIONS


# ── Name validation ────────────────────────────────────────────────────────────

def test_name_required_on_create():
    _, errors = clean_event_fields({'name': ''})
    assert 'name' in errors

def test_name_whitespace_only_is_invalid():
    _, errors = clean_event_fields({'name': '   '})
    assert 'name' in errors

def test_name_absent_on_create_uses_default():
    cleaned, errors = clean_event_fields({})
    assert 'name' not in errors
    assert cleaned['name'] == 'New Event'

def test_name_stripped_on_create():
    cleaned, errors = clean_event_fields({'name': '  My Event  '})
    assert cleaned['name'] == 'My Event'
    assert not errors

def test_name_absent_in_partial_not_cleaned():
    cleaned, _ = clean_event_fields({}, partial=True)
    assert 'name' not in cleaned

def test_name_empty_in_partial_produces_error():
    _, errors = clean_event_fields({'name': ''}, partial=True)
    assert 'name' in errors


# ── Payment URL validation ─────────────────────────────────────────────────────

def test_payment_url_rejected():
    _, errors = clean_event_fields({'payment_url': 'javascript:void(0)'})
    assert 'payment_url' in errors

def test_payment_url_data_scheme_rejected():
    _, errors = clean_event_fields({'payment_url': 'data:text/html,<h1>xss</h1>'})
    assert 'payment_url' in errors

def test_payment_url_empty_accepted():
    cleaned, errors = clean_event_fields({'payment_url': ''})
    assert not errors
    assert cleaned['payment_url'] == ''

def test_payment_url_https_accepted():
    cleaned, errors = clean_event_fields({'payment_url': 'https://paypal.me/myevent'})
    assert not errors
    assert cleaned['payment_url'] == 'https://paypal.me/myevent'

def test_payment_url_scheme_added_when_missing():
    cleaned, errors = clean_event_fields({'payment_url': 'paypal.me/myevent'})
    assert not errors
    assert cleaned['payment_url'].startswith('https://')


# ── Proxy limit ────────────────────────────────────────────────────────────────

def test_proxy_limit_clamped_to_zero_for_negatives():
    cleaned, errors = clean_event_fields({'proxy_limit': -5})
    assert cleaned['proxy_limit'] == 0
    assert not errors

def test_proxy_limit_zero_accepted():
    cleaned, errors = clean_event_fields({'proxy_limit': 0})
    assert cleaned['proxy_limit'] == 0
    assert not errors

def test_proxy_limit_positive_accepted():
    cleaned, errors = clean_event_fields({'proxy_limit': 15})
    assert cleaned['proxy_limit'] == 15
    assert not errors

def test_proxy_limit_non_int_clamped():
    cleaned, _ = clean_event_fields({'proxy_limit': 'ten'})
    assert cleaned['proxy_limit'] == 0


# ── Enum fields fall back to defaults ─────────────────────────────────────────

def test_structure_unknown_falls_back_to_empty():
    cleaned, _ = clean_event_fields({'structure': 'invalid'})
    assert cleaned['structure'] == ''

def test_structure_valid_accepted():
    for s in STRUCTURES:
        cleaned, _ = clean_event_fields({'structure': s})
        assert cleaned['structure'] == s

def test_proxy_policy_unknown_falls_back():
    cleaned, _ = clean_event_fields({'proxy_policy': 'bogus'})
    assert cleaned['proxy_policy'] == 'unlimited'

def test_planned_cut_size_invalid_falls_back():
    cleaned, _ = clean_event_fields({'planned_cut_size': 7})
    assert cleaned['planned_cut_size'] == 0

def test_planned_cut_size_valid_accepted():
    for n in (4, 8, 16):
        cleaned, _ = clean_event_fields({'planned_cut_size': n})
        assert cleaned['planned_cut_size'] == n


# ── Tags ───────────────────────────────────────────────────────────────────────

def test_tags_preserves_canonical_order():
    cleaned, _ = clean_event_fields({'tags': ['Spotlight Series', 'Weekly Play']})
    assert cleaned['tags'] == ['Weekly Play', 'Spotlight Series']

def test_tags_filters_unrecognised():
    cleaned, _ = clean_event_fields({'tags': ['Weekly Play', 'FakeTag']})
    assert cleaned['tags'] == ['Weekly Play']

def test_tags_absent_defaults_to_empty_list():
    cleaned, _ = clean_event_fields({})
    assert cleaned['tags'] == []


# ── Partial mode skips absent fields ──────────────────────────────────────────

def test_partial_skips_absent_field():
    cleaned, _ = clean_event_fields({'game': 'MTG'}, partial=True)
    assert 'game' in cleaned
    assert 'tags' not in cleaned
    assert 'proxy_limit' not in cleaned

def test_partial_cleans_present_field():
    cleaned, _ = clean_event_fields({'proxy_limit': -1}, partial=True)
    assert cleaned['proxy_limit'] == 0

def test_partial_status_passthrough():
    cleaned, _ = clean_event_fields({'status': 'active'}, partial=True)
    assert cleaned['status'] == 'active'

def test_partial_registration_passthrough():
    cleaned, _ = clean_event_fields({'registration': 'closed'}, partial=True)
    assert cleaned['registration'] == 'closed'

def test_partial_status_not_included_on_create():
    cleaned, _ = clean_event_fields({'status': 'active'})
    assert 'status' not in cleaned


# ── Field caps ─────────────────────────────────────────────────────────────────

def test_description_capped_at_5000():
    cleaned, _ = clean_event_fields({'description': 'x' * 6000})
    assert len(cleaned['description']) == 5000

def test_rules_capped_at_5000():
    cleaned, _ = clean_event_fields({'rules': 'x' * 6000})
    assert len(cleaned['rules']) == 5000

def test_brand_text_capped_at_300():
    cleaned, _ = clean_event_fields({'brand_text': 'x' * 400})
    assert len(cleaned['brand_text']) == 300

def test_entry_code_capped_and_stripped():
    cleaned, _ = clean_event_fields({'entry_code': '  ' + 'a' * 70 + '  '})
    assert len(cleaned['entry_code']) == 64
    assert not cleaned['entry_code'].startswith(' ')


# ── Coordinate validation ──────────────────────────────────────────────────────

def test_lat_out_of_range_returns_none():
    cleaned, _ = clean_event_fields({'lat': 200})
    assert cleaned['lat'] is None

def test_lat_valid():
    cleaned, _ = clean_event_fields({'lat': 45.5})
    assert cleaned['lat'] == pytest.approx(45.5)

def test_lng_out_of_range_returns_none():
    cleaned, _ = clean_event_fields({'lng': -200})
    assert cleaned['lng'] is None

def test_coord_non_numeric_returns_none():
    cleaned, _ = clean_event_fields({'lat': 'north'})
    assert cleaned['lat'] is None


# ── best_of field ─────────────────────────────────────────────────────────────

def test_best_of_defaults_to_3():
    cleaned, _ = clean_event_fields({})
    assert cleaned['best_of'] == 3

def test_best_of_1_accepted():
    cleaned, _ = clean_event_fields({'best_of': 1})
    assert cleaned['best_of'] == 1

def test_best_of_3_accepted():
    cleaned, _ = clean_event_fields({'best_of': 3})
    assert cleaned['best_of'] == 3

def test_best_of_invalid_falls_back_to_3():
    cleaned, _ = clean_event_fields({'best_of': 2})
    assert cleaned['best_of'] == 3

def test_best_of_string_parsed():
    cleaned, _ = clean_event_fields({'best_of': '1'})
    assert cleaned['best_of'] == 1

def test_best_of_non_int_falls_back_to_3():
    cleaned, _ = clean_event_fields({'best_of': 'five'})
    assert cleaned['best_of'] == 3

def test_best_of_absent_in_partial_not_cleaned():
    cleaned, _ = clean_event_fields({}, partial=True)
    assert 'best_of' not in cleaned

def test_best_of_present_in_partial_cleaned():
    cleaned, _ = clean_event_fields({'best_of': 1}, partial=True)
    assert cleaned['best_of'] == 1


# ── Full create: has all expected fields ──────────────────────────────────────

def test_create_returns_complete_field_set():
    cleaned, errors = clean_event_fields({'name': 'Test Event'})
    assert not errors
    required = {'name', 'game', 'event_type', 'format', 'date', 'tags', 'structure',
                'validation_format', 'proxy_policy', 'proxy_limit', 'round_timer_minutes',
                'registration_type', 'registration_cap', 'self_service_drop_enabled'}
    for f in required:
        assert f in cleaned, f'Missing field: {f}'
