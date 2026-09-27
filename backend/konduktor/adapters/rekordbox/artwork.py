"""Cover art the way a Pioneer library stores it: two small square JPEGs.

Measured on a rekordbox 7 USB export rather than assumed:

  * two sizes per image — **80 x 80** (`<n>.jpg`) and **240 x 240** (`<n>_m.jpg`);
  * **baseline JPEG at quality 85, 4:2:0** — its quantisation table is libjpeg's
    standard table scaled to exactly 85, with no EXIF and a plain JFIF header;
  * a non-square cover is **letterboxed onto black**, aspect kept and centred.
    Motorola's 4112 x 1112 art matches rekordbox's own to a mean of 1.8 levels
    that way; a centre crop is off by 26, a stretch by 19.5.

Where the files go and how the database points at them is the target's business
(a OneLibrary drive: `PIONEER/Artwork/00001/`, see its exporter).
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

SMALL, MEDIUM = 80, 240
_QUALITY = 85


def _square(image, size: int):
    from PIL import Image

    fitted = image.copy()
    fitted.thumbnail((size, size), Image.BICUBIC)   # the closest of Pillow's filters
    canvas = Image.new("RGB", (size, size), (0, 0, 0))
    canvas.paste(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
    return canvas


def _jpeg(image) -> bytes:
    out = io.BytesIO()
    # subsampling=2 is 4:2:0; progressive/optimize off = the baseline rekordbox writes.
    image.save(out, format="JPEG", quality=_QUALITY, subsampling=2,
               progressive=False, optimize=False)
    return out.getvalue()


def pioneer_jpegs(data: bytes) -> tuple[bytes, bytes] | None:
    """(80 px JPEG, 240 px JPEG) from any image the source library holds, or None
    when it cannot be read — a bad cover costs the artwork, not the export."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as src:
            src.load()
            rgb = src.convert("RGB")      # PNG alpha / palette / CMYK -> RGB
    except Exception as ex:  # noqa: BLE001 — any unreadable image
        log.warning("could not read cover art (%s); exporting without it", ex)
        return None
    return _jpeg(_square(rgb, SMALL)), _jpeg(_square(rgb, MEDIUM))
