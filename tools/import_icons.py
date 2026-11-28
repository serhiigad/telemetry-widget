# -*- coding: utf-8 -*-
"""Нарезает iconpack.png (4×4, белые иконки на чёрном) в отдельные PNG.

Запуск:  python tools/import_icons.py [путь_к_листу]
Результат: assets/icons/<name>.png — 64×64, белый цвет, альфа из яркости.
"""
import os
import sys

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "assets", "icons")
SIZE = 64
PAD = 4

NAMES = [
    "cpu", "gpu", "wireless", "headphones",
    "earbuds", "mouse", "keyboard", "gamepad",
    "speaker", "watch", "phone", "unknown",
    "battery", "charging", "warning", "thermometer",
]


def main(path=None):
    path = path or os.path.join(BASE, "iconpack.png")
    src = Image.open(path).convert("L")
    w, h = src.size
    cols = rows = 4
    cw, ch = w / cols, h / rows
    os.makedirs(OUT, exist_ok=True)
    preview = Image.new("RGBA", (4 * 96, 4 * 96), (18, 18, 24, 255))
    draw = ImageDraw.Draw(preview)
    for idx, name in enumerate(NAMES):
        row, col = divmod(idx, cols)
        x0, y0 = int(col * cw), int(row * ch)
        x1, y1 = int((col + 1) * cw), int((row + 1) * ch)
        cell = src.crop((x0, y0, x1, y1))
        mask = cell.point(lambda v: 255 if v > 40 else 0)
        bbox = mask.getbbox()
        if bbox is None:
            raise SystemExit(f"пустая ячейка: {name}")
        glyph = cell.crop(bbox)
        # вписываем в квадрат, сохраняя пропорции
        box = SIZE - PAD * 2
        scale = min(box / glyph.width, box / glyph.height)
        nw, nh = max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))
        glyph = glyph.resize((nw, nh), Image.LANCZOS)
        alpha = glyph.point(lambda v: v)          # яркость → прозрачность
        icon = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 0))
        white = Image.new("L", (nw, nh), 255)
        icon.paste(Image.merge("RGBA", (white, white, white, alpha)),
                   ((SIZE - nw) // 2, (SIZE - nh) // 2))
        icon.save(os.path.join(OUT, name + ".png"))
        # превью 4×4
        pv = icon.resize((96, 96), Image.LANCZOS)
        px, py = col * 96, row * 96
        preview.alpha_composite(pv, (px, py))
        draw.rectangle([px + 2, py + 2, px + 93, py + 93],
                       outline=(255, 255, 255, 24))
        draw.text((px + 8, py + 80), name, fill=(160, 166, 180, 255))
    preview.convert("RGB").save(os.path.join(OUT, "_preview.png"))
    print("готово:", len(NAMES), "иконок →", OUT)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
