"""Сбор посещённых сайтов из локальной истории браузеров.

Поддерживаются Chromium-браузеры (Chrome, Edge, Яндекс.Браузер, Brave, Opera)
и Firefox. Модуль читает только историю посещений: URL, заголовок страницы и
время. Сами страницы, пароли и содержимое вкладок не читаются.

Данные забираются инкрементально: в state хранится время последнего визита по
каждому профилю, поэтому повторно одни и те же записи не учитываются.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

# Разница между эпохами Windows (1601) и Unix (1970), секунды.
_WINDOWS_EPOCH_OFFSET = 11644473600

# Сколько истории брать при самом первом запуске (секунды назад).
_FIRST_RUN_LOOKBACK = 30 * 60

# Максимум записей с одного профиля за один проход.
_MAX_ROWS = 500

_SKIP_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _local_appdata() -> Path | None:
    raw = os.environ.get("LOCALAPPDATA")
    return Path(raw) if raw else None


def _appdata() -> Path | None:
    raw = os.environ.get("APPDATA")
    return Path(raw) if raw else None


def _chromium_roots() -> list[tuple[str, Path]]:
    """Пары (имя браузера, папка User Data)."""
    roots: list[tuple[str, Path]] = []
    local = _local_appdata()
    roaming = _appdata()
    if local:
        roots.extend(
            [
                ("chrome", local / "Google" / "Chrome" / "User Data"),
                ("edge", local / "Microsoft" / "Edge" / "User Data"),
                ("yandex", local / "Yandex" / "YandexBrowser" / "User Data"),
                ("brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ]
        )
    if roaming:
        roots.append(("opera", roaming / "Opera Software" / "Opera Stable"))
    return roots


def _chromium_databases() -> Iterable[tuple[str, Path]]:
    """Все файлы History у Chromium-браузеров."""
    for browser, root in _chromium_roots():
        if not root.exists():
            continue
        candidates: list[Path] = []
        # Opera и некоторые сборки держат History прямо в корне.
        if (root / "History").exists():
            candidates.append(root)
        # Остальные — в профилях Default / Profile 1 / Profile 2 ...
        try:
            for child in root.iterdir():
                if child.is_dir() and (child / "History").exists():
                    candidates.append(child)
        except OSError:
            continue
        for profile_dir in candidates:
            key = f"{browser}:{profile_dir.name}"
            yield key, profile_dir / "History"


def _firefox_databases() -> Iterable[tuple[str, Path]]:
    roaming = _appdata()
    if not roaming:
        return
    profiles_root = roaming / "Mozilla" / "Firefox" / "Profiles"
    if not profiles_root.exists():
        return
    try:
        for profile_dir in profiles_root.iterdir():
            db = profile_dir / "places.sqlite"
            if db.exists():
                yield f"firefox:{profile_dir.name}", db
    except OSError:
        return


def _copy_database(source: Path) -> Path | None:
    """Копирует базу во временную папку, чтобы не мешать работающему браузеру."""
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="controlbot-hist-"))
        target = temp_dir / source.name
        shutil.copy2(source, target)
        for suffix in ("-wal", "-shm"):
            sidecar = source.with_name(source.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, temp_dir / (source.name + suffix))
        return target
    except OSError:
        return None


def _cleanup(path: Path) -> None:
    try:
        shutil.rmtree(path.parent, ignore_errors=True)
    except OSError:
        pass


def _domain_from_url(url: str) -> str | None:
    if not url:
        return None
    lowered = url.strip().lower()
    if not lowered.startswith(("http://", "https://")):
        return None
    try:
        host = (urlparse(lowered).hostname or "").strip(".")
    except ValueError:
        return None
    if not host or host in _SKIP_HOSTS:
        return None
    if host.endswith((".local", ".lan", ".internal")):
        return None
    if host.startswith("www."):
        host = host[4:]
    return host


def _read_chromium(db_path: Path, since_unix: float) -> list[dict[str, Any]]:
    since_chrome = int((since_unix + _WINDOWS_EPOCH_OFFSET) * 1_000_000)
    rows: list[dict[str, Any]] = []
    copied = _copy_database(db_path)
    if copied is None:
        return rows
    try:
        connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        try:
            cursor = connection.execute(
                "SELECT urls.url, urls.title, visits.visit_time "
                "FROM visits JOIN urls ON urls.id = visits.url "
                "WHERE visits.visit_time > ? "
                "ORDER BY visits.visit_time ASC LIMIT ?",
                (since_chrome, _MAX_ROWS),
            )
            for url, title, visit_time in cursor:
                unix_ts = float(visit_time) / 1_000_000 - _WINDOWS_EPOCH_OFFSET
                rows.append({"url": url or "", "title": title or "", "ts": unix_ts})
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        return []
    finally:
        _cleanup(copied)
    return rows


def _read_firefox(db_path: Path, since_unix: float) -> list[dict[str, Any]]:
    since_firefox = int(since_unix * 1_000_000)
    rows: list[dict[str, Any]] = []
    copied = _copy_database(db_path)
    if copied is None:
        return rows
    try:
        connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        try:
            cursor = connection.execute(
                "SELECT p.url, p.title, v.visit_date "
                "FROM moz_historyvisits v JOIN moz_places p ON p.id = v.place_id "
                "WHERE v.visit_date > ? "
                "ORDER BY v.visit_date ASC LIMIT ?",
                (since_firefox, _MAX_ROWS),
            )
            for url, title, visit_date in cursor:
                rows.append(
                    {
                        "url": url or "",
                        "title": title or "",
                        "ts": float(visit_date) / 1_000_000,
                    }
                )
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        return []
    finally:
        _cleanup(copied)
    return rows


def collect_visits(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Возвращает новые визиты и обновляет state (ключ профиля → last ts).

    state — обычный dict, который вызывающая сторона хранит в своём JSON.
    """
    now = time.time()
    visits: list[dict[str, Any]] = []

    databases: list[tuple[str, Path, str]] = [
        (key, path, "chromium") for key, path in _chromium_databases()
    ]
    databases.extend((key, path, "firefox") for key, path in _firefox_databases())

    for key, db_path, kind in databases:
        raw_last = state.get(key)
        since = float(raw_last) if isinstance(raw_last, (int, float)) else now - _FIRST_RUN_LOOKBACK

        try:
            if kind == "firefox":
                rows = _read_firefox(db_path, since)
            else:
                rows = _read_chromium(db_path, since)
        except Exception:
            rows = []

        if not rows:
            # Даже если новых записей нет, сдвигаем отметку, чтобы не читать
            # один и тот же хвост истории каждый раз.
            state[key] = now
            continue

        browser = key.split(":", 1)[0]
        max_ts = since
        for row in rows:
            domain = _domain_from_url(row["url"])
            max_ts = max(max_ts, row["ts"])
            if not domain:
                continue
            visits.append(
                {
                    "browser": browser,
                    "domain": domain,
                    "url": row["url"],
                    "title": (row["title"] or "").strip(),
                    "ts": row["ts"],
                }
            )
        state[key] = max_ts

    # Убираем возможные дубли (один и тот же визит из разных профилей).
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for visit in visits:
        signature = (visit["url"], int(visit["ts"]))
        unique.setdefault(signature, visit)

    result = sorted(unique.values(), key=lambda item: item["ts"])
    for item in result:
        item["time"] = datetime.fromtimestamp(item["ts"]).strftime("%H:%M")
    return result
