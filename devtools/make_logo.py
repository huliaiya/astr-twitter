# -*- coding: utf-8 -*-
"""生成插件图标 logo.png（纯标准库，不依赖 Pillow）。

    python devtools/make_logo.py [输出路径]

画的是：推特蓝圆角方块 + 白色 X + 右下角白色播放三角（表示「解析出媒体」）。
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZE = 256
BLUE = (29, 155, 240, 255)
WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)
RADIUS = 56


def make_pixels(size: int = SIZE) -> list[list[tuple[int, int, int, int]]]:
    pixels: list[list[tuple[int, int, int, int]]] = []
    center = size / 2
    thickness = size * 0.085
    margin = size * 0.26
    # 播放三角的三个顶点
    tri = (
        (size * 0.60, size * 0.58),
        (size * 0.60, size * 0.86),
        (size * 0.86, size * 0.72),
    )

    def in_triangle(x: float, y: float) -> bool:
        (x1, y1), (x2, y2), (x3, y3) = tri
        d1 = (x - x2) * (y1 - y2) - (x1 - x2) * (y - y2)
        d2 = (x - x3) * (y2 - y3) - (x2 - x3) * (y - y3)
        d3 = (x - x1) * (y3 - y1) - (x3 - x1) * (y - y1)
        has_neg = d1 < 0 or d2 < 0 or d3 < 0
        has_pos = d1 > 0 or d2 > 0 or d3 > 0
        return not (has_neg and has_pos)

    def in_rounded_square(x: float, y: float) -> bool:
        for corner_x in (RADIUS, size - RADIUS):
            for corner_y in (RADIUS, size - RADIUS):
                if (x < RADIUS or x > size - RADIUS) and (
                    y < RADIUS or y > size - RADIUS
                ):
                    if abs(x - corner_x) > RADIUS and abs(y - corner_y) > RADIUS:
                        continue
                    if (x - corner_x) ** 2 + (y - corner_y) ** 2 > RADIUS**2:
                        inside_x = RADIUS <= x <= size - RADIUS
                        inside_y = RADIUS <= y <= size - RADIUS
                        if not (inside_x or inside_y):
                            return False
        return True

    for py in range(size):
        row: list[tuple[int, int, int, int]] = []
        for px in range(size):
            x = px + 0.5
            y = py + 0.5
            if not in_rounded_square(x, y):
                row.append(TRANSPARENT)
                continue
            color = BLUE
            # 白色 X：两条对角线附近
            if abs(y - x) < thickness or abs(y - (size - x)) < thickness:
                if margin <= x <= size - margin and margin <= y <= size - margin:
                    color = WHITE
            if in_triangle(x, y):
                color = WHITE
            row.append(color)
        pixels.append(row)
    return pixels


def write_png(path: Path, pixels: list[list[tuple[int, int, int, int]]]) -> None:
    height = len(pixels)
    width = len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "logo.png"
    write_png(out, make_pixels())
    print(f"已生成 {out} ({out.stat().st_size} 字节)")


if __name__ == "__main__":
    main()
