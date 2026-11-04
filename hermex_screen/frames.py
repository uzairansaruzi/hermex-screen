"""Turns a driver capture into what the viewer reads: a JPEG no wider than 1920 pixels, skipped
when the screen hasn't changed. The driver's capture has no pointer, so the phone's is drawn in."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Optional

MAX_WIDTH = 1920
_QUALITY = 70
_CURSOR = [(0, 0), (0, 18), (5, 14), (9, 22), (12, 20), (8, 13), (14, 13)]


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    width: int
    height: int
    source_width: int
    source_height: int


def encode(png: bytes, pointer: Optional[tuple[int, int]]) -> Frame:
    from PIL import Image, ImageDraw
    with Image.open(io.BytesIO(png)) as source:
        image = source.convert("RGB")
    source_width, source_height = image.size
    if source_width > MAX_WIDTH:
        image = image.resize((MAX_WIDTH, round(source_height * MAX_WIDTH / source_width)), Image.Resampling.BILINEAR)
    if pointer is not None:
        points = [(pointer[0] + x, pointer[1] + y) for x, y in _CURSOR]
        ImageDraw.Draw(image).polygon(points, fill=(255, 255, 255), outline=(0, 0, 0))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=_QUALITY)
    return Frame(out.getvalue(), image.width, image.height, source_width, source_height)


def fingerprint(png: bytes, pointer: Optional[tuple[int, int]]) -> bytes:
    """What decides that the viewer has this frame already."""
    digest = hashlib.blake2b(png, digest_size=16)
    digest.update(repr(pointer).encode())
    return digest.digest()
