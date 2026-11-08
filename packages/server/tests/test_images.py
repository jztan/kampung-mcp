import io
import os
import struct
import zlib

import pytest
from PIL import Image, PngImagePlugin

from kampung_server.errors import KampungError
from kampung_server.images import normalize_image


def _png(
    w: int, h: int, color: tuple[int, int, int] = (200, 30, 30), **kw: object
) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG", **kw)  # type: ignore[arg-type]
    return out.getvalue()


def _noise_png(w: int, h: int) -> bytes:
    out = io.BytesIO()
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(out, "PNG")
    return out.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_small_png_round_trips_and_keeps_its_type() -> None:
    data, kind = normalize_image(_png(40, 30))
    assert kind == "image/png" and _open(data).size == (40, 30)


def test_jpeg_comes_back_as_jpeg() -> None:
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(out, "JPEG")
    data, kind = normalize_image(out.getvalue())
    assert kind == "image/jpeg" and _open(data).format == "JPEG"


def test_long_edge_is_capped_at_1568() -> None:
    data, _ = normalize_image(_png(3000, 2000))
    assert _open(data).size == (1568, 1045)


def test_exif_gps_and_text_chunks_are_dropped() -> None:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret-note")
    png, _ = normalize_image(_png(20, 20, pnginfo=info))
    assert b"secret-note" not in png and not _open(png).info.get("Comment")
    exif = Image.Exif()
    exif[0x8825] = {2: (1.0, 2.0, 3.0)}  # GPSInfo
    out = io.BytesIO()
    Image.new("RGB", (20, 20)).save(out, "JPEG", exif=exif)
    jpeg, _ = normalize_image(out.getvalue())
    assert not _open(jpeg).getexif()


def test_bytes_after_the_end_marker_are_dropped() -> None:
    data, _ = normalize_image(_png(20, 20) + b"hidden-payload")
    assert b"hidden-payload" not in data


def test_orientation_is_applied_before_metadata_goes() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90
    out = io.BytesIO()
    Image.new("RGB", (40, 20)).save(out, "JPEG", exif=exif)
    data, _ = normalize_image(out.getvalue())
    assert _open(data).size == (20, 40)


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF"])
def test_other_image_formats_are_refused(fmt: str) -> None:
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, fmt)
    with pytest.raises(KampungError) as e:
        normalize_image(out.getvalue())
    assert e.value.code == "unsupported_attachment_type"


@pytest.mark.parametrize(
    "data", [b"%!PS-Adobe-3.0 EPSF-3.0\n", b"not an image at all", b""]
)
def test_eps_and_garbage_are_refused(data: bytes) -> None:
    with pytest.raises(KampungError) as e:
        normalize_image(data)
    assert e.value.code == "unsupported_attachment_type"


def _bomb_png(w: int, h: int) -> bytes:
    """A valid PNG header claiming w x h, with almost no pixel data."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    head = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", head)
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


def test_decompression_bomb_is_refused_before_decoding() -> None:
    with pytest.raises(KampungError) as e:
        normalize_image(_bomb_png(10000, 10000))
    assert e.value.code == "attachment_too_large"


def test_animated_png_keeps_only_the_first_frame() -> None:
    out = io.BytesIO()
    frames = [Image.new("RGB", (16, 16), c) for c in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(out, "PNG", save_all=True, append_images=frames[1:])
    data, _ = normalize_image(out.getvalue())
    img = _open(data)
    assert getattr(img, "n_frames", 1) == 1
    assert img.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_still_too_large_after_encoding_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kampung_server import images

    monkeypatch.setattr(images, "MAX_ATTACHMENT_IMAGE_BYTES", 10_000)
    with pytest.raises(KampungError) as e:
        normalize_image(_noise_png(200, 200))
    assert e.value.code == "attachment_too_large"


def test_header_past_pillows_own_bomb_limit_is_too_large_inline() -> None:
    with pytest.raises(KampungError) as e:
        normalize_image(_bomb_png(20000, 20000))
    assert e.value.code == "attachment_too_large"


def test_image_over_8_megapixels_is_refused_from_its_header() -> None:
    # 3000 x 3000 is 9 million pixels: refused before any pixel is decoded.
    with pytest.raises(KampungError) as e:
        normalize_image(_bomb_png(3000, 3000))
    assert e.value.code == "attachment_too_large"
    assert "8000000 pixels" in e.value.hint


def test_image_just_under_the_cap_is_still_shrunk() -> None:
    data, _ = normalize_image(_png(2800, 2800))
    assert max(_open(data).size) == 1568


def test_at_most_two_images_decode_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    from kampung_server import images

    lock = threading.Lock()
    running = [0, 0]  # now, most seen
    real = images.ImageOps.exif_transpose

    def slow(img: Image.Image) -> Image.Image:
        with lock:
            running[0] += 1
            running[1] = max(running[1], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return real(img)

    monkeypatch.setattr(images.ImageOps, "exif_transpose", slow)
    data = _png(32, 32)
    threads = [threading.Thread(target=normalize_image, args=(data,)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert running[1] == 2
