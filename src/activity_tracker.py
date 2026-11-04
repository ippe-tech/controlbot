"""Учёт активности приложений для ControlBot.

Модуль отслеживает:
- активное (переднее) окно и приложение;
- время, проведённое в каждом приложении (за вычетом АФК);
- заголовки окон внутри приложений (опционально);
- системную неактивность (АФК) по отсутствию ввода мыши/клавиатуры;
- список открытых видимых окон по приложениям.

Данные хранятся в JSON-файле с автоматической очисткой истории
и учитываются по календарным дням.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, time as dt_time, timedelta
from html import escape
from pathlib import Path
from typing import Any

from browser_sites import collect_visits

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    HAS_PSUTIL = False

LOGGER = logging.getLogger("controlbot.activity")

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _dwmapi = ctypes.windll.dwmapi

    _DWMWA_CLOAKED = 14
    _GWL_EXSTYLE = -20
    _WS_EX_TOOLWINDOW = 0x00000080

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    _ENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )


# =============================================================================
# WINDOWS API WRAPPERS
# =============================================================================

def _get_idle_seconds_windows() -> float:
    """Seconds since last keyboard/mouse input (system-wide)."""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    millis = _kernel32.GetTickCount() - info.dwTime
    return max(0.0, millis / 1000.0)


def _get_foreground_window_windows() -> tuple[int, str]:
    """Return (pid, window_title) of the foreground window."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return 0, ""

    pid = ctypes.wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return int(pid.value or 0), ""

    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return int(pid.value or 0), buffer.value


def _list_visible_windows_windows() -> list[tuple[int, str]]:
    """Return (pid, title) for visible, non-cloaked top-level windows."""
    results: list[tuple[int, str]] = []

    def on_window(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True

        exstyle = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if exstyle & _WS_EX_TOOLWINDOW:
            return True

        cloaked = ctypes.wintypes.DWORD(0)
        _dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        if cloaked.value:
            return True

        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True

        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if not title:
            return True

        pid = ctypes.wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        results.append((int(pid.value or 0), title))
        return True

    callback = _ENUMPROC(on_window)
    _user32.EnumWindows(callback, 0)
    return results


if IS_WINDOWS:
    get_idle_seconds = _get_idle_seconds_windows
    get_foreground_window = _get_foreground_window_windows
    list_visible_windows = _list_visible_windows_windows
else:
    def get_idle_seconds() -> float:  # noqa: D401
        return 0.0

    def get_foreground_window() -> tuple[int, str]:  # noqa: D401
        return 0, ""

    def list_visible_windows() -> list[tuple[int, str]]:  # noqa: D401
        return []


# =============================================================================
# HELPERS
# =============================================================================

def format_duration_short(seconds: float) -> str:
    """Format seconds compactly: '2 ч 5 мин', '12 мин 30 сек', '45 сек'."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if minutes:
        return f"{minutes} мин" if secs == 0 else f"{minutes} мин {secs} сек"
    return f"{secs} сек"


def _shorten(text: str, limit: int) -> str:
    """Collapse whitespace and trim long text."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)] + "…"


def _fit_message(text: str, limit: int = 3900) -> str:
    """Fit text into the Telegram message limit without breaking HTML tags."""
    if len(text) <= limit:
        return text

    cut = text[:limit]
    if "<" in cut:
        last_open = cut.rfind("<")
        last_close = cut.rfind(">")
        if last_open > last_close:
            cut = cut[:last_open]
    return cut + "\n\n…Сообщение обрезано."


# =============================================================================
# ПЕРИОДЫ СУТОК И ВЫБОРКА ПО ВРЕМЕНИ
# =============================================================================

# Ночь принадлежит дню, в который она ЗАКАНЧИВАЕТСЯ:
# «ночь с 29.10 по 30.10» = 21:00 29.10 → 08:00 30.10, входит в отчёт за 30.10.
PERIOD_ORDER = ("ночь", "утро", "день", "вечер")
PERIODS: dict[str, dict[str, Any]] = {
    "ночь": {"label": "Ночь", "emoji": "🌙", "start": 21, "end": 8},
    "утро": {"label": "Утро", "emoji": "🌅", "start": 8, "end": 12},
    "день": {"label": "День", "emoji": "☀️", "start": 12, "end": 18},
    "вечер": {"label": "Вечер", "emoji": "🌆", "start": 18, "end": 21},
}

_PERIOD_ALIASES = {
    "ночь": "ночь", "night": "ночь",
    "утро": "утро", "morning": "утро",
    "день": "день", "day": "день",
    "вечер": "вечер", "evening": "вечер",
}

_DATE_ALIASES = {"сегодня": 0, "today": 0, "вчера": 1, "yesterday": 1}

_BROWSER_TAIL_RE = re.compile(
    r"\s*[-–—]\s*(?:Google\s*Chrome|Chrome|Mozilla\s*Firefox|Firefox|"
    r"Microsoft\s*Edge|Edge|Яндекс(?:\.Браузер)?|Opera|Brave|Vivaldi|Safari)\s*$",
    re.IGNORECASE,
)


def normalize_period(raw: str | None) -> str | None:
    """«Ночь»/«night»/… → ключ периода; иначе None."""
    return _PERIOD_ALIASES.get((raw or "").strip().lower())


def normalize_date_arg(raw: str | None) -> str | None:
    """«сегодня»/«вчера»/«ГГГГ-ММ-ДД» → ключ дня; иначе None."""
    value = (raw or "").strip().lower()
    if not value:
        return date.today().isoformat()
    if value in _DATE_ALIASES:
        return (date.today() - timedelta(days=_DATE_ALIASES[value])).isoformat()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return parsed.date().isoformat()


def period_window(day_key: str, period: str) -> tuple[float, float]:
    """Локальные границы периода (epoch).

    Для ночи — с 21:00 предыдущего дня до 08:00 текущего.
    """
    day = date.fromisoformat(day_key)
    spec = PERIODS[period]
    start_hour, end_hour = spec["start"], spec["end"]
    if start_hour > end_hour:
        start = datetime.combine(day - timedelta(days=1), dt_time(start_hour, 0))
        end = datetime.combine(day, dt_time(end_hour, 0))
    else:
        start = datetime.combine(day, dt_time(start_hour, 0))
        end = datetime.combine(day, dt_time(end_hour, 0))
    return start.timestamp(), end.timestamp()


def period_header(day_key: str, period: str, html: bool = True) -> str:
    """Заголовок в формате: «Ночь с 29.10.2026 по 30.10.2026 с 21:00 по 8:00»."""
    day = date.fromisoformat(day_key)
    spec = PERIODS[period]
    start_hour, end_hour = spec["start"], spec["end"]
    if start_hour > end_hour:
        prev = day - timedelta(days=1)
        head = (
            f"{spec['emoji']} {spec['label']} с {prev.strftime('%d.%m.%Y')} "
            f"по {day.strftime('%d.%m.%Y')} с {start_hour}:00 по {end_hour}:00"
        )
    else:
        head = (
            f"{spec['emoji']} {spec['label']} {day.strftime('%d.%m.%Y')} "
            f"с {start_hour}:00 по {end_hour}:00"
        )
    return f"<b>{head}</b>" if html else head


def parse_time_window(date_key: str, raw: str) -> tuple[float, float, str] | None:
    """«ночь» или «21:00-23:30» → (start_ts, end_ts, подпись) или None."""
    period = normalize_period(raw)
    if period is not None:
        start_ts, end_ts = period_window(date_key, period)
        return start_ts, end_ts, period_header(date_key, period, html=False)

    match = re.match(
        r"^(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})$",
        (raw or "").strip(),
    )
    if not match:
        return None
    start_h, start_m, end_h, end_m = (int(group) for group in match.groups())
    if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
        return None

    day = date.fromisoformat(date_key)
    start = datetime.combine(day, dt_time(start_h, start_m))
    end = datetime.combine(day, dt_time(end_h, end_m))
    if end <= start:
        end += timedelta(days=1)  # диапазон через полночь
    if end.date() == day:
        label = f"{day.strftime('%d.%m.%Y')} с {start_h}:{start_m:02d} по {end_h}:{end_m:02d}"
    else:
        label = (
            f"с {day.strftime('%d.%m.%Y')} {start_h}:{start_m:02d} "
            f"по {end.strftime('%d.%m.%Y')} {end_h}:{end_m:02d}"
        )
    return start.timestamp(), end.timestamp(), label


def _display_title(title: str) -> str:
    """Убирает хвост браузера («— Google Chrome» и т.п.) — остаётся сайт/страница."""
    text = (title or "").strip()
    original = text
    while True:
        stripped = _BROWSER_TAIL_RE.sub("", text).strip()
        if stripped == text or not stripped:
            break
        text = stripped
    return text or original


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def split_message(text: str, limit: int = 3800) -> list[str]:
    """Режет длинный текст на части по строкам (теги HTML не разрываются)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                parts.append(current)
                current = ""
            while len(line) > limit:  # одна гигантская строка — режем жёстко
                parts.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


# =============================================================================
# TRACKER
# =============================================================================

class ActivityTracker:
    """Tracks foreground applications, AFK time and persists daily stats."""

    def __init__(
        self,
        data_path: Path | str,
        afk_threshold_seconds: int = 300,
        track_titles: bool = True,
        retention_days: int = 3,
        save_interval_seconds: int = 120,
        enabled: bool = True,
        title_limit: int = 120,
        titles_per_app_limit: int = 50,
        min_session_seconds: int = 10,
        log_gap_threshold_seconds: int = 900,
        max_log_records: int = 30000,
        browser_sites: bool = True,
        browser_sites_interval: int = 60,
    ) -> None:
        self.data_path = Path(data_path)
        self.afk_threshold_seconds = max(30, int(afk_threshold_seconds))
        self.track_titles = bool(track_titles)
        self.retention_days = max(1, int(retention_days))
        self.save_interval_seconds = max(30, int(save_interval_seconds))
        self.enabled = bool(enabled)
        self._title_limit = title_limit
        self._titles_per_app_limit = titles_per_app_limit
        self._min_session_seconds = max(0, int(min_session_seconds))
        self._log_gap_threshold = max(0, int(log_gap_threshold_seconds))
        self._max_log_records = max(1000, int(max_log_records))
        self.browser_sites = bool(browser_sites)
        self.browser_sites_interval = max(30, int(browser_sites_interval))

        self._state: dict[str, Any] = {
            "version": 2,
            "last_summary_date": "",
            "browser_seen": {},
            "days": {},
            "log": [],
            "open": None,
        }
        self._last_tick: float | None = None
        self._last_save: float = 0.0
        self._last_sites_check: float = 0.0
        self._current: dict[str, Any] | None = None
        self._current_active: float = 0.0
        self._pid_cache: dict[int, tuple[float, str]] = {}

        self._load_sync()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_sync(self) -> None:
        if not self.data_path.exists():
            return

        try:
            raw = json.loads(self.data_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                days = raw.get("days")
                log = raw.get("log")
                open_ses = raw.get("open")
                browser_seen = raw.get("browser_seen")
                self._state = {
                    "version": raw.get("version", 1),
                    "last_summary_date": str(raw.get("last_summary_date", "") or ""),
                    "browser_seen": browser_seen if isinstance(browser_seen, dict) else {},
                    "days": days if isinstance(days, dict) else {},
                    "log": log if isinstance(log, list) else [],
                    "open": open_ses if isinstance(open_ses, dict) else None,
                }
        except (OSError, json.JSONDecodeError) as exc:
            backup = self.data_path.with_suffix(".corrupt.json")
            LOGGER.error("Файл активности повреждён (%s). Резервная копия: %s", exc, backup)
            try:
                os.replace(self.data_path, backup)
            except OSError:
                pass
            return

        # Незакрытая сессия с прошлого запуска: если файл давно не сохранялся
        # (ПК был выключен), закрываем её по времени последнего сохранения —
        # иначе простой попал бы в длительность сессии.
        open_ses = self._state.get("open")
        if open_ses:
            try:
                mtime = self.data_path.stat().st_mtime
            except OSError:
                mtime = time.time()
            self._close_session(min(time.time(), mtime))

    def _save_sync(self) -> None:
        self._prune_state()
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.data_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(self._state, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.data_path)
        except OSError as exc:
            LOGGER.error("Не удалось сохранить данные активности: %s", exc)

    async def save(self) -> None:
        await asyncio.to_thread(self._save_sync)

    def _prune_state(self) -> None:
        cutoff = (date.today() - timedelta(days=self.retention_days - 1)).isoformat()
        days = self._state["days"]
        for key in [key for key in days if key < cutoff]:
            del days[key]

        for day in days.values():
            for app in day.get("apps", {}).values():
                titles = app.get("titles")
                if titles and len(titles) > self._titles_per_app_limit:
                    top = sorted(titles.items(), key=lambda kv: kv[1], reverse=True)
                    app["titles"] = dict(top[: self._titles_per_app_limit])
            for site in day.get("sites", {}).values():
                titles = site.get("titles")
                if titles and len(titles) > 30:
                    top = sorted(titles.items(), key=lambda kv: kv[1], reverse=True)
                    site["titles"] = dict(top[:30])

        # Журнал сессий хранится ту же глубину, что и дни.
        cutoff_ts = datetime.combine(
            date.today() - timedelta(days=self.retention_days - 1), dt_time.min
        ).timestamp()
        log = self._state.get("log")
        if log:
            self._state["log"] = [
                rec for rec in log if float(rec.get("start", 0.0) or 0.0) >= cutoff_ts
            ]
            if len(self._state["log"]) > self._max_log_records:
                self._state["log"] = self._state["log"][-self._max_log_records :]

        open_ses = self._state.get("open")
        if open_ses and float(open_ses.get("start", 0.0) or 0.0) < cutoff_ts:
            self._state["open"] = None

    # ------------------------------------------------------------------
    # Internal bookkeeping
    # ------------------------------------------------------------------

    def _today_key(self) -> str:
        return date.today().isoformat()

    def _day(self, key: str) -> dict[str, Any]:
        days = self._state["days"]
        if key not in days:
            days[key] = {
                "apps": {},
                "sites": {},
                "afk_seconds": 0.0,
                "unknown_seconds": 0.0,
                "first_activity": None,
                "last_activity": None,
            }
        return days[key]

    def _resolve_exe(self, pid: int) -> str | None:
        if not pid or psutil is None:
            return None

        now = time.time()
        cached = self._pid_cache.get(pid)
        if cached and cached[0] > now:
            return cached[1] or None

        name: str | None = None
        try:
            name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            name = None
        except Exception:
            name = None

        self._pid_cache[pid] = (now + 60.0, name or "")
        if len(self._pid_cache) > 1024:
            for stale_pid in [
                pid_key for pid_key, (expiry, _) in self._pid_cache.items() if expiry <= now
            ]:
                del self._pid_cache[stale_pid]
        return name

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Periodic job: measure the elapsed interval and credit it."""
        if not self.enabled or not IS_WINDOWS:
            return

        try:
            self._record_tick()
        except Exception as exc:
            LOGGER.error("Ошибка тика учёта активности: %s", exc)
            return

        await self._collect_sites()

        now = time.time()
        if now - self._last_save >= self.save_interval_seconds:
            self._last_save = now
            await self.save()

    async def _collect_sites(self) -> None:
        """Забирает новые визиты из истории браузеров и раскладывает по дням."""
        if not self.browser_sites:
            return

        now = time.time()
        if now - self._last_sites_check < self.browser_sites_interval:
            return
        self._last_sites_check = now

        state = self._state.setdefault("browser_seen", {})
        if not isinstance(state, dict):
            state = {}
            self._state["browser_seen"] = state

        try:
            visits = await asyncio.to_thread(collect_visits, state)
        except Exception as exc:
            LOGGER.error("Ошибка чтения истории браузеров: %s", exc)
            return

        if not visits:
            return

        for visit in visits:
            day_key = date.fromtimestamp(visit["ts"]).isoformat()
            day = self._state["days"].get(day_key)
            if day is None:
                # Не создаём «пустой» день ради одного визита, если дата
                # выходит за пределы хранения.
                cutoff = (date.today() - timedelta(days=self.retention_days - 1)).isoformat()
                if day_key < cutoff:
                    continue
                day = self._day(day_key)

            sites = day.setdefault("sites", {})
            domain = visit["domain"]
            entry = sites.setdefault(
                domain,
                {"visits": 0, "last": 0.0, "first": visit["ts"], "titles": {}},
            )
            entry["visits"] += 1
            entry["last"] = max(entry.get("last", 0.0), visit["ts"])
            entry["first"] = min(entry.get("first", visit["ts"]), visit["ts"])
            title = _shorten(visit.get("title") or "", 100)
            if title:
                titles = entry.setdefault("titles", {})
                titles[title] = titles.get(title, 0) + 1

    def _record_tick(self) -> None:
        now = time.time()
        last = self._last_tick
        self._last_tick = now

        if last is None:
            return

        delta = now - last
        if delta <= 0:
            return

        if delta > 600:
            # Долгая пауза (сон/гибернация/остановка бота) — не приписываем
            # это время приложениям, но и не теряем: считаем АФК.
            self._day(self._today_key())["afk_seconds"] += delta
            # Сессия, открытая до паузы, прекратилась не позднее последнего тика.
            self._close_session(last)
            self._state["open"] = None
            return

        pid, title = get_foreground_window()
        idle = get_idle_seconds()
        afk = idle >= self.afk_threshold_seconds
        exe = self._resolve_exe(pid)
        day = self._day(self._today_key())

        if afk:
            day["afk_seconds"] += delta
        elif exe:
            app = day["apps"].setdefault(
                exe, {"active_seconds": 0.0, "switches": 0, "last_used": 0.0, "titles": {}}
            )
            app["active_seconds"] += delta
            app["last_used"] = now
            day["last_activity"] = now
            if day["first_activity"] is None:
                day["first_activity"] = now
            if self.track_titles and title:
                title_key = _shorten(title, self._title_limit)
                titles = app["titles"]
                titles[title_key] = titles.get(title_key, 0.0) + delta
        else:
            # Нет окна (рабочий стол без фокуса, экран блокировки) или
            # процесс не определяется (запущен от администратора).
            day["unknown_seconds"] += delta
            day["last_activity"] = now

        prev_exe = self._current["exe"] if self._current else None
        if exe and exe != prev_exe:
            if self._current is not None and not afk:
                switched_app = day["apps"].get(exe)
                if switched_app is not None:
                    switched_app["switches"] += 1
            self._current = {
                "exe": exe,
                "title": _shorten(title, 100),
                "pid": pid,
                "since": now,
            }
            self._current_active = 0.0
        elif self._current:
            self._current["title"] = _shorten(title, 100)

        if not afk and exe:
            self._current_active += delta

        # --- Журнал сессий: когда и что было открыто --------------------
        session_title = ""
        if self.track_titles and title:
            session_title = _shorten(title, self._title_limit)
        focus = (exe, session_title) if (exe and not afk) else None

        open_ses = self._state.get("open")
        open_key = (open_ses.get("app"), open_ses.get("title")) if open_ses else None
        if focus != open_key:
            self._close_session(now)
            if focus is not None:
                self._state["open"] = {
                    "app": exe,
                    "title": session_title,
                    "start": now,
                }

    # ------------------------------------------------------------------
    # Live snapshot
    # ------------------------------------------------------------------

    async def snapshot_now(self) -> dict[str, Any]:
        """Live snapshot: foreground window, input idle, visible windows, day totals."""

        if not self.enabled:
            return {"available": False, "reason": "disabled"}

        def collect() -> dict[str, Any]:
            pid, title = get_foreground_window()
            idle = get_idle_seconds()
            windows = list_visible_windows()
            return {"pid": pid, "title": title, "idle": idle, "windows": windows}

        data = await asyncio.to_thread(collect)

        exe = self._resolve_exe(data["pid"])

        grouped: dict[str, list[str]] = {}
        for window_pid, window_title in data["windows"]:
            window_exe = self._resolve_exe(window_pid) or "unknown.exe"
            grouped.setdefault(window_exe, []).append(_shorten(window_title, 70))

        visible = [
            {"exe": name, "titles": titles, "count": len(titles)}
            for name, titles in grouped.items()
        ]
        visible.sort(key=lambda item: (-item["count"], item["exe"].lower()))

        day = self._state["days"].get(self._today_key(), {})
        active_today = sum(
            app.get("active_seconds", 0.0) for app in day.get("apps", {}).values()
        )

        current = self._current or {}
        same_window = bool(exe) and current.get("exe") == exe
        return {
            "available": True,
            "exe": exe,
            "title": _shorten(data["title"], 100),
            "idle": data["idle"],
            "afk": data["idle"] >= self.afk_threshold_seconds,
            "session_seconds": self._current_active if same_window else 0.0,
            "session_since": current.get("since") if same_window else None,
            "visible": visible[:15],
            "today": {
                "active": active_today,
                "afk": day.get("afk_seconds", 0.0),
                "unknown": day.get("unknown_seconds", 0.0),
                "first_activity": day.get("first_activity"),
            },
        }

    # ------------------------------------------------------------------
    # Aggregation and reports
    # ------------------------------------------------------------------

    def _recent_day_keys(self, days: int) -> list[str]:
        today = date.today()
        return [(today - timedelta(days=offset)).isoformat() for offset in range(max(1, days))]

    def _aggregate(self, day_keys: list[str]) -> dict[str, Any]:
        apps: dict[str, dict[str, Any]] = {}
        totals: dict[str, Any] = {
            "active": 0.0,
            "afk": 0.0,
            "unknown": 0.0,
            "switches": 0,
            "first_activity": None,
            "last_activity": None,
        }

        for key in day_keys:
            day = self._state["days"].get(key)
            if not day:
                continue

            totals["afk"] += day.get("afk_seconds", 0.0)
            totals["unknown"] += day.get("unknown_seconds", 0.0)

            first = day.get("first_activity")
            last = day.get("last_activity")
            if first and (totals["first_activity"] is None or first < totals["first_activity"]):
                totals["first_activity"] = first
            if last and (totals["last_activity"] is None or last > totals["last_activity"]):
                totals["last_activity"] = last

            for exe, info in day.get("apps", {}).items():
                agg = apps.setdefault(
                    exe, {"active": 0.0, "switches": 0, "last_used": 0.0, "titles": {}}
                )
                agg["active"] += info.get("active_seconds", 0.0)
                agg["switches"] += info.get("switches", 0)
                agg["last_used"] = max(agg["last_used"], info.get("last_used", 0.0))
                for title, seconds in info.get("titles", {}).items():
                    agg["titles"][title] = agg["titles"].get(title, 0.0) + seconds

        ranked = sorted(apps.items(), key=lambda kv: kv[1]["active"], reverse=True)
        totals["active"] = sum(info["active"] for _, info in ranked)
        totals["switches"] = sum(info["switches"] for _, info in ranked)
        return {"apps": ranked, "totals": totals}

    def build_topapps_text(self, days: int = 1) -> str:
        """Ranking of applications by active time for the last N days."""
        if not self.enabled:
            return "⏱ Учёт активности отключён."

        agg = self._aggregate(self._recent_day_keys(days))
        period = "сегодня" if days == 1 else f"за {days} дн."
        lines = [f"🏆 <b>Топ приложений — {escape(period)}</b>", ""]

        if not agg["apps"]:
            lines.append("Пока нет данных. Учёт работает в фоне, зайди позже.")
            return _fit_message("\n".join(lines))

        for index, (exe, info) in enumerate(agg["apps"][:12], 1):
            lines.append(
                f"{index}. <b>{escape(exe)}</b> — {format_duration_short(info['active'])}"
                f" · {info['switches']} перекл."
            )
            top_titles = sorted(info["titles"].items(), key=lambda kv: kv[1], reverse=True)[:3]
            for title, seconds in top_titles:
                lines.append(f"   └ {escape(_shorten(title, 60))} — {format_duration_short(seconds)}")

        totals = agg["totals"]
        lines.append("")
        lines.append(f"📈 Активно всего: <b>{format_duration_short(totals['active'])}</b>")
        lines.append(f"💤 АФК: {format_duration_short(totals['afk'])}")
        if totals["unknown"]:
            lines.append(f"❔ Не определено: {format_duration_short(totals['unknown'])}")
        return _fit_message("\n".join(lines))

    def build_activity_text(self, date_key: str | None = None) -> str:
        """Detailed report for one day."""
        if not self.enabled:
            return "⏱ Учёт активности отключён."

        key = date_key or self._today_key()
        day = self._state["days"].get(key)
        lines = [f"📅 <b>Отчёт за {escape(key)}</b>", ""]

        if not day:
            lines.append("Нет данных за этот день.")
            return _fit_message("\n".join(lines))

        apps = sorted(
            day.get("apps", {}).items(),
            key=lambda kv: kv[1].get("active_seconds", 0.0),
            reverse=True,
        )
        active_total = sum(info.get("active_seconds", 0.0) for _, info in apps)

        lines.append(f"⏱ Активно: <b>{format_duration_short(active_total)}</b>")
        lines.append(f"💤 АФК: {format_duration_short(day.get('afk_seconds', 0.0))}")
        if day.get("unknown_seconds"):
            lines.append(f"❔ Не определено: {format_duration_short(day['unknown_seconds'])}")
        if day.get("first_activity"):
            first_text = time.strftime("%H:%M", time.localtime(day["first_activity"]))
            last_text = "—"
            if day.get("last_activity"):
                last_text = time.strftime("%H:%M", time.localtime(day["last_activity"]))
            lines.append(f"🕒 Активность: с {first_text} до {last_text}")

        lines.append("")
        if not apps:
            lines.append("Приложения не зафиксированы.")
        else:
            lines.append("<b>Приложения:</b>")
            for index, (exe, info) in enumerate(apps[:12], 1):
                lines.append(
                    f"{index}. <b>{escape(exe)}</b> — {format_duration_short(info.get('active_seconds', 0.0))}"
                    f" · {info.get('switches', 0)} перекл."
                )
                if self.track_titles and info.get("titles"):
                    top_titles = sorted(
                        info["titles"].items(), key=lambda kv: kv[1], reverse=True
                    )[:3]
                    for title, seconds in top_titles:
                        lines.append(
                            f"   └ {escape(_shorten(title, 64))} — {format_duration_short(seconds)}"
                        )
        return _fit_message("\n".join(lines))

    async def build_apps_text(self) -> str:
        """'What is open right now' report."""
        if not self.enabled:
            return "⏱ Учёт активности отключён."

        snap = await self.snapshot_now()
        if not snap.get("available"):
            return "❌ Срез активности недоступен (нужна Windows)."

        lines = ["⏱ <b>Активность — сейчас</b>", ""]

        if snap["exe"]:
            lines.append(f"🖥 Активное окно: <b>{escape(snap['exe'])}</b>")
            if snap.get("title"):
                lines.append(f"📄 Заголовок: «{escape(snap['title'])}»")
            if snap.get("session_since"):
                started = time.strftime("%H:%M", time.localtime(snap["session_since"]))
                lines.append(
                    f"⏳ В этой сессии: {format_duration_short(snap.get('session_seconds', 0.0))}"
                    f" (с {started})"
                )
        else:
            lines.append("🖥 Активное окно: не определено (рабочий стол / экран блокировки)")

        if snap["afk"]:
            idle_text = (
                f"🌙 АФК {format_duration_short(snap['idle'])}"
                f" (порог {format_duration_short(self.afk_threshold_seconds)})"
            )
        else:
            idle_text = f"✅ активен (простой {format_duration_short(snap['idle'])})"
        lines.append(f"💤 Простой ввода: {idle_text}")

        today = snap.get("today", {})
        lines.append("")
        lines.append("📊 <b>Сегодня:</b>")
        lines.append(f"• Активно: {format_duration_short(today.get('active', 0.0))}")
        lines.append(f"• АФК: {format_duration_short(today.get('afk', 0.0))}")
        if today.get("unknown"):
            lines.append(f"• Не определено: {format_duration_short(today['unknown'])}")
        if today.get("first_activity"):
            started = time.strftime("%H:%M", time.localtime(today["first_activity"]))
            lines.append(f"• Учёт с {started}")

        visible = snap.get("visible") or []
        if visible:
            lines.append("")
            lines.append("📂 <b>Открытые окна:</b>")
            for item in visible[:12]:
                preview = ", ".join(f"«{escape(t)}»" for t in item["titles"][:2])
                extra = f" и ещё {item['count'] - 2}" if item["count"] > 2 else ""
                lines.append(f"• {escape(item['exe'])} ×{item['count']}: {preview}{extra}")

        return _fit_message("\n".join(lines))

    # ------------------------------------------------------------------
    # Журнал сессий: что и когда было открыто
    # ------------------------------------------------------------------

    def _close_session(self, at_ts: float) -> None:
        """Закрывает открытую сессию (фокус сменился, АФК, остановка)."""
        open_ses = self._state.get("open")
        if not open_ses:
            return
        start = float(open_ses.get("start", 0.0) or 0.0)
        duration = at_ts - start
        if self._min_session_seconds <= duration < 24 * 3600:
            self._state.setdefault("log", []).append(
                {
                    "app": str(open_ses.get("app") or "unknown"),
                    "title": str(open_ses.get("title") or ""),
                    "start": start,
                    "dur": round(duration, 1),
                }
            )
        self._state["open"] = None

    async def close_current_session(self) -> None:
        """Корректно закрывает текущую сессию (вызывается при остановке бота)."""
        self._close_session(time.time())

    def stored_day_keys(self) -> list[str]:
        """Дни, по которым есть данные (сегодня добавляется всегда)."""
        keys = [
            key
            for key, day in self._state["days"].items()
            if day.get("apps") or day.get("afk_seconds") or day.get("unknown_seconds")
        ]
        today = self._today_key()
        if today not in keys:
            keys.append(today)
        return sorted(keys)

    def _sessions_for_window(
        self, start_ts: float, end_ts: float, now: float
    ) -> list[dict[str, Any]]:
        """Сессии, пересекающиеся с окном, обрезанные по его границам."""
        raw: list[tuple[float, float, str, str, bool]] = []
        for rec in self._state.get("log", []):
            start = float(rec.get("start", 0.0) or 0.0)
            end = start + float(rec.get("dur", 0.0) or 0.0)
            if end <= start_ts or start >= end_ts:
                continue
            raw.append(
                (start, end, str(rec.get("app", "")), str(rec.get("title", "")), False)
            )

        open_ses = self._state.get("open")
        if open_ses:
            start = float(open_ses.get("start", 0.0) or 0.0)
            end = min(now, end_ts)
            if start < end_ts and end > start_ts:
                raw.append(
                    (
                        start,
                        end,
                        str(open_ses.get("app", "")),
                        str(open_ses.get("title", "")),
                        now < end_ts,
                    )
                )

        raw.sort(key=lambda item: item[0])
        rows: list[dict[str, Any]] = []
        for start, end, app, title, ongoing in raw:
            clipped_start = max(start, start_ts)
            clipped_end = min(end, end_ts)
            if clipped_end - clipped_start < 1.0:
                continue
            rows.append(
                {
                    "start": clipped_start,
                    "end": clipped_end,
                    "app": app,
                    "title": title,
                    "ongoing": ongoing,
                }
            )
        return rows

    def _build_log_blocks(
        self, day_keys: list[str], period: str | None, html: bool, now: float
    ) -> list[str]:
        """Блоки журнала по периодам суток для перечисленных дней."""
        blocks: list[str] = []
        for day_key in day_keys:
            for name in ([period] if period else PERIOD_ORDER):
                start_ts, end_ts = period_window(day_key, name)
                if start_ts > now and not period:
                    continue  # период ещё не наступил — в общем журнале не показываем

                lines = [period_header(day_key, name, html=html), ""]
                rows = self._sessions_for_window(start_ts, end_ts, now)
                if not rows:
                    if start_ts > now:
                        lines.append("— период ещё не наступил.")
                    else:
                        lines.append("— нет зафиксированной активности.")
                else:
                    last_end = start_ts
                    for row in rows:
                        gap = row["start"] - last_end
                        if gap >= self._log_gap_threshold:
                            lines.append(
                                f"{_hm(last_end)}–{_hm(row['start'])} — 💤 нет активности"
                                f" ({format_duration_short(gap)})"
                            )
                        app_text = escape(row["app"]) if html else row["app"]
                        title_disp = _display_title(row["title"])
                        if title_disp:
                            title_disp = _shorten(title_disp, 64)
                            if html:
                                title_disp = escape(title_disp)
                            title_part = f" — «{title_disp}»"
                        else:
                            title_part = ""
                        suffix = " ⏳ ещё открыто" if row["ongoing"] else ""
                        lines.append(
                            f"{_hm(row['start'])}–{_hm(row['end'])} — {app_text}"
                            f"{title_part}"
                            f" ({format_duration_short(row['end'] - row['start'])})"
                            f"{suffix}"
                        )
                        last_end = row["end"]

                    tail_end = min(end_ts, now)
                    if tail_end - last_end >= self._log_gap_threshold:
                        lines.append(
                            f"{_hm(last_end)}–{_hm(tail_end)} — 💤 нет активности"
                            f" ({format_duration_short(tail_end - last_end)})"
                        )
                blocks.append("\n".join(lines))
        return blocks

    def build_log_text(self, date_key: str | None = None, period: str | None = None) -> str:
        """Журнал за один день по периодам суток (HTML для Telegram)."""
        if not self.enabled:
            return "⏱ Учёт активности отключён."

        key = date_key or self._today_key()
        blocks = self._build_log_blocks([key], period, html=True, now=time.time())
        if not blocks:
            return "📜 За этот день данных пока нет."
        return "\n\n".join(blocks)

    def build_log_plain(self, day_keys: list[str] | None = None) -> str:
        """Полный журнал в plain-text (для выгрузки файлом)."""
        keys = day_keys or self.stored_day_keys()
        blocks = self._build_log_blocks(keys, None, html=False, now=time.time())
        if not blocks:
            return "Журнал пуст."
        return "📜 ЖУРНАЛ АКТИВНОСТИ — полный, по периодам суток\n\n" + "\n\n".join(blocks)

    def build_window_summary_text(self, start_ts: float, end_ts: float, label: str) -> str:
        """Сводка по произвольному интервалу времени (для /activity с выборкой)."""
        if not self.enabled:
            return "⏱ Учёт активности отключён."

        rows = self._sessions_for_window(start_ts, end_ts, time.time())
        lines = [f"⏱ <b>Активность — {escape(label)}</b>", ""]

        if not rows:
            lines.append("В этом интервале активность не зафиксирована.")
            return _fit_message("\n".join(lines))

        per_app: dict[str, dict[str, Any]] = {}
        for row in rows:
            info = per_app.setdefault(row["app"], {"seconds": 0.0, "titles": {}})
            spent = row["end"] - row["start"]
            info["seconds"] += spent
            if row["title"]:
                info["titles"][row["title"]] = info["titles"].get(row["title"], 0.0) + spent

        total = sum(info["seconds"] for info in per_app.values())
        ranked = sorted(per_app.items(), key=lambda kv: kv[1]["seconds"], reverse=True)
        lines.append(f"⏱ Активно в интервале: <b>{format_duration_short(total)}</b>")
        lines.append(f"🖥 Приложений: {len(ranked)}")
        lines.append("")
        for index, (app, info) in enumerate(ranked[:12], 1):
            lines.append(
                f"{index}. <b>{escape(app)}</b> — {format_duration_short(info['seconds'])}"
            )
            top_titles = sorted(info["titles"].items(), key=lambda kv: kv[1], reverse=True)[:3]
            for title, seconds in top_titles:
                lines.append(
                    f"   └ {escape(_shorten(_display_title(title), 60))}"
                    f" — {format_duration_short(seconds)}"
                )
        return _fit_message("\n".join(lines))

    # ------------------------------------------------------------------
    # Daily summary bookkeeping
    # ------------------------------------------------------------------

    def is_summary_sent(self, day_key: str) -> bool:
        return self._state.get("last_summary_date") == day_key

    def mark_summary_sent(self, day_key: str) -> None:
        self._state["last_summary_date"] = day_key

    def build_sites_text(self, days: int = 1) -> str:
        """Отчёт по посещённым сайтам за последние N дней."""
        if not self.browser_sites:
            return "🌐 Учёт сайтов отключён (BROWSER_SITES_ENABLED=0)."

        sites: dict[str, dict[str, Any]] = {}
        for key in self._recent_day_keys(days):
            day = self._state["days"].get(key) or {}
            for domain, info in day.get("sites", {}).items():
                entry = sites.setdefault(
                    domain,
                    {"visits": 0, "last": 0.0, "first": 0.0, "titles": {}},
                )
                entry["visits"] += int(info.get("visits", 0))
                entry["last"] = max(entry["last"], float(info.get("last", 0.0)))
                first = float(info.get("first", 0.0))
                if first and (not entry["first"] or first < entry["first"]):
                    entry["first"] = first
                for title, count in info.get("titles", {}).items():
                    entry["titles"][title] = entry["titles"].get(title, 0) + int(count)

        period = "сегодня" if days == 1 else f"за {days} дн."
        lines = [f"🌐 <b>Сайты — {escape(period)}</b>", ""]
        if not sites:
            lines.append("Пока нет данных: история браузеров читается раз в минуту.")
            return _fit_message("\n".join(lines))

        ranked = sorted(sites.items(), key=lambda kv: kv[1]["visits"], reverse=True)
        for index, (domain, info) in enumerate(ranked[:15], 1):
            last_text = ""
            if info["last"]:
                last_text = " · последний " + time.strftime("%H:%M", time.localtime(info["last"]))
            lines.append(
                f"{index}. <b>{escape(domain)}</b> — {info['visits']} визит(ов){last_text}"
            )
            top_titles = sorted(info["titles"].items(), key=lambda kv: kv[1], reverse=True)[:2]
            for title, count in top_titles:
                lines.append(f"   └ {escape(_shorten(title, 70))} ×{count}")

        total_visits = sum(info["visits"] for info in sites.values())
        lines.append("")
        lines.append(f"Всего визитов: <b>{total_visits}</b>, сайтов: <b>{len(sites)}</b>")
        return _fit_message("\n".join(lines))

    def build_daily_summary_text(self) -> str:
        text = self.build_topapps_text(days=1)
        if self.browser_sites:
            text += "\n\n" + self.build_sites_text(days=1)
        return _fit_message(text)
