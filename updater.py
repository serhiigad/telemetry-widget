# -*- coding: utf-8 -*-
"""Проверка и установка обновлений виджета с официального репозитория.

Схема намеренно простая и безопасная:

1. В репозитории лежит `version.json`:

       {
         "version": "1.1.0",
         "url": "https://github.com/serhiigad/telemetry-widget/releases/...",
         "sha256": "…64 шестнадцатеричных символа…",
         "notes": "что нового"
       }

2. Виджет раз в несколько часов читает этот файл. Если версия новее —
   скачивает архив и сверяет SHA-256 (без совпадения хэша установка
   отклоняется).
3. Проверенный архив распаковывается в `updates/pending/`. Потом по кнопке
   «Обновить и перезапустить» файлы копируются поверх установленных, а
   `VERSION` обновляется.
4. `config.json`, `history.json`, `stats.json`, `config.example.json` и папка
   `updates/` никогда не перезаписываются.

Ничего не запускается и не распаковывается без проверки контрольной суммы.
"""

import hashlib
import json
import os
import shutil
import urllib.request
import zipfile

VERSION_FILE = "VERSION"
STAGE_DIR = "updates"
PENDING_DIR = os.path.join(STAGE_DIR, "pending")
STATE_FILE = os.path.join(STAGE_DIR, "state.json")
PROTECTED = {"config.json", "history.json", "stats.json",
             "config.example.json", "updates"}
SKIP_DIRS = {"__pycache__", ".git", "updates"}
USER_AGENT = "TelemetryWidget"


class UpdateError(Exception):
    """Ошибка проверки, скачивания или распаковки обновления."""


def parse_version(text):
    """«1.10.2» → (1, 10, 2): сравниваем числами, а не строками."""
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def local_version(app_dir):
    try:
        with open(os.path.join(app_dir, VERSION_FILE), "r", encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _fetch(url, timeout=15):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def check(url, app_dir, timeout=15):
    """Описание новой версии или None, если обновляться не нужно."""
    try:
        info = json.loads(_fetch(url, timeout).decode("utf-8"))
    except Exception as exc:                       # сеть, DNS, битый JSON
        raise UpdateError(f"нет связи с GitHub: {exc}") from exc
    if not isinstance(info, dict) or not info.get("version"):
        raise UpdateError("в version.json нет поля version")
    if parse_version(info["version"]) <= parse_version(local_version(app_dir)):
        return None
    if not str(info.get("url") or "").startswith(("http://", "https://")):
        raise UpdateError("в version.json нет ссылки на архив")
    digest = str(info.get("sha256") or "").strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise UpdateError("в version.json нет корректного SHA-256")
    return info


def stage(info, app_dir):
    """Скачать архив, проверить SHA-256 и распаковать в updates/pending."""
    version = str(info.get("version")).strip()
    digest = str(info.get("sha256")).strip().lower()
    try:
        data = _fetch(str(info.get("url")), timeout=120)
    except Exception as exc:                       # сеть, таймаут, 404
        raise UpdateError(f"не удалось скачать архив: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != digest:
        raise UpdateError("контрольная сумма не совпала — обновление отклонено")

    stage_root = os.path.join(app_dir, STAGE_DIR)
    pending_dir = os.path.join(app_dir, PENDING_DIR)
    os.makedirs(stage_root, exist_ok=True)
    shutil.rmtree(pending_dir, ignore_errors=True)
    os.makedirs(pending_dir, exist_ok=True)
    zip_path = os.path.join(stage_root, f"update-{version}.zip")
    with open(zip_path, "wb") as f:
        f.write(data)
    try:
        _unpack(zip_path, pending_dir)
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    state = {"version": version, "sha256": digest, "url": info.get("url"),
             "notes": str(info.get("notes") or "")}
    with open(os.path.join(app_dir, STATE_FILE), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return state


def _unpack(zip_path, dest):
    """Распаковать архив, не выпуская файлы за пределы папки обновления."""
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            target = os.path.realpath(os.path.join(root, name))
            if target != root and not target.startswith(root + os.sep):
                raise UpdateError(
                    "архив пытается писать за пределы папки обновления")
        zf.extractall(root)


def pending(app_dir):
    """Описание готового к установке обновления (None — ничего нет)."""
    try:
        with open(os.path.join(app_dir, STATE_FILE), "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not state.get("version"):
        return None
    if not os.path.isdir(os.path.join(app_dir, PENDING_DIR)):
        return None
    return state


def apply_pending(app_dir):
    """Скопировать проверенные файлы поверх установленных.

    Возвращает установленную версию или None, если устанавливать нечего.
    """
    state = pending(app_dir)
    if not state:
        return None
    src = os.path.join(app_dir, PENDING_DIR)
    for base, dirs, files in os.walk(src):
        rel_dir = os.path.relpath(base, src)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        target_dir = app_dir if rel_dir == "." else os.path.join(app_dir, rel_dir)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            if rel_dir == "." and name in PROTECTED:
                continue                       # личные данные не трогаем
            shutil.copy2(os.path.join(base, name),
                         os.path.join(target_dir, name))
    with open(os.path.join(app_dir, VERSION_FILE), "w", encoding="utf-8") as f:
        f.write(str(state["version"]).strip() + "\n")
    shutil.rmtree(src, ignore_errors=True)
    try:
        os.remove(os.path.join(app_dir, STATE_FILE))
    except OSError:
        pass
    return str(state["version"])


def discard(app_dir):
    """Выбросить скачанное обновление (например, если оно не подошло)."""
    shutil.rmtree(os.path.join(app_dir, PENDING_DIR), ignore_errors=True)
    try:
        os.remove(os.path.join(app_dir, STATE_FILE))
    except OSError:
        pass
