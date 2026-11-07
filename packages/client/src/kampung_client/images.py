"""Shrink an image before upload, with the server's own rules.

Screenshots are often over a megabyte, so the adapter shrinks every image
to what Claude keeps anyway before it checks the 512 KB cap. The pixel cap
comes first, so an image bomb in the outbox cannot take the adapter down,
and one Codex adapter serves several windows. The server re-encodes again
on arrival; this copy only makes ordinary screenshots fit.
"""

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .outbox import OutboxError

IMAGE_LONG_EDGE = 1568  # the server applies the same rules
IMAGE_MAX_PIXELS = 25_000_000
MAX_ATTACHMENT_IMAGE_BYTES = 512 * 1024

UNSUPPORTED = OutboxError("unsupported_attachment_type", "Images must be PNG or JPEG.")


def _encode(img: Image.Image, fmt: str) -> bytes:
    out = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    else:
        img.save(out, "PNG", optimize=True)
    return out.getvalue()


def shrink(data: bytes) -> tuple[bytes, str]:
    """The image re-encoded with no metadata, and its media type."""
    try:
        opened = Image.open(io.BytesIO(data), formats=["PNG", "JPEG"])
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise UNSUPPORTED from e
    width, height = opened.size
    if width * height > IMAGE_MAX_PIXELS:
        raise OutboxError(
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
            img: Image.Image = ImageOps.exif_transpose(opened)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise OutboxError("attachment_too_large", "The image is too large.") from e
    except (OSError, ValueError, SyntaxError) as e:
        raise UNSUPPORTED from e
    img.thumbnail((IMAGE_LONG_EDGE, IMAGE_LONG_EDGE))
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
        raise OutboxError(
            "attachment_too_large",
            f"The image is {len(best)} bytes after shrinking; the limit is "
            f"{MAX_ATTACHMENT_IMAGE_BYTES}.",
        )
    return best, kind
