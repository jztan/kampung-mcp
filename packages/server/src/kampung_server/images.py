"""Re-encode image attachments on the server.

The adapter shrinks images before upload, but a modified client or a forged
call can send anything, so the server decodes every image itself and stores
only what it encoded: PNG or JPEG, at most IMAGE_LONG_EDGE on the long edge,
first frame only, and no metadata (text chunks, EXIF and GPS, bytes after
the end marker).
"""

import io
import threading
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import KampungError
from .limits import (
    IMAGE_DECODE_WAIT,
    IMAGE_DECODES,
    IMAGE_LONG_EDGE,
    IMAGE_MAX_PIXELS,
    MAX_ATTACHMENT_IMAGE_BYTES,
)

# Decoded pixels are the costly part: at most IMAGE_DECODES images hold them
# at once, however many sends arrive together.
_DECODE_SLOTS = threading.BoundedSemaphore(IMAGE_DECODES)


def _unsupported() -> KampungError:
    # A fresh error each time: a shared instance would carry one request's
    # traceback and cause into another, across threads.
    return KampungError("unsupported_attachment_type", "Images must be PNG or JPEG.")


def _encode(img: Image.Image, fmt: str) -> bytes:
    out = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    else:
        img.save(out, "PNG", optimize=True)
    return out.getvalue()


def normalize_image(data: bytes) -> tuple[bytes, str]:
    """The image re-encoded with no metadata, and its media type."""
    # The slot is taken before Pillow sees a byte: opening a PNG already
    # expands its text chunks (up to 64 MB), so opening counts as decoding.
    if not _DECODE_SLOTS.acquire(timeout=IMAGE_DECODE_WAIT):
        raise KampungError(
            "busy", "The server is decoding other images. Try again in a moment."
        )
    try:
        img, source_format = _decoded(data)
    finally:
        _DECODE_SLOTS.release()
    return _clean(img, source_format)


def _decoded(data: bytes) -> tuple[Image.Image, str | None]:
    """Opened, checked and shrunk; called with a decode slot held."""
    try:
        opened = Image.open(io.BytesIO(data), formats=["PNG", "JPEG"])
    except Image.DecompressionBombError as e:
        # Pillow refuses a huge header itself, before our own pixel check.
        raise KampungError("attachment_too_large", "The image is too large.") from e
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise _unsupported() from e
    width, height = opened.size
    if width * height > IMAGE_MAX_PIXELS:
        raise KampungError(
            "attachment_too_large",
            f"The image is {width} x {height}; at most "
            f"{IMAGE_MAX_PIXELS} pixels are accepted.",
        )
    source_format = opened.format
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            opened.seek(0)
            opened.load()
            # In place, so there is one full-size copy, not two, and the
            # shrink below frees it while the slot is still held.
            ImageOps.exif_transpose(opened, in_place=True)
            opened.thumbnail((IMAGE_LONG_EDGE, IMAGE_LONG_EDGE))
            img: Image.Image = opened
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise KampungError("attachment_too_large", "The image is too large.") from e
    except (OSError, ValueError, SyntaxError) as e:
        raise _unsupported() from e
    return img, source_format


def _clean(img: Image.Image, source_format: str | None) -> tuple[bytes, str]:
    # A fresh image carries pixels only: no info, EXIF or ICC profile.
    mode = "RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB"
    clean = Image.new(mode, img.size)
    clean.paste(img.convert(mode))
    png = _encode(clean, "PNG")
    jpeg = _encode(clean, "JPEG") if mode == "RGB" else None
    if source_format == "JPEG" and jpeg is not None:
        best, kind = jpeg, "image/jpeg"
    elif jpeg is not None and len(jpeg) < len(png):
        best, kind = jpeg, "image/jpeg"
    else:
        best, kind = png, "image/png"
    if len(best) > MAX_ATTACHMENT_IMAGE_BYTES:
        raise KampungError(
            "attachment_too_large",
            f"The image is {len(best)} bytes after shrinking; the limit is "
            f"{MAX_ATTACHMENT_IMAGE_BYTES}.",
        )
    return best, kind
