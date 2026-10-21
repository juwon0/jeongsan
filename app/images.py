import hashlib
import io

from PIL import Image, ImageOps

try:  # iPhones save HEIC by default
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - HEIC photos then fail to open with a clear message
    pass
else:
    register_heif_opener()

MAX_SIDE = 1800  # small print stays legible, and the model gets a photo it can read quickly
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


class BadPhoto(ValueError):
    """The upload looked like a photo but could not be opened."""


def sniff(data: bytes) -> str | None:
    """Check the file signature instead of trusting the file name or the browser's Content-Type."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return "heic"
    return None


def photo_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def prepare(data: bytes) -> bytes:
    """Upright, at most MAX_SIDE pixels, re-encoded as JPEG. EXIF, GPS included, does not survive the trip."""
    try:
        with Image.open(io.BytesIO(data)) as original:
            img = ImageOps.exif_transpose(original)
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                img = Image.new("RGB", rgba.size, "white")
                img.paste(rgba, mask=rgba.getchannel("A"))
            else:
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, "JPEG", quality=90)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise BadPhoto(str(exc)) from exc
    return out.getvalue()
