"""
Storage security tests.

Verifies that image uploads reject decompression bombs — crafted image files
that are small on disk but expand to gigabytes in memory, potentially
exhausting instance RAM.
"""

import io
import pytest
from PIL import Image


def _make_png(width: int, height: int) -> bytes:
    img = Image.new('RGB', (width, height), color='red')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def test_pixel_limit_is_set_below_decompression_bomb_threshold():
    """storage.py must cap Image.MAX_IMAGE_PIXELS to a finite, safe value."""
    import PIL.Image as pil
    # Importing storage triggers the module-level MAX_IMAGE_PIXELS assignment.
    import storage  # noqa: F401
    assert pil.MAX_IMAGE_PIXELS is not None, 'MAX_IMAGE_PIXELS must not be None'
    assert pil.MAX_IMAGE_PIXELS <= 50_000_000, (
        f'Expected ≤ 50 MP, got {pil.MAX_IMAGE_PIXELS}; '
        'storage.py must set Image.MAX_IMAGE_PIXELS to block decompression bombs'
    )


def test_upload_avatar_rejects_image_exceeding_pixel_cap(monkeypatch):
    """An image larger than 2× MAX_IMAGE_PIXELS must raise ValueError."""
    from storage import upload_avatar
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 100)
    # 15×15 = 225 > 2×100 = 200 → PIL raises DecompressionBombError
    raw = _make_png(15, 15)
    with pytest.raises(ValueError, match='not a valid image'):
        upload_avatar('test_user', raw)


def test_upload_avatar_accepts_small_valid_image(monkeypatch):
    """Normal small images must pass validation."""
    from unittest.mock import MagicMock, patch
    from storage import upload_avatar

    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 10_000)

    # 5×5 = 25 pixels — well within any limit
    raw = _make_png(5, 5)

    # Mock out the GCS upload so we don't need credentials
    with patch('storage._bucket') as mock_bucket:
        mock_blob = MagicMock()
        mock_bucket.return_value.blob.return_value = mock_blob
        url, obj_name = upload_avatar('test_user', raw)

    assert url.startswith('https://storage.googleapis.com/')
    assert obj_name.startswith('avatars/test_user/')
