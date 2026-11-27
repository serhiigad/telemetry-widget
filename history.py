# -*- coding: utf-8 -*-
"""История заряда и статистика циклов «заряд → разряд».

`history.json` — точки заряда за последние сутки (для графика).
`stats.json` — длительная статистика: сколько устройство проработало от одной
зарядки до следующей. Если заряд вырос (устройство заряжали), текущий цикл
завершается и его время сохраняется. Данные устройств, которые не появлялись
больше года, удаляются.
"""

import json
import os
import time

KEEP_SEC = 24 * 3600      # сколько храним точки графика (сутки)
STEP_SEC = 300            # минимальный интервал между точками (5 минут)
MAX_POINTS = KEEP_SEC // STEP_SEC + 12

STATS_KEEP_SEC = 365 * 24 * 3600   # статистику устройства храним год
KEEP_DAYS = 60            # дневной расход (для графика по дням) — 60 дней
CHARGE_RISE = 2           # рост заряда на 2 % считаем началом зарядки
MAX_GAP_SEC = 150         # больший разрыв (виджет/устройство спало) не считаем
MAX_SESSIONS = 60         # сколько циклов храним на устройство
MIN_SESSION_SEC = 600     # короткие циклы не берём в среднее
MIN_SESSION_DROP = 5      # и циклы с падением меньше 5 % тоже
MAX_STEP_DROP = 30        # защита от одиночного «скачка» датчика, %


def _key(name):
    return (name or "").strip().lower()


def _num(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _day_key(ts):
    """Локальная дата в виде «ГГГГ-ММ-ДД» (ключ дневной статистики)."""
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


def _valid_day(day):
    try:
        time.strptime(str(day), "%Y-%m-%d")
        return True
    except Exception:
        return False


class History:
    """Кольцевая история заряда: load() → add() по мере обновления данных."""

    def __init__(self, path):
        self.path = path
        self.devices = {}     # key -> {"name": ..., "points": [[ts, level], ...]}
        self._dirty = False
        self.load()

    # ------------------------------------------------------------ файл
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(devices, dict):
            return
        now = time.time()
        for key, item in devices.items():
            if not isinstance(item, dict):
                continue
            points = []
            for p in item.get("points") or []:
                try:
                    ts, level = float(p[0]), int(p[1])
                except Exception:
                    continue
                if now - ts <= KEEP_SEC and 0 <= level <= 100:
                    points.append([ts, level])
            if points:
                self.devices[str(key)] = {
                    "name": str(item.get("name") or key),
                    "points": points[-MAX_POINTS:],
                }

    def save(self):
        if not self._dirty:
            return
        data = {"devices": self.devices}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception:
            pass

    # ---------------------------------------------------------- данные
    def add(self, name, level, now=None):
        """Добавить точку заряда (level = None — пропустить)."""
        if level is None:
            return False
        try:
            level = int(level)
        except Exception:
            return False
        if not 0 <= level <= 100:
            return False
        now = float(now if now is not None else time.time())
        key = _key(name)
        if not key:
            return False
        item = self.devices.setdefault(key, {"name": name, "points": []})
        item["name"] = name or item.get("name") or key
        points = item["points"]
        if points and now - points[-1][0] < STEP_SEC:
            return False                  # слишком часто — точку не пишем
        points.append([now, level])
        if len(points) > MAX_POINTS:
            del points[:-MAX_POINTS]
        self._dirty = True
        return True

    def prune(self, now=None):
        """Убрать точки старше суток (вызывается при старте и раз в час)."""
        now = float(now if now is not None else time.time())
        changed = False
        for key in list(self.devices):
            points = [p for p in self.devices[key]["points"]
                      if now - p[0] <= KEEP_SEC]
            if points != self.devices[key]["points"]:
                self.devices[key]["points"] = points
                changed = True
            if not points:
                del self.devices[key]
                changed = True
        if changed:
            self._dirty = True
        return changed

    def series(self, key):
        item = self.devices.get(_key(key))
        return list(item["points"]) if item else []

    def name_of(self, key):
        item = self.devices.get(_key(key))
        return item["name"] if item else key

    def keys(self):
        """Ключи устройств, у которых есть точки (свежие — первыми)."""
        return sorted(self.devices,
                      key=lambda k: self.devices[k]["points"][-1][0]
                      if self.devices[k]["points"] else 0, reverse=True)

    def clear(self):
        self.devices = {}
        self._dirty = True
        self.save()

    def to_dict(self):
        return {"devices": self.devices}

    def replace(self, raw):
        """Заменить историю данными из резервной копии."""
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(devices, dict):
            raise ValueError("в резервной копии нет истории заряда")
        self.devices = {}
        for key, item in devices.items():
            if not isinstance(item, dict):
                continue
            points = []
            for p in item.get("points") or []:
                try:
                    ts, level = float(p[0]), int(p[1])
                except Exception:
                    continue
                if 0 <= level <= 100:
                    points.append([ts, level])
            if points:
                self.devices[str(key)] = {
                    "name": str(item.get("name") or key),
                    "points": points[-MAX_POINTS:],
                }
        self._dirty = True


class ChargeStats:
    """Сколько времени устройство работает от одной зарядки.

    Цикл начинается, когда устройство впервые появилось онлайн (или сразу
    после зарядки), и длится, пока заряд не начнёт расти. Время копится только
    пока устройство онлайн: выключенное/ушедшее в офлайн время не считается.
    Оценка «хватит ещё» — по текущей скорости разряда этого цикла.
    """

    def __init__(self, path):
        self.path = path
        self.devices = {}
        self._dirty = False
        self.load()

    # ------------------------------------------------------------ файл
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return
        self._apply_raw(raw)
        # старые файлы не знали о дневной статистике — восстановим её
        # приблизительно из завершённых циклов
        if any(not item.get("daily") for item in self.devices.values()):
            for item in self.devices.values():
                if not item.get("daily"):
                    item["daily"] = self._seed_daily(item)
            self._dirty = True
        if self._prune_days():
            self._dirty = True

    def _apply_raw(self, raw):
        """Загрузить словарь устройств из файла/резервной копии."""
        self.devices = {}
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(devices, dict):
            return False
        for key, item in devices.items():
            if not isinstance(item, dict):
                continue
            session = self._clean_session(item.get("session"))
            sessions = []
            for s in item.get("sessions") or []:
                done = self._clean_done(s)
                if done is not None:
                    sessions.append(done)
            daily = {}
            raw_daily = item.get("daily")
            if isinstance(raw_daily, dict):
                for day, value in raw_daily.items():
                    clean = self._clean_daily(value)
                    if _valid_day(day) and clean is not None:
                        daily[str(day)] = clean
            self.devices[str(key)] = {
                "name": str(item.get("name") or key),
                "last_seen": _num(item.get("last_seen")),
                "session": session,
                "sessions": sessions[-MAX_SESSIONS:],
                "daily": daily,
            }
        return True

    def to_dict(self):
        return {"devices": self.devices}

    def replace(self, raw):
        """Заменить статистику данными из резервной копии (с проверкой)."""
        if not self._apply_raw(raw):
            raise ValueError("в резервной копии нет данных статистики")
        if any(not item.get("daily") for item in self.devices.values()):
            for item in self.devices.values():
                if not item.get("daily"):
                    item["daily"] = self._seed_daily(item)
        self._prune_days()
        self._dirty = True

    @staticmethod
    def _clean_daily(raw):
        if not isinstance(raw, dict):
            return None
        drain = _num(raw.get("drain"))
        online = _num(raw.get("online"))
        if online <= 0:
            return None
        return {
            "drain": max(0.0, min(10000.0, drain)),
            "online": max(0.0, min(2 * 86400.0, online)),
        }

    @staticmethod
    def _seed_daily(item):
        """Разложить завершённые циклы по дням (для старых файлов).

        Точное время внутри цикла неизвестно, поэтому секунды и проценты
        распределяются пропорционально пересечению цикла с календарным днём.
        """
        out = {}
        for s in item.get("sessions") or []:
            start = _num(s.get("started"))
            end = _num(s.get("ended"))
            seconds = _num(s.get("seconds"))
            drop = max(0.0, _num(s.get("from")) - _num(s.get("to")))
            if not start or not end or end <= start or seconds <= 0:
                continue
            span = end - start
            cur = start
            guard = 0
            while cur < end and guard < 400:
                guard += 1
                lt = time.localtime(cur)
                midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1,
                                        0, 0, 0, 0, 0, -1))
                chunk_end = min(end, midnight)
                chunk = max(0.0, chunk_end - cur)
                frac = chunk / span
                day = _day_key(cur)
                bucket = out.setdefault(day, {"drain": 0.0, "online": 0.0})
                bucket["online"] += seconds * frac
                bucket["drain"] += drop * frac
                if chunk_end <= cur:
                    cur += 3600
                else:
                    cur = chunk_end
        return out

    def _prune_days(self):
        cutoff = _day_key(time.time() - KEEP_DAYS * 86400)
        changed = False
        for item in self.devices.values():
            daily = item.get("daily") or {}
            for day in list(daily):
                if day < cutoff:
                    del daily[day]
                    changed = True
        return changed

    @staticmethod
    def _clean_session(raw):
        if not isinstance(raw, dict):
            return None
        try:
            start_level = int(raw.get("start_level"))
            last_level = int(raw.get("last_level", start_level))
            low = int(raw.get("low", start_level))
        except Exception:
            return None
        if not 0 <= start_level <= 100 or not 0 <= last_level <= 100:
            return None
        return {
            "started": _num(raw.get("started")),
            "start_level": start_level,
            "low": max(0, min(100, low)),
            "last_level": max(0, min(100, last_level)),
            "active": max(0.0, _num(raw.get("active"))),
            "last_seen": _num(raw.get("last_seen")),
        }

    @staticmethod
    def _clean_done(raw):
        if not isinstance(raw, dict):
            return None
        try:
            from_level = int(raw.get("from"))
            to_level = int(raw.get("to"))
        except Exception:
            return None
        seconds = _num(raw.get("seconds"))
        if seconds <= 0 or not 0 <= from_level <= 100 or not 0 <= to_level <= 100:
            return None
        return {
            "started": _num(raw.get("started")),
            "ended": _num(raw.get("ended")),
            "seconds": seconds,
            "from": from_level,
            "to": to_level,
        }

    def save(self):
        if not self._dirty:
            return
        data = {"devices": self.devices}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception:
            pass

    # ---------------------------------------------------------- данные
    def note(self, name, level, now=None):
        """Учесть онлайн-замер заряда. True — если цикл завершился зарядкой."""
        if level is None:
            return False
        try:
            level = int(level)
        except Exception:
            return False
        if not 0 <= level <= 100:
            return False
        now = float(now if now is not None else time.time())
        key = _key(name)
        if not key:
            return False
        item = self.devices.get(key)
        if item is None:
            item = self.devices[key] = {
                "name": name, "last_seen": now,
                "session": None, "sessions": [], "daily": {},
            }
        item.setdefault("daily", {})
        item["name"] = name or item.get("name") or key
        item["last_seen"] = now
        self._dirty = True

        session = item.get("session")
        if session is None:
            item["session"] = self._new_session(level, now)
            return False

        # учитываем только непрерывное нахождение онлайн
        prev = _num(session.get("last_seen"))
        prev_level = int(session.get("last_level", level))
        if prev and 0 <= now - prev <= MAX_GAP_SEC:
            gap = now - prev
            session["active"] = _num(session.get("active")) + gap
            # дневной расход: рост заряда (зарядка) не учитываем вовсе,
            # а ровные участки — это работа/ожидание на батарее, их считаем
            if level <= prev_level:
                bucket = item["daily"].setdefault(
                    _day_key(now), {"drain": 0.0, "online": 0.0})
                bucket["online"] += gap
                if level < prev_level:
                    bucket["drain"] += min(prev_level - level, MAX_STEP_DROP)
        session["last_seen"] = now

        low = int(session.get("low", level))
        if level >= low + CHARGE_RISE:
            # устройство заряжается — закрываем цикл «работал от заряда»
            seconds = _num(session.get("active"))
            start_level = int(session.get("start_level", level))
            # цикл, в котором не успели разрядиться (например, блок принесли
            # с зарядки и тут же поставили обратно), в статистику не пишем:
            # иначе каждый шаг зарядки +2 % добавлял бы цикл длиной в минуту
            if seconds >= MIN_SESSION_SEC and start_level - low >= MIN_SESSION_DROP:
                item.setdefault("sessions", []).append({
                    "started": _num(session.get("started")),
                    "ended": now,
                    "seconds": seconds,
                    "from": start_level,
                    "to": low,
                })
                del item["sessions"][:-MAX_SESSIONS]
            item["session"] = self._new_session(level, now)
            return True

        if level < low:
            session["low"] = level
        session["last_level"] = level
        return False

    @staticmethod
    def _new_session(level, now):
        return {
            "started": now,
            "start_level": level,
            "low": level,
            "last_level": level,
            "active": 0.0,
            "last_seen": now,
        }

    def report(self, key, now=None):
        """Сводка: текущий цикл, оценка остатка и статистика прошлых циклов."""
        item = self.devices.get(_key(key))
        if not item:
            return None
        session = item.get("session")
        report = {"name": item.get("name") or key, "avg": None,
                  "last": None, "active": None, "start_level": None,
                  "level": None, "empty_in": None, "cycles": 0,
                  "current_rate": None, "avg_rate": None}
        done = [s for s in item.get("sessions") or []
                if s["seconds"] >= MIN_SESSION_SEC
                and s["from"] - s["to"] >= MIN_SESSION_DROP]
        report["cycles"] = len(done)
        if done:
            report["avg"] = sum(s["seconds"] for s in done) / len(done)
            report["last"] = done[-1]
            total_sec = sum(s["seconds"] for s in done)
            total_drop = sum(s["from"] - s["to"] for s in done)
            if total_sec > 0 and total_drop > 0:
                report["avg_rate"] = total_drop / (total_sec / 3600.0)
        if session:
            active = _num(session.get("active"))
            start_level = int(session.get("start_level", 0))
            level = int(session.get("last_level", start_level))
            report["active"] = active
            report["start_level"] = start_level
            report["level"] = level
            drop = start_level - level
            if active > 0 and drop > 0 and level > 0:
                rate = drop / (active / 3600.0)
                report["current_rate"] = rate
                report["empty_in"] = level / rate * 3600.0
            elif report["avg_rate"] and level:
                # цикл только начался — оценка по среднему расходу прошлых
                report["empty_in"] = level / report["avg_rate"] * 3600.0
        return report

    def daily_rates(self, key, days=7, now=None):
        """Средний расход, %/ч, по дням (от старых к новым).

        Дни без заметного онлайна (< 5 минут) пропускаются: по ним нельзя
        судить о расходе. Возвращаются словари с day/rate/drain/online.
        """
        item = self.devices.get(_key(key))
        if not item:
            return []
        now = float(now if now is not None else time.time())
        wanted = {_day_key(now - i * 86400) for i in range(max(1, int(days)))}
        out = []
        for day in sorted(wanted):
            value = (item.get("daily") or {}).get(day)
            if not value or value.get("online", 0) < 300:
                continue
            rate = value["drain"] / (value["online"] / 3600.0)
            out.append({"day": day, "rate": max(0.0, rate),
                        "drain": value["drain"], "online": value["online"]})
        return out

    def prune(self, now=None):
        """Удалить старые дни и устройства, не появлявшиеся больше года."""
        now = float(now if now is not None else time.time())
        changed = False
        cutoff = _day_key(now - KEEP_DAYS * 86400)
        for item in self.devices.values():
            for day in list(item.get("daily") or {}):
                if day < cutoff:
                    del item["daily"][day]
                    changed = True
        for key in list(self.devices):
            last = _num(self.devices[key].get("last_seen"))
            if not last or now - last > STATS_KEEP_SEC:
                del self.devices[key]
                changed = True
        if changed:
            self._dirty = True
        return changed

    def clear(self):
        self.devices = {}
        self._dirty = True
        self.save()
