"""
Profile-avatar uploads to Google Cloud Storage.

Images are validated with Pillow, resized to fit within 512×512, re-encoded as
JPEG (which strips EXIF and normalizes the format), and stored in a public-read
bucket. Each upload gets a unique object name so the public URL changes and
caches don't serve a stale image; the previous object is deleted by the caller.
"""

import io
import uuid

from google.cloud import storage as gcs
from PIL import Image

BUCKET_NAME = 'cardboard-party-avatars'
_MAX_DIM = 512
_client = None


def _bucket():
    global _client
    if _client is None:
        _client = gcs.Client()
    return _client.bucket(BUCKET_NAME)


def upload_avatar(google_id: str, raw: bytes) -> tuple[str, str]:
    """
    Validate/resize/upload an avatar. Returns (public_url, object_name).
    Raises ValueError if the bytes are not a decodable image.
    """
    try:
        Image.open(io.BytesIO(raw)).verify()      # cheap validity check
        img = Image.open(io.BytesIO(raw))          # reopen (verify() exhausts it)
        img = img.convert('RGB')
    except Exception:
        raise ValueError('That file is not a valid image')

    # Center-crop to a square, then resize — avatars are shown in circular/square
    # frames, so a square source never distorts regardless of CSS.
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    target = min(side, _MAX_DIM)  # don't upscale past the original
    img = img.resize((target, target), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    img.save(out, format='JPEG', quality=88)
    out.seek(0)

    object_name = f'avatars/{google_id}/{uuid.uuid4().hex}.jpg'
    blob = _bucket().blob(object_name)
    blob.cache_control = 'public, max-age=86400'
    blob.upload_from_file(out, content_type='image/jpeg')
    return f'https://storage.googleapis.com/{BUCKET_NAME}/{object_name}', object_name


_BRAND_MAX_DIM = 1200
_THEME_BG = (20, 17, 31)   # var(--bg) #14111f, for flattening transparency


def upload_brand_image(event_id: str, raw: bytes) -> tuple[str, str]:
    """Validate/resize/upload an event brand image. Unlike avatars this keeps the
    original aspect ratio (it's a banner/logo, not a framed thumbnail), fitting
    within 1200×1200. Transparency is flattened onto the dark theme background.
    Returns (public_url, object_name); raises ValueError for non-images.
    """
    try:
        Image.open(io.BytesIO(raw)).verify()
        img = Image.open(io.BytesIO(raw))
    except Exception:
        raise ValueError('That file is not a valid image')

    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
        bg = Image.new('RGB', img.size, _THEME_BG)
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert('RGB')

    img.thumbnail((_BRAND_MAX_DIM, _BRAND_MAX_DIM), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    img.save(out, format='JPEG', quality=85)
    out.seek(0)

    object_name = f'brand/{event_id}/{uuid.uuid4().hex}.jpg'
    blob = _bucket().blob(object_name)
    blob.cache_control = 'public, max-age=86400'
    blob.upload_from_file(out, content_type='image/jpeg')
    return f'https://storage.googleapis.com/{BUCKET_NAME}/{object_name}', object_name


def delete_object(object_name: str):
    """Best-effort delete of a previous avatar object."""
    if not object_name:
        return
    try:
        _bucket().blob(object_name).delete()
    except Exception as e:
        print(f'avatar delete error: {e}')
