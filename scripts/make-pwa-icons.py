#!/usr/bin/env python3
"""Regenerate the default PWA icons in web/icons/ (stdlib only).

A vinyl record with a play triangle on the Arcade colours. Run from anywhere:
python3 scripts/make-pwa-icons.py
"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "web" / "icons"
BG = (10, 10, 10)
DISC = (24, 24, 28)
GROOVE = (60, 60, 68)
LABEL = (255, 44, 65)
PLAY = (10, 10, 10)
SS = 3  # supersampling per axis


def pixel(x, y, scale):
    """Colour at (x, y) in unit coordinates; `scale` shrinks the artwork."""
    dx, dy = (x - 0.5) / scale, (y - 0.5) / scale
    r2 = dx * dx + dy * dy
    if r2 > 0.46**2:
        return BG
    # Play triangle on the label
    if r2 < 0.2**2:
        px, py = dx + 0.04, dy
        if -0.1 < px < 0.1 and abs(py) < (0.1 - px) * 0.58:
            return PLAY
        return LABEL
    for ring in (0.26, 0.31, 0.36, 0.41):
        if abs(r2**0.5 - ring) < 0.006:
            return GROOVE
    return DISC


def render(size, scale):
    rows = []
    for j in range(size):
        row = bytearray([0])
        for i in range(size):
            acc = [0, 0, 0]
            for sj in range(SS):
                for si in range(SS):
                    c = pixel(
                        (i + (si + 0.5) / SS) / size,
                        (j + (sj + 0.5) / SS) / size,
                        scale,
                    )
                    for k in range(3):
                        acc[k] += c[k]
            row += bytes(v // (SS * SS) for v in acc)
        rows.append(bytes(row))
    return rows


def png(size, scale, path):
    raw = b"".join(render(size, scale))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw, 9))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    png(192, 1.08, OUT / "icon-192.png")
    png(512, 1.08, OUT / "icon-512.png")
    png(180, 1.08, OUT / "apple-touch-icon.png")
    # Maskable: full-bleed background, artwork inside the 80 % safe circle.
    png(512, 0.78, OUT / "icon-maskable-512.png")
