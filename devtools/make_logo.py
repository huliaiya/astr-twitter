# -*- coding: utf-8 -*-
"""生成插件图标 logo.png —— 用官方 X 标志，纯标准库（不依赖 Pillow / cairosvg）。

    python devtools/make_logo.py                 # 生成 logo.png
    python devtools/make_logo.py --preview       # 终端 ASCII 预览，方便肉眼检查
    python devtools/make_logo.py --invert        # 白底黑 X（默认是黑底白 X）
    python devtools/make_logo.py out.png

标志来自 simple-icons 的官方 X 字形（与 X 品牌标志一致，CC0 收录）：
    X_PATH = "M14.234 10.162 ... z"   （24x24 视图框，纯直线路径）

流程：解析路径 → 4 倍超采样扫描线填充（抗锯齿）→ 合成到圆角方底 → 手写 PNG。
"""

from __future__ import annotations

import re
import struct
import sys
import zlib
from pathlib import Path

# --------------------------------------------------------------------------- #
# 官方 X 字形（simple-icons: x.svg，24x24）
# --------------------------------------------------------------------------- #
VIEWBOX = 24.0
X_PATH = (
    "M14.234 10.162 22.977 0h-2.072l-7.591 8.824L7.251 0H.258l9.168 13.343"
    "L.258 24H2.33l8.016-9.318L16.749 24h6.993zm-2.837 3.299-.929-1.329"
    "L3.076 1.56h3.182l5.965 8.532.929 1.329 7.754 11.09h-3.182z"
)

SIZE = 256
SS = 4  # 超采样倍数（每边），4 → 16 个子像素
GLYPH_RATIO = 0.62  # X 占图标宽度的比例（其余是留白）
CORNER_RATIO = 0.22  # 圆角半径占边长比例

TOKEN_RE = re.compile(r"[MmLlHhVvZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
NUM_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")


def parse_path(d: str) -> list[list[tuple[float, float]]]:
    """解析只含直线命令（M/L/H/V/Z）的 SVG 路径为若干闭合多边形。"""
    tokens = TOKEN_RE.findall(d.replace(",", " "))
    polygons: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    x = y = start_x = start_y = 0.0
    command = ""
    index = 0

    def take_numbers(count: int) -> list[float] | None:
        nonlocal index
        values: list[float] = []
        while len(values) < count:
            if index >= len(tokens) or not NUM_RE.fullmatch(tokens[index]):
                return None
            values.append(float(tokens[index]))
            index += 1
        return values

    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token
            index += 1
            if command in "Zz":
                if current:
                    polygons.append(current)
                    current = []
                x, y = start_x, start_y
                continue
        if not command:
            raise ValueError("路径不是以命令开头")
        # 隐式重复：M/m 之后的坐标对按 L/l 处理
        effective = command
        if effective in ("M", "m") and current:
            effective = "L" if effective == "M" else "l"

        if effective in "Mm":
            values = take_numbers(2)
            if values is None:
                break
            if current:
                polygons.append(current)
            x, y = (values if effective == "M" else (x + values[0], y + values[1]))
            start_x, start_y = x, y
            current = [(x, y)]
        elif effective in "Ll":
            values = take_numbers(2)
            if values is None:
                break
            x, y = (
                values if effective == "L" else (x + values[0], y + values[1])
            )
            current.append((x, y))
        elif effective in "Hh":
            values = take_numbers(1)
            if values is None:
                break
            x = values[0] if effective == "H" else x + values[0]
            current.append((x, y))
        elif effective in "Vv":
            values = take_numbers(1)
            if values is None:
                break
            y = values[0] if effective == "V" else y + values[0]
            current.append((x, y))
        else:
            raise ValueError(f"不支持的路径命令：{command}")

    if current:
        polygons.append(current)
    return polygons


def fill_mask(
    polygons: list[list[tuple[float, float]]],
    width: int,
    height: int,
    scale: float,
    offset: float,
    even_odd: bool = False,
) -> list[bytearray]:
    """扫描线填充，返回 width×height 的覆盖率（0-255）灰度掩膜。

    even_odd=False 用非零环绕规则（与浏览器默认一致）。
    """
    edges: list[tuple[float, float, float, float, int]] = []
    for polygon in polygons:
        points = [(px * scale + offset, py * scale + offset) for px, py in polygon]
        for i in range(len(points)):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % len(points)]
            if y1 != y2:
                # 记录方向符号，供非零环绕使用
                edges.append((x1, y1, x2, y2, 1 if y2 > y1 else -1))

    mask = [bytearray(width) for _ in range(height)]
    for row in range(height):
        y_center = row + 0.5
        crossings: list[tuple[float, int]] = []
        for x1, y1, x2, y2, direction in edges:
            if (y1 <= y_center < y2) or (y2 <= y_center < y1):
                t = (y_center - y1) / (y2 - y1)
                crossings.append((x1 + t * (x2 - x1), direction))
        if not crossings:
            continue
        crossings.sort()
        spans: list[tuple[int, int]] = []
        if even_odd:
            for i in range(0, len(crossings) - 1, 2):
                spans.append((crossings[i][0], crossings[i + 1][0]))
        else:
            winding = 0
            span_start = 0.0
            for position, direction in crossings:
                if winding == 0:
                    span_start = position
                winding += direction
                if winding == 0:
                    spans.append((span_start, position))
        line = mask[row]
        for left, right in spans:
            start = max(0, int(left))
            end = min(width - 1, int(right))
            for column in range(start, end + 1):
                pixel_left, pixel_right = column, column + 1
                overlap = min(right, pixel_right) - max(left, pixel_left)
                if overlap > 0:
                    value = int(round(255 * min(1.0, overlap)))
                    if value > line[column]:
                        line[column] = value
    return mask


def downsample(mask: list[bytearray], factor: int) -> list[bytearray]:
    """把超采样掩膜按 factor×factor 平均，得到抗锯齿结果。"""
    height = len(mask) // factor
    width = len(mask[0]) // factor
    total = factor * factor
    out: list[bytearray] = []
    for row in range(height):
        line = bytearray(width)
        for column in range(width):
            acc = 0
            for dy in range(factor):
                source = mask[row * factor + dy]
                base = column * factor
                acc += sum(source[base : base + factor])
            line[column] = acc // total
        out.append(line)
    return out


def rounded_square_mask(size: int, radius: int) -> list[bytearray]:
    """圆角方形掩膜（抗锯齿：按到圆心的距离算覆盖率）。"""
    mask = [bytearray(size) for _ in range(size)]
    for row in range(size):
        line = mask[row]
        for column in range(size):
            inside = 0
            for sub_y in (0.25, 0.75):
                for sub_x in (0.25, 0.75):
                    x = column + sub_x
                    y = row + sub_y
                    if x < radius and y < radius:
                        ok = (x - radius) ** 2 + (y - radius) ** 2 <= radius**2
                    elif x > size - radius and y < radius:
                        ok = (x - (size - radius)) ** 2 + (y - radius) ** 2 <= radius**2
                    elif x < radius and y > size - radius:
                        ok = (x - radius) ** 2 + (y - (size - radius)) ** 2 <= radius**2
                    elif x > size - radius and y > size - radius:
                        ok = (
                            (x - (size - radius)) ** 2 + (y - (size - radius)) ** 2
                            <= radius**2
                        )
                    else:
                        ok = True
                    inside += 1 if ok else 0
            line[column] = round(255 * inside / 4)
    return mask


def compose(
    size: int = SIZE,
    *,
    invert: bool = False,
    supersample: int = SS,
) -> list[list[tuple[int, int, int, int]]]:
    """合成图标：圆角方底 + 官方 X 字形。"""
    background = (255, 255, 255, 255) if invert else (0, 0, 0, 255)
    foreground = (0, 0, 0, 255) if invert else (255, 255, 255, 255)
    transparent = (0, 0, 0, 0)

    big = size * supersample
    glyph = size * GLYPH_RATIO
    scale = (glyph * supersample) / VIEWBOX
    offset = ((size - glyph) / 2) * supersample
    polygons = parse_path(X_PATH)
    mask = downsample(
        fill_mask(polygons, big, big, scale, offset), supersample
    )
    corners = downsample(
        rounded_square_mask(big, int(size * CORNER_RATIO * supersample)), supersample
    )

    pixels: list[list[tuple[int, int, int, int]]] = []
    for row in range(size):
        line: list[tuple[int, int, int, int]] = []
        for column in range(size):
            alpha = corners[row][column] / 255
            if alpha <= 0:
                line.append(transparent)
                continue
            coverage = mask[row][column] / 255
            r = round(background[0] * (1 - coverage) + foreground[0] * coverage)
            g = round(background[1] * (1 - coverage) + foreground[1] * coverage)
            b = round(background[2] * (1 - coverage) + foreground[2] * coverage)
            line.append((r, g, b, int(round(alpha * 255))))
        pixels.append(line)
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


def preview(pixels: list[list[tuple[int, int, int, int]]], columns: int = 48) -> None:
    """终端 ASCII 预览（纯标准库肉眼检查用）。"""
    size = len(pixels)
    step = size / columns
    ramp = " .:-=+*#%@"
    for row in range(columns // 2):
        line = []
        for column in range(columns):
            x = int(column * step)
            y = int(row * 2 * step)
            r, g, b, a = pixels[y][x]
            if a < 40:
                line.append(" ")
                continue
            # 白 X 在黑底上：亮度就是字的覆盖率
            brightness = (r + g + b) / 3 / 255
            line.append(ramp[min(len(ramp) - 1, int(brightness * (len(ramp) - 1)))])
        print("".join(line))


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    invert = "--invert" in sys.argv
    out = Path(args[0]) if args else Path(__file__).resolve().parents[1] / "logo.png"
    pixels = compose(invert=invert)
    if "--preview" in sys.argv:
        preview(pixels)
        return
    write_png(out, pixels)
    print(f"已生成 {out} ({out.stat().st_size} 字节, {SIZE}x{SIZE}, 官方 X 标志)")


if __name__ == "__main__":
    main()
