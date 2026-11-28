# -*- coding: utf-8 -*-
"""
Виджет на рабочий стол Windows: температура и загрузка CPU/GPU,
заряд Bluetooth-устройств (наушники, мышь и другие).
Тёмная стеклянная панель в духе Tesla / Apple.

Управление:
  • ЛКМ + перетаскивание — перемещение виджета (в обычном режиме)
  • кнопка-пин в строке TELEMETRY — включить клик-сквозной режим «поверх всех окон»
    (выйти — удерживать её, кнопка отсчитывает секунды 5 → 0; пока режим включён,
    иконка горит акцентным цветом)
  • шестерёнка в строке TELEMETRY или ПКМ — настройки
"""

import ctypes
import json
import math
import os
import sys
import time
import winreg
from ctypes import wintypes

from PySide6.QtCore import (
    Qt, QEasingCurve, QPoint, QPointF, QRect, QRectF, QSize, QTimer,
    QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QIcon,
    QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QColorDialog, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QFontComboBox, QFormLayout, QFrame,
    QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSystemTrayIcon, QToolTip,
    QVBoxLayout, QWidget,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
import monitor  # noqa: E402
import history  # noqa: E402

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
HISTORY_PATH = os.path.join(APP_DIR, "history.json")
STATS_PATH = os.path.join(APP_DIR, "stats.json")
ICON_DIR = os.path.join(APP_DIR, "assets", "icons")
AUTOSTART_NAME = "DesktopTelemetryWidget"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
IPC_NAME = "DesktopTelemetryWidget-single"   # одна копия виджета на сеанс

DEFAULT_CONFIG = {
    "font": "Segoe UI",
    "scale": 1.0,
    "opacity": 90,
    "hover_fade": True,      # гаснуть при наведении (клик-сквозной режим)
    "hover_opacity": 5,      # до какой прозрачности гаснет, %
    "always_on_top": False,  # клик-сквозной режим «поверх всех окон» (📌)
    "refresh_sec": 2.0,
    "accent": "#E31937",
    "auto_accent": True,     # плавно уводить акцент в красный при нагреве/нагрузке
    "status_color": "",      # цвет статусных подписей (пусто = как «accent»)
    "pos": None,
    "hide_offline": False,
    "ignore": [],            # игнорируемые устройства (системные имена)
    "minimal": False,        # компактный режим (только цифры, без полос)
    "autostart": False,      # запуск вместе с Windows (по умолчанию выкл.)
    "headphones": {"match": ["rb-m500b", "panasonic"]},
    "mouse": {"match": ["x11mouse", "mouse"]},
    "low_battery": 20,       # уведомлять о заряде ниже, % (0 = выключено)
    "overheat": 85,          # уведомлять о температуре выше, °C (0 = выключено)
    "names": [],             # свои названия: [["rb-m500b", "Наушники"], ...]
    "screen": "",            # монитор, на котором стоит виджет
}

HP_HINTS = ("наушник", "гарнитур", "headphone", "headset", "airpod", "earbud",
            "buds", "soundcore", "tozo", "wh-", "wf-", "panasonic", "rb-m500")
MS_HINTS = ("мышь", "mouse")

TXT_DIM = "#7A7A84"
TXT_OFF = "#57575F"
TXT_MID = "#B8B8C0"
TXT = "#F2F2F5"

# тёмная тема окна настроек
DIALOG_QSS = """
QDialog { background: #13131A; }
QLabel { color: #E9E9EF; background: transparent; }
QLineEdit, QComboBox, QFontComboBox, QSpinBox, QDoubleSpinBox {
    background: #1D1D26; color: #F2F2F5;
    border: 1px solid #2F2F3A; border-radius: 6px;
    padding: 3px 6px; min-height: 20px;
    selection-background-color: #E31937;
}
QComboBox::drop-down, QFontComboBox::drop-down {
    border: none; width: 18px;
}
QComboBox QAbstractItemView, QFontComboBox QAbstractItemView {
    background: #1D1D26; color: #F2F2F5;
    border: 1px solid #2F2F3A; outline: none;
    selection-background-color: #E31937;
}
QPushButton {
    background: #23232D; color: #F2F2F5;
    border: 1px solid #34343F; border-radius: 6px;
    padding: 6px 16px; min-width: 74px;
}
QPushButton:hover { background: #2C2C38; }
QPushButton:pressed { background: #1B1B23; }
/* стрелки спинбоксов скрываем: при NoButtons Qt не рисует текст,
   если на спинбокс навешен стиль, поэтому прячем их шириной */
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 0; border: none;
}
"""

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg["headphones"] = dict(DEFAULT_CONFIG["headphones"])
    cfg["mouse"] = dict(DEFAULT_CONFIG["mouse"])
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in cfg})
        if isinstance(saved.get("headphones"), dict):
            cfg["headphones"].update(saved["headphones"])
        if isinstance(saved.get("mouse"), dict):
            cfg["mouse"].update(saved["mouse"])
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _launch_command():
    """Команда автозапуска: pythonw.exe (без окна консоли) + app.py."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    elif exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    return f'"{exe}" "{os.path.join(APP_DIR, "app.py")}"'


def autostart_command():
    """Строка из реестра или None, если автозапуск не включён."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return str(winreg.QueryValueEx(k, AUTOSTART_NAME)[0])
    except OSError:
        return None


def autostart_enabled():
    return autostart_command() is not None


def set_autostart(enable):
    """Включает/выключает автозапуск. Возвращает фактическое состояние."""
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  _launch_command())
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        return False
    return autostart_enabled() == bool(enable)


def _no_mouse(widget):
    """События мыши уходят родителю — проще двигать виджет за любое место."""
    widget.setAttribute(Qt.WA_TransparentForMouseEvents)


def _fmt_span(seconds):
    """Длительность по-русски: «2 ч 15 мин», «3 д 4 ч», «45 мин». """
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "—"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return f"{seconds} с"


def _fmt_left(seconds, short=False):
    """Сколько ещё протянёт: «6 ч 20 мин», в компактном режиме — «6 ч»."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    if hours >= 24:
        days = hours // 24
        return f"{days} д" if short else f"{days} д {hours % 24} ч"
    if short:
        return f"{hours} ч" if hours else f"{max(1, minutes)} мин"
    if hours:
        return f"{hours} ч {minutes} мин" if minutes >= 5 else f"{hours} ч"
    return f"{max(1, minutes)} мин"


def _fmt_rate(rate):
    """Расход в процентах за час: «4,2 %/ч»."""
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return "—"
    return f"{rate:.1f}".replace(".", ",") + " %/ч"


def _fmt_until(seconds):
    """Когда наступит событие: «до 18:40» (ближайшие двое суток) или срок."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if seconds <= 0:
        return "скоро"
    if seconds <= 48 * 3600:
        return time.strftime("%H:%M", time.localtime(time.time() + seconds))
    return ""


def _day_label(day):
    """«сегодня», «вчера» или «26.11» для дневной статистики."""
    try:
        date = time.strptime(str(day), "%Y-%m-%d")
    except Exception:
        return str(day)
    now = time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    if str(day) == today:
        return "сегодня"
    if str(day) == time.strftime("%Y-%m-%d",
                                 time.localtime(time.time() - 86400)):
        return "вчера"
    return time.strftime("%d.%m", date)


def _forecast_series(points, window_sec=2 * 3600, max_gap_sec=900):
    """Как менялась оценка «сколько ещё протянёт» по истории заряда.

    Для каждой точки берём окно последних двух часов, суммируем только
    снижение заряда (зарядку игнорируем) и делим на время окна. Получаем
    расход %/ч и запас хода в часах. Возвращает [[ts, часы], ...].
    """
    out = []
    pts = list(points or [])
    for i, (ts, level) in enumerate(pts):
        drain = 0.0
        span = 0.0
        j = i - 1
        while j >= 0 and ts - pts[j][0] <= window_sec:
            gap = pts[j + 1][0] - pts[j][0]
            if 0 < gap <= max_gap_sec:
                span += gap
                drop = pts[j][1] - pts[j + 1][1]
                if drop > 0:
                    drain += drop
            j -= 1
        if span < 1800 or drain < 1 or level <= 0:
            continue
        rate = drain / (span / 3600.0)
        out.append([ts, min(level / rate, 240.0)])
    # сглаживаем: шаг заряда на 1 % даёт заметный «зубец» в оценке
    if len(out) >= 5:
        smooth = []
        for i in range(len(out)):
            chunk = out[max(0, i - 2):i + 3]
            smooth.append([out[i][0],
                           sum(v for _t, v in chunk) / len(chunk)])
        out = smooth
    return out


def _blend(c1, c2, t):
    return QColor(
        round(c1.red() + (c2.red() - c1.red()) * t),
        round(c1.green() + (c2.green() - c1.green()) * t),
        round(c1.blue() + (c2.blue() - c1.blue()) * t),
    )


# --------------------------------------------------------- растровые иконки

_ICON_CACHE = {}


def icon_pixmap(name, px, color, dpr=1.0):
    """Белая PNG-иконка, тонированная в нужный цвет (с кэшем)."""
    px = max(1, int(round(px)))
    color = QColor(color)
    key = (name, px, color.rgba(), round(float(dpr), 2))
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    path = os.path.join(ICON_DIR, str(name) + ".png")
    src = QPixmap(path)
    if src.isNull():
        return None
    src = src.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    tinted = QPixmap(src.size())
    tinted.fill(Qt.GlobalColor.transparent)
    p = QPainter(tinted)
    p.drawPixmap(0, 0, src)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(tinted.rect(), color)
    p.end()
    tinted.setDevicePixelRatio(float(dpr))
    _ICON_CACHE[key] = tinted
    return tinted


class IconLabel(QWidget):
    """Маленькая иконка из assets/icons с тонированием под тему."""

    def __init__(self, name, size, color=TXT_MID, fallback=""):
        super().__init__()
        self._name = name
        self._size = max(8, int(size))
        self._color = QColor(color)
        self._fallback = fallback
        self.setFixedSize(self._size, self._size)
        _no_mouse(self)

    def set_color(self, color):
        color = QColor(color)
        if color != self._color:
            self._color = color
            self.update()

    def set_icon(self, name, fallback=None):
        if name != self._name:
            self._name = name
            self.update()
        if fallback is not None:
            self._fallback = fallback

    def paintEvent(self, _e):
        dpr = max(1.0, float(self.devicePixelRatioF()))
        pm = icon_pixmap(self._name, self._size * dpr, self._color, dpr)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if pm is not None:
            p.drawPixmap(0, 0, pm)
            return
        if self._fallback:
            font = QFont("Segoe UI")
            font.setPointSize(max(6, int(round(self._size * 0.62))))
            p.setFont(font)
            p.setPen(QPen(self._color))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       self._fallback)


def _ex_style(hwnd):
    get_ptr = getattr(ctypes.windll.user32, "GetWindowLongPtrW", None)
    if get_ptr is None:
        return ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    get_ptr.restype = ctypes.c_long
    return get_ptr(hwnd, GWL_EXSTYLE)


def _set_ex_style(hwnd, style):
    set_ptr = getattr(ctypes.windll.user32, "SetWindowLongPtrW", None)
    if set_ptr is None:
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    else:
        set_ptr(hwnd, GWL_EXSTYLE, style)


# ------------------------------------------- глобальный хук мыши (иконки)

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT),
                ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               wintypes.WPARAM, wintypes.LPARAM)

_user32 = ctypes.windll.user32
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC,
                                      wintypes.HINSTANCE, wintypes.DWORD]
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL

# --------------------------------- удержание «поверх всех окон» (topmost)

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
WS_EX_TOPMOST = 0x00000008

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000

_WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD,
                                   wintypes.HWND, wintypes.LONG, wintypes.LONG,
                                   wintypes.DWORD, wintypes.DWORD)

_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HINSTANCE, _WINEVENTPROC,
                                    wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD]
_user32.SetWinEventHook.restype = ctypes.c_void_p
_user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
_user32.UnhookWinEvent.restype = wintypes.BOOL
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND,
                                  ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
GW_HWNDPREV = 3


# ---------------------------------------------------------------- иконки

GLYPH_RULES = (
    (("headphone", "headset", "airpod", "earbud", "buds", "наушник", "гарнитур",
      "wh-", "wf-", "panasonic", "rb-m500"), "🎧"),
    (("mouse", "мышь"), "🖱"),
    (("keyboard", "клавиатур", "kbd"), "⌨"),
    (("gamepad", "controller", "xbox", "джойстик", "геймпад", "dualsense"), "🎮"),
    (("speaker", "колонк", "звук", "jbl"), "🔊"),
    (("watch", "часы", "band", "fit"), "⌚"),
    (("phone", "телефон", "iphone", "galaxy s"), "📱"),
)

# порядок важен: earbuds проверяем раньше общих наушников
ICON_RULES = (
    (("airpod", "earbud", "buds", "tws", "freebuds", "galaxy buds",
      "наушник-вкладыш"), "earbuds"),
    (("headphone", "headset", "наушник", "гарнитур", "wh-", "wf-",
      "panasonic", "rb-m500"), "headphones"),
    (("mouse", "мышь", "superlight"), "mouse"),
    (("keyboard", "клавиатур", "kbd", "keychron"), "keyboard"),
    (("gamepad", "controller", "xbox", "джойстик", "геймпад",
      "dualsense"), "gamepad"),
    (("speaker", "колонк", "звук", "jbl"), "speaker"),
    (("watch", "часы", "band", "fit"), "watch"),
    (("phone", "телефон", "iphone", "galaxy s"), "phone"),
)


def device_icon(name):
    nl = (name or "").lower()
    for words, icon in ICON_RULES:
        if any(w in nl for w in words):
            return icon
    return "unknown"


def device_glyph(name):
    nl = (name or "").lower()
    for words, glyph in GLYPH_RULES:
        if any(w in nl for w in words):
            return glyph
    return "🔵"


def apply_name_map(name, pairs):
    """Своё название устройства по подстроке (регистр не важен)."""
    low = (name or "").lower()
    for pair in pairs or []:
        try:
            pattern, alias = pair[0], pair[1]
        except Exception:
            continue
        if pattern and str(pattern).lower() in low:
            return str(alias)
    return name


# ------------------------------------------------------- векторные иконки

def _pushpin_path(rect):
    """Контур канцелярской кнопки в системе координат 24×24."""
    scale = min(rect.width(), rect.height()) / 24.0

    def pt(x, y):
        return QPointF(rect.left() + x * scale, rect.top() + y * scale)

    path = QPainterPath()
    path.moveTo(pt(16, 9))
    path.lineTo(pt(16, 4))
    path.lineTo(pt(17, 4))
    path.cubicTo(pt(17.55, 4), pt(18, 3.55), pt(18, 3))
    path.cubicTo(pt(18, 2.45), pt(17.55, 2), pt(17, 2))
    path.lineTo(pt(7, 2))
    path.cubicTo(pt(6.45, 2), pt(6, 2.45), pt(6, 3))
    path.cubicTo(pt(6, 3.55), pt(6.45, 4), pt(7, 4))
    path.lineTo(pt(8, 4))
    path.lineTo(pt(8, 9))
    path.cubicTo(pt(8, 10.66), pt(6.66, 12), pt(5, 12))
    path.lineTo(pt(5, 14))
    path.lineTo(pt(10.97, 14))
    path.lineTo(pt(10.97, 21))
    path.lineTo(pt(11.97, 22))
    path.lineTo(pt(12.97, 21))
    path.lineTo(pt(12.97, 14))
    path.lineTo(pt(21, 14))
    path.lineTo(pt(21, 12))
    path.cubicTo(pt(19.34, 12), pt(18, 10.66), pt(18, 9))
    path.closeSubpath()
    return path


def _paint_pin(p, rect, color):
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPath(_pushpin_path(rect))


def _paint_gear(p, rect, color):
    r = QRectF(rect)
    radius = min(r.width(), r.height()) * 0.44
    p.save()
    p.translate(r.center())
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    for i in range(8):
        p.save()
        p.rotate(i * 45.0)
        p.drawRoundedRect(QRectF(-radius * 0.16, -radius * 1.02,
                                 radius * 0.32, radius * 0.36),
                          radius * 0.08, radius * 0.08)
        p.restore()
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(color, max(1.2, radius * 0.34)))
    p.drawEllipse(QRectF(-radius * 0.64, -radius * 0.64,
                         radius * 1.28, radius * 1.28))
    p.restore()


GLYPHS = {"pin": _paint_pin, "gear": _paint_gear}


def paint_glyph(painter, kind, rect, color):
    GLYPHS[kind](painter, rect, QColor(color))


# ------------------------------------------------------------- иконки-кнопки

class IconButton(QWidget):
    """Кнопка-иконка в строке TELEMETRY.

    Состояние показывается самой иконкой (цветом), а не заливкой кнопки.
    Левый клик всегда «съедается» кнопкой: иначе событие всплывёт к виджету,
    тот начнёт перетаскивание и станет владельцем захвата мыши — отпускание
    до кнопки не дойдёт и обработчик не сработает.
    """

    def __init__(self, kind, scale=1.0, family="Segoe UI",
                 on_press=None, on_release=None):
        super().__init__()
        self.kind = kind
        self._scale = float(scale)
        self._family = family
        self._pressed = False
        self._hover = False
        self._active = False
        self._count = None
        self._count_color = None
        self._dimmed = False
        self.accent = QColor("#E31937")
        self.on_press = on_press
        self.on_release = on_release
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self._sc(26), self._sc(26))

    def _sc(self, v):
        return max(1, int(round(v * self._scale)))

    # ---------------------------------------------------------- состояние
    def set_accent(self, color):
        self.accent = QColor(color)
        self.update()

    def set_active(self, on):
        self._active = bool(on)
        self.update()

    def isActive(self):
        return self._active

    def set_hover(self, on):
        """Подсветка под курсором (в клик-сквозном режиме наведение считает виджет)."""
        on = bool(on)
        if on != self._hover:
            self._hover = on
            self.update()

    def set_dimmed(self, on):
        """Недоступная иконка: приглушённая и без реакции на клик."""
        on = bool(on)
        if on != self._dimmed:
            self._dimmed = on
            if on:
                self._hover = False
            self.setCursor(Qt.CursorShape.ArrowCursor if on
                           else Qt.CursorShape.PointingHandCursor)
            self.update()

    def set_count(self, value, color=None):
        """Показать цифру отсчёта вместо иконки (None — снова иконка)."""
        self._count = None if value is None else int(value)
        self._count_color = None if color is None else QColor(color)
        self.update()

    def set_scale(self, scale):
        self._scale = float(scale)
        self.setFixedSize(self._sc(26), self._sc(26))
        self.update()

    def _glyph_color(self):
        if self._dimmed:
            return QColor(TXT_OFF)
        if self._active:
            return self.accent
        return QColor(TXT_MID if self._hover else TXT_DIM)

    # ------------------------------------------------------------ отрисовка
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        if self._hover or self._pressed:
            # подсветка только под курсором/нажатием — постоянной заливки нет
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 26 if self._pressed else 16))
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                              self._sc(7), self._sc(7))
        if self._count is not None:
            p.setPen(QPen(self._count_color or self._glyph_color()))
            font = QFont(self._family)
            font.setPointSize(max(7, int(round(10.5 * self._scale))))
            font.setWeight(QFont.Weight.DemiBold)
            p.setFont(font)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self._count))
            return
        pad = self._sc(6)
        paint_glyph(p, self.kind, rect.adjusted(pad, pad, -pad, -pad),
                    self._glyph_color())

    # ---------------------------------------------------------------- мышь
    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            if self.on_press:
                self.on_press()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            if self.on_release:
                self.on_release()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        e.accept()   # второй клик не должен проваливаться в виджет


class ToggleSwitch(QWidget):
    """Переключатель-ползунок в стиле iOS (тёмная тема виджета)."""

    toggled = Signal(bool)

    def __init__(self, checked=False, accent="#E31937", scale=1.0, parent=None):
        super().__init__(parent)
        self._checked = bool(checked)
        self._pos = 1.0 if self._checked else 0.0
        self._accent = QColor(accent)
        self._scale = float(scale)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self._size())
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def _sc(self, v):
        return max(1, int(round(v * self._scale)))

    def _size(self):
        return QSize(max(36, self._sc(44)), max(19, self._sc(24)))

    def isChecked(self):
        return self._checked

    def setChecked(self, checked, animate=True):
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        target = 1.0 if checked else 0.0
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._anim.stop()
            self._pos = target
            self.update()

    def _on_anim(self, value):
        self._pos = float(value)
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
            self.toggled.emit(self._checked)
            e.accept()
            return
        super().mousePressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        radius = h / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_blend(QColor("#2A2A34"), self._accent, self._pos))
        p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)
        pad = self._sc(2)
        d = h - pad * 2
        x = pad + self._pos * (w - d - pad * 2)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(x, pad, d, d))


class Sparkline(QWidget):
    """Мини-график заряда: время по X, проценты по Y."""

    def __init__(self, accent="#E31937", parent=None):
        super().__init__(parent)
        self._points = []
        self._accent = QColor(accent)
        self._auto = False
        self._fill = True
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setFixedHeight(34)

    def set_points(self, points):
        self._points = list(points or [])
        self.update()

    def set_auto_scale(self, on):
        """Масштаб по данным (для прогноза в часах)."""
        self._auto = bool(on)
        self.update()

    def set_fill(self, on):
        self._fill = bool(on)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 3, -1, -3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 10))
        p.drawRoundedRect(rect, 4, 4)
        if len(self._points) < 2:
            p.setPen(QPen(QColor(TXT_OFF)))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "мало данных")
            return
        t0, t1 = self._points[0][0], self._points[-1][0]
        span = max(1.0, t1 - t0)
        top, bottom = 100.0, 0.0
        if self._auto:
            values = [float(p[1]) for p in self._points]
            lo, hi = min(values), max(values)
            pad = max(0.5, (hi - lo) * 0.15)
            top, bottom = hi + pad, max(0.0, lo - pad)
            if top - bottom < 1.0:
                top = bottom + 1.0
        w, h = rect.width(), rect.height()

        def pt(ts, level):
            x = rect.left() + (ts - t0) / span * w
            level = max(bottom, min(top, level))
            y = rect.bottom() - (level - bottom) / (top - bottom) * h
            return QPointF(x, y)

        path = QPainterPath(pt(*self._points[0]))
        for ts, level in self._points[1:]:
            path.lineTo(pt(ts, level))
        if self._fill:
            fill = QPainterPath(path)
            fill.lineTo(rect.right(), rect.bottom())
            fill.lineTo(rect.left(), rect.bottom())
            fill.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(self._accent.red(), self._accent.green(),
                              self._accent.blue(), 38))
            p.drawPath(fill)
        p.setPen(QPen(self._accent, 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


class DayRates(QWidget):
    """Компактные полоски «средний расход, %/ч» по дням."""

    def __init__(self, accent="#E31937", parent=None):
        super().__init__(parent)
        self._accent = QColor(accent)
        self._data = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setFixedHeight(24)

    def set_rates(self, rates, max_days=5):
        self._data = list(rates or [])[-max_days:]
        self.setFixedHeight(max(24, len(self._data) * 18 + 4))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(QFont("Segoe UI", 8))
        if not self._data:
            p.setPen(QPen(QColor(TXT_OFF)))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "нет данных")
            return
        top = max([1.0] + [float(r["rate"]) for r in self._data])
        row_h = self.height() / len(self._data)
        label_w = 46
        value_w = 72
        for i, item in enumerate(self._data):
            y = i * row_h
            p.setPen(QPen(QColor(TXT_DIM)))
            p.drawText(QRectF(0, y, label_w - 4, row_h),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       _day_label(item["day"]))
            bx = label_w
            bw = max(1.0, self.width() - label_w - value_w)
            bar_h = max(4.0, row_h - 7)
            by = y + (row_h - bar_h) / 2.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 12))
            p.drawRoundedRect(QRectF(bx, by, bw, bar_h), 2, 2)
            fill = min(bw, bw * max(0.0, float(item["rate"])) / top)
            color = QColor(self._accent)
            color.setAlpha(200)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(bx, by, fill, bar_h), 2, 2)
            p.setPen(QPen(QColor(TXT_MID)))
            p.drawText(QRectF(bx + bw + 4, y, value_w - 4, row_h),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       _fmt_rate(item["rate"]))


class HistoryDialog(QDialog):
    """История заряда по устройствам за последние сутки."""

    def __init__(self, widget):
        super().__init__(widget)
        self.widget_ref = widget
        accent = widget.cfg.get("accent", "#E31937")
        self.setWindowTitle("История заряда")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(470)
        self.setStyleSheet(DIALOG_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = QLabel("ЗАРЯД ЗА ПОСЛЕДНИЕ 24 ЧАСА")
        f = title.font()
        f.setPointSize(8)
        f.setWeight(QFont.Weight.DemiBold)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        title.setFont(f)
        title.setStyleSheet(f"color: {TXT_DIM}; background: transparent;")
        root.addWidget(title)

        # строк может стать много (график заряда + прогноз + расход по дням),
        # поэтому список прокручивается и не растягивает окно на весь экран
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(150)
        scroll.setMaximumHeight(580)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }"
                             "QScrollArea > QWidget > QWidget "
                             "{ background: transparent; }")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self._rows = QVBoxLayout(content)
        self._rows.setContentsMargins(0, 0, 22, 0)
        self._rows.setSpacing(12)
        scroll.setWidget(content)
        self._scroll = scroll
        root.addWidget(scroll, 1)

        self._empty = QLabel("Пока нет данных: история копится, пока виджет "
                             "работает (точка раз в 5 минут).")
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet(f"color: {TXT_DIM}; background: transparent;")
        root.addWidget(self._empty)

        bottom = QHBoxLayout()
        clear = QPushButton("Очистить историю")
        clear.clicked.connect(self._clear)
        bottom.addWidget(clear)
        bottom.addStretch(1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        root.addLayout(bottom)

        self._accent = accent
        self._reload()

    def _reload(self):
        while self._rows.count():
            item = self._rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        hist = self.widget_ref.history
        hist.prune()
        stats = getattr(self.widget_ref, "stats", None)
        is_ignored = getattr(self.widget_ref, "_is_ignored",
                             lambda _name: False)
        keys = [k for k in hist.keys() if not is_ignored(hist.name_of(k))]
        if stats is not None:                 # устройства со статистикой, но
            for key in stats.devices:         # без точек за последние сутки
                if key in keys:
                    continue
                name = stats.devices[key].get("name") or key
                if not is_ignored(name) and stats.report(key):
                    keys.append(key)
        self._empty.setVisible(not keys)
        self._scroll.setVisible(bool(keys))
        pairs = self.widget_ref.cfg.get("names") or []
        for key in keys:
            points = hist.series(key)
            rep = stats.report(key) if stats is not None else None
            if not points and not rep:
                continue
            holder = QWidget()
            lay = QVBoxLayout(holder)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(3)
            head = QHBoxLayout()
            if key in hist.devices:
                raw_name = hist.name_of(key)
            elif stats is not None and key in stats.devices:
                raw_name = stats.devices[key].get("name") or key
            else:
                raw_name = key
            name = apply_name_map(raw_name, pairs)
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(f"color: {TXT}; background: transparent;")
            head.addWidget(name_lbl, 1)
            if points:
                levels = [p[1] for p in points]
                last = levels[-1]
                stat = QLabel(f"{last} % · {min(levels)}–{max(levels)} %")
                stat.setStyleSheet(f"color: {TXT_DIM}; background: transparent;"
                                   " font-size: 8pt;")
                head.addWidget(stat)
            lay.addLayout(head)
            # сколько устройство работает от одной зарядки и когда разрядится
            bits = []
            if rep:
                if rep.get("avg"):
                    bits.append(f"от 1 заряда ≈ {_fmt_span(rep['avg'])}")
                if rep.get("active"):
                    bits.append(f"сейчас {_fmt_span(rep['active'])}")
                if rep.get("empty_in"):
                    text = f"до разряда ≈ {_fmt_span(rep['empty_in'])}"
                    clock = _fmt_until(rep["empty_in"])
                    if clock:
                        text += f" (около {clock})"
                    bits.append(text)
                rate = rep.get("current_rate") or rep.get("avg_rate")
                if rate:
                    bits.append(f"расход ≈ {_fmt_rate(rate)}")
                if rep.get("cycles"):
                    bits.append(f"циклов {rep['cycles']}")
            if bits:
                info = QLabel(" · ".join(bits))
                info.setWordWrap(True)
                info.setStyleSheet(f"color: {TXT_DIM}; background: transparent;")
                lay.addWidget(info)
            if points:
                line = Sparkline(self._accent)
                line.set_points(points)
                lay.addWidget(line)
                # прогноз: сколько ещё протянёт в каждой точке истории
                forecast = _forecast_series(points)
                if len(forecast) >= 2:
                    now_hours = forecast[-1][1]
                    cap = QLabel("ПРОГНОЗ · ХВАТИТ ЕЩЁ, Ч"
                                 f"  (сейчас ≈ {_fmt_span(now_hours * 3600)})")
                    cap.setWordWrap(True)
                    cap.setStyleSheet(f"color: {TXT_OFF}; background: transparent;"
                                      " font-size: 8pt;")
                    lay.addWidget(cap)
                    f_line = Sparkline(self._accent)
                    f_line.setFixedHeight(28)
                    f_line.set_auto_scale(True)
                    f_line.set_fill(False)
                    f_line.set_points(forecast)
                    f_line.setToolTip("Оценка по расходу за предыдущие 2 часа")
                    lay.addWidget(f_line)
            # средний расход по дням (накапливается в stats.json)
            if stats is not None:
                rates = stats.daily_rates(key, 7)
                if rates:
                    cap = QLabel("РАСХОД ПО ДНЯМ, %/Ч")
                    cap.setStyleSheet(f"color: {TXT_OFF}; background: transparent;"
                                      " font-size: 8pt;")
                    lay.addWidget(cap)
                    chart = DayRates(self._accent)
                    chart.set_rates(rates)
                    lay.addWidget(chart)
            self._rows.addWidget(holder)
        self.adjustSize()

    def _clear(self):
        self.widget_ref.history.clear()
        stats = getattr(self.widget_ref, "stats", None)
        if stats is not None:
            stats.clear()                 # вместе с графиком — статистика циклов
        self._reload()


# ------------------------------------------------------------------- Виджет

class Widget(QWidget):
    HOLD_TICK_MS = 50
    HOLD_SEC = 5.0           # 5 секунд удержания 📌
    ZERO_HOLD_MS = 700       # сколько держится «0» после разблокировки
    HOVER_POLL_MS = 80

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool)
        self.cfg = load_config()
        self._drag_offset = None
        self._built = False
        self._bt_sig = None
        self._bt_rows = []
        self._bt_devs = []
        self._click_through = bool(self.cfg.get("always_on_top"))
        self._hover_hot = False
        self._ticks = 0
        self._hold_start = None      # time.monotonic() начала удержания 📌
        self._restore_seq = 0
        self._flash_left = 0
        self._flash_base = float(self.cfg.get("opacity", 90)) / 100.0
        self.minimal = bool(self.cfg.get("minimal"))
        self._hook = None            # хэндл хука мыши (клик-сквозной режим)
        self._hook_proc = None       # ссылка, иначе колбэк соберёт GC
        self._settings_open = False

        # плавное затухание под курсором (клик-сквозной режим)
        self._fade = 1.0
        self._fade_target = 1.0
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setDuration(220)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.valueChanged.connect(self._apply_fade)

        # автоцвет: спокойный акцент → красный при нагреве/нагрузке
        self._accent_base = QColor(self.cfg.get("accent", "#E31937"))
        self._accent_hot = QColor("#FF2B1B")
        self._accent_now = QColor(self._accent_base)
        self._accent_factor = 0.0
        self._accent_labels = []       # подписи, окрашенные акцентом сейчас
        self._bars = []                # (полоска, радиус) для перекраски
        self._accent_anim = QVariantAnimation(self)
        self._accent_anim.setDuration(650)
        self._accent_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._accent_anim.valueChanged.connect(self._on_accent_anim)
        self._accent_anim.finished.connect(self._sync_tray)

        # история заряда, уведомления и трей
        self.history = history.History(HISTORY_PATH)
        if self.history.prune():
            self.history.save()
        self.stats = history.ChargeStats(STATS_PATH)
        if self.stats.prune():
            self.stats.save()
        self._low_notified = set()   # устройства, о которых уже предупредили
        self._hot = False            # уже предупредили о перегреве
        self._bt_all = []            # последний полный список устройств (для настроек)
        self.tray = None
        self._tray_actions = {}
        self._history_dlg = None

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("Telemetry Widget")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)   # всегда поверх окон
        # Виджет — единственное «главное» окно приложения. У окон типа Qt::Tool
        # этот флаг по умолчанию снят, и тогда закрытие окна настроек Qt считает
        # закрытием последнего окна — приложение завершалось. Возвращаем флаг:
        # диалог закрывается — виджет живёт, а закрытие самого виджета = выход.
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, True)
        self._sync_autostart()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

        # опрос курсора: над иконками клик-сквозность временно отключается
        self._hover = QTimer(self)
        self._hover.setInterval(self.HOVER_POLL_MS)
        self._hover.timeout.connect(self._poll_hover)

        # удержание 📌 для выхода из клик-сквозного режима
        self._hold = QTimer(self)
        self._hold.setInterval(self.HOLD_TICK_MS)
        self._hold.timeout.connect(self._hold_tick)

        # после разблокировки ждём, пока физическая кнопка мыши будет отпущена:
        # иначе это же нажатие сразу заново закрепляет виджет
        self._pin_guard = False
        self._guard_n = 0
        self._guard = QTimer(self)
        self._guard.setInterval(50)
        self._guard.timeout.connect(self._guard_tick)

        # отложенная запись конфига (настройки применяются на лету)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(lambda: save_config(self.cfg))

        # «поверх всех окон»: хук смены активного окна + страховка по таймеру
        self._fg_hook = None
        self._fg_proc = None
        self._topmost = QTimer(self)
        self._topmost.setInterval(3000)
        self._topmost.timeout.connect(self._enforce_window_state)

        self._build()
        self._apply_flags()
        self._update_pin_look()
        self._setup_tray()
        self._setup_screens()
        self.refresh()
        self.timer.start(int(self.cfg["refresh_sec"] * 1000))
        self._hover.start()
        self._topmost.start()

        # история: подчищаем старые точки раз в час
        self._history_timer = QTimer(self)
        self._history_timer.setInterval(3600 * 1000)
        self._history_timer.timeout.connect(self._prune_history)
        self._history_timer.start()

    # ------------------------------------------------------------ настройки

    def _apply_flags(self):
        self.setWindowOpacity(float(self.cfg.get("opacity", 90)) / 100.0)
        self.show()
        self.raise_()
        self._update_ex_style()
        self._sync_mouse_hook()
        self._install_foreground_watch()
        self._apply_topmost()
        # окно только что создано — Windows может переставить Z-порядок
        QTimer.singleShot(400, self._apply_topmost)
        QTimer.singleShot(1500, self._apply_topmost)

    def _sync_autostart(self):
        """Автозапуск: включён — держим путь в реестре актуальным (папку виджета
        могли перенести), выключен — просто сверяем состояние с конфигом."""
        if bool(self.cfg.get("autostart")) and autostart_command() != _launch_command():
            try:
                set_autostart(True)
            except OSError:
                pass
        self.cfg["autostart"] = autostart_enabled()

    # ------------------------------------------------ трей, уведомления

    def _setup_tray(self):
        """Иконка в области уведомлений: доступ к виджету, когда он закреплён."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = QIcon(self._tray_pixmap())
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Telemetry Widget")
        menu = QMenu()

        self._tray_actions["toggle"] = QAction("Скрыть виджет", menu)
        self._tray_actions["toggle"].triggered.connect(self._toggle_visible)
        menu.addAction(self._tray_actions["toggle"])
        menu.addSeparator()

        act = QAction("Настройки…", menu)
        act.triggered.connect(self._open_settings)
        menu.addAction(act)

        act = QAction("История заряда…", menu)
        act.triggered.connect(self._open_history)
        menu.addAction(act)
        menu.addSeparator()

        act = QAction("Закрепить поверх окон", menu)
        act.setCheckable(True)
        act.triggered.connect(lambda on: self.set_click_through(bool(on)))
        self._tray_actions["pin"] = act
        menu.addAction(act)

        act = QAction("Компактный режим", menu)
        act.setCheckable(True)
        act.triggered.connect(lambda on: self.set_minimal(bool(on)))
        self._tray_actions["minimal"] = act
        menu.addAction(act)
        menu.addSeparator()

        act = QAction("Выход", menu)
        act.triggered.connect(QApplication.instance().quit)
        menu.addAction(act)

        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        self._sync_tray()

    def _tray_pixmap(self):
        """Иконка трея: канцелярская кнопка акцентным цветом."""
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        paint_glyph(p, "pin", QRectF(8, 8, 48, 48),
                    QColor(getattr(self, "_accent_now",
                                   self.cfg.get("accent", "#E31937"))))
        p.end()
        return pm

    def _sync_tray(self):
        if self.tray is None:
            return
        self.tray.setIcon(QIcon(self._tray_pixmap()))
        pin = self._tray_actions.get("pin")
        if pin is not None:
            pin.setChecked(self._click_through)
        mini = self._tray_actions.get("minimal")
        if mini is not None:
            mini.setChecked(bool(self.cfg.get("minimal")))
        tog = self._tray_actions.get("toggle")
        if tog is not None:
            tog.setText("Скрыть виджет" if self.isVisible() else "Показать виджет")

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visible()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._open_settings()

    def _toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self._apply_topmost()
        self._sync_tray()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_mouse_hook()
        QTimer.singleShot(0, self._sync_tray)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._animate_fade(1.0)          # скрытый — не в затухании
        self._sync_mouse_hook()          # скрытый виджет не ловит клики
        QTimer.singleShot(0, self._sync_tray)

    def _notify(self, title, text):
        """Уведомление Windows через иконку трея."""
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, text,
                                  QSystemTrayIcon.MessageIcon.Warning, 6000)

    def _open_history(self):
        if self._history_dlg is not None:
            try:
                self._history_dlg.raise_()
                self._history_dlg.activateWindow()
                return
            except RuntimeError:
                self._history_dlg = None
        dlg = HistoryDialog(self)
        self._history_dlg = dlg
        dlg.finished.connect(lambda *_: setattr(self, "_history_dlg", None))
        dlg.exec()

    def _prune_history(self):
        if self.history.prune():
            self.history.save()
        if self.stats.prune():
            self.stats.save()
        else:
            self.stats.save()        # заодно сбрасываем накопленное на диск

    def display_name(self, name):
        return apply_name_map(name, self.cfg.get("names") or [])

    # ------------------------------------------------------- мониторы

    def _setup_screens(self):
        app = QApplication.instance()
        if app is None:
            return
        app.screenAdded.connect(lambda *_: self._on_screens_changed())
        app.screenRemoved.connect(lambda *_: self._on_screens_changed())
        app.primaryScreenChanged.connect(lambda *_: self._on_screens_changed())
        for scr in app.screens():
            scr.geometryChanged.connect(lambda *_: self._on_screens_changed())

    def _screen_by_name(self, name):
        if not name:
            return None
        for scr in QApplication.screens():
            if scr.name() == name:
                return scr
        return None

    def _on_screens_changed(self):
        """Сменили/отключили монитор — виджет не должен уехать за край."""
        screens = QApplication.screens()
        if not screens:
            return
        rect = self.frameGeometry()
        if any(s.availableGeometry().intersects(rect) for s in screens):
            self._ensure_visible()
            return
        scr = self._screen_by_name(self.cfg.get("screen")) or \
            QApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            self.move(g.left() + 40, g.top() + 84)

    def _ensure_visible(self):
        scr = self.screen() or QApplication.primaryScreen()
        if scr is None:
            return
        g = scr.availableGeometry()
        x = min(max(self.x(), g.left()), max(g.left(), g.right() - self.width()))
        y = min(max(self.y(), g.top()), max(g.top(), g.bottom() - self.height()))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def set_click_through(self, on):
        self._click_through = bool(on)
        self.cfg["always_on_top"] = self._click_through
        save_config(self.cfg)
        self._hover_hot = False
        self._animate_fade(1.0)
        self._sync_mouse_hook()
        self._update_pin_look()
        self._update_ex_style()
        self._sync_tray()

    def _update_ex_style(self):
        """WS_EX_TRANSPARENT = клики проходят сквозь виджет к окнам под ним.

        Пока режим включён, окно целиком «прозрачно» для мыши, а клики по
        иконкам ловит низкоуровневый хук (_mouse_hook) — он срабатывает
        мгновенно, без гонки с таймером наведения.
        """
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        style = _ex_style(hwnd)
        if self._click_through:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        _set_ex_style(hwnd, style)
        self._apply_topmost()

    # ---------------------------------------- «поверх всех окон» (topmost)

    def _apply_topmost(self):
        """Поднять окно в верхнюю полосу Z-порядка, не забирая фокус.

        Qt-флаг WindowStaysOnTopHint применяется только при создании окна,
        поэтому полноэкранные приложения (игры) могут оказаться выше.
        HWND_TOPMOST переставляем сами — при старте, при смене активного
        окна и периодически.
        """
        if not self.isVisible():
            return
        try:
            hwnd = int(self.winId())
            _user32.SetWindowPos(ctypes.c_void_p(hwnd),
                                 ctypes.c_void_p(HWND_TOPMOST),
                                 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                                 | SWP_NOOWNERZORDER)
        except Exception:
            pass

    def _install_foreground_watch(self):
        """Слушаем смену активного окна: игра вышла на передний план — поднимаемся."""
        if self._fg_hook is not None:
            return
        try:
            self._fg_proc = _WINEVENTPROC(self._on_foreground_change)
            self._fg_hook = _user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                None, self._fg_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
            if not self._fg_hook:
                self._fg_proc = None
        except Exception:
            self._fg_hook = None
            self._fg_proc = None

    def _uninstall_foreground_watch(self):
        if self._fg_hook is not None:
            try:
                _user32.UnhookWinEvent(self._fg_hook)
            except Exception:
                pass
            self._fg_hook = None
            self._fg_proc = None

    def _on_foreground_change(self, _hook, _event, hwnd, _obj, _child,
                              _thread, _ms):
        """Другое окно стало активным (например, запустилась игра)."""
        try:
            own = int(self.winId())
        except Exception:
            return
        if hwnd and hwnd == own:
            return
        self._apply_topmost()
        # игры нередко ставят себя поверх уже после получения фокуса
        QTimer.singleShot(350, self._apply_topmost)
        QTimer.singleShot(1500, self._apply_topmost)
        QTimer.singleShot(3000, self._apply_topmost)

    def _enforce_window_state(self):
        """Страховка: Qt/система могли сбросить topmost или клик-сквозность."""
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        if not hwnd:
            return
        if not (_ex_style(hwnd) & WS_EX_TOPMOST) or self._covered_by_topmost():
            self._apply_topmost()        # флаг потерян или чужая программа выше
        style = _ex_style(hwnd)
        want = (style | WS_EX_TRANSPARENT) if self._click_through \
            else (style & ~WS_EX_TRANSPARENT)
        if want != style:
            _set_ex_style(hwnd, want)

    def _covered_by_topmost(self):
        """Есть ли над виджетом видимое topmost-окно, перекрывающее его."""
        try:
            hwnd = int(self.winId())
            own = wintypes.RECT()
            if not _user32.GetWindowRect(hwnd, ctypes.byref(own)):
                return False
            h = _user32.GetWindow(hwnd, GW_HWNDPREV)
            while h:
                if _user32.IsWindowVisible(h) and (_ex_style(int(h)) & WS_EX_TOPMOST):
                    r = wintypes.RECT()
                    if _user32.GetWindowRect(h, ctypes.byref(r)):
                        if (r.left < own.right and own.left < r.right
                                and r.top < own.bottom and own.top < r.bottom):
                            return True
                h = _user32.GetWindow(h, GW_HWNDPREV)
        except Exception:
            pass
        return False

    # ------------------------------------------------ хук мыши для иконок

    def _sync_mouse_hook(self):
        """Хук нужен в клик-сквозном режиме и пока ждём отпускания кнопки после него.

        Скрытый виджет не должен перехватывать клики: без проверки isVisible()
        хук «съедал» бы нажатия в том месте экрана, где виджет был до скрытия.
        """
        if ((self._click_through or self._pin_guard)
                and not self._settings_open and self.isVisible()):
            self._install_mouse_hook()
        else:
            self._uninstall_mouse_hook()

    def _install_mouse_hook(self):
        if self._hook is not None:
            return
        try:
            self._hook_proc = _HOOKPROC(self._mouse_hook)
            self._hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._hook_proc,
                                                   None, 0)
            if not self._hook:
                self._hook_proc = None
        except Exception:
            self._hook = None
            self._hook_proc = None

    def _uninstall_mouse_hook(self):
        if self._hook is not None:
            try:
                _user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None
            self._hook_proc = None

    def _icon_at(self, pos):
        """Какая иконка под точкой (в координатах виджета) или None."""
        if self.pin_btn.geometry().contains(pos):
            return "pin"
        if self.gear_btn.geometry().contains(pos):
            return "gear"
        return None

    def _mouse_hook(self, ncode, wparam, lparam):
        """Клики по 📌 в клик-сквозном режиме.

        Окно помечено WS_EX_TRANSPARENT, поэтому мышь до него не доходит.
        Здесь мы точно знаем координаты нажатия и «съедаем» только клики
        по кнопке-пин (она нужна, чтобы снять закрепление). Клики по
        шестерёнке и по остальной панели уходят в окна под виджетом.
        """
        if ncode != 0:
            return _user32.CallNextHookEx(None, ncode, wparam, lparam)
        try:
            # после снятия закрепления ждём физического отпускания кнопки мыши
            if self._pin_guard:
                if wparam == WM_LBUTTONUP:
                    QTimer.singleShot(0, self._end_pin_guard)
                return _user32.CallNextHookEx(None, ncode, wparam, lparam)
            if (self._click_through and not self._settings_open
                    and wparam in (WM_LBUTTONDOWN, WM_LBUTTONUP)):
                info = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                pos = self.mapFromGlobal(QPoint(info.pt.x, info.pt.y))
                target = self._icon_at(pos)
                if target == "pin":
                    if wparam == WM_LBUTTONDOWN:
                        QTimer.singleShot(0, self._pin_press)
                    else:
                        QTimer.singleShot(0, self._pin_release)
                    return 1            # не пропускаем клик дальше
                # кнопку нажали на 📌, а отпустили мимо — отменяем отсчёт
                if wparam == WM_LBUTTONUP and self._hold.isActive():
                    QTimer.singleShot(0, self._pin_release)
        except Exception:
            pass
        return _user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _poll_hover(self):
        """Подсветка иконок под курсором и плавное затухание виджета."""
        pos = self.mapFromGlobal(QCursor.pos())
        inside = self.isVisible() and self.rect().contains(pos)
        self._update_fade(inside)
        if not self._click_through:
            self.pin_btn.set_hover(False)
            self.gear_btn.set_hover(False)
            return
        target = self._icon_at(pos)
        self.pin_btn.set_hover(target == "pin")
        self.gear_btn.set_hover(False)   # в закреплённом режиме ⚙ недоступна

    def _update_fade(self, inside):
        """В клик-сквозном режиме виджет под курсором тает до hover_opacity:
        видно и нажимаемо всё, что под ним; остаётся только кнопка 📌."""
        try:
            low = float(self.cfg.get("hover_opacity", 5)) / 100.0
        except (TypeError, ValueError):
            low = 0.05
        low = max(0.0, min(1.0, low))
        active = (self._click_through and inside and not self._settings_open
                  and not self._hold.isActive() and not self._pin_guard
                  and bool(self.cfg.get("hover_fade", True)))
        self._animate_fade(low if active else 1.0)

    def _animate_fade(self, target):
        target = max(0.0, min(1.0, float(target)))
        if abs(target - self._fade_target) < 1e-3:
            return
        self._fade_target = target
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._fade)
        self._fade_anim.setEndValue(target)
        self._fade_anim.start()

    def _apply_fade(self, value):
        """Прозрачность содержимого: панель рисуется с alpha, элементы —
        графическим эффектом. 📌 и ⚙ всегда сохраняют обычную прозрачность:
        ими нужно попадать, когда всё остальное почти растворилось."""
        self._fade = max(0.0, min(1.0, float(value)))
        faded = self._fade < 0.999
        keep = [b for b in (getattr(self, "pin_btn", None),
                            getattr(self, "gear_btn", None)) if b is not None]
        for child in self.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            if child in keep:
                continue
            if faded:
                eff = child.graphicsEffect()
                if not isinstance(eff, QGraphicsOpacityEffect):
                    eff = QGraphicsOpacityEffect(child)
                    child.setGraphicsEffect(eff)
                eff.setOpacity(self._fade)
            elif child.graphicsEffect() is not None:
                child.setGraphicsEffect(None)
        self.update()

    # ------------------------------------------------------ автоцвет акцента

    def _restyle_bar(self, bar, radius):
        bar.setStyleSheet(
            "QProgressBar { background: rgba(255,255,255,0.09); border: none;"
            f" border-radius: {radius:.0f}px; }}\n"
            f"QProgressBar::chunk {{ background: {self._accent_now.name()};"
            f" border-radius: {radius:.0f}px; }}"
        )

    def _status_color(self):
        """Цвет статусных подписей: процент при низком заряде, горячая
        температура. Пусто в настройках — берём акцент (цвет полос)."""
        color = QColor(str(self.cfg.get("status_color") or "").strip())
        if color.isValid():
            return color
        return QColor(self._accent_now)

    def _apply_accent_color(self, color):
        """Перекрасить полоски, «горячие» подписи и 📌 текущим акцентом."""
        self._accent_now = QColor(color)
        for bar, radius in list(getattr(self, "_bars", [])):
            try:
                self._restyle_bar(bar, radius)
            except RuntimeError:                 # полоска уже удалена
                pass
        status = self._status_color().name()
        for lbl in list(getattr(self, "_accent_labels", [])):
            try:
                lbl.setStyleSheet(
                    f"color: {status}; background: transparent;")
            except RuntimeError:
                pass
        pin = getattr(self, "pin_btn", None)
        if pin is not None:
            pin.set_accent(self._accent_now)

    def _on_accent_anim(self, value):
        self._accent_factor = max(0.0, min(1.0, float(value)))
        self._apply_accent_color(
            _blend(self._accent_base, self._accent_hot, self._accent_factor))

    def _animate_accent(self, target):
        target = max(0.0, min(1.0, float(target)))
        if abs(target - self._accent_factor) < 0.005:
            return
        self._accent_anim.stop()
        self._accent_anim.setStartValue(self._accent_factor)
        self._accent_anim.setEndValue(target)
        self._accent_anim.start()

    def _accent_target(self, hottest, loads, overheat):
        """0 — спокойный цвет, 1 — красный: температура важнее нагрузки."""
        if not bool(self.cfg.get("auto_accent", True)):
            return 0.0
        hot = int(overheat or 0) or 90
        factor = 0.0
        if hottest is not None:
            start = max(40.0, hot - 30.0)
            factor = (float(hottest) - start) / max(1.0, hot - start)
        load = max([float(x) for x in loads if x is not None] or [0.0])
        load_factor = max(0.0, (load - 60.0) / 40.0) * 0.7
        return max(0.0, min(1.0, max(factor, load_factor)))

    # --------------------------------------------------- игнорируемые устройства

    def _is_ignored(self, name):
        key = str(name or "").strip().casefold()
        if not key:
            return False
        return any(key == str(x).strip().casefold()
                   for x in (self.cfg.get("ignore") or [])
                   if str(x).strip())

    def known_devices(self):
        """Все известные системные имена: онлайн, история, статистика, ignore."""
        names = []

        def add(name):
            name = str(name or "").strip()
            if name and all(name.casefold() != n.casefold() for n in names):
                names.append(name)

        for dev in getattr(self, "_bt_all", []) or []:
            add(dev.get("name"))
        for item in self.stats.devices.values():
            add(item.get("name"))
        for item in self.history.devices.values():
            add(item.get("name"))
        for name in self.cfg.get("ignore") or []:
            add(name)
        return names

    # ------------------------------------------------ экспорт / импорт данных

    def export_data(self, path):
        """Сохранить настройки, историю и статистику в один JSON-файл."""
        cfg = {k: v for k, v in self.cfg.items()
               if k not in ("pos", "screen")}   # позиция привязана к монитору
        payload = {
            "format": "telemetry-widget-backup",
            "version": 1,
            "exported": int(time.time()),
            "config": cfg,
            "stats": self.stats.to_dict(),
            "history": self.history.to_dict(),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _import_config(self, raw):
        """Аккуратно перенести настройки из копии, не ломая типы/диапазоны."""
        def num(key, lo, hi, integer=False):
            if key not in raw:
                return
            try:
                value = int(raw[key]) if integer else float(raw[key])
            except (TypeError, ValueError):
                return
            value = max(lo, min(hi, value))
            self.cfg[key] = value if not integer else int(value)

        font = str(raw.get("font") or "").strip()
        if font:
            self.cfg["font"] = font
        num("scale", 0.5, 2.5)
        num("opacity", 25, 100, integer=True)
        num("hover_opacity", 0, 90, integer=True)
        num("refresh_sec", 1.0, 10.0)
        for key in ("hover_fade", "auto_accent", "hide_offline", "minimal"):
            if key in raw:
                self.cfg[key] = bool(raw[key])
        color = QColor(str(raw.get("accent") or ""))
        if color.isValid():
            self.cfg["accent"] = color.name().upper()
        if "status_color" in raw:
            # пустая строка — «как цвет полос» (акцент)
            text = str(raw.get("status_color") or "").strip()
            color = QColor(text)
            self.cfg["status_color"] = color.name().upper() if color.isValid() else ""
        low = raw.get("low_battery", self.cfg.get("low_battery"))
        try:
            low = int(low or 0)
        except (TypeError, ValueError):
            low = 0
        self.cfg["low_battery"] = max(5, min(50, low)) if low else 0
        hot = raw.get("overheat", self.cfg.get("overheat"))
        try:
            hot = int(hot or 0)
        except (TypeError, ValueError):
            hot = 0
        self.cfg["overheat"] = max(50, min(110, hot)) if hot else 0
        names = []
        for pair in raw.get("names") or []:
            if (isinstance(pair, (list, tuple)) and len(pair) >= 2
                    and str(pair[0]).strip() and str(pair[1]).strip()):
                names.append([str(pair[0]).strip(), str(pair[1]).strip()])
        if "names" in raw:
            self.cfg["names"] = names
        if "ignore" in raw:
            self.cfg["ignore"] = [str(n).strip() for n in raw.get("ignore") or []
                                  if str(n).strip()]
        for key in ("headphones", "mouse"):
            value = raw.get(key)
            if isinstance(value, dict):
                matches = [str(m).strip() for m in value.get("match") or []
                           if str(m).strip()]
                self.cfg[key] = {"match": matches}

    def import_data(self, path):
        """Заменить настройки и статистику данными из резервной копии.

        Позиция окна, монитор и автозапуск не импортируются: они привязаны
        к этой машине. Возвращает число устройств в импортированной статистике.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("файл не похож на резервную копию виджета")
        stats_raw = data.get("stats")
        if not isinstance(stats_raw, dict) or not isinstance(
                stats_raw.get("devices"), dict):
            raise ValueError("в файле нет статистики устройств")
        cfg_raw = data.get("config")
        if not isinstance(cfg_raw, dict):
            raise ValueError("в файле нет настроек")
        hist_raw = data.get("history")
        if hist_raw is not None and (not isinstance(hist_raw, dict)
                                    or not isinstance(hist_raw.get("devices"),
                                                      dict)):
            raise ValueError("история заряда в файле повреждена")

        self._import_config(cfg_raw)
        self.stats.replace(stats_raw)
        self.stats.save()
        if isinstance(hist_raw, dict):
            self.history.replace(hist_raw)
            self.history.save()
        self._low_notified.clear()
        self._hot = False
        save_config(self.cfg)
        self.apply_config()
        return len(self.stats.devices)

    def apply_config(self):
        self.timer.setInterval(int(self.cfg["refresh_sec"] * 1000))
        self._accent_base = QColor(self.cfg.get("accent", "#E31937"))
        if not bool(self.cfg.get("auto_accent", True)):
            self._accent_anim.stop()
            self._accent_factor = 0.0
            self._accent_now = QColor(self._accent_base)
        else:
            self._accent_now = _blend(self._accent_base, self._accent_hot,
                                      self._accent_factor)
        self._build()
        self._apply_flags()
        self.adjustSize()
        self._update_pin_look()
        self._sync_mouse_hook()
        self.refresh()

    def _reset_position(self):
        self.cfg["pos"] = None
        self._place_default()
        save_config(self.cfg)

    def _place_default(self):
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.right() - self.width() - 42, geo.top() + 84)

    # ------------------------------------------------------------ интерфейс

    def _font(self, size, weight=QFont.Weight.Normal, spacing=0.0):
        f = QFont(self.cfg.get("font", "Segoe UI"))
        f.setPointSize(max(6, int(round(size * float(self.cfg.get("scale", 1.0))))))
        f.setWeight(weight)
        if spacing:
            f.setLetterSpacing(QFont.AbsoluteSpacing,
                               spacing * float(self.cfg.get("scale", 1.0)))
        return f

    def _sc(self, v):
        return max(1, int(round(v * float(self.cfg.get("scale", 1.0)))))

    def _label(self, text, size, color=TXT, weight=QFont.Weight.Normal, spacing=0.0):
        lbl = QLabel(text)
        lbl.setFont(self._font(size, weight, spacing))
        lbl.setStyleSheet(f"color: {color}; background: transparent;")
        _no_mouse(lbl)
        return lbl

    def _bar(self, height=4):
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(self._sc(height))
        radius = self._sc(height) / 2.0
        bar.setStyleSheet(
            "QProgressBar { background: rgba(255,255,255,0.09); border: none;"
            f" border-radius: {radius:.0f}px; }}\n"
            f"QProgressBar::chunk {{ background: {self._accent_now.name()};"
            f" border-radius: {radius:.0f}px; }}"
        )
        self._bars.append((bar, radius))
        _no_mouse(bar)
        return bar

    def _sep(self):
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        _no_mouse(line)
        return line

    def _value_row(self):
        row = QHBoxLayout()
        row.setSpacing(self._sc(3))
        big = self._label("—", 30, TXT, QFont.Weight.Light)
        unit = self._label("°C", 11, TXT_DIM)
        unit.setAlignment(Qt.AlignmentFlag.AlignLeft
                          | Qt.AlignmentFlag.AlignBottom)
        # Поднимаем «°C» так, чтобы её оптический центр совпал с центром
        # цифр: при выравнивании по низу метки единица читалась как нижний
        # индекс. Отступ считается из метрик шрифта, поэтому работает при
        # любом шрифте, размере и масштабе.
        fb = QFontMetricsF(self._font(30, QFont.Weight.Light))
        fu = QFontMetricsF(self._font(11))
        lift = (fb.capHeight() - fu.capHeight()) / 2.0
        pad = (fb.descent() - fu.descent()) + max(0.0, lift)
        unit.setContentsMargins(0, 0, 0, max(0, int(round(pad))))
        row.addWidget(big)
        row.addWidget(unit)
        row.addStretch(1)
        return row, big

    def _section(self, icon, title):
        box = QVBoxLayout()
        box.setSpacing(self._sc(6))
        head = QHBoxLayout()
        head.setSpacing(self._sc(5))
        head.addWidget(IconLabel(icon, self._sc(14), TXT_DIM))
        head.addWidget(self._label(title.upper(), 10, TXT_DIM, spacing=1.8))
        head.addStretch(1)
        usage = self._label("—", 10, TXT_DIM, spacing=0.6)
        head.addWidget(usage)
        box.addLayout(head)
        bar = self._bar()
        box.addWidget(bar)
        vrow, big = self._value_row()
        box.addLayout(vrow)
        return box, usage, bar, big

    def _device_row(self, icon, fallback=""):
        """Ряд устройства: иконка + имя + процент + полоска заряда и остаток."""
        row = QVBoxLayout()
        row.setSpacing(self._sc(4))
        head = QHBoxLayout()
        head.setSpacing(self._sc(8))
        glyph_lbl = IconLabel(icon, self._sc(15), TXT_MID, fallback)
        name_lbl = self._label("", 11, TXT_MID)
        # имя занимает остаток ширины и обрезается с «…», не выдавливая процент
        name_lbl.setSizePolicy(QSizePolicy.Policy.Ignored,
                               QSizePolicy.Policy.Preferred)
        pct_lbl = self._label("", 11, TXT, QFont.Weight.Light)
        head.addWidget(glyph_lbl)
        head.addWidget(name_lbl, 1)
        head.addWidget(pct_lbl)
        row.addLayout(head)
        # остаток «≈ 6 ч 20 мин» — справа от полоски: высота ряда не растёт
        foot = QHBoxLayout()
        foot.setSpacing(self._sc(6))
        left_lbl = self._label("", 8, TXT_DIM)
        left_lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        left_lbl.hide()
        bar = self._bar(3)
        # полоска ужимается, освобождая место подпись остатка
        bar.setMinimumWidth(self._sc(24))
        foot.addWidget(bar, 1)
        foot.addWidget(left_lbl)
        row.addLayout(foot)
        return row, glyph_lbl, name_lbl, pct_lbl, bar, left_lbl

    def _device_row_compact(self, icon, fallback=""):
        """Компактный ряд: иконка + имя + процент, без полоски.

        Остаток заряда выводится второй строкой: в узкой панели отдельная
        подпись в той же строке не помещается при крупном шрифте.
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(self._sc(5))
        glyph_lbl = IconLabel(icon, self._sc(13), TXT_MID, fallback)
        name_lbl = self._label("", 9, TXT_MID)
        name_lbl.setFixedWidth(self._sc(104))
        pct_lbl = self._label("", 9, TXT, QFont.Weight.Light)
        left_lbl = self._label("", 8, TXT_DIM)
        left_lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        left_lbl.hide()
        row.addWidget(glyph_lbl)
        row.addWidget(name_lbl)
        row.addStretch(1)
        row.addWidget(pct_lbl)
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(self._sc(1))
        box.addLayout(row)
        box.addWidget(left_lbl)
        return box, glyph_lbl, name_lbl, pct_lbl, None, left_lbl

    def _build(self):
        old = self.layout()
        if old is not None:
            QWidget().setLayout(old)  # отвязываем старое дерево
        self._bt_sig = None          # старые ряды устройств будут пересобраны
        self._bt_rows = []
        self._bt_devs = []
        self._bt_holder = None       # контейнер раздела (скрывается, если пусто)
        self._bt_visible = None
        self._bars = []              # старые полоски удалятся вместе с деревом
        self._accent_labels = []

        self.minimal = bool(self.cfg.get("minimal"))
        root = QVBoxLayout(self)
        root.setContentsMargins(self._sc(18), self._sc(15), self._sc(18),
                                self._sc(13) if self.minimal else self._sc(16))
        root.setSpacing(self._sc(9) if self.minimal else self._sc(12))

        # строка заголовка: TELEMETRY … 📌 ⚙ (иконки интегрированы сюда)
        head = QHBoxLayout()
        head.setSpacing(self._sc(4))
        head.addWidget(self._label("TELEMETRY", 9 if self.minimal else 10, TXT_MID,
                                   spacing=2.4 if self.minimal else 3.2))
        head.addStretch(1)
        sc = float(self.cfg.get("scale", 1.0))
        family = self.cfg.get("font", "Segoe UI")
        self.pin_btn = IconButton("pin", sc, family, on_press=self._pin_press,
                                  on_release=self._pin_release)
        self.gear_btn = IconButton("gear", sc, family, on_release=self._open_settings)
        head.addWidget(self.pin_btn)
        head.addWidget(self.gear_btn)
        root.addLayout(head)
        root.addWidget(self._sep())

        if self.minimal:
            self._build_minimal(root)
        else:
            self._build_full(root)

        # размер пересчитываем и сразу, и после обработки запроса раскладки:
        # во время пересборки кэш минимального размера ещё старый
        QTimer.singleShot(0, self._fit_height)

        if not self._built and self.cfg.get("pos"):
            x, y = self.cfg["pos"]
            # монитор мог отключиться — тогда возвращаемся на основной;
            # при первом запуске новой версии имя монитора ещё не сохранено,
            # поэтому ищем экран по самой позиции (иначе виджет уедет на основной)
            scr = (self._screen_by_name(self.cfg.get("screen"))
                   or QGuiApplication.screenAt(QPoint(int(x), int(y)))
                   or self.screen() or QApplication.primaryScreen())
            if scr is not None:
                g = scr.availableGeometry()
                x = min(max(int(x), g.left()), max(g.left(), g.right() - self.width()))
                y = min(max(int(y), g.top()), max(g.top(), g.bottom() - self.height()))
            self.move(int(x), int(y))
            QTimer.singleShot(0, self._ensure_visible)
        elif not self._built:
            self._place_default()
        self._built = True
        self._apply_fade(self._fade)
        self._apply_accent_color(self._accent_now)

    def _fit_height(self):
        """Подогнать высоту окна под содержимое (после пересборки раскладки)."""
        lay = self.layout()
        if lay is None:
            return
        lay.activate()
        want = max(lay.sizeHint().height(), lay.minimumSize().height())
        if self.height() != want:
            self.resize(self.width(), want)
        self._apply_bt_names()      # ширина имён стала известна — обрезаем как надо

    def _build_full(self, root):
        cpu_box, cpu_usage, cpu_bar, cpu_big = self._section("cpu", "CPU")
        root.addLayout(cpu_box)

        root.addWidget(self._sep())

        gpu_box, gpu_usage, gpu_bar, gpu_big = self._section("gpu", "GPU")
        root.addLayout(gpu_box)

        self._bt_sep = self._sep()
        root.addWidget(self._bt_sep)

        # «Беспроводные» — отдельный контейнер: если показывать нечего,
        # раздел прячется целиком вместе с разделителем (см. refresh)
        bt_holder = QWidget()
        _no_mouse(bt_holder)
        bt_box = QVBoxLayout(bt_holder)
        bt_box.setContentsMargins(0, 0, 0, 0)
        bt_box.setSpacing(self._sc(8))
        bt_head = QHBoxLayout()
        bt_head.setSpacing(self._sc(5))
        bt_head.addWidget(IconLabel("wireless", self._sc(14), TXT_DIM))
        bt_head.addWidget(self._label("БЕСПРОВОДНЫЕ", 10, TXT_DIM, spacing=1.8))
        bt_head.addStretch(1)
        bt_box.addLayout(bt_head)
        self._bt_lay = QVBoxLayout()
        self._bt_lay.setSpacing(self._sc(8))
        bt_box.addLayout(self._bt_lay)
        root.addWidget(bt_holder)
        self._bt_holder = bt_holder

        self.lbl = {
            "cpu_temp": cpu_big, "cpu_use": cpu_usage, "cpu_bar": cpu_bar,
            "gpu_temp": gpu_big, "gpu_use": gpu_usage, "gpu_bar": gpu_bar,
        }
        self.setFixedWidth(self._sc(264))
        self._fit_height()

    def _build_minimal(self, root):
        """Компактный режим: две строки цифр + список устройств без полос."""
        self.lbl = {}
        for icon, title, key in (("cpu", "CPU", "cpu"), ("gpu", "GPU", "gpu")):
            row = QHBoxLayout()
            row.setSpacing(self._sc(6))
            row.addWidget(IconLabel(icon, self._sc(12), TXT_DIM))
            row.addWidget(self._label(title, 9, TXT_DIM, spacing=1.4))
            row.addStretch(1)
            big = self._label("—", 17, TXT, QFont.Weight.Light)
            big.setAlignment(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter)
            use = self._label("—", 9, TXT_DIM)
            use.setAlignment(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(big)
            row.addWidget(use)
            root.addLayout(row)
            self.lbl[key + "_temp"] = big
            self.lbl[key + "_use"] = use
            self.lbl[key + "_bar"] = None

        self._bt_sep = self._sep()
        root.addWidget(self._bt_sep)
        bt_holder = QWidget()
        _no_mouse(bt_holder)
        self._bt_lay = QVBoxLayout(bt_holder)
        self._bt_lay.setContentsMargins(0, 0, 0, 0)
        self._bt_lay.setSpacing(self._sc(3))
        root.addWidget(bt_holder)
        self._bt_holder = bt_holder

        self.setFixedWidth(self._sc(210))
        self._fit_height()

    def set_minimal(self, on):
        """Быстрое переключение компактного режима (меню / настройки)."""
        on = bool(on)
        if on == bool(self.cfg.get("minimal")):
            return
        self.cfg["minimal"] = on
        save_config(self.cfg)
        self.apply_config()
        self._sync_tray()

    # -------------------------------------------------------------- данные

    def _match_device(self, devices, slot):
        """Подбор ОНЛАЙН-устройства для слота (наушники/мышь)."""
        words = [w.lower() for w in (self.cfg[slot].get("match") or []) if w.strip()]
        pool = [d for d in devices if d.get("online")]
        if words:
            for dev in pool:
                nl = dev["name"].lower()
                if any(w in nl for w in words):
                    return dev
            return None
        hints = HP_HINTS if slot == "headphones" else MS_HINTS
        for dev in pool:
            if any(h in dev["name"].lower() for h in hints):
                return dev
        return None

    def _rebuild_bt_rows(self, rows, sig):
        while self._bt_lay.count():
            item = self._bt_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._bt_rows = []
        self._bt_devs = list(rows)
        for dev in rows:
            holder = QWidget()
            _no_mouse(holder)
            icon = dev.get("_icon") or device_icon(dev["name"])
            fallback = device_glyph(dev["name"])
            if self.minimal:
                lay, glyph_lbl, name_lbl, pct_lbl, bar, left_lbl = \
                    self._device_row_compact(icon, fallback)
            else:
                lay, glyph_lbl, name_lbl, pct_lbl, bar, left_lbl = \
                    self._device_row(icon, fallback)
            holder.setLayout(lay)
            self._bt_lay.addWidget(holder)
            self._bt_rows.append((glyph_lbl, name_lbl, pct_lbl, bar, left_lbl))
        self._bt_sig = sig

    def refresh(self):
        d = monitor.snapshot()
        minimal = self.minimal
        overheat = int(self.cfg.get("overheat") or 0)
        temps = [d.get("cpu_temp"), d.get("gpu_temp")]
        hottest = max([t for t in temps if t is not None], default=None)
        self._animate_accent(self._accent_target(
            hottest, [d.get("cpu_load"), d.get("gpu_load")], overheat))
        status = self._status_color().name()
        self._accent_labels = []

        for key, temp_key, load_key in (("cpu", "cpu_temp", "cpu_load"),
                                        ("gpu", "gpu_temp", "gpu_load")):
            temp = d[temp_key]
            hot = bool(overheat and temp is not None and temp >= overheat)
            color = status if hot else TXT
            temp_lbl = self.lbl[key + "_temp"]
            if hot:
                self._accent_labels.append(temp_lbl)
            if minimal:
                temp_lbl.setText("—" if temp is None else f"{temp}°")
            else:
                temp_lbl.setText("—" if temp is None else str(temp))
            temp_lbl.setStyleSheet(f"color: {color}; background: transparent;")
            load = d[load_key]
            bar = self.lbl.get(key + "_bar")
            if bar is not None:
                bar.setValue(int(load) if load is not None else 0)
            if load is None:
                usage = "—"
            elif minimal:
                usage = f"{load} %"
            else:
                usage = f"{load} % ЗАГРУЗКА"
            self.lbl[key + "_use"].setText(usage)

        self._check_overheat(hottest, overheat)
        self._sync_tray_tip(d)

        self._bt_all = list(d["bt"])        # полный список — для настроек
        devices = [x for x in d["bt"] if not self._is_ignored(x["name"])]
        hp = self._match_device(devices, "headphones")
        ms = self._match_device(devices, "mouse")
        used = {id(x) for x in (hp, ms) if x}
        hide = bool(self.cfg.get("hide_offline"))

        rows = [x for x in (hp, ms) if x]
        rows.extend(x for x in devices if id(x) not in used
                    and not (not x["online"] and hide))
        # офлайн-устройства — в конце списка (порядок остальных не меняется)
        rows.sort(key=lambda r: 0 if r["online"] else 1)
        # слоты «наушники»/«мышь» задают иконку даже для нестандартных имён
        for dev in rows:
            dev["_icon"] = device_icon(dev["name"])
        if hp:
            hp["_icon"] = "headphones"
        if ms:
            ms["_icon"] = "mouse"

        sig = tuple((r["name"], r["online"]) for r in rows)
        changed = sig != self._bt_sig
        if changed:
            self._rebuild_bt_rows(rows, sig)

        # ни одного устройства в списке — скрываем раздел «Беспроводные»
        # целиком: заголовок, список и разделитель
        visible = bool(rows)
        if visible != self._bt_visible:
            self._bt_visible = visible
            if self._bt_holder is not None:
                self._bt_holder.setVisible(visible)
            if self._bt_sep is not None:
                self._bt_sep.setVisible(visible)
            changed = True
        if changed:
            QTimer.singleShot(0, self._fit_height)

        fit = False
        for (glyph_lbl, name_lbl, pct_lbl, bar, left_lbl), dev in zip(self._bt_rows,
                                                                      rows):
            online, level = dev["online"], dev["level"]
            self._track_battery(dev)
            name_lbl.setStyleSheet(
                f"color: {TXT_MID if online else TXT_OFF}; background: transparent;")
            glyph_lbl.set_color(TXT_MID if online else TXT_OFF)
            if online and level is not None:
                pct_lbl.setText(f"{level} %")
                warn_at = int(self.cfg.get("low_battery") or 0) or 20
                low_now = level <= warn_at
                if low_now:
                    self._accent_labels.append(pct_lbl)
                pct_lbl.setStyleSheet(
                    f"color: {status if low_now else TXT};"
                    "background: transparent;")
                if bar is not None:
                    bar.setValue(int(level))
            else:
                pct_lbl.setText("—" if online else "офлайн")
                pct_lbl.setStyleSheet(f"color: {TXT_OFF}; background: transparent;")
                if bar is not None:
                    bar.setValue(0)

            # остаток: «≈ 6 ч 20 мин» из статистики циклов разряда
            rep = self.stats.report(dev["name"])
            empty_in = rep.get("empty_in") if online and rep else None
            left_text = "≈ " + _fmt_left(empty_in, self.minimal) if empty_in else ""
            if bool(left_lbl.text()) != bool(left_text):
                fit = True            # в компактном режиме меняется высота ряда
            left_lbl.setText(left_text)
            left_lbl.setVisible(bool(left_text))

        self._apply_bt_names()
        if fit:
            QTimer.singleShot(0, self._fit_height)

    def _track_battery(self, dev):
        """История заряда, статистика циклов и предупреждение о низком заряде."""
        name, online, level = dev["name"], dev["online"], dev["level"]
        low = int(self.cfg.get("low_battery") or 0)
        if not online or level is None:
            self._low_notified.discard(name)
            return
        if self.history.add(name, level):
            self.history.save()
            self.stats.save()             # раз в 5 минут фиксируем накопленное
        if self.stats.note(name, level):
            self.stats.save()             # цикл завершился зарядкой
        if not low:
            self._low_notified.discard(name)
            return
        if level <= low:
            if name not in self._low_notified:
                self._low_notified.add(name)
                self._notify("Низкий заряд",
                             f"{self.display_name(name)}: {level} % "
                             f"(порог {low} %)")
        elif level > low + 5:              # заряд восстановился — снова следим
            self._low_notified.discard(name)

    def _check_overheat(self, hottest, overheat):
        """Уведомление о перегреве: срабатывает один раз, гистерезис 5 °C."""
        if not overheat or hottest is None:
            if not overheat:
                self._hot = False
            return
        if hottest >= overheat and not self._hot:
            self._hot = True
            self._notify("Перегрев",
                         f"Температура {hottest} °C — выше порога {overheat} °C")
        elif hottest <= overheat - 5:
            self._hot = False

    def _sync_tray_tip(self, d):
        if self.tray is None:
            return
        parts = []
        for key, label in (("cpu", "CPU"), ("gpu", "GPU")):
            temp, load = d.get(key + "_temp"), d.get(key + "_load")
            bits = []
            if load is not None:
                bits.append(f"{load} %")
            if temp is not None:
                bits.append(f"{temp} °C")
            if bits:
                parts.append(f"{label} " + " / ".join(bits))
        self.tray.setToolTip("Telemetry · " + " · ".join(parts)
                             if parts else "Telemetry Widget")

    def _apply_bt_names(self):
        """Имена устройств обрезаются по фактической ширине (после раскладки)."""
        try:
            for row, dev in zip(self._bt_rows, self._bt_devs):
                name_lbl = row[1]
                text = self.display_name(dev["name"])
                width = name_lbl.width() - 2
                if width < 20:                   # раскладка ещё не посчитана
                    name_lbl.setText(text)
                else:
                    name_lbl.setText(name_lbl.fontMetrics().elidedText(
                        text, Qt.TextElideMode.ElideRight, width))
        except RuntimeError:                     # старые подписи уже удалены
            pass

    # ------------------------------------------------------------ иконки 📌/⚙

    def _update_pin_look(self):
        accent = self._accent_now
        self.pin_btn.set_accent(accent)
        self.pin_btn.set_count(None)
        self.pin_btn.set_active(self._click_through)
        if self._click_through:
            self.pin_btn.setToolTip(
                "Закреплено: клики проходят сквозь виджет к окнам под ним.\n"
                "Удерживайте кнопку 5 секунд — закрепление снимется, "
                "и настройки снова станут доступны.")
        else:
            self.pin_btn.setToolTip(
                "Закрепить поверх всех окон (клик-сквозной режим)")
        # в закреплённом режиме настройки недоступны: клик по ⚙ проходит сквозь
        self.gear_btn.set_dimmed(self._click_through)
        self.gear_btn.setToolTip(
            "Недоступно: сначала снимите закрепление (удерживайте 📌)"
            if self._click_through else "Настройки")

    def _update_hold_text(self, left, color=None):
        """Обратный отсчёт на кнопке: 5, 4, 3, 2, 1, 0."""
        self.pin_btn.set_count(max(0, int(left)), color)

    def _pin_press(self):
        if self._pin_guard:
            return                         # это то же нажатие, что сняло закрепление
        self._restore_seq += 1             # отменяем отложенный возврат иконки
        if not self._click_through:
            self.set_click_through(True)   # одиночный клик включает режим
            return
        self._hold_start = time.monotonic()   # в активном режиме запускаем отсчёт
        self._ticks = 0
        self.pin_btn.set_accent(self._accent_now)
        self._update_hold_text(round(self.HOLD_SEC), QColor(TXT))
        self._hold.start()

    def _hold_tick(self):
        if self._hold_start is None:
            self._hold.stop()
            return
        elapsed = time.monotonic() - self._hold_start
        self._ticks = int(elapsed * 1000 / self.HOLD_TICK_MS)
        left = self.HOLD_SEC - elapsed
        if left <= 0:
            self._hold.stop()
            self._hold_start = None
            self._ticks = 0
            self._pin_guard = True          # ждём отпускания кнопки мыши
            self._guard_n = 0
            self._guard.start()
            self.set_click_through(False)   # режим снят — «0» на кнопке виден
            self._sync_mouse_hook()         # хук остаётся, чтобы поймать отпускание
            self.pin_btn.set_count(0, QColor(TXT))
            self._restore_seq += 1
            seq = self._restore_seq
            QTimer.singleShot(self.ZERO_HOLD_MS,
                              lambda: self._restore_pin_icon(seq))
            return
        frac = min(1.0, elapsed / self.HOLD_SEC)
        color = _blend(QColor(TXT), QColor(TXT_MID), frac)
        self._update_hold_text(math.ceil(left), color)

    def _end_pin_guard(self):
        """Кнопку мыши отпустили — снова разрешаем клики по 📌."""
        self._pin_guard = False
        self._guard.stop()
        self._sync_mouse_hook()

    def _guard_tick(self):
        """Подстраховка на случай, если хук не поймал отпускание кнопки."""
        self._guard_n += 1
        if self._hook is not None and self._guard_n < 100:
            return                        # отпускание поймает хук мыши
        if (self._hook is None
                or not (ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)):
            self._end_pin_guard()

    def _restore_pin_icon(self, seq):
        if seq != self._restore_seq or self._hold.isActive():
            return                          # отсчёт уже начали заново
        try:
            self._update_pin_look()
        except RuntimeError:                # кнопку уже пересобрали
            pass

    def _pin_release(self):
        if self._hold.isActive():
            self._hold.stop()
            self._hold_start = None
            self._ticks = 0
            self._update_pin_look()
            QToolTip.showText(
                self.pin_btn.mapToGlobal(self.pin_btn.rect().center()),
                "Удерживайте 📌 до нуля, чтобы убрать режим «поверх всех окон»",
                self.pin_btn)

    # ------------------------------------------------------- окно и мышь

    def signal_already_running(self):
        """Вторая копия не запустилась — подсвечиваем уже работающий виджет."""
        self.show()
        self.raise_()
        QToolTip.showText(
            self.mapToGlobal(QPoint(self.width() // 2, self._sc(8))),
            "Виджет уже запущен", self)
        self._flash(3)

    def _flash(self, times):
        self._flash_left = max(1, int(times))
        self._flash_base = float(self.cfg.get("opacity", 90)) / 100.0
        self._flash_step()

    def _flash_step(self):
        if self._flash_left <= 0:
            self.setWindowOpacity(self._flash_base)
            return
        self._flash_left -= 1
        self.setWindowOpacity(1.0)
        QTimer.singleShot(170, self._flash_down)

    def _flash_down(self):
        self.setWindowOpacity(self._flash_base)
        QTimer.singleShot(130, self._flash_step)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        radius = self._sc(20)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fade = getattr(self, "_fade", 1.0)
        bg = QColor("#0B0B0E")
        bg.setAlpha(round(255 * fade))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, radius, radius)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, round(24 * fade)), 1))
        p.drawRoundedRect(rect, radius, radius)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()

    def mouseReleaseEvent(self, e):
        if self._drag_offset is not None:
            self._drag_offset = None
            self.cfg["pos"] = [self.x(), self.y()]
            save_config(self.cfg)

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.addAction("⚙  Настройки…", self._open_settings)
        menu.addAction("📈  История заряда…", self._open_history)
        compact = menu.addAction("◱  Компактный режим")
        compact.setCheckable(True)
        compact.setChecked(bool(self.cfg.get("minimal")))
        compact.toggled.connect(self.set_minimal)
        menu.addAction("Сбросить положение", self._reset_position)
        menu.addSeparator()
        menu.addAction("Выход", QApplication.instance().quit)
        menu.exec(e.globalPos())

    def _open_settings(self):
        if self._settings_open:
            return
        self._settings_open = True
        self._sync_mouse_hook()          # хук на время диалога не нужен
        try:
            try:
                dlg = SettingsDialog(self)
            except Exception as exc:                  # noqa: BLE001
                QMessageBox.warning(self, "Настройки",
                                    f"Не удалось открыть настройки:\n{exc}")
                return
            dlg.changed.connect(self.apply_live)
            dlg.exec()
            dlg.flush()                  # применить то, что не успело (ввод текста)
            self.cfg["autostart"] = autostart_enabled()
            save_config(self.cfg)
        finally:
            self._settings_open = False
            self._sync_mouse_hook()

    def apply_live(self, cfg_partial):
        """Живое применение: ползунок/поле меняются — виджет сразу такой же.

        Окно настроек при этом не закрывается и остаётся на переднем плане.
        """
        if not cfg_partial:
            return
        self.cfg.update(cfg_partial)
        self.timer.setInterval(max(200, int(float(self.cfg["refresh_sec"]) * 1000)))
        self.setWindowOpacity(float(self.cfg.get("opacity", 90)) / 100.0)
        if not bool(self.cfg.get("hover_fade", True)):
            self._animate_fade(1.0)
        self._accent_base = QColor(self.cfg.get("accent", "#E31937"))
        if not bool(self.cfg.get("auto_accent", True)):
            self._accent_anim.stop()
            self._accent_factor = 0.0
            self._accent_now = QColor(self._accent_base)
        else:
            self._accent_now = _blend(self._accent_base, self._accent_hot,
                                      self._accent_factor)
        self._build()
        self._update_ex_style()
        self._sync_mouse_hook()
        self.adjustSize()
        self._update_pin_look()
        self._apply_topmost()
        self._sync_tray()
        self.refresh()
        self._save_soon()

    def _save_soon(self):
        """Конфиг пишется на диск с небольшой задержкой (без спама при перетаскивании ползунка)."""
        self._save_timer.start()

    def persist(self):
        """Сохранить позицию, монитор и настройки (при закрытии окна и при выходе)."""
        self.cfg["pos"] = [self.x(), self.y()]
        scr = self.screen()
        if scr is not None:
            self.cfg["screen"] = scr.name()
        save_config(self.cfg)

    def closeEvent(self, _e):
        self._uninstall_mouse_hook()
        self._uninstall_foreground_watch()
        self._topmost.stop()
        self._history_timer.stop()
        self._shutdown()
        self.persist()

    def _shutdown(self):
        """Выход: убрать иконку трея и сохранить историю заряда."""
        if self.tray is not None:
            self.tray.hide()
        self.history.save()
        self.stats.save()


# ---------------------------------------------------------------- Настройки

class SettingsDialog(QDialog):
    """Тёмное окно настроек: всё применяется сразу, подтверждать нечего."""

    changed = Signal(dict)

    def __init__(self, widget):
        super().__init__(widget)
        self.widget_ref = widget
        cfg = widget.cfg
        accent = cfg.get("accent", "#E31937")
        self._last_accent = accent
        self._last_status = str(cfg.get("status_color") or "").strip().upper()
        scale = float(cfg.get("scale", 1.0))

        self.setWindowTitle("Настройки виджета")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(470)
        self.setStyleSheet(DIALOG_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(4)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft
                               | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        root.addLayout(form)

        # ------------------------------------------------------------- Вид
        form.addRow(self._head("Вид"))

        self.font_cb = QFontComboBox()
        self.font_cb.setCurrentFont(QFont(cfg.get("font", "Segoe UI")))
        form.addRow("Шрифт", self.font_cb)

        self.scale_sp = QDoubleSpinBox()
        self.scale_sp.setRange(0.5, 2.5)
        self.scale_sp.setSingleStep(0.05)
        self.scale_sp.setDecimals(2)
        self.scale_sp.setValue(scale)
        form.addRow("Размер окна (масштаб)", self.scale_sp)

        self.opacity_sp = QSpinBox()
        self.opacity_sp.setRange(25, 100)
        self.opacity_sp.setSuffix(" %")
        self.opacity_sp.setValue(int(cfg.get("opacity", 90)))
        form.addRow("Прозрачность", self.opacity_sp)

        self.hover_sw = ToggleSwitch(bool(cfg.get("hover_fade", True)), accent,
                                     max(0.9, scale))
        self.hover_sp = QSpinBox()
        self.hover_sp.setRange(0, 90)
        self.hover_sp.setSuffix(" %")
        self.hover_sp.setValue(int(cfg.get("hover_opacity", 5)))
        form.addRow("Наведение мыши",
                    self._spin_row(self.hover_sw, self.hover_sp,
                                   "гаснет под курсором, 📌 и ⚙ остаются"))

        accent_row = QHBoxLayout()
        accent_row.setSpacing(8)
        self.accent_edit = QLineEdit(str(accent))
        self.accent_edit.setPlaceholderText("#RRGGBB")
        pick = QPushButton("Палитра…")
        pick.clicked.connect(self._pick_color)
        accent_row.addWidget(self.accent_edit, 1)
        accent_row.addWidget(pick)
        form.addRow("Цвет полос", accent_row)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.status_edit = QLineEdit(str(cfg.get("status_color") or ""))
        self.status_edit.setPlaceholderText("пусто = как цвет полос")
        self.status_edit.setToolTip(
            "Температура при перегреве и процент при низком заряде.\n"
            "Пусто — берётся «Цвет полос» (с учётом автоцвета).")
        status_pick = QPushButton("Палитра…")
        status_pick.clicked.connect(self._pick_status_color)
        status_row.addWidget(self.status_edit, 1)
        status_row.addWidget(status_pick)
        form.addRow("Цвет статусов", status_row)

        self.auto_sw = ToggleSwitch(bool(cfg.get("auto_accent", True)), accent,
                                    max(0.9, scale))
        form.addRow("Автоцвет",
                    self._toggle_row(self.auto_sw,
                                     "плавно уводит акцент в красный при нагреве"))

        self.minimal_sw = ToggleSwitch(bool(cfg.get("minimal")), accent,
                                       max(0.9, scale))
        form.addRow("Компактный режим",
                    self._toggle_row(self.minimal_sw,
                                     "только цифры, без полос и разделов"))

        # ---------------------------------------------------------- Данные
        form.addRow(self._head("Данные"))

        self.refresh_sp = QDoubleSpinBox()
        self.refresh_sp.setRange(1.0, 10.0)
        self.refresh_sp.setSingleStep(0.5)
        self.refresh_sp.setSuffix(" с")
        self.refresh_sp.setValue(float(cfg.get("refresh_sec", 2.0)))
        form.addRow("Интервал обновления", self.refresh_sp)

        self.hp_edit = QLineEdit(", ".join(cfg["headphones"].get("match") or []))
        self.hp_edit.setPlaceholderText("пусто = автоопределение (наушники)")
        form.addRow("Наушники — фильтр имени", self.hp_edit)

        self.ms_edit = QLineEdit(", ".join(cfg["mouse"].get("match") or []))
        self.ms_edit.setPlaceholderText("пусто = автоопределение (мышь)")
        form.addRow("Мышь — фильтр имени", self.ms_edit)

        self.hide_sw = ToggleSwitch(bool(cfg.get("hide_offline")), accent,
                                    max(0.9, scale))
        form.addRow("Скрывать неактивные",
                    self._toggle_row(self.hide_sw, "офлайн-устройства не в списке"))

        self._ignore = [str(n).strip() for n in (cfg.get("ignore") or [])
                        if str(n).strip()]
        self.ignore_btn = QPushButton()
        self.ignore_btn.setStyleSheet(
            "QPushButton { padding: 3px 10px; min-width: 0; }")
        self.ignore_btn.clicked.connect(self._show_ignore_menu)
        ignore_row = QHBoxLayout()
        ignore_row.setContentsMargins(0, 0, 0, 0)
        ignore_row.setSpacing(10)
        ignore_row.addWidget(self.ignore_btn)
        ignore_hint = QLabel("скрыть с панели, из истории и уведомлений")
        ignore_hint.setStyleSheet(f"color: {TXT_DIM}; background: transparent;")
        ignore_row.addWidget(ignore_hint, 1)
        form.addRow("Игнорировать устройства", ignore_row)
        self._update_ignore_btn()

        # ---------------------------------------------------------- Сигналы
        form.addRow(self._head("Сигналы"))

        self.low_sw = ToggleSwitch(int(cfg.get("low_battery") or 0) > 0, accent,
                                   max(0.9, scale))
        self.low_sp = QSpinBox()
        self.low_sp.setRange(5, 50)
        self.low_sp.setSuffix(" %")
        self.low_sp.setValue(int(cfg.get("low_battery") or 20))
        form.addRow("Низкий заряд",
                    self._spin_row(self.low_sw, self.low_sp,
                                   "уведомление ниже порога"))

        self.hot_sw = ToggleSwitch(int(cfg.get("overheat") or 0) > 0, accent,
                                   max(0.9, scale))
        self.hot_sp = QSpinBox()
        self.hot_sp.setRange(50, 110)
        self.hot_sp.setSuffix(" °C")
        self.hot_sp.setValue(int(cfg.get("overheat") or 85))
        form.addRow("Перегрев",
                    self._spin_row(self.hot_sw, self.hot_sp,
                                   "уведомление выше порога"))

        raw_names = [[str(p[0]).strip(), str(p[1]).strip()]
                     for p in (cfg.get("names") or [])
                     if isinstance(p, (list, tuple)) and len(p) >= 2
                     and str(p[0]).strip() and str(p[1]).strip()]
        self._name_entries = []
        known = []
        for dev in getattr(self.widget_ref, "_bt_all", []) or []:
            name = (dev.get("name") or "").strip()
            if name and name not in known:
                known.append(name)
        for pattern, alias in raw_names:
            # старые записи хранили часть имени — подставляем живое имя
            # устройства, если оно однозначно находится
            matches = [n for n in known if pattern.lower() in n.lower()]
            device = matches[0] if len(matches) == 1 else pattern
            if any(device.lower() == d.lower()
                   for d, _a in self._name_entries):
                continue                      # одно устройство — одна строка
            self._name_entries.append([device, alias])
        self._name_rows = []
        names_wrap = QWidget()
        names_lay = QVBoxLayout(names_wrap)
        names_lay.setContentsMargins(0, 0, 0, 0)
        names_lay.setSpacing(5)
        self._name_box = QVBoxLayout()
        self._name_box.setContentsMargins(0, 0, 0, 0)
        self._name_box.setSpacing(6)
        names_lay.addLayout(self._name_box)
        names_hint = QLabel("Выберите устройство из списка и напишите своё "
                            "название. Список учитывает «Скрывать неактивные».")
        names_hint.setWordWrap(True)
        names_hint.setStyleSheet(f"color: {TXT_DIM}; background: transparent;")
        names_footer = QHBoxLayout()
        names_footer.setContentsMargins(0, 0, 0, 0)
        names_footer.setSpacing(8)
        names_footer.addWidget(names_hint, 1)
        refresh_names = QPushButton("Обновить")
        refresh_names.setToolTip("Подтянуть устройства, подключённые после "
                                 "открытия настроек")
        refresh_names.setStyleSheet(
            "QPushButton { padding: 3px 10px; min-width: 0; }")
        refresh_names.clicked.connect(lambda: self._rebuild_name_rows())
        names_footer.addWidget(refresh_names)
        names_lay.addLayout(names_footer)
        form.addRow("Свои названия", names_wrap)
        self._rebuild_name_rows()

        history_btn = QPushButton("История заряда…")
        history_btn.clicked.connect(self.widget_ref._open_history)
        form.addRow("", history_btn)

        # --------------------------------------------------------- Система
        form.addRow(self._head("Система"))

        self.autostart_sw = ToggleSwitch(autostart_enabled(), accent,
                                         max(0.9, scale))
        form.addRow("Запускать с Windows",
                    self._toggle_row(self.autostart_sw,
                                     "автостарт при входе в систему (pythonw)"))

        # -------------------------------------------------- резервная копия
        form.addRow(self._head("Резервная копия"))

        backup_row = QHBoxLayout()
        backup_row.setContentsMargins(0, 0, 0, 0)
        backup_row.setSpacing(8)
        export_btn = QPushButton("Экспорт…")
        export_btn.setToolTip("Сохранить настройки, историю заряда и статистику")
        export_btn.clicked.connect(self._export_data)
        import_btn = QPushButton("Импорт…")
        import_btn.setToolTip("Заменить настройки и статистику данными из файла")
        import_btn.clicked.connect(self._import_data)
        backup_row.addWidget(export_btn)
        backup_row.addWidget(import_btn)
        backup_row.addStretch(1)
        backup_hint = QLabel("один JSON-файл; позиция и автозапуск не переносятся")
        backup_hint.setStyleSheet(f"color: {TXT_DIM}; background: transparent;")
        backup_row.addWidget(backup_hint)
        form.addRow("Настройки и статистика", backup_row)

        root.addSpacing(8)
        # стрелки спинбоксов скрыты в QSS (при NoButtons со стилем Qt теряет
        # текст значения), поля просто растягиваются по ширине
        for spin in (self.scale_sp, self.opacity_sp, self.hover_sp,
                     self.refresh_sp, self.low_sp, self.hot_sp):
            spin.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Fixed)
        self.low_sp.setEnabled(self.low_sw.isChecked())
        self.hot_sp.setEnabled(self.hot_sw.isChecked())
        self.hover_sp.setEnabled(self.hover_sw.isChecked())

        bottom = QHBoxLayout()
        hint = QLabel("Применяется сразу — сохранять не нужно")
        hint.setStyleSheet(f"color: {TXT_DIM}; background: transparent;")
        bottom.addWidget(hint, 1)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        root.addSpacing(6)
        root.addLayout(bottom)

        # ---- живые связи: любое изменение тут же уходит в виджет
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(self._emit_change)

        self.font_cb.currentFontChanged.connect(self._emit_change)
        self.scale_sp.valueChanged.connect(self._emit_change)
        self.opacity_sp.valueChanged.connect(self._emit_change)
        self.hover_sw.toggled.connect(self._hover_toggled)
        self.hover_sp.valueChanged.connect(self._emit_change)
        self.refresh_sp.valueChanged.connect(self._emit_change)
        self.accent_edit.textChanged.connect(self._text_changed)
        self.status_edit.textChanged.connect(self._text_changed)
        self.hp_edit.textChanged.connect(self._text_changed)
        self.ms_edit.textChanged.connect(self._text_changed)
        self.hide_sw.toggled.connect(self._hide_toggled)
        self.auto_sw.toggled.connect(self._emit_change)
        self.minimal_sw.toggled.connect(self._emit_change)
        self.autostart_sw.toggled.connect(self._toggle_autostart)
        self.low_sw.toggled.connect(self._signal_changed)
        self.hot_sw.toggled.connect(self._signal_changed)
        self.low_sp.valueChanged.connect(self._signal_changed)
        self.hot_sp.valueChanged.connect(self._signal_changed)

        self.adjustSize()

    # ---------------------------------------------------------- элементы

    def _head(self, text):
        lbl = QLabel(text.upper())
        f = lbl.font()
        f.setPointSize(8)
        f.setWeight(QFont.Weight.DemiBold)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        lbl.setFont(f)
        lbl.setStyleSheet(f"color: {TXT_DIM}; background: transparent;"
                          " padding: 12px 0 2px 0;")
        return lbl

    def _toggle_row(self, toggle, hint_text):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(toggle)
        hint = QLabel(hint_text)
        hint.setStyleSheet(f"color: {TXT_DIM}; background: transparent;")
        row.addWidget(hint, 1)
        return row

    def _spin_row(self, toggle, spin, hint_text):
        """Переключатель + поле значения + пояснение."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(toggle)
        spin.setFixedWidth(78)
        row.addWidget(spin)
        hint = QLabel(hint_text)
        hint.setStyleSheet(f"color: {TXT_DIM}; background: transparent;")
        row.addWidget(hint, 1)
        return row

    # ------------------------------------------------- свои названия
    #
    # Строки собираются из пар «устройство → название». Вместо ручного ввода
    # системного имени (бывает китайским/нечитаемым) устройство выбирается из
    # выпадающего списка; последняя строка всегда пустая — выбрали устройство,
    # и автоматически появляется следующая.

    def _name_choices(self):
        """Устройства для списка: онлайн, плюс офлайн если их не скрывают."""
        hide = self.hide_sw.isChecked()
        online, offline = [], []
        for dev in getattr(self.widget_ref, "_bt_all", []) or []:
            name = (dev.get("name") or "").strip()
            if not name or self.widget_ref._is_ignored(name):
                continue
            (online if dev.get("online") else offline).append(name)
        choices = []
        for name in online + ([] if hide else offline):
            if name not in choices:
                choices.append(name)
        # сохранённое название не пропадает, даже если устройство сейчас офлайн
        for dev, _alias in self._name_entries:
            if dev and dev not in choices:
                choices.append(dev)
        return choices

    def _name_row(self, idx, dev, alias, choices):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        used = {d.lower() for j, (d, _a) in enumerate(self._name_entries)
                if j != idx and d}
        items = [c for c in choices if c.lower() not in used]
        if dev and dev not in items:
            items.insert(0, dev)

        combo = QComboBox()
        combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Fixed)
        if idx is None:
            combo.addItem("— выберите устройство —", "")
        for name in items:
            combo.addItem(name, name)
        if dev:
            pos = combo.findData(dev)
            if pos >= 0:
                combo.setCurrentIndex(pos)

        edit = QLineEdit(alias)
        edit.setPlaceholderText("Своё название")
        edit.setEnabled(bool(dev))
        edit.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

        btn = QPushButton("✕")
        btn.setFixedWidth(28)
        btn.setToolTip("Убрать название")
        btn.setStyleSheet("QPushButton { min-width: 0; padding: 0; }")

        row.addWidget(combo, 1)
        row.addWidget(edit, 1)
        if idx is None:
            btn.hide()
        row.addWidget(btn)

        if idx is None:
            combo.currentIndexChanged.connect(
                lambda *_, c=combo: self._name_device_picked(c))
        else:
            combo.currentIndexChanged.connect(
                lambda *_, i=idx, c=combo: self._name_device_changed(i, c))
            edit.textChanged.connect(
                lambda text, i=idx: self._name_alias_changed(i, text))
            btn.clicked.connect(
                lambda *_, i=idx: self._remove_name_entry(i))

        self._name_rows.append((box, combo, edit, btn))
        return box

    def _rebuild_name_rows(self, focus=None):
        while self._name_box.count():
            item = self._name_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._name_rows = []
        choices = self._name_choices()
        for idx, (dev, alias) in enumerate(self._name_entries):
            self._name_box.addWidget(self._name_row(idx, dev, alias, choices))
        self._name_box.addWidget(self._name_row(None, "", "", choices))
        if focus is not None:
            QTimer.singleShot(0, lambda: self._focus_name_input(focus))

    def _focus_name_input(self, idx):
        if 0 <= idx < len(self._name_rows):
            edit = self._name_rows[idx][2]
            edit.setFocus()

    def _name_device_picked(self, combo):
        """Выбрали устройство в пустой строке → добавляем следующую пустую."""
        dev = combo.currentData()
        if not dev:
            return
        idx = len(self._name_entries)
        self._name_entries.append([dev, ""])
        QTimer.singleShot(0, lambda: self._rebuild_name_rows(idx))
        self._emit_change()

    def _name_device_changed(self, idx, combo):
        dev = combo.currentData()
        if not dev or not 0 <= idx < len(self._name_entries):
            return
        self._name_entries[idx][0] = dev
        QTimer.singleShot(0, self._rebuild_name_rows)   # обновить списки
        self._emit_change()

    def _name_alias_changed(self, idx, text):
        if 0 <= idx < len(self._name_entries):
            self._name_entries[idx][1] = text
        self._text_changed()

    def _remove_name_entry(self, idx):
        if 0 <= idx < len(self._name_entries):
            del self._name_entries[idx]
        self._rebuild_name_rows()     # сразу: иначе быстрые клики путают индексы
        self._emit_change()

    def _hide_toggled(self, *_args):
        """Скрывать неактивные → список устройств в названиях тоже меняется."""
        self._rebuild_name_rows()
        self._emit_change()

    # -------------------------------------------- игнорируемые устройства

    def _is_ignored_local(self, name):
        key = str(name or "").strip().casefold()
        return any(key == str(x).strip().casefold() for x in self._ignore)

    def _ignore_label(self, name):
        alias = apply_name_map(name, self.widget_ref.cfg.get("names") or [])
        if alias and alias.casefold() != str(name).casefold():
            return f"{alias} ({name})"
        return str(name)

    def _update_ignore_btn(self):
        count = len(self._ignore)
        self.ignore_btn.setText(f"Скрыто: {count}" if count else "Выбрать…")
        if count:
            self.ignore_btn.setToolTip("Скрыты: " + ", ".join(self._ignore))
        else:
            self.ignore_btn.setToolTip("Устройства не скрыты")

    def _show_ignore_menu(self):
        menu = QMenu(self)
        names = self.widget_ref.known_devices()
        if not names:
            act = menu.addAction("Нет известных устройств")
            act.setEnabled(False)
        for name in names:
            act = menu.addAction(self._ignore_label(name))
            act.setCheckable(True)
            act.setChecked(self._is_ignored_local(name))
            act.toggled.connect(
                lambda on, _a=act, n=name: self._toggle_ignore(n, on))
        menu.exec(self.ignore_btn.mapToGlobal(
            QPoint(0, self.ignore_btn.height())))

    def _toggle_ignore(self, name, on):
        key = str(name).strip().casefold()
        if on:
            if not self._is_ignored_local(name):
                self._ignore.append(str(name).strip())
        else:
            self._ignore = [n for n in self._ignore
                            if str(n).strip().casefold() != key]
        self._update_ignore_btn()
        self._emit_change()

    # ------------------------------------------------ экспорт / импорт

    def _export_data(self):
        default = os.path.join(APP_DIR,
                               f"telemetry-{time.strftime('%Y-%m-%d')}.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт настроек и статистики", default, "JSON (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            self.widget_ref.export_data(path)
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.warning(self, "Экспорт",
                                f"Не удалось сохранить файл:\n{exc}")
            return
        QMessageBox.information(self, "Экспорт",
                                f"Настройки и статистика сохранены:\n{path}")

    def _import_data(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Импорт настроек и статистики", APP_DIR, "JSON (*.json)")
        if not path:
            return
        answer = QMessageBox.question(
            self, "Импорт",
            "Заменить текущие настройки и статистику данными из файла?\n"
            "Позиция окна, монитор и автозапуск останутся прежними.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self.widget_ref.import_data(path)
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.warning(self, "Импорт",
                                f"Не удалось импортировать файл:\n{exc}")
            return
        QMessageBox.information(
            self, "Импорт",
            f"Готово. Устройств в статистике: {count}.\n"
            "Окно настроек закроется, чтобы показать импортированные значения.")
        self.accept()

    def _pick_color(self):
        color = QColorDialog.getColor(QColor(self.accent_edit.text() or "#E31937"), self,
                                      "Цвет полос")
        if color.isValid():
            self.accent_edit.setText(color.name())

    def _pick_status_color(self):
        current = QColor(self.status_edit.text().strip())
        if not current.isValid():
            current = QColor(self.accent_edit.text().strip() or "#E31937")
        color = QColorDialog.getColor(current, self, "Цвет статусов")
        if color.isValid():
            self.status_edit.setText(color.name())

    # ------------------------------------------------- живое применение

    def _text_changed(self, *_args):
        """Ввод текста (цвет, фильтры) применяем с паузой, чтобы не дёргать виджет."""
        self._debounce.start()

    def _emit_change(self, *_args):
        self.changed.emit(self._collect())

    def flush(self):
        """До-применить отложенное изменение (вызывается при закрытии окна)."""
        if self._debounce.isActive():
            self._debounce.stop()
            self._emit_change()

    def _toggle_autostart(self, on):
        set_autostart(bool(on))
        actual = autostart_enabled()
        if actual != bool(on):
            self.autostart_sw.setChecked(actual)   # не удалось — вернуть как было
        self._emit_change()

    def _hover_toggled(self, *_args):
        self.hover_sp.setEnabled(self.hover_sw.isChecked())
        self._emit_change()

    def _signal_changed(self, *_args):
        """Низкий заряд / перегрев: порог активен только при включённом тумблере."""
        self.low_sp.setEnabled(self.low_sw.isChecked())
        self.hot_sp.setEnabled(self.hot_sw.isChecked())
        self._emit_change()

    def _collect(self):
        text = self.accent_edit.text().strip()
        color = QColor(text)
        if color.isValid():
            accent = color.name().upper()        # всегда #RRGGBB
            self._last_accent = accent
        else:
            accent = self._last_accent           # недописанный hex не сбрасывает цвет

        # статусный цвет: пустое поле — снова «как цвет полос»
        status_text = self.status_edit.text().strip()
        if not status_text:
            self._last_status = ""
        else:
            status_color = QColor(status_text)
            if status_color.isValid():
                self._last_status = status_color.name().upper()

        def parse(text):
            return [w.strip() for w in text.split(",") if w.strip()]

        return {
            "font": self.font_cb.currentFont().family(),
            "scale": self.scale_sp.value(),
            "opacity": self.opacity_sp.value(),
            "hover_fade": self.hover_sw.isChecked(),
            "hover_opacity": self.hover_sp.value(),
            "refresh_sec": self.refresh_sp.value(),
            "accent": accent,
            "status_color": self._last_status,
            "auto_accent": self.auto_sw.isChecked(),
            "hide_offline": self.hide_sw.isChecked(),
            "ignore": list(self._ignore),
            "minimal": self.minimal_sw.isChecked(),
            "autostart": autostart_enabled(),
            "low_battery": self.low_sp.value() if self.low_sw.isChecked() else 0,
            "overheat": self.hot_sp.value() if self.hot_sw.isChecked() else 0,
            "names": [[dev.strip(), alias.strip()]
                      for dev, alias in self._name_entries
                      if dev.strip() and alias.strip()],
            "headphones": {"match": parse(self.hp_edit.text())},
            "mouse": {"match": parse(self.ms_edit.text())},
        }


# ---------------------------------------------------- одна копия виджета

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\DesktopTelemetryWidget"
_instance_mutex = None      # хэндл живёт столько же, сколько процесс


def acquire_single_instance():
    """True — мы первая копия. Именованный мьютекс снимается сам при выходе."""
    global _instance_mutex
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                          ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        already = kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    except Exception:
        return True          # не смогли проверить — не мешаем запуску
    if not handle:
        return True
    if already:
        kernel32.CloseHandle(handle)
        return False
    _instance_mutex = handle
    return True


def start_instance_server(app):
    """Канал для второй копии: она попросит показать работающий виджет."""
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    return server if server.listen(IPC_NAME) else None


def notify_running_instance(attempts=4, pause_ms=150):
    """Просим работающую копию показать себя. True — если она на связи."""
    for _ in range(max(1, attempts)):
        sock = QLocalSocket()
        sock.connectToServer(IPC_NAME)
        if sock.waitForConnected(pause_ms):
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(pause_ms)
            sock.disconnectFromServer()
            return True
    return False


def handle_instance_request(server, widget):
    """Пришёл запрос от второй копии — подсвечиваем работающий виджет."""
    while server.hasPendingConnections():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.disconnectFromServer()
            conn.deleteLater()
    widget.signal_already_running()


# ---------------------------------------------------------------------- main

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Telemetry Widget")
    app.setQuitOnLastWindowClosed(True)

    # ---- одна копия на сеанс: вторая подсвечивает первую и сразу выходит
    if not acquire_single_instance():
        if not notify_running_instance():
            QMessageBox.information(
                None, "Виджет уже запущен",
                "Копия виджета уже работает — вторую запускать не нужно.")
        return 0

    server = start_instance_server(app)
    w = Widget()
    w.show()
    if server is not None:
        server.newConnection.connect(lambda: handle_instance_request(server, w))
        if server.hasPendingConnections():   # запрос пришёл раньше подключения
            handle_instance_request(server, w)

    app.aboutToQuit.connect(w.persist)          # настройки/позиция при выходе
    app.aboutToQuit.connect(w._shutdown)        # трей и история заряда
    app.aboutToQuit.connect(w._uninstall_mouse_hook)   # снять хук мыши
    app.aboutToQuit.connect(w._uninstall_foreground_watch)
    if os.environ.get("WIDGET_AUTOTEST"):
        QTimer.singleShot(4000, app.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
