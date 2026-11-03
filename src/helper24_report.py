#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""helper24_report.py — отчёт ControlBot в контур Helper24.

Раз в 12 часов (08:00 и 21:00) бот собирает компактный срез: что делал ПК,
сколько было активно/АФК, состояние системы, ошибки из bot.log — и отправляет
его зашифрованной пачкой (Fernet) в секретный ntfy-топик. На сервере
`ntfy_poller.py` кладёт событие kind="digest" в SQLite, Helper24 его читает,
структурирует и присылает владельцу готовый отчёт.

Зависимость: cryptography (pip install cryptography).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

LOGGER = logging.getLogger("controlbot.helper24")

# Секреты вшиваются только в сборку (GitHub Actions), в репозитории их нет.
try:
    from build_secrets import SECRETS as _BUILD_SECRETS  # type: ignore
except Exception:  # noqa: BLE001
    _BUILD_SECRETS = {}


DEFAULT_NTFY_URL = ""
DEFAULT_FERNET_KEY = ""


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    return str(_BUILD_SECRETS.get(name, default) or "").strip()


def enabled() -> bool:
    """Отчёты включены, если есть ключ шифрования и cryptography."""
    return bool(_env("HELPER24_FERNET_KEY", DEFAULT_FERNET_KEY)) and Fernet is not None


def device_id(pc_name: str) -> str:
    return _env("HELPER24_DEVICE_ID") or "pc-" + "".join(
        ch.lower() if ch.isalnum() else "-" for ch in (pc_name or socket.gethostname())
    ).strip("-")


# ---------------------------------------------------------------------------
# Сбор среза
# ---------------------------------------------------------------------------

def _activity(path: Path, since_ts: float) -> dict[str, Any]:
    if not path.exists():
        return {"available": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False}

    per_app: dict[str, float] = {}
    titles: dict[str, float] = {}
    switches = 0

    for record in state.get("log") or []:
        try:
            start = float(record.get("start") or 0)
            duration = float(record.get("dur") or 0)
        except (TypeError, ValueError):
            continue
        if start < since_ts or duration < 5:
            continue
        app = str(record.get("app") or "unknown")
        per_app[app] = per_app.get(app, 0.0) + duration
        switches += 1
        title = str(record.get("title") or "").strip()
        if title:
            titles[title] = titles.get(title, 0.0) + duration

    afk = 0.0
    for day_key, day in (state.get("days") or {}).items():
        try:
            day_start = datetime.fromisoformat(day_key).timestamp()
        except ValueError:
            continue
        if day_start + 86400 < since_ts:
            continue
        afk += float(day.get("afk_seconds") or 0)

    top_apps = sorted(per_app.items(), key=lambda kv: kv[1], reverse=True)[:8]
    top_titles = sorted(titles.items(), key=lambda kv: kv[1], reverse=True)[:5]

    return {
        "available": True,
        "active_minutes": round(sum(per_app.values()) / 60),
        "afk_minutes": round(afk / 60),
        "switches": switches,
        "apps": [{"app": a, "minutes": round(s / 60)} for a, s in top_apps],
        "titles": [{"title": t[:70], "minutes": round(s / 60)} for t, s in top_titles],
    }


def _system() -> dict[str, Any]:
    if psutil is None:
        return {"available": False}
    data: dict[str, Any] = {"available": True}
    try:
        data["cpu"] = round(psutil.cpu_percent(interval=1), 1)
        memory = psutil.virtual_memory()
        data["ram"] = round(memory.percent, 1)
        data["ram_gb"] = f"{memory.used / 1024**3:.1f}/{memory.total / 1024**3:.1f}"
        data["disk"] = round(psutil.disk_usage("C:\\").percent, 1) if os.name == "nt" else round(
            psutil.disk_usage("/").percent, 1
        )
        data["uptime_hours"] = round((time.time() - psutil.boot_time()) / 3600, 1)
        if hasattr(psutil, "sensors_battery"):
            battery = psutil.sensors_battery()
            if battery:
                data["battery"] = round(battery.percent, 1)
                data["power"] = "сеть" if battery.power_plugged else "батарея"
    except Exception as exc:  # noqa: BLE001
        data["error"] = str(exc)
    return data


def _log_tail(path: Path, limit: int = 6) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return []
    problems = [line.strip() for line in lines if " ERROR " in line or " WARNING " in line]
    return [line[:160] for line in problems[-limit:]]


def build_digest(base_dir: Path, pc_name: str, club: str, version: str, since_ts: float) -> dict[str, Any]:
    return {
        "kind": "controlbot_digest",
        "version": version,
        "pc": pc_name or socket.gethostname(),
        "club": club,
        "host": socket.gethostname(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period_since": datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M"),
        "activity": _activity(base_dir / "activity_data.json", since_ts),
        "system": _system(),
        "problems": _log_tail(base_dir / "bot.log"),
    }


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def _summary(digest: dict[str, Any]) -> str:
    activity = digest.get("activity") or {}
    if not activity.get("available"):
        return f"ControlBot {digest['pc']}: нет данных активности"
    top = ", ".join(f"{a['app']} {a['minutes']}м" for a in activity.get("apps", [])[:4])
    return (
        f"ControlBot {digest['pc']}: активно {activity.get('active_minutes', 0)} мин, "
        f"АФК {activity.get('afk_minutes', 0)} мин. {top}"
    )


def send_digest(base_dir: Path, pc_name: str, club: str, version: str, since_ts: float) -> bool:
    """Собрать и отправить отчёт. Возвращает True при успехе."""
    if not enabled():
        LOGGER.info("Отчёты Helper24 отключены (нет cryptography или ключа).")
        return False

    digest = build_digest(base_dir, pc_name, club, version, since_ts)
    payload = {
        "device_id": device_id(pc_name),
        "club": club,
        "pc": pc_name or socket.gethostname(),
        "events": [
            {
                "uid": hashlib.sha1(
                    f"digest|{digest['pc']}|{int(time.time())}".encode("utf-8")
                ).hexdigest()[:24],
                "ts": time.time(),
                "kind": "digest",
                "app": "controlbot",
                "title": _summary(digest)[:180],
                "url": "",
                "domain": "",
                "duration": 0,
                "meta": json.dumps(digest, ensure_ascii=False),
            }
        ],
    }

    body = Fernet(_env("HELPER24_FERNET_KEY", DEFAULT_FERNET_KEY).encode("ascii")).encrypt(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    request = urllib.request.Request(
        _env("HELPER24_NTFY_URL", DEFAULT_NTFY_URL),
        data=body,
        method="POST",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Не удалось отправить отчёт Helper24: %s", exc)
        return False

    LOGGER.info("Отчёт Helper24 отправлен: %s", digest["pc"])
    return True
