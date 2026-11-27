# -*- coding: utf-8 -*-
"""Готовит скриншоты и обложки для GitHub (синтетические данные)."""
import os
import shutil
import sys
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from PySide6.QtCore import QEventLoop, QPoint, QSize, QTimer, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import app as A
import history as H
import monitor

OUT = os.path.join(BASE, "screenshots")
RAW = os.path.join(OUT, "_raw")
os.makedirs(RAW, exist_ok=True)

NOW = time.time()
ALIASES = {
    "Sony WH-1000XM4": "Sony",
    "Logitech G Pro X Superlight": "Superlight",
    "Keychron K3": "Keychron",
    "Xbox Wireless Controller": "Xbox",
}
CFG = {
    "font": "Segoe UI", "scale": 1.0, "opacity": 100,
    "hover_fade": False, "hover_opacity": 5, "always_on_top": False,
    "refresh_sec": 2.0, "accent": "#E31937", "auto_accent": True,
    "pos": None, "hide_offline": False, "ignore": [],
    "minimal": False, "autostart": False,
    "headphones": {"match": ["sony"]},
    "mouse": {"match": ["superlight"]},
    "low_battery": 20, "overheat": 85,
    "names": [[k, v] for k, v in ALIASES.items()],
    "screen": "",
}
DEVICES = [
    {"name": "Sony WH-1000XM4", "online": True, "level": 82},
    {"name": "Logitech G Pro X Superlight", "online": True, "level": 46},
    {"name": "Keychron K3", "online": True, "level": 100},
    {"name": "Xbox Wireless Controller", "online": False, "level": None},
]


def snapshot():
    return {
        "cpu_temp": 61, "cpu_load": 24,
        "gpu_temp": 67, "gpu_load": 38,
        "bt": [dict(d) for d in DEVICES],
    }


A.load_config = lambda: dict(CFG)
A.save_config = lambda _cfg: None
A.HISTORY_PATH = os.path.join(OUT, "_history.json")
A.STATS_PATH = os.path.join(OUT, "_stats.json")
monitor.snapshot = snapshot

app = QApplication(["assets", "-platform", "windows"])
app.setQuitOnLastWindowClosed(False)


def pump(ms=200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def grab_hd(widget, path, scale=2):
    """Рендер окна в 2× без потери резкости текста."""
    size = widget.size()
    img = QImage(QSize(size.width() * scale, size.height() * scale),
                 QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.scale(scale, scale)
    widget.render(painter, QPoint(0, 0))
    painter.end()
    img.save(path)
    return size.width() * scale, size.height() * scale


def save_widget(w, path):
    w.setWindowOpacity(1.0)
    w.show()
    pump(350)
    result = grab_hd(w, path)
    w.hide()
    return result


w = A.Widget()
w.move(-20000, -20000)
if w.tray is not None:
    w.tray.hide()

# ---- полноразмерный виджет
w.cfg.update(CFG)
w.cfg["minimal"] = False
w.cfg["hide_offline"] = False
w.apply_config()
pump(250)
print("full:", save_widget(w, os.path.join(RAW, "widget-full.png")))

# ---- компактный виджет
w.cfg["minimal"] = True
w.cfg["hide_offline"] = True
w.apply_config()
pump(250)
print("compact:", save_widget(w, os.path.join(RAW, "widget-compact.png")))

# ---- окно настроек
w.cfg.update(CFG)
w.cfg["minimal"] = False
w.cfg["hide_offline"] = False
w.cfg["ignore"] = ["Xbox Wireless Controller"]
w.apply_config()
pump(250)
dlg = A.SettingsDialog(w)
dlg.show()
pump(350)
print("settings:", grab_hd(dlg, os.path.join(RAW, "settings.png")))
dlg.close()

# ---- окно истории с реалистичными данными
hist = H.History(os.path.join(OUT, "_shot_hist.json"))
stats = H.ChargeStats(os.path.join(OUT, "_shot_stats.json"))
levels = {
    "Sony WH-1000XM4": (96, 58),
    "Logitech G Pro X Superlight": (88, 41),
    "Keychron K3": (100, 74),
    "Xbox Wireless Controller": (70, 52),
}
for name, (top, bottom) in levels.items():
    points = []
    for i in range(289):
        level = top - (top - bottom) * i / 288
        points.append([NOW - 24 * 3600 + i * 300, int(round(level))])
    hist.devices[name.casefold()] = {"name": name, "points": points}
    daily = {}
    rates = [2.6, 1.9, 2.3, 1.5, 2.0, 1.7]
    hours = [3.6, 4.1, 4.6, 5.1, 5.6, 6.1]
    for d in range(6):
        day = time.strftime("%Y-%m-%d", time.localtime(NOW - d * 86400))
        rate = rates[(d + bottom) % len(rates)]
        daily[day] = {"drain": rate * hours[d], "online": hours[d] * 3600}
    stats.devices[name.casefold()] = {
        "name": name, "last_seen": NOW,
        "session": {"started": NOW - 3 * 3600, "start_level": 70,
                    "low": int(bottom), "last_level": int(bottom),
                    "active": 3 * 3600, "last_seen": NOW},
        "sessions": [{"started": NOW - 30 * 3600,
                      "ended": NOW - 4 * 3600,
                      "seconds": 22 * 3600, "from": top - 4,
                      "to": bottom}],
        "daily": daily,
    }
fake = QWidget()
fake.cfg = {"names": [[k, v] for k, v in ALIASES.items()]}
fake.history = hist
fake.stats = stats
fake._is_ignored = lambda _n: False
hd = A.HistoryDialog(fake)
hd.show()
pump(500)
hd.resize(560, 700)
pump(350)
print("history:", grab_hd(hd, os.path.join(RAW, "history.png")))
hd.close()
w.close()
app.quit()

# ---------------------------------------------------------------- композиция
BG_TOP = (10, 13, 20)
BG_BOTTOM = (23, 28, 40)
ACCENT = (227, 25, 55)
MUTED = (176, 181, 194)
DIM = (116, 121, 136)


def font(name, size):
    for path in (name, "segoeuib.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(os.path.join(r"C:\Windows\Fonts", path),
                                      size)
        except Exception:
            continue
    return ImageFont.load_default()


f_title = font("seguisb.ttf", 50)
f_sub = font("segoeui.ttf", 23)
f_small = font("segoeui.ttf", 18)
f_tiny = font("segoeui.ttf", 15)


def _glow(size, cx, cy, radius, color, strength):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius * 0.72))
    mask = mask.point(lambda v: int(v * strength / 255))
    return Image.new("RGB", size, color), mask


def background(w, h):
    img = Image.new("RGB", (w, h), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t)
                  for i in range(3))
        d.line([(0, y), (w, y)], fill=c)

    # мягкое красное свечение справа сверху и холодное — слева снизу
    warm, warm_mask = _glow((w, h), int(w * 0.86), int(-h * 0.10),
                            int(max(w, h) * 0.62), ACCENT, 58)
    cold, cold_mask = _glow((w, h), int(w * 0.06), int(h * 1.05),
                            int(max(w, h) * 0.55), (58, 82, 142), 34)
    img = Image.composite(warm, img, warm_mask)
    img = Image.composite(cold, img, cold_mask)

    # лёгкая виньетка, чтобы карточка читалась в центре
    vig = Image.new("L", (w, h), 0)
    ImageDraw.Draw(vig).ellipse(
        [int(-w * 0.25), int(-h * 0.35), int(w * 1.25), int(h * 1.35)],
        fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(int(max(w, h) * 0.22)))
    dark = Image.new("RGB", (w, h), (4, 6, 10))
    img = Image.composite(img, dark, vig)

    # очень тихий шум убирает бандинг градиента
    noise = Image.effect_noise((w, h), 9).convert("RGB")
    img = Image.blend(img, noise, 0.012)
    return img


def paste_card(base, path, box, scale=1.0, radius=34, shadow=True):
    card = Image.open(path).convert("RGBA")
    card = card.crop(card.getbbox())
    if scale != 1.0:
        card = card.resize((round(card.width * scale),
                            round(card.height * scale)), Image.LANCZOS)
    mask = Image.new("L", card.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, card.width - 1, card.height - 1], radius=radius, fill=255)
    card.putalpha(Image.composite(card.getchannel("A"), mask, mask))
    ImageDraw.Draw(card).rounded_rectangle(
        [0, 0, card.width - 1, card.height - 1], radius=radius,
        outline=(255, 255, 255, 30), width=2)
    x, y = box
    if shadow:
        sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle(
            [x + 10, y + 18, x + card.width + 10, y + card.height + 18],
            radius=radius, fill=(0, 0, 0, 165))
        sh = sh.filter(ImageFilter.GaussianBlur(24))
        base.alpha_composite(sh)
    base.alpha_composite(card, (x, y))
    return card.size


def save_plain(raw, out, w, h, pad=44, scale=1.0):
    src = Image.open(raw).convert("RGBA")
    src = src.crop(src.getbbox())
    src_w = round(src.width * scale)
    src_h = round(src.height * scale)
    bg = background(max(w, src_w + pad * 2),
                    max(h, src_h + pad * 2)).convert("RGBA")
    x = (bg.width - src_w) // 2
    y = (bg.height - src_h) // 2
    paste_card(bg, raw, (x, y), scale=scale)
    bg.convert("RGB").save(out, quality=95)


save_plain(os.path.join(RAW, "widget-full.png"),
           os.path.join(OUT, "widget-full.png"), 700, 820, scale=0.62)
save_plain(os.path.join(RAW, "widget-compact.png"),
           os.path.join(OUT, "widget-compact.png"), 520, 500, scale=0.90)
save_plain(os.path.join(RAW, "history.png"),
           os.path.join(OUT, "history.png"), 720, 840, scale=0.54)
save_plain(os.path.join(RAW, "settings.png"),
           os.path.join(OUT, "settings.png"), 780, 1090, scale=0.46)

# hero + social preview
hero = background(1280, 800).convert("RGBA")
d = ImageDraw.Draw(hero)
d.text((72, 86), "TELEMETRY", font=f_title, fill=(245, 247, 252))
d.text((72, 142), "WIDGET", font=f_title, fill=ACCENT)
d.text((74, 232), "Температуры, заряд и статистика", font=f_sub, fill=MUTED)
d.text((74, 266), "прямо на рабочем столе Windows", font=f_sub, fill=MUTED)
d.line([(74, 322), (446, 322)], fill=(255, 255, 255, 22), width=1)
bullets = [
    "CPU и GPU: температура, загрузка, история",
    "Заряд Bluetooth-устройств и прогноз разряда",
    "Плавное затухание, автоцвет, свои названия",
]
yy = 358
for text in bullets:
    d.ellipse([76, yy + 7, 84, yy + 15], fill=ACCENT)
    d.text((100, yy), text, font=f_small, fill=(218, 221, 229))
    yy += 38
d.text((74, 718), "PySide6  ·  Windows 10/11  ·  локальные данные",
       font=f_tiny, fill=DIM)
paste_card(hero, os.path.join(RAW, "widget-full.png"), (848, 64),
           scale=0.62, radius=34)
hero.convert("RGB").save(os.path.join(OUT, "hero.png"), quality=95)

social = background(1280, 640).convert("RGBA")
d = ImageDraw.Draw(social)
d.text((64, 92), "TELEMETRY", font=font("seguisb.ttf", 42),
       fill=(245, 247, 252))
d.text((64, 140), "WIDGET", font=font("seguisb.ttf", 42), fill=ACCENT)
d.text((66, 212), "Температуры, заряд и статистика", font=font("segoeui.ttf", 21),
       fill=MUTED)
d.text((66, 244), "на рабочем столе Windows", font=font("segoeui.ttf", 21),
       fill=MUTED)
d.text((66, 562), "PySide6  ·  Windows 10/11  ·  локальные данные",
       font=font("segoeui.ttf", 16), fill=DIM)
paste_card(social, os.path.join(RAW, "widget-full.png"), (914, 30),
           scale=0.54, radius=34)
social.convert("RGB").save(os.path.join(OUT, "social-preview.png"), quality=95)

shutil.rmtree(RAW, ignore_errors=True)
for p in (A.HISTORY_PATH, A.STATS_PATH,
          os.path.join(OUT, "_shot_hist.json"),
          os.path.join(OUT, "_shot_stats.json")):
    if os.path.exists(p):
        os.remove(p)
print("готово:", sorted(os.listdir(OUT)))
