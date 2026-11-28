# -*- coding: utf-8 -*-
"""Собирает архив обновления и обновляет version.json.

Запуск:  python tools/make_release.py 1.2.0 ["что нового"]

Результат:
  • `dist/widget-<версия>.zip` — файлы проекта без .git, личных данных и
    папки updates;
  • `VERSION` — новая версия;
  • `version.json` — версия, ссылка на архив и его SHA-256.

Архив нужно приложить к release на GitHub, а `version.json` закоммитить:
виджет читает именно его.
"""
import hashlib
import json
import os
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "https://github.com/serhiigad/telemetry-widget/releases/download"
SKIP_DIRS = {".git", "__pycache__", "updates", "dist", "screenshots",
             "screenshots/_raw"}
SKIP_FILES = {"config.json", "history.json", "stats.json", "iconpack.png"}


def collect():
    for base, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(base, BASE)
        for name in sorted(files):
            if name in SKIP_FILES or name.endswith((".pyc", ".zip", ".log")):
                continue
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            yield rel


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: python tools/make_release.py 1.2.0 [notes]")
    version = argv[1].strip().lstrip("vV")
    notes = argv[2] if len(argv) > 2 else ""
    dist = os.path.join(BASE, "dist")
    os.makedirs(dist, exist_ok=True)
    # VERSION пишем до упаковки, иначе в архив попадёт старая версия
    with open(os.path.join(BASE, "VERSION"), "w", encoding="utf-8") as f:
        f.write(version + "\n")
    archive = os.path.join(dist, f"widget-{version}.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in collect():
            zf.write(os.path.join(BASE, rel), rel)
    with open(archive, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    info = {"version": version,
            "url": f"{REPO}/v{version}/widget-{version}.zip",
            "sha256": digest, "notes": notes}
    with open(os.path.join(BASE, "version.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("архив:", archive)
    print("sha256:", digest)
    print("version.json обновлён — закоммитьте его и приложите архив к release")


if __name__ == "__main__":
    main(sys.argv)
