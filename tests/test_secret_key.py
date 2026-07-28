"""
Production secret-key validation tests.

If FLASK_SECRET_KEY is absent (or falls back to the hardcoded dev placeholder),
a production deployment must fail loudly at startup rather than silently using
a publicly-known signing key that any attacker can exploit to forge sessions.
"""

import pytest


def test_insecure_dev_key_raises_in_production():
    """Production mode with the placeholder key must raise RuntimeError."""
    from main import _validate_production_config
    with pytest.raises(RuntimeError, match='FLASK_SECRET_KEY'):
        _validate_production_config('dev-only-insecure-key', is_prod=True)


def test_empty_key_raises_in_production():
    """Production mode with an empty key must raise RuntimeError."""
    from main import _validate_production_config
    with pytest.raises(RuntimeError, match='FLASK_SECRET_KEY'):
        _validate_production_config('', is_prod=True)


def test_strong_key_passes_in_production():
    """A real random secret must not raise in production mode."""
    from main import _validate_production_config
    _validate_production_config('s0m3-l0ng-r4nd0m-s3cr3t-k3y-xyz', is_prod=True)


def test_insecure_key_allowed_in_dev():
    """The dev placeholder is fine outside of production (local or staging)."""
    from main import _validate_production_config
    _validate_production_config('dev-only-insecure-key', is_prod=False)
