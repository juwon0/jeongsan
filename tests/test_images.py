import io

import pytest
from PIL import Image

from app.images import MAX_SIDE, BadPhoto, photo_id, prepare, sniff


def test_sniff_checks_the_bytes_not_the_name(make_photo):
    assert sniff(make_photo()) == "jpeg"
    assert sniff(make_photo(fmt="PNG")) == "png"
    assert sniff(make_photo(fmt="WEBP")) == "webp"
    assert sniff(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00") == "heic"
    assert sniff(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00") is None  # a video
    assert sniff(b"%PDF-1.7") is None
    assert sniff(b"") is None


def test_prepare_shrinks_big_photos_and_keeps_small_ones(make_photo):
    big = Image.open(io.BytesIO(prepare(make_photo((4032, 3024)))))
    small = Image.open(io.BytesIO(prepare(make_photo((800, 600)))))

    assert big.format == "JPEG" and max(big.size) == MAX_SIDE
    assert small.size == (800, 600)


def test_prepare_turns_the_photo_upright_and_drops_exif(make_photo):
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to view
    exif[0x8825] = {2: (37.0, 30.0, 0.0)}  # a GPS position
    out = Image.open(io.BytesIO(prepare(make_photo((300, 100), exif=exif.tobytes()))))

    assert out.size == (100, 300)
    assert dict(out.getexif()) == {}


def test_prepare_puts_transparent_screenshots_on_white(make_photo):
    out = Image.open(io.BytesIO(prepare(make_photo((10, 10), color=(0, 0, 0, 0), fmt="PNG"))))

    assert out.mode == "RGB"
    assert out.getpixel((5, 5)) == (255, 255, 255)


def test_prepare_converts_iphone_heic(make_photo):
    pillow_heif = pytest.importorskip("pillow_heif")
    if not pillow_heif.libheif_info().get("HEIF"):
        pytest.skip("this libheif build cannot write HEIC")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "white").save(buf, "HEIF")

    assert sniff(buf.getvalue()) == "heic"
    assert Image.open(io.BytesIO(prepare(buf.getvalue()))).format == "JPEG"


def test_prepare_rejects_a_broken_photo(make_photo):
    with pytest.raises(BadPhoto):
        prepare(make_photo()[:40])
    with pytest.raises(BadPhoto):
        prepare(b"\xff\xd8\xff not really a jpeg")


def test_photo_id_is_stable_and_short(make_photo):
    assert photo_id(make_photo()) == photo_id(make_photo())
    assert photo_id(make_photo()) != photo_id(make_photo(color="black"))
    assert len(photo_id(b"x")) == 16
