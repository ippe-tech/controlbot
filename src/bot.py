import asyncio
import atexit
import ctypes
import getpass
import hashlib
import json
import logging
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import time as dt_time
from html import escape
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from PIL import Image, ImageGrab
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Try to import optional dependencies
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

from activity_tracker import ActivityTracker
import access_store

try:
    import helper24_report
except ImportError:  # модуля нет рядом — отчёты просто отключатся
    helper24_report = None  # type: ignore[assignment]

APP_VERSION = "2026.11.13"
UPDATE_MANIFEST_URL = os.getenv(
    "UPDATE_MANIFEST_URL", "https://gym24server.online/controlbot/latest.json"
)
UPDATE_SOURCE = os.getenv("UPDATE_SOURCE", "github").strip().lower()
UPDATE_REPO = os.getenv("UPDATE_REPO", "ippe-tech/controlbot").strip()
UPDATE_TOKEN = os.getenv("UPDATE_TOKEN", "").strip()


def app_dir() -> Path:
    """Папка программы: рядом с .exe (PyInstaller) или с bot.py."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()


def setup_logging() -> None:
    """Логи в консоль и в bot.log с ротацией (5 МБ × 5 копий)."""
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:  # в оконной сборке exe консоли нет
        handlers.append(logging.StreamHandler())
    try:
        handlers.append(
            RotatingFileHandler(
                APP_DIR / "bot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )
    except OSError:
        pass

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


setup_logging()
LOGGER = logging.getLogger("controlbot")

# =============================================================================
# CONSTANTS
# =============================================================================

# Button labels
BUTTON_BRIGHTNESS_DOWN = "🔅 Яркость -"
BUTTON_BRIGHTNESS_UP = "🔆 Яркость +"
BUTTON_VOLUME_DOWN = "🔉 Громкость -"
BUTTON_VOLUME_UP = "🔊 Громкость +"
BUTTON_MUTE_SOUND = "🔇 Без звука"
BUTTON_SCREENSHOT = "📷 Скриншот"
BUTTON_WEBCAM = "📹 Веб-камера"
BUTTON_STATUS = "📊 Статус"
BUTTON_PROCESSES = "⚙️ Процессы"
BUTTON_LOCK = "🔒 Заблокировать ПК"
BUTTON_MEDIA_PLAY = "⏯️ Пуск/Пауза"
BUTTON_MEDIA_NEXT = "⏭️ Следующий"
BUTTON_MEDIA_PREV = "⏮️ Предыдущий"
BUTTON_RESTART = "🔄 Перезагрузить ПК"
BUTTON_SHUTDOWN = "⏻ Выключить ПК"
BUTTON_ACTIVITY = "⏱ Активность"

# Callback data
CALLBACK_CONFIRM_REBOOT = "confirm_reboot"
CALLBACK_CONFIRM_SHUTDOWN = "confirm_shutdown"
CALLBACK_CANCEL = "cancel_action"
CALLBACK_CANCEL_TIMER = "cancel_timer"

# Windows constants
FULL_MUTE_STEPS = 50
KEYEVENTF_KEYUP = 0x0002
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1

# Keyboard layouts
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BUTTON_STATUS), KeyboardButton(BUTTON_SCREENSHOT)],
        [KeyboardButton(BUTTON_VOLUME_DOWN), KeyboardButton(BUTTON_VOLUME_UP)],
        [KeyboardButton(BUTTON_MUTE_SOUND), KeyboardButton(BUTTON_WEBCAM)],
        [KeyboardButton(BUTTON_MEDIA_PREV), KeyboardButton(BUTTON_MEDIA_PLAY), KeyboardButton(BUTTON_MEDIA_NEXT)],
        [KeyboardButton(BUTTON_LOCK), KeyboardButton(BUTTON_PROCESSES)],
        [KeyboardButton(BUTTON_ACTIVITY)],
        [KeyboardButton(BUTTON_RESTART), KeyboardButton(BUTTON_SHUTDOWN)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ScheduledTask:
    """Represents a scheduled task."""
    task_type: str  # "shutdown", "reboot", "alarm", "message"
    execute_at: float  # Unix timestamp
    created_at: float
    message: str = ""
    chat_id: int = 0

@dataclass
class BotConfig:
    token: str
    allowed_chat_id: int | None
    brightness_step_percent: int
    shutdown_delay_seconds: int
    volume_down_steps: int
    low_battery_threshold: int = 20
    high_disk_threshold: int = 90
    high_cpu_threshold: int = 90
    high_ram_threshold: int = 90
    monitoring_interval: int = 60
    activity_enabled: bool = True
    activity_afk_seconds: int = 300
    activity_track_titles: bool = True
    activity_retention_days: int = 30
    activity_summary_enabled: bool = True
    activity_summary_hour: int = 21
    allowed_chat_ids: tuple[int, ...] = ()
    club_name: str = ""
    pc_name: str = ""
    allow_webcam: bool = False
    browser_sites_enabled: bool = True
    browser_sites_interval: int = 60


# =============================================================================
# CONFIG LOADING
# =============================================================================

def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def load_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default)).strip() or str(default)

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом.") from exc

    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} должен быть в диапазоне от {minimum} до {maximum}.")

    return value


def load_config() -> BotConfig:
    load_env_file(APP_DIR / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN. Укажите его в .env.")

    allowed_raw = (
        os.getenv("ALLOWED_CHAT_IDS", "").strip()
        or os.getenv("ALLOWED_CHAT_ID", "").strip()
    )
    allowed_chat_ids: list[int] = []
    for chunk in re.split(r"[,\s;]+", allowed_raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            allowed_chat_ids.append(int(chunk))
        except ValueError:
            raise RuntimeError(
                f"ALLOWED_CHAT_IDS должен содержать числовые chat_id, получено: {chunk!r}"
            )
    allowed_chat_id = allowed_chat_ids[0] if allowed_chat_ids else None

    return BotConfig(
        token=token,
        allowed_chat_id=allowed_chat_id,
        allowed_chat_ids=tuple(allowed_chat_ids),
        club_name=os.getenv("CLUB_NAME", "").strip(),
        pc_name=os.getenv("PC_NAME", "").strip(),
        allow_webcam=load_bool_env("ALLOW_WEBCAM", False),
        browser_sites_enabled=load_bool_env("BROWSER_SITES_ENABLED", True),
        browser_sites_interval=load_int_env("BROWSER_SITES_INTERVAL", 60, 30, 600),
        brightness_step_percent=load_int_env("BRIGHTNESS_STEP_PERCENT", 10, 1, 100),
        shutdown_delay_seconds=load_int_env("SHUTDOWN_DELAY_SECONDS", 30, 0, 600),
        volume_down_steps=load_int_env("VOLUME_DOWN_STEPS", 2, 1, 10),
        low_battery_threshold=load_int_env("LOW_BATTERY_THRESHOLD", 20, 5, 50),
        high_disk_threshold=load_int_env("HIGH_DISK_THRESHOLD", 90, 50, 99),
        high_cpu_threshold=load_int_env("HIGH_CPU_THRESHOLD", 90, 50, 100),
        high_ram_threshold=load_int_env("HIGH_RAM_THRESHOLD", 90, 50, 100),
        monitoring_interval=load_int_env("MONITORING_INTERVAL", 60, 30, 3600),
        activity_enabled=load_bool_env("ACTIVITY_ENABLED", True),
        activity_afk_seconds=load_int_env("ACTIVITY_AFK_SECONDS", 300, 60, 7200),
        activity_track_titles=load_bool_env("ACTIVITY_TRACK_TITLES", True),
        activity_retention_days=load_int_env("ACTIVITY_RETENTION_DAYS", 30, 1, 365),
        activity_summary_enabled=load_bool_env("ACTIVITY_SUMMARY_ENABLED", True),
        activity_summary_hour=load_int_env("ACTIVITY_SUMMARY_HOUR", 21, 0, 23),
    )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    return context.application.bot_data["config"]


def has_job_queue(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.application.job_queue is not None


async def ensure_job_queue_available(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if has_job_queue(context):
        return True

    await update.effective_message.reply_text(
        "Планирование недоступно. Установите python-telegram-bot[job-queue] и перезапустите бота."
    )
    return False


def get_scheduled_tasks(context: ContextTypes.DEFAULT_TYPE) -> list[ScheduledTask]:
    return context.application.bot_data.get("scheduled_tasks", [])


def add_scheduled_task(context: ContextTypes.DEFAULT_TYPE, task: ScheduledTask) -> None:
    tasks = get_scheduled_tasks(context)
    tasks.append(task)
    context.application.bot_data["scheduled_tasks"] = tasks


def remove_scheduled_task(context: ContextTypes.DEFAULT_TYPE, index: int) -> bool:
    tasks = get_scheduled_tasks(context)
    if 0 <= index < len(tasks):
        tasks.pop(index)
        context.application.bot_data["scheduled_tasks"] = tasks
        return True
    return False


def clear_scheduled_tasks(context: ContextTypes.DEFAULT_TYPE, task_type: str | None = None) -> int:
    tasks = get_scheduled_tasks(context)
    if task_type:
        new_tasks = [t for t in tasks if t.task_type != task_type]
        count = len(tasks) - len(new_tasks)
    else:
        new_tasks = []
        count = len(tasks)
    context.application.bot_data["scheduled_tasks"] = new_tasks
    return count


def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


def is_authorized(update: Update, config: BotConfig) -> bool:
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return False
    store = access_store.get_store()
    if store is not None and store.is_allowed(chat.id):
        return True
    if chat.id in config.allowed_chat_ids:
        return True
    return config.allowed_chat_id is not None and chat.id == config.allowed_chat_id


async def send_menu(message, text: str) -> None:
    await message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def send_setup_message(update: Update) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_text(
        "🤖 Бот запущен в режиме настройки.\n"
        f"📱 Ваш chat_id: <code>{chat.id}</code>\n"
        "⚙️ Укажите ALLOWED_CHAT_ID в .env и перезапустите бота.",
        parse_mode="HTML",
    )


async def send_unauthorized_message(update: Update) -> None:
    user = update.effective_user
    store = access_store.get_store()
    if user is None or store is None or store.is_allowed(user.id):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return

    if store.is_pending(user.id):
        await update.effective_message.reply_text(
            "⛔ Доступ запрещён.\n📨 Заявка уже отправлена владельцу — ожидайте решения."
        )
        return

    store.add_pending(user.id, user.username or "", user.full_name or "")
    await update.effective_message.reply_text(
        "⛔ Доступ запрещён.\n📨 Заявка отправлена владельцу — ожидайте решения."
    )

    owner = store.owner_id
    if not owner:
        return
    who = user.full_name or str(user.id)
    if user.username:
        who += f" (@{user.username})"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Выдать доступ", callback_data=f"access:ok:{user.id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"access:no:{user.id}"),
            ]
        ]
    )
    try:
        await update.get_bot().send_message(
            owner,
            f"🆕 <b>Запрос доступа к ControlBot</b>\n\n👤 {escape(who)}\n🆔 <code>{user.id}</code>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Не удалось отправить заявку владельцу: %s", exc)


async def access_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список тех, кому разрешён доступ."""
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    store = access_store.get_store()
    if store is None:
        await update.effective_message.reply_text("Хранилище доступов не инициализировано.")
        return

    lines = ["👥 <b>Доступ к боту</b>", ""]
    for user_id, info in store.allowed_items():
        mark = "👑" if store.is_owner(user_id) else "✅"
        name = info.get("name") or "без имени"
        uname = f" (@{info['username']})" if info.get("username") else ""
        lines.append(f"{mark} {escape(name)}{escape(uname)} — <code>{user_id}</code>")

    pending = store.pending_items()
    if pending:
        lines.append("")
        lines.append("🕐 <b>Заявки:</b>")
        for user_id, info in pending:
            name = info.get("name") or "без имени"
            lines.append(f"🆕 {escape(name)} — <code>{user_id}</code>")

    lines.append("")
    lines.append("Выдать: <code>/grant ID</code> · отозвать: <code>/revoke ID</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выдать доступ по ID: /grant 123456789."""
    config = get_config(context)
    user = update.effective_user
    store = access_store.get_store()
    if store is None or user is None or not store.is_owner(user.id):
        await update.effective_message.reply_text("⛔ Только владелец бота.")
        return

    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text("Использование: /grant 123456789")
        return

    target = int(args[0])
    if store.grant(target, by=user.id):
        await update.effective_message.reply_text(f"✅ Доступ выдан: {target}")
        try:
            await context.bot.send_message(target, "✅ Вам выдали доступ к ControlBot. Отправьте /start.")
        except Exception:  # noqa: BLE001
            pass
    else:
        await update.effective_message.reply_text(f"ℹ️ У {target} уже есть доступ.")


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отозвать доступ: /revoke 123456789."""
    user = update.effective_user
    store = access_store.get_store()
    if store is None or user is None or not store.is_owner(user.id):
        await update.effective_message.reply_text("⛔ Только владелец бота.")
        return

    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text("Использование: /revoke 123456789")
        return

    target = int(args[0])
    if store.revoke(target):
        await update.effective_message.reply_text(f"🚫 Доступ отозван: {target}")
    else:
        await update.effective_message.reply_text(
            f"❓ Нельзя отозвать: нет доступа или это владелец ({target})."
        )


async def access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки ✅/❌ в заявке владельцу."""
    query = update.callback_query
    user = update.effective_user
    store = access_store.get_store()
    if query is None or user is None or store is None or not store.is_owner(user.id):
        if query is not None:
            await query.answer("Только владелец бота.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await query.answer()
        return

    action, target = parts[1], int(parts[2])
    await query.answer()

    if action == "ok":
        store.grant(target, by=user.id)
        await query.edit_message_text(f"✅ Доступ выдан: {target}")
        try:
            await context.bot.send_message(
                target, "✅ Вам выдали доступ к ControlBot. Отправьте /start."
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        store.reject(target)
        await query.edit_message_text(f"❌ Заявка отклонена: {target}")


def build_power_action_markup(callback_data: str, button_text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(button_text, callback_data=callback_data)],
            [InlineKeyboardButton("❌ Отмена", callback_data=CALLBACK_CANCEL)],
        ]
    )


# =============================================================================
# SYSTEM FUNCTIONS
# =============================================================================

def run_powershell(command: str) -> tuple[bool, str]:
    """Execute PowerShell command and return (success, output)."""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.returncode == 0:
        return True, completed.stdout.strip()

    details = (completed.stderr or completed.stdout).strip()
    if not details:
        details = f"PowerShell завершился с кодом {completed.returncode}"
    return False, details


def run_command(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    """Execute command and return (success, output)."""
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if completed.returncode == 0:
            return True, completed.stdout.strip()
        return False, (completed.stderr or completed.stdout).strip() or f"Код выхода: {completed.returncode}"
    except subprocess.TimeoutExpired:
        return False, "Превышено время ожидания команды"
    except Exception as e:
        return False, str(e)


# =============================================================================
# MONITORING FUNCTIONS
# =============================================================================

def get_system_status() -> dict[str, Any]:
    """Get comprehensive system status."""
    status = {
        "cpu_percent": 0.0,
        "ram_percent": 0.0,
        "ram_used_gb": 0.0,
        "ram_total_gb": 0.0,
        "disk_percent": 0.0,
        "disk_used_gb": 0.0,
        "disk_total_gb": 0.0,
        "uptime_hours": 0.0,
        "boot_time": "",
        "battery": None,
        "network": {"sent_mb": 0, "recv_mb": 0},
        "processes": 0,
    }

    if not HAS_PSUTIL:
        return status

    try:
        # CPU
        status["cpu_percent"] = psutil.cpu_percent(interval=1)
        
        # RAM
        ram = psutil.virtual_memory()
        status["ram_percent"] = ram.percent
        status["ram_used_gb"] = round(ram.used / (1024**3), 1)
        status["ram_total_gb"] = round(ram.total / (1024**3), 1)
        
        # Disk
        disk = psutil.disk_usage("C:\\")
        status["disk_percent"] = disk.percent
        status["disk_used_gb"] = round(disk.used / (1024**3), 1)
        status["disk_total_gb"] = round(disk.total / (1024**3), 1)
        
        # Uptime
        boot_time = psutil.boot_time()
        uptime_seconds = time.time() - boot_time
        status["uptime_hours"] = round(uptime_seconds / 3600, 1)
        status["boot_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(boot_time))
        
        # Battery
        if hasattr(psutil, "sensors_battery"):
            battery = psutil.sensors_battery()
            if battery:
                status["battery"] = {
                    "percent": battery.percent,
                    "power_plugged": battery.power_plugged,
                    "secs_left": getattr(battery, "secs_left", None),
                }
        
        # Network
        net = psutil.net_io_counters()
        status["network"] = {
            "sent_mb": round(net.bytes_sent / (1024**2), 1),
            "recv_mb": round(net.bytes_recv / (1024**2), 1),
        }
        
        # Processes count
        status["processes"] = len(psutil.pids())
        
    except Exception as e:
        LOGGER.error("Ошибка при получении состояния системы: %s", e)

    return status


def get_top_processes(sort_by: str = "cpu", limit: int = 10) -> list[dict]:
    """Get top processes by CPU or memory usage."""
    if not HAS_PSUTIL:
        return []

    processes = []
    try:
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                processes.append({
                    "pid": proc.info["pid"],
                    "name": proc.info["name"],
                    "cpu": proc.info["cpu_percent"] or 0,
                    "memory": proc.info["memory_percent"] or 0,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        # Sort
        key = "cpu" if sort_by == "cpu" else "memory"
        processes.sort(key=lambda x: x[key], reverse=True)
        
    except Exception as e:
        LOGGER.error("Ошибка при получении списка процессов: %s", e)

    return processes[:limit]


def shorten_text(text: str, limit: int) -> str:
    """Trim long text for Telegram output."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def get_process_suspicion_reasons(name: str | None, exe_path: str | None) -> list[str]:
    """Return heuristic reasons why a process may require inspection."""
    process_name = (name or "").strip().lower()
    path_text = (exe_path or "").strip()
    if not process_name:
        return []

    reasons: list[str] = []
    normalized_path = path_text.lower().replace("/", "\\")
    windir = os.environ.get("WINDIR", r"C:\Windows").lower().replace("/", "\\")

    suspicious_locations = (
        "\\appdata\\local\\temp\\",
        "\\downloads\\",
        "\\users\\public\\",
        "\\$recycle.bin\\",
        "\\windows\\temp\\",
        "\\temp\\",
    )
    location_labels = {
        "\\appdata\\local\\temp\\": "Temp",
        "\\downloads\\": "Downloads",
        "\\users\\public\\": "Public",
        "\\$recycle.bin\\": "$Recycle.Bin",
        "\\windows\\temp\\": r"Windows\Temp",
        "\\temp\\": "Temp",
    }

    for location in suspicious_locations:
        if location in normalized_path:
            reasons.append(f"запуск из папки {location_labels[location]}")
            break

    if re.search(r"\.(jpg|jpeg|png|gif|pdf|doc|docx|xls|xlsx|txt)\.exe$", process_name):
        reasons.append("двойное расширение в имени файла")

    if normalized_path.endswith(".scr"):
        reasons.append("исполняемый файл с расширением .scr")

    system_name_rules = {
        "svchost.exe": (f"{windir}\\system32\\", f"{windir}\\syswow64\\"),
        "services.exe": (f"{windir}\\system32\\",),
        "lsass.exe": (f"{windir}\\system32\\",),
        "csrss.exe": (f"{windir}\\system32\\",),
        "winlogon.exe": (f"{windir}\\system32\\",),
        "smss.exe": (f"{windir}\\system32\\",),
        "spoolsv.exe": (f"{windir}\\system32\\",),
        "taskhostw.exe": (f"{windir}\\system32\\",),
        "dwm.exe": (f"{windir}\\system32\\",),
        "fontdrvhost.exe": (f"{windir}\\system32\\",),
        "explorer.exe": (f"{windir}\\",),
    }

    if process_name in system_name_rules:
        allowed_prefixes = system_name_rules[process_name]
        if not normalized_path:
            reasons.append("не удалось определить путь системного процесса")
        elif not any(normalized_path.startswith(prefix) for prefix in allowed_prefixes):
            reasons.append("системное имя процесса вне стандартной папки Windows")

    return reasons


def get_all_processes(page: int = 1, page_size: int = 15) -> dict[str, Any]:
    """Return paginated process list with heuristic suspicious markers."""
    if not HAS_PSUTIL:
        return {"items": [], "page": 1, "pages": 1, "total": 0, "suspicious_total": 0}

    items: list[dict[str, Any]] = []
    try:
        for proc in psutil.process_iter(
            ["pid", "name", "username", "exe", "memory_percent", "status"]
        ):
            try:
                name = proc.info.get("name") or "unknown"
                exe_path = proc.info.get("exe") or ""
                reasons = get_process_suspicion_reasons(name, exe_path)
                items.append(
                    {
                        "pid": proc.info.get("pid", 0),
                        "name": name,
                        "username": proc.info.get("username") or "",
                        "exe": exe_path,
                        "memory": round(float(proc.info.get("memory_percent") or 0), 1),
                        "status": proc.info.get("status") or "unknown",
                        "reasons": reasons,
                        "suspicious": bool(reasons),
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        LOGGER.error("Ошибка при получении полного списка процессов: %s", e)

    items.sort(key=lambda item: (not item["suspicious"], item["name"].lower(), item["pid"]))

    total = len(items)
    suspicious_total = sum(1 for item in items if item["suspicious"])
    pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(max(page, 1), pages)
    start = (current_page - 1) * page_size
    end = start + page_size

    return {
        "items": items[start:end],
        "page": current_page,
        "pages": pages,
        "total": total,
        "suspicious_total": suspicious_total,
        "page_size": page_size,
    }


def kill_process_by_name(name: str) -> tuple[bool, str]:
    """Kill process by name or PID."""
    if not HAS_PSUTIL:
        return False, "psutil не установлен"
    
    try:
        # Try as PID first
        try:
            pid = int(name)
            proc = psutil.Process(pid)
            proc_name = proc.name()
            proc.kill()
            return True, f"Процесс '{proc_name}' (PID: {pid}) завершен"
        except ValueError:
            pass
        
        # Kill by name
        killed = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if name.lower() in proc.info["name"].lower():
                    proc.kill()
                    killed.append(f"{proc.info['name']} (PID: {proc.info['pid']})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        if killed:
            return True, "Завершены процессы:\n" + "\n".join(killed)
        return False, f"Процесс с именем '{name}' не найден"
        
    except Exception as e:
        return False, str(e)


def get_network_connections() -> dict:
    """Get network connections summary."""
    if not HAS_PSUTIL:
        return {"error": "psutil не установлен"}
    
    try:
        connections = psutil.net_connections(kind="inet")
        established = sum(1 for c in connections if c.status == "ESTABLISHED")
        listening = sum(1 for c in connections if c.status == "LISTEN")
        
        # Get network IO
        net_io = psutil.net_io_counters()
        
        return {
            "established": established,
            "listening": listening,
            "total": len(connections),
            "bytes_sent_mb": round(net_io.bytes_sent / (1024**2), 1),
            "bytes_recv_mb": round(net_io.bytes_recv / (1024**2), 1),
        }
    except Exception as e:
        return {"error": str(e)}


def format_duration(total_seconds: int | float | None) -> str:
    """Format seconds to a short human-readable duration."""
    if total_seconds is None or total_seconds < 0:
        return "неизвестно"

    remaining = int(total_seconds)
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours or days:
        parts.append(f"{hours} ч")
    if minutes or hours or days:
        parts.append(f"{minutes} мин")
    parts.append(f"{seconds} сек")
    return " ".join(parts)


def get_uptime_details() -> dict[str, Any]:
    """Get system boot time and uptime details."""
    if not HAS_PSUTIL:
        return {"available": False}

    try:
        boot_time = psutil.boot_time()
        uptime_seconds = max(0, int(time.time() - boot_time))
        return {
            "available": True,
            "boot_timestamp": boot_time,
            "boot_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(boot_time)),
            "uptime_seconds": uptime_seconds,
            "uptime_text": format_duration(uptime_seconds),
        }
    except Exception as e:
        LOGGER.error("Ошибка при получении времени работы системы: %s", e)
        return {"available": False, "error": str(e)}


def get_system_info() -> dict[str, Any]:
    """Collect basic host information."""
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "username": getpass.getuser(),
        "computer_name": os.environ.get("COMPUTERNAME", socket.gethostname()),
        "user_domain": os.environ.get("USERDOMAIN", ""),
        "os_name": platform.platform(aliased=True),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "architecture": platform.machine() or platform.architecture()[0],
        "python_version": platform.python_version(),
    }

    processor = platform.processor().strip()
    if processor:
        info["processor"] = processor

    if HAS_PSUTIL:
        try:
            info["logical_cpus"] = psutil.cpu_count() or 0
            info["physical_cpus"] = psutil.cpu_count(logical=False) or 0
        except Exception as e:
            LOGGER.error("Ошибка при получении информации о CPU: %s", e)

    uptime = get_uptime_details()
    if uptime.get("available"):
        info["boot_time"] = uptime["boot_time"]
        info["uptime_text"] = uptime["uptime_text"]

    return info


def get_drive_overview() -> list[dict[str, Any]]:
    """Return mounted disks with usage information."""
    if not HAS_PSUTIL:
        return []

    drives: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    try:
        for partition in psutil.disk_partitions(all=False):
            key = (partition.device, partition.mountpoint)
            if key in seen:
                continue
            seen.add(key)

            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except (PermissionError, OSError):
                continue

            drives.append(
                {
                    "device": partition.device,
                    "mountpoint": partition.mountpoint,
                    "fstype": partition.fstype or "unknown",
                    "percent": round(usage.percent, 1),
                    "used_gb": round(usage.used / (1024**3), 1),
                    "free_gb": round(usage.free / (1024**3), 1),
                    "total_gb": round(usage.total / (1024**3), 1),
                }
            )
    except Exception as e:
        LOGGER.error("Ошибка при получении списка дисков: %s", e)

    drives.sort(key=lambda item: item["mountpoint"].lower())
    return drives


def get_ip_addresses() -> dict[str, Any]:
    """Return local IP addresses grouped by interface."""
    result: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "interfaces": [],
    }

    if HAS_PSUTIL:
        try:
            for interface_name, addresses in psutil.net_if_addrs().items():
                ipv4: list[str] = []
                ipv6: list[str] = []
                for address in addresses:
                    if address.family == socket.AF_INET:
                        if address.address.startswith("127.") or address.address.startswith("169.254."):
                            continue
                        ipv4.append(address.address)
                    elif getattr(socket, "AF_INET6", None) and address.family == socket.AF_INET6:
                        clean_address = address.address.split("%", 1)[0]
                        if clean_address == "::1":
                            continue
                        ipv6.append(clean_address)

                ipv4 = sorted(set(ipv4))
                ipv6 = sorted(set(ipv6))
                if ipv4 or ipv6:
                    result["interfaces"].append(
                        {
                            "name": interface_name,
                            "ipv4": ipv4,
                            "ipv6": ipv6,
                        }
                    )
        except Exception as e:
            LOGGER.error("Ошибка при получении IP-адресов: %s", e)

    if not result["interfaces"]:
        try:
            fallback_addresses = sorted(
                {
                    address
                    for address in socket.gethostbyname_ex(result["hostname"])[2]
                    if not address.startswith("127.")
                }
            )
        except OSError:
            fallback_addresses = []

        if fallback_addresses:
            result["interfaces"].append(
                {
                    "name": "host",
                    "ipv4": fallback_addresses,
                    "ipv6": [],
                }
            )

    result["interfaces"].sort(key=lambda item: item["name"].lower())
    return result


def get_battery_info() -> dict[str, Any] | None:
    """Return battery information when available."""
    if not HAS_PSUTIL or not hasattr(psutil, "sensors_battery"):
        return None

    try:
        battery = psutil.sensors_battery()
    except Exception as e:
        LOGGER.error("Ошибка при получении данных о батарее: %s", e)
        return None

    if not battery:
        return None

    power_time_unknown = getattr(psutil, "POWER_TIME_UNKNOWN", -1)
    power_time_unlimited = getattr(psutil, "POWER_TIME_UNLIMITED", -2)
    secs_left = getattr(battery, "secs_left", None)

    if secs_left in (power_time_unknown, power_time_unlimited):
        time_left = "неизвестно"
    else:
        time_left = format_duration(secs_left)

    return {
        "percent": round(battery.percent, 1),
        "power_plugged": battery.power_plugged,
        "secs_left": secs_left,
        "time_left": time_left,
    }


def get_services(status_filter: str = "all", limit: int = 20) -> list[dict[str, str]]:
    """Return Windows services."""
    if not HAS_PSUTIL or not hasattr(psutil, "win_service_iter"):
        return []

    services: list[dict[str, str]] = []
    try:
        for service in psutil.win_service_iter():
            try:
                service_info = service.as_dict()
            except Exception:
                continue

            status = str(service_info.get("status", "unknown"))
            if status_filter != "all" and status != status_filter:
                continue

            services.append(
                {
                    "name": str(service_info.get("name", "")),
                    "display_name": str(service_info.get("display_name", "")),
                    "status": status,
                    "start_type": str(service_info.get("start_type", "unknown")),
                }
            )
    except Exception as e:
        LOGGER.error("Ошибка при получении списка служб: %s", e)

    services.sort(key=lambda item: (item["status"] != "running", item["display_name"].lower(), item["name"].lower()))
    return services[:limit]


def build_alert_messages(
    status: dict[str, Any],
    config: BotConfig,
    include_cpu: bool = False,
) -> list[str]:
    """Build alert messages from current system status."""
    alerts: list[str] = []

    if status["battery"] and not status["battery"]["power_plugged"]:
        if status["battery"]["percent"] <= config.low_battery_threshold:
            alerts.append(f"🔋 Низкий заряд батареи: {status['battery']['percent']}%")

    if status["disk_percent"] >= config.high_disk_threshold:
        alerts.append(f"💾 Высокая загрузка диска: {status['disk_percent']}%")

    if include_cpu and status["cpu_percent"] >= config.high_cpu_threshold:
        alerts.append(f"🖥️ Высокая загрузка ЦП: {status['cpu_percent']}%")

    if status["ram_percent"] >= config.high_ram_threshold:
        alerts.append(f"🧠 Высокая загрузка ОЗУ: {status['ram_percent']}%")

    return alerts


def parse_limit_argument(
    args: list[str],
    default: int = 10,
    minimum: int = 1,
    maximum: int = 30,
) -> tuple[bool, int | str]:
    """Parse optional numeric limit argument."""
    if not args:
        return True, default

    try:
        value = int(args[0])
    except ValueError:
        return False, "Лимит должен быть целым числом."

    if value < minimum or value > maximum:
        return False, f"Лимит должен быть в диапазоне от {minimum} до {maximum}."

    return True, value


def get_cpu_details() -> dict[str, Any]:
    """Return detailed CPU information."""
    if not HAS_PSUTIL:
        return {"available": False}

    try:
        per_core = psutil.cpu_percent(interval=1, percpu=True)
        total_percent = round(sum(per_core) / len(per_core), 1) if per_core else round(psutil.cpu_percent(interval=1), 1)
        freq = psutil.cpu_freq()

        details: dict[str, Any] = {
            "available": True,
            "total_percent": total_percent,
            "per_core": [round(value, 1) for value in per_core],
            "logical_cpus": psutil.cpu_count() or 0,
            "physical_cpus": psutil.cpu_count(logical=False) or 0,
        }

        if freq:
            details["freq_current"] = round(freq.current, 1)
            details["freq_min"] = round(freq.min, 1)
            details["freq_max"] = round(freq.max, 1)

        return details
    except Exception as e:
        LOGGER.error("Ошибка при получении информации о ЦП: %s", e)
        return {"available": False, "error": str(e)}


def get_memory_details() -> dict[str, Any]:
    """Return detailed memory usage."""
    if not HAS_PSUTIL:
        return {"available": False}

    try:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "available": True,
            "virtual_total_gb": round(virtual.total / (1024**3), 1),
            "virtual_used_gb": round(virtual.used / (1024**3), 1),
            "virtual_available_gb": round(virtual.available / (1024**3), 1),
            "virtual_percent": round(virtual.percent, 1),
            "swap_total_gb": round(swap.total / (1024**3), 1),
            "swap_used_gb": round(swap.used / (1024**3), 1),
            "swap_percent": round(swap.percent, 1),
        }
    except Exception as e:
        LOGGER.error("Ошибка при получении информации о памяти: %s", e)
        return {"available": False, "error": str(e)}


def get_disk_usage_for_path(path_text: str) -> dict[str, Any]:
    """Return disk usage for a specific existing path."""
    if not HAS_PSUTIL:
        return {"available": False}

    try:
        target = Path(path_text).expanduser()
        if not target.exists():
            return {"available": False, "error": f"Путь не найден: {path_text}"}

        resolved = target.resolve()
        usage = psutil.disk_usage(str(resolved))
        return {
            "available": True,
            "path": str(resolved),
            "percent": round(usage.percent, 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "total_gb": round(usage.total / (1024**3), 1),
        }
    except Exception as e:
        LOGGER.error("Ошибка при получении использования диска для %s: %s", path_text, e)
        return {"available": False, "error": str(e)}


def get_interface_overview() -> list[dict[str, Any]]:
    """Return network interfaces with status and addresses."""
    if not HAS_PSUTIL:
        return []

    interfaces: list[dict[str, Any]] = []
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()

        for name in sorted(set(stats) | set(addrs)):
            stat = stats.get(name)
            addresses = addrs.get(name, [])
            ipv4 = sorted(
                {
                    address.address
                    for address in addresses
                    if address.family == socket.AF_INET
                    and not address.address.startswith("127.")
                    and not address.address.startswith("169.254.")
                }
            )
            ipv6 = sorted(
                {
                    address.address.split("%", 1)[0]
                    for address in addresses
                    if getattr(socket, "AF_INET6", None)
                    and address.family == socket.AF_INET6
                    and address.address.split("%", 1)[0] != "::1"
                }
            )

            interfaces.append(
                {
                    "name": name,
                    "is_up": bool(stat.isup) if stat else False,
                    "speed": stat.speed if stat else 0,
                    "mtu": stat.mtu if stat else 0,
                    "ipv4": ipv4,
                    "ipv6": ipv6,
                }
            )
    except Exception as e:
        LOGGER.error("Ошибка при получении сетевых интерфейсов: %s", e)

    return interfaces


def get_temperature_info() -> list[dict[str, Any]]:
    """Return temperature sensors when available."""
    if not HAS_PSUTIL or not hasattr(psutil, "sensors_temperatures"):
        return []

    entries: list[dict[str, Any]] = []
    try:
        sensors = psutil.sensors_temperatures()
    except Exception as e:
        LOGGER.error("Ошибка при получении температур: %s", e)
        return []

    for sensor_name, values in sensors.items():
        for value in values:
            current = getattr(value, "current", None)
            if current is None:
                continue

            entries.append(
                {
                    "sensor": sensor_name or "sensor",
                    "label": getattr(value, "label", "") or "датчик",
                    "current": round(float(current), 1),
                    "high": round(float(value.high), 1) if getattr(value, "high", None) is not None else None,
                    "critical": round(float(value.critical), 1) if getattr(value, "critical", None) is not None else None,
                }
            )

    entries.sort(key=lambda item: item["current"], reverse=True)
    return entries


def get_logged_users() -> list[dict[str, Any]]:
    """Return logged in users."""
    if not HAS_PSUTIL or not hasattr(psutil, "users"):
        return []

    users: list[dict[str, Any]] = []
    try:
        for user in psutil.users():
            users.append(
                {
                    "name": user.name,
                    "terminal": user.terminal or "",
                    "host": user.host or "",
                    "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(user.started)) if user.started else "",
                }
            )
    except Exception as e:
        LOGGER.error("Ошибка при получении списка пользователей: %s", e)

    return users


# =============================================================================
# BRIGHTNESS & VOLUME FUNCTIONS
# =============================================================================

def change_brightness(delta_percent: int) -> tuple[bool, str]:
    command = f"""
$ErrorActionPreference = 'Stop'
$current = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop |
    Select-Object -First 1 -ExpandProperty CurrentBrightness
if ($null -eq $current) {{
    throw "Не найден совместимый интерфейс управления яркостью монитора."
}}
$target = [Math]::Max(0, [Math]::Min(100, [int]$current + ({delta_percent})))
Invoke-CimMethod -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -MethodName WmiSetBrightness -Arguments @{{ Timeout = [uint32]0; Brightness = [byte]$target }} -ErrorAction Stop | Out-Null
Write-Output $target
""".strip()
    success, details = run_powershell(command)
    if success:
        return True, details or "обновлено"
    return False, details


def tap_virtual_key(vk_code: int, repeat_count: int = 1) -> tuple[bool, str]:
    try:
        user32 = ctypes.windll.user32
        for _ in range(repeat_count):
            user32.keybd_event(vk_code, 0, 0, 0)
            user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
    except OSError as exc:
        return False, str(exc)
    return True, ""


def change_volume(step_count: int, vk_code: int) -> tuple[bool, str]:
    return tap_virtual_key(vk_code, repeat_count=step_count)


def mute_sound() -> tuple[bool, str]:
    return change_volume(FULL_MUTE_STEPS, VK_VOLUME_DOWN)


# =============================================================================
# POWER FUNCTIONS
# =============================================================================

def schedule_power_action(action_flag: str, delay_seconds: int) -> tuple[bool, str]:
    completed = subprocess.run(
        [
            "shutdown",
            action_flag,
            "/t",
            str(delay_seconds),
            "/c",
            "Команда питания получена от Telegram ControlBot",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.returncode == 0:
        return True, ""

    details = (completed.stderr or completed.stdout).strip()
    if not details:
        details = f"shutdown завершился с кодом {completed.returncode}"
    return False, details


def cancel_shutdown() -> tuple[bool, str]:
    """Cancel scheduled shutdown/restart."""
    return run_command(["shutdown", "/a"])


def lock_workstation() -> tuple[bool, str]:
    """Lock the workstation."""
    try:
        ctypes.windll.user32.LockWorkStation()
        return True, "Компьютер заблокирован"
    except Exception as e:
        return False, str(e)


def logout_user() -> tuple[bool, str]:
    """Log out current user."""
    return run_command(["shutdown", "/l"])


# =============================================================================
# SCREENSHOT & WEBCAM FUNCTIONS
# =============================================================================

def take_screenshot() -> tuple[bool, BytesIO | str]:
    """Take a screenshot of all monitors."""
    try:
        screenshot = ImageGrab.grab()
        buffer = BytesIO()
        screenshot.save(buffer, format="PNG")
        buffer.seek(0)
        return True, buffer
    except Exception as exc:
        return False, str(exc)


def take_webcam_photo() -> tuple[bool, BytesIO | str]:
    """Take a photo from webcam."""
    if not HAS_CV2:
        return False, "OpenCV (cv2) не установлен. Выполните: pip install opencv-python"
    
    cap = None
    try:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return False, "Не удалось открыть веб-камеру"
        
        # Wait for camera to initialize
        time.sleep(0.5)
        
        ret, frame = cap.read()
        if not ret:
            return False, "Не удалось получить кадр с камеры"
        
        # Convert to RGB and save to buffer
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        buffer.seek(0)
        return True, buffer
        
    except Exception as exc:
        return False, str(exc)
    finally:
        if cap:
            cap.release()


# =============================================================================
# CLIPBOARD & HOTKEY FUNCTIONS
# =============================================================================

def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Copy text to clipboard."""
    if not HAS_PYPERCLIP:
        # Fallback using PowerShell
        escaped = text.replace("'", "''")
        success, output = run_powershell(f"Set-Clipboard -Value '{escaped}'")
        if success:
            return True, "Текст скопирован в буфер обмена"
        return False, output
    
    try:
        pyperclip.copy(text)
        return True, "Текст скопирован в буфер обмена"
    except Exception as e:
        return False, str(e)


def send_hotkey(*keys: str) -> tuple[bool, str]:
    """Send hotkey combination using PowerShell."""
    # Map common key names to PowerShell SendKeys format
    key_map = {
        "ctrl": "^",
        "alt": "%",
        "shift": "+",
        "win": "#{ESC}",
        "enter": "{ENTER}",
        "tab": "{TAB}",
        "esc": "{ESC}",
        "delete": "{DELETE}",
        "backspace": "{BS}",
        "space": " ",
        "up": "{UP}",
        "down": "{DOWN}",
        "left": "{LEFT}",
        "right": "{RIGHT}",
        "f1": "{F1}", "f2": "{F2}", "f3": "{F3}", "f4": "{F4}",
        "f5": "{F5}", "f6": "{F6}", "f7": "{F7}", "f8": "{F8}",
        "f9": "{F9}", "f10": "{F10}", "f11": "{F11}", "f12": "{F12}",
    }
    
    send_string = ""
    for key in keys:
        key_lower = key.lower()
        if key_lower in key_map:
            send_string += key_map[key_lower]
        elif len(key) == 1:
            send_string += key
        else:
            return False, f"Неизвестная клавиша: {key}"
    
    # Use PowerShell to send keys
    command = f'''
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait("{send_string}")
'''
    success, output = run_powershell(command)
    if success:
        return True, f"Отправлена комбинация: {'+'.join(keys)}"
    return False, output


# =============================================================================
# FILE OPERATIONS
# =============================================================================

def list_directory(path: str) -> tuple[bool, str]:
    """List files in directory."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return False, f"Путь не существует: {path}"
        if not p.is_dir():
            return False, f"Это не папка: {path}"
        
        items = []
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            icon = "📁" if item.is_dir() else "📄"
            size = ""
            if item.is_file():
                try:
                    size_bytes = item.stat().st_size
                    if size_bytes < 1024:
                        size = f" ({size_bytes} B)"
                    elif size_bytes < 1024**2:
                        size = f" ({size_bytes/1024:.1f} KB)"
                    else:
                        size = f" ({size_bytes/1024**2:.1f} MB)"
                except:
                    pass
            items.append(f"{icon} {item.name}{size}")
        
        result = f"📂 {p}\n\n" + "\n".join(items[:100])
        if len(items) > 100:
            result += f"\n\n... и еще {len(items) - 100} элементов"
        return True, result
        
    except Exception as e:
        return False, str(e)


def get_file(path: str) -> tuple[bool, BytesIO | str, str | None]:
    """Get file for sending."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return False, f"Файл не существует: {path}", None
        if not p.is_file():
            return False, f"Это не файл: {path}", None
        
        # Check file size (Telegram limit is 50MB for bots)
        size_mb = p.stat().st_size / (1024**2)
        if size_mb > 50:
            return False, f"Файл слишком большой ({size_mb:.1f} МБ). Лимит Telegram для ботов: 50 МБ.", None
        
        buffer = BytesIO(p.read_bytes())
        buffer.seek(0)
        return True, buffer, p.name
        
    except Exception as e:
        return False, str(e), None


def open_file(path: str) -> tuple[bool, str]:
    """Open file with default program."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return False, f"Файл не существует: {path}"

        os.startfile(str(p))
        return True, f"Открыт файл: {p.name}"
        
    except Exception as e:
        return False, str(e)


# =============================================================================
# MEDIA CONTROL FUNCTIONS
# =============================================================================

def media_play_pause() -> tuple[bool, str]:
    """Send media play/pause key."""
    return tap_virtual_key(VK_MEDIA_PLAY_PAUSE)


def media_next_track() -> tuple[bool, str]:
    """Send media next track key."""
    return tap_virtual_key(VK_MEDIA_NEXT_TRACK)


def media_prev_track() -> tuple[bool, str]:
    """Send media previous track key."""
    return tap_virtual_key(VK_MEDIA_PREV_TRACK)


# =============================================================================
# SCHEDULED TASKS HANDLER
# =============================================================================

async def check_scheduled_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check and execute scheduled tasks."""
    tasks = get_scheduled_tasks(context)
    current_time = time.time()
    tasks_to_remove = []
    
    for i, task in enumerate(tasks):
        if current_time >= task.execute_at:
            try:
                if task.task_type == "shutdown":
                    schedule_power_action("/s", 0)
                    if task.chat_id:
                        await context.bot.send_message(task.chat_id, "⏻ Выполняется запланированное выключение...")
                elif task.task_type == "reboot":
                    schedule_power_action("/r", 0)
                    if task.chat_id:
                        await context.bot.send_message(task.chat_id, "🔄 Выполняется запланированная перезагрузка...")
                elif task.task_type == "alarm":
                    if task.chat_id:
                        await context.bot.send_message(task.chat_id, f"⏰ Будильник!\n\n{task.message}")
                elif task.task_type == "message":
                    if task.chat_id:
                        await context.bot.send_message(task.chat_id, f"📢 Напоминание:\n\n{task.message}")
            except Exception as e:
                LOGGER.error("Ошибка при выполнении запланированной задачи: %s", e)
            
            tasks_to_remove.append(i)
    
    # Remove executed tasks (in reverse order to preserve indices)
    for i in reversed(tasks_to_remove):
        remove_scheduled_task(context, i)


# =============================================================================
# MONITORING ALERT HANDLER
# =============================================================================

async def monitoring_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check system status and send alerts."""
    config = get_config(context)
    
    if not config.allowed_chat_id:
        return
    
    status = get_system_status()
    alerts = []
    
    # Battery alert
    if status["battery"] and not status["battery"]["power_plugged"]:
        if status["battery"]["percent"] <= config.low_battery_threshold:
            alerts.append(f"🔋 Низкий заряд батареи: {status['battery']['percent']}%")
    
    # Disk alert
    if status["disk_percent"] >= config.high_disk_threshold:
        alerts.append(f"💾 Высокая загрузка диска: {status['disk_percent']}%")
    
    # CPU alert (disabled by default as it fluctuates)
    # if status["cpu_percent"] >= config.high_cpu_threshold:
    #     alerts.append(f"🖥️ High CPU usage: {status['cpu_percent']}%")
    
    # RAM alert
    if status["ram_percent"] >= config.high_ram_threshold:
        alerts.append(f"🧠 Высокая загрузка ОЗУ: {status['ram_percent']}%")
    
    if alerts:
        message = "⚠️ Системные предупреждения:\n\n" + "\n".join(alerts)
        try:
            await context.bot.send_message(config.allowed_chat_id, message)
        except Exception as e:
            LOGGER.error("Ошибка при отправке предупреждения: %s", e)


# =============================================================================
# COMMAND HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)

    if not is_private_chat(update):
        await update.effective_message.reply_text("Используйте бота только в личном чате.")
        return

    if config.allowed_chat_id is None:
        await send_setup_message(update)
        return

    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    help_text = """
🎮 <b>ControlBot готов к работе!</b>

<b>📊 Мониторинг:</b>
/status - состояние системы
/cpu - загрузка и частота процессора
/memory - использование ОЗУ и подкачки
/disk [путь] - использование диска для пути
/interfaces - сетевые интерфейсы
/topcpu [limit] - топ процессов по ЦП
/topmem [limit] - топ процессов по памяти
/temps - температурные датчики
/users - активные сессии пользователей
/info - сведения о ПК и Windows
/drives - диски и свободное место
/uptime - время работы системы
/whoami - текущий пользователь
/ip - локальные IP-адреса
/battery - состояние батареи
/alerts - текущие предупреждения
/ping - проверка отклика
/processes [cpu|mem] - список процессов
/processes_all [page] - все процессы с пометками
/kill &lt;name|pid&gt; - завершить процесс
/network - информация о сети
/services [all|running|stopped] [limit] - службы Windows

<b>🔊 Звук:</b>
/volume_down - уменьшить громкость
/volume_up - увеличить громкость
/mute - выключить звук

<b>📷 Захват:</b>
/screenshot - сделать скриншот
/webcam - фото с веб-камеры

<b>⏯ Мультимедиа:</b>
/play - пуск/пауза
/next - следующий трек
/prev - предыдущий трек

<b>📁 Файлы:</b>
 /ls &lt;путь&gt; - список файлов в папке
 /get &lt;путь&gt; - скачать файл
 /open &lt;путь&gt; - открыть файл

<b>📋 Буфер и клавиши:</b>
 /clip &lt;текст&gt; - скопировать в буфер обмена
 /hotkey &lt;клавиши&gt; - отправить сочетание клавиш

<b>⏱ Активность:</b>
 /apps - что открыто сейчас
 /topapps [today|7d|30d] - топ приложений по времени
 /activity [ГГГГ-ММ-ДД] - отчёт за день
 /sites [today|7d|30d] - сайты из истории браузеров

<b>⏰ Расписание:</b>
 /timer &lt;мин&gt; [shutdown|reboot|msg] - отложенное действие
 /alarm &lt;ЧЧ:ММ&gt; &lt;сообщение&gt; - будильник
/timers - список запланированных задач
/cancel [all|&lt;id&gt;] - отменить задачу

<b>🔐 Питание и сессия:</b>
/lock - заблокировать компьютер
/logout - выйти из системы
/reboot - перезагрузить ПК
/shutdown - выключить ПК

"""
    await update.effective_message.reply_text(
        help_text,
        reply_markup=MAIN_KEYBOARD,
        parse_mode="HTML",
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    chat = update.effective_chat
    if not chat:
        return

    await update.effective_message.reply_text(
        f"📱 chat_id: <code>{chat.id}</code>\n"
        f"📋 тип чата: {chat.type}",
        parse_mode="HTML",
    )


# =============================================================================
# STATUS & MONITORING COMMANDS
# =============================================================================

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not HAS_PSUTIL:
        await update.effective_message.reply_text(
            "❌ psutil не установлен.\n"
            "Установите его командой: pip install psutil"
        )
        return

    status = await asyncio.to_thread(get_system_status)

    # Build status message
    place = " / ".join(part for part in (config.club_name, config.pc_name) if part)
    lines = [f"📊 <b>Состояние системы</b>{(' — ' + place) if place else ''}\n"]
    lines.append(f"🤖 <b>Версия:</b> {APP_VERSION}")
    
    # CPU
    cpu_emoji = "🟢" if status["cpu_percent"] < 70 else "🟡" if status["cpu_percent"] < 90 else "🔴"
    lines.append(f"{cpu_emoji} <b>ЦП:</b> {status['cpu_percent']}%")
    
    # RAM
    ram_emoji = "🟢" if status["ram_percent"] < 70 else "🟡" if status["ram_percent"] < 90 else "🔴"
    lines.append(f"{ram_emoji} <b>ОЗУ:</b> {status['ram_percent']}% ({status['ram_used_gb']}/{status['ram_total_gb']} ГБ)")
    
    # Disk
    disk_emoji = "🟢" if status["disk_percent"] < 70 else "🟡" if status["disk_percent"] < 90 else "🔴"
    lines.append(f"{disk_emoji} <b>Диск:</b> {status['disk_percent']}% ({status['disk_used_gb']}/{status['disk_total_gb']} ГБ)")
    
    # Uptime
    lines.append(f"\n⏱️ <b>Время работы:</b> {status['uptime_hours']} ч")
    lines.append(f"🔄 <b>Запуск системы:</b> {status['boot_time']}")
    
    # Battery
    if status["battery"]:
        bat = status["battery"]
        plug = "🔌" if bat["power_plugged"] else "🔋"
        bat_emoji = "🟢" if bat["percent"] > 50 else "🟡" if bat["percent"] > 20 else "🔴"
        lines.append(f"\n{plug} <b>Батарея:</b> {bat_emoji} {bat['percent']}%")
    
    # Network
    lines.append(f"\n🌐 <b>Сеть:</b> ↑{status['network']['sent_mb']} МБ / ↓{status['network']['recv_mb']} МБ")
    lines.append(f"📋 <b>Процессы:</b> {status['processes']}")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def processes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not HAS_PSUTIL:
        await update.effective_message.reply_text(
            "❌ psutil не установлен.\n"
            "Установите его командой: pip install psutil"
        )
        return

    # Determine sort order
    args = context.args or []
    sort_by = "cpu" if not args or args[0] not in ["cpu", "mem", "memory"] else ("cpu" if args[0] == "cpu" else "memory")
    
    processes = get_top_processes(sort_by=sort_by, limit=15)
    
    lines = [f"⚙️ <b>Процессы (сортировка: {sort_by})</b>\n"]
    lines.append("```\nPID      CPU%   MEM%   Имя")
    lines.append("-" * 40)
    
    for p in processes:
        lines.append(f"{p['pid']:<8} {p['cpu']:<6.1f} {p['memory']:<6.1f} {p['name'][:20]}")
    
    lines.append("```")
    lines.append("\n💡 Используйте /kill &lt;name|pid&gt; для завершения процесса")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def processes_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not HAS_PSUTIL:
        await update.effective_message.reply_text(
            "❌ psutil не установлен.\n"
            "Установите его командой: pip install psutil"
        )
        return

    page = 1
    if context.args:
        try:
            page = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text(
                "❌ Использование: /processes_all [page]\nПример: /processes_all 2"
            )
            return

    result = get_all_processes(page=page, page_size=15)
    if not result["items"]:
        await update.effective_message.reply_text("❌ Не удалось получить список процессов.")
        return

    lines = [
        f"⚙️ <b>Все процессы - страница {result['page']}/{result['pages']}</b>\n",
        f"Всего процессов: <b>{result['total']}</b>",
        f"Подозрительных по эвристике: <b>{result['suspicious_total']}</b>",
        "",
    ]

    for proc in result["items"]:
        icon = "🚨" if proc["suspicious"] else "•"
        user = shorten_text(proc["username"] or "unknown", 22)
        status_text = shorten_text(proc["status"], 12)
        lines.append(
            f"{icon} <b>{escape(shorten_text(proc['name'], 28))}</b> · PID <code>{proc['pid']}</code>"
        )
        lines.append(
            f"{escape(user)} · {escape(status_text)} · RAM {proc['memory']:.1f}%"
        )
        if proc["exe"]:
            lines.append(f"<code>{escape(shorten_text(proc['exe'], 68))}</code>")
        if proc["reasons"]:
            reason_text = "; ".join(proc["reasons"])
            lines.append(f"Подозрительно: {escape(shorten_text(reason_text, 120))}")
        lines.append("")

    if result["page"] < result["pages"]:
        lines.append(f"➡️ Дальше: <code>/processes_all {result['page'] + 1}</code>")
    if result["page"] > 1:
        lines.append(f"⬅️ Назад: <code>/processes_all {result['page'] - 1}</code>")
    lines.append("💡 Пометка «подозрительно» эвристическая и не означает, что процесс вредоносный.")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def kill_process_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not context.args:
        await update.effective_message.reply_text(
            "❌ Использование: /kill &lt;имя_процесса|pid&gt;\n"
            "Пример: /kill notepad или /kill 1234",
            parse_mode="HTML",
        )
        return

    target = " ".join(context.args)
    success, message = kill_process_by_name(target)
    
    if success:
        await update.effective_message.reply_text(f"✅ {message}")
    else:
        await update.effective_message.reply_text(f"❌ {message}")


async def network_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not HAS_PSUTIL:
        await update.effective_message.reply_text(
            "❌ psutil не установлен.\n"
            "Установите его командой: pip install psutil"
        )
        return

    net = get_network_connections()
    
    if "error" in net:
        await update.effective_message.reply_text(f"❌ Ошибка: {net['error']}")
        return

    lines = ["🌐 <b>Состояние сети</b>\n"]
    lines.append(f"🔌 <b>Установлено:</b> {net['established']} соединений")
    lines.append(f"👂 <b>Прослушивается:</b> {net['listening']} портов")
    lines.append(f"📊 <b>Всего:</b> {net['total']} соединений")
    lines.append(f"\n📤 <b>Отправлено:</b> {net['bytes_sent_mb']} МБ")
    lines.append(f"📥 <b>Получено:</b> {net['bytes_recv_mb']} МБ")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def cpu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    details = get_cpu_details()
    if not details.get("available"):
        await update.effective_message.reply_text("❌ Не удалось получить информацию о ЦП.")
        return

    lines = ["🖥️ <b>Процессор</b>\n"]
    lines.append(f"Общая загрузка: <b>{details['total_percent']}%</b>")
    lines.append(f"Ядра / потоки: <b>{details['physical_cpus'] or '?'}</b> / <b>{details['logical_cpus']}</b>")

    if "freq_current" in details:
        lines.append(
            f"Частота: <b>{details['freq_current']}</b> МГц"
            f" (мин. {details['freq_min']} / макс. {details['freq_max']})"
        )

    if details["per_core"]:
        preview = ", ".join(
            f"{index + 1}:{value}%"
            for index, value in enumerate(details["per_core"][:16])
        )
        if len(details["per_core"]) > 16:
            preview += ", ..."
        lines.append(f"По ядрам: <code>{escape(preview)}</code>")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    details = get_memory_details()
    if not details.get("available"):
        await update.effective_message.reply_text("❌ Не удалось получить информацию о памяти.")
        return

    lines = ["🧠 <b>Память</b>\n"]
    lines.append(
        f"ОЗУ: <b>{details['virtual_percent']}%</b> "
        f"({details['virtual_used_gb']} / {details['virtual_total_gb']} ГБ)"
    )
    lines.append(f"Свободно: <b>{details['virtual_available_gb']} ГБ</b>")
    lines.append(
        f"Подкачка: <b>{details['swap_percent']}%</b> "
        f"({details['swap_used_gb']} / {details['swap_total_gb']} ГБ)"
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def disk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    target_path = " ".join(context.args).strip() if context.args else "C:\\"
    details = get_disk_usage_for_path(target_path)
    if not details.get("available"):
        await update.effective_message.reply_text(f"❌ {details.get('error', 'Не удалось получить данные о диске.')}")
        return

    percent = details["percent"]
    emoji = "🟢" if percent < 70 else "🟡" if percent < 90 else "🔴"
    lines = ["💾 <b>Использование диска</b>\n"]
    lines.append(f"Путь: <code>{escape(details['path'])}</code>")
    lines.append(f"{emoji} Занято: <b>{percent}%</b>")
    lines.append(f"Использовано: <b>{details['used_gb']} ГБ</b>")
    lines.append(f"Свободно: <b>{details['free_gb']} ГБ</b>")
    lines.append(f"Всего: <b>{details['total_gb']} ГБ</b>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def interfaces_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    interfaces = get_interface_overview()
    if not interfaces:
        await update.effective_message.reply_text("❌ Не удалось получить список сетевых интерфейсов.")
        return

    lines = ["📡 <b>Сетевые интерфейсы</b>\n"]
    for interface in interfaces[:12]:
        state = "включен" if interface["is_up"] else "выключен"
        speed_text = f"{interface['speed']} Мбит/с" if interface["speed"] else "неизвестно"
        lines.append(f"<b>{escape(interface['name'])}</b> · {escape(state)}")
        lines.append(f"Скорость: {escape(speed_text)} · MTU: {interface['mtu']}")
        if interface["ipv4"]:
            lines.append(f"IPv4: <code>{escape(', '.join(interface['ipv4']))}</code>")
        if interface["ipv6"]:
            lines.append(f"IPv6: <code>{escape(', '.join(interface['ipv6'][:3]))}</code>")
        lines.append("")

    await update.effective_message.reply_text("\n".join(lines).strip(), parse_mode="HTML")


async def topcpu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    ok, limit_or_message = parse_limit_argument(context.args, default=10, minimum=1, maximum=20)
    if not ok:
        await update.effective_message.reply_text(f"❌ {limit_or_message}")
        return

    processes = get_top_processes(sort_by="cpu", limit=int(limit_or_message))
    if not processes:
        await update.effective_message.reply_text("❌ Не удалось получить список процессов.")
        return

    lines = ["🔥 <b>Топ процессов по ЦП</b>\n"]
    for index, proc in enumerate(processes, 1):
        name = escape((proc["name"] or "unknown")[:32])
        lines.append(f"{index}. <code>{name}</code> · PID {proc['pid']} · {proc['cpu']:.1f}%")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def topmem_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    ok, limit_or_message = parse_limit_argument(context.args, default=10, minimum=1, maximum=20)
    if not ok:
        await update.effective_message.reply_text(f"❌ {limit_or_message}")
        return

    processes = get_top_processes(sort_by="memory", limit=int(limit_or_message))
    if not processes:
        await update.effective_message.reply_text("❌ Не удалось получить список процессов.")
        return

    lines = ["🧮 <b>Топ процессов по памяти</b>\n"]
    for index, proc in enumerate(processes, 1):
        name = escape((proc["name"] or "unknown")[:32])
        lines.append(f"{index}. <code>{name}</code> · PID {proc['pid']} · {proc['memory']:.1f}%")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def temps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    entries = get_temperature_info()
    if not entries:
        await update.effective_message.reply_text("❌ Температурные датчики недоступны или не поддерживаются.")
        return

    lines = ["🌡️ <b>Температуры</b>\n"]
    for entry in entries[:15]:
        line = f"<b>{escape(entry['sensor'])}</b> / {escape(entry['label'])}: <b>{entry['current']}°C</b>"
        if entry["high"] is not None:
            line += f" · high {entry['high']}°C"
        if entry["critical"] is not None:
            line += f" · crit {entry['critical']}°C"
        lines.append(line)

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    users = get_logged_users()
    if not users:
        await update.effective_message.reply_text("❌ Активные пользовательские сессии не найдены.")
        return

    lines = ["👥 <b>Пользовательские сессии</b>\n"]
    for user in users:
        lines.append(f"<b>{escape(user['name'])}</b>")
        if user["host"]:
            lines.append(f"Хост: <code>{escape(user['host'])}</code>")
        if user["terminal"]:
            lines.append(f"Сеанс: <code>{escape(user['terminal'])}</code>")
        if user["started"]:
            lines.append(f"Вход: {escape(user['started'])}")
        lines.append("")

    await update.effective_message.reply_text("\n".join(lines).strip(), parse_mode="HTML")


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    info = get_system_info()
    lines = ["💻 <b>Информация о ПК</b>\n"]
    lines.append(f"🖥️ <b>Имя компьютера:</b> <code>{escape(info['computer_name'])}</code>")

    if info.get("fqdn") and info["fqdn"] != info["hostname"]:
        lines.append(f"🌐 <b>FQDN:</b> <code>{escape(info['fqdn'])}</code>")

    user_domain = info.get("user_domain", "")
    full_user = f"{user_domain}\\{info['username']}" if user_domain else info["username"]
    lines.append(f"👤 <b>Пользователь:</b> <code>{escape(full_user)}</code>")
    lines.append(f"🪟 <b>Windows:</b> {escape(info['os_name'])}")
    lines.append(f"🧱 <b>Архитектура:</b> {escape(info['architecture'])}")

    if info.get("processor"):
        lines.append(f"🖥️ <b>Процессор:</b> {escape(info['processor'])}")

    if info.get("logical_cpus"):
        physical = info.get("physical_cpus") or "?"
        lines.append(f"🧮 <b>Ядра / потоки:</b> {physical} / {info['logical_cpus']}")

    if info.get("uptime_text"):
        lines.append(f"⏱️ <b>Аптайм:</b> {escape(info['uptime_text'])}")
        lines.append(f"🔄 <b>Запуск:</b> {escape(info['boot_time'])}")

    lines.append(f"🐍 <b>Python:</b> {escape(info['python_version'])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def drives_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not HAS_PSUTIL:
        await update.effective_message.reply_text(
            "❌ psutil не установлен.\n"
            "Установите его командой: pip install psutil"
        )
        return

    drives = get_drive_overview()
    if not drives:
        await update.effective_message.reply_text("❌ Не удалось получить список дисков.")
        return

    lines = ["💽 <b>Диски и разделы</b>\n"]
    for drive in drives:
        percent = drive["percent"]
        emoji = "🟢" if percent < 70 else "🟡" if percent < 90 else "🔴"
        lines.append(
            f"{emoji} <b>{escape(drive['mountpoint'])}</b> ({escape(drive['fstype'])})\n"
            f"<code>{escape(drive['device'])}</code>\n"
            f"Занято: {percent}% | {drive['used_gb']} / {drive['total_gb']} ГБ\n"
            f"Свободно: {drive['free_gb']} ГБ"
        )
        lines.append("")

    await update.effective_message.reply_text("\n".join(lines).strip(), parse_mode="HTML")


async def uptime_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    details = get_uptime_details()
    if not details.get("available"):
        await update.effective_message.reply_text("❌ Не удалось получить время работы системы.")
        return

    lines = ["⏱️ <b>Время работы системы</b>\n"]
    lines.append(f"Аптайм: <b>{escape(details['uptime_text'])}</b>")
    lines.append(f"Последний запуск: <b>{escape(details['boot_time'])}</b>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    username = os.environ.get("USERNAME", getpass.getuser())
    domain = os.environ.get("USERDOMAIN", "")
    computer_name = os.environ.get("COMPUTERNAME", socket.gethostname())
    profile = os.environ.get("USERPROFILE", "")
    full_user = f"{domain}\\{username}" if domain else username

    lines = ["👤 <b>Текущий пользователь</b>\n"]
    lines.append(f"Учетная запись: <code>{escape(full_user)}</code>")
    lines.append(f"Компьютер: <code>{escape(computer_name)}</code>")
    if profile:
        lines.append(f"Профиль: <code>{escape(profile)}</code>")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def ip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    ip_info = get_ip_addresses()
    interfaces = ip_info["interfaces"]
    if not interfaces:
        await update.effective_message.reply_text("❌ Локальные IP-адреса не найдены.")
        return

    lines = ["🌐 <b>Локальные IP-адреса</b>\n"]
    lines.append(f"Узел: <code>{escape(ip_info['hostname'])}</code>")
    for interface in interfaces:
        lines.append(f"\n<b>{escape(interface['name'])}</b>")
        if interface["ipv4"]:
            lines.append(f"IPv4: <code>{escape(', '.join(interface['ipv4']))}</code>")
        if interface["ipv6"]:
            lines.append(f"IPv6: <code>{escape(', '.join(interface['ipv6']))}</code>")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def battery_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    battery = get_battery_info()
    if not battery:
        await update.effective_message.reply_text("❌ Батарея не обнаружена или не поддерживается.")
        return

    state_text = "от сети" if battery["power_plugged"] else "от батареи"
    remaining_text = "заряжается / подключено питание" if battery["power_plugged"] else battery["time_left"]

    lines = ["🔋 <b>Батарея</b>\n"]
    lines.append(f"Заряд: <b>{battery['percent']}%</b>")
    lines.append(f"Режим: <b>{escape(state_text)}</b>")
    lines.append(f"Осталось: <b>{escape(remaining_text)}</b>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def services_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not HAS_PSUTIL or not hasattr(psutil, "win_service_iter"):
        await update.effective_message.reply_text(
            "❌ Получение служб Windows недоступно в этой сборке psutil."
        )
        return

    status_filter = "all"
    limit = 20

    if context.args:
        requested_status = context.args[0].lower()
        if requested_status in {"all", "running", "stopped"}:
            status_filter = requested_status
            if len(context.args) > 1:
                try:
                    limit = max(1, min(50, int(context.args[1])))
                except ValueError:
                    await update.effective_message.reply_text(
                        "❌ Лимит должен быть целым числом. Пример: /services running 15"
                    )
                    return
        else:
            await update.effective_message.reply_text(
                "❌ Использование: /services [all|running|stopped] [limit]"
            )
            return

    services = get_services(status_filter=status_filter, limit=limit)
    if not services:
        await update.effective_message.reply_text("❌ Список служб пуст или недоступен.")
        return

    title_map = {
        "all": "все",
        "running": "только запущенные",
        "stopped": "только остановленные",
    }
    lines = [f"🧩 <b>Службы Windows ({title_map[status_filter]})</b>\n"]
    for service in services:
        emoji = "🟢" if service["status"] == "running" else "⚪"
        lines.append(
            f"{emoji} <b>{escape(service['display_name'] or service['name'])}</b>\n"
            f"<code>{escape(service['name'])}</code> · {escape(service['status'])} · {escape(service['start_type'])}"
        )
        lines.append("")

    await update.effective_message.reply_text("\n".join(lines).strip(), parse_mode="HTML")


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not HAS_PSUTIL:
        await update.effective_message.reply_text(
            "❌ psutil не установлен.\n"
            "Установите его командой: pip install psutil"
        )
        return

    status = get_system_status()
    alerts = build_alert_messages(status, config, include_cpu=True)
    if not alerts:
        await update.effective_message.reply_text("✅ Сейчас критичных предупреждений нет.")
        return

    lines = ["⚠️ <b>Системные предупреждения</b>\n"]
    lines.extend(alerts)
    lines.append("")
    lines.append(
        "Пороги: "
        f"батарея <= {config.low_battery_threshold}%, "
        f"диск >= {config.high_disk_threshold}%, "
        f"ЦП >= {config.high_cpu_threshold}%, "
        f"ОЗУ >= {config.high_ram_threshold}%"
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    started_at = time.perf_counter()
    message = await update.effective_message.reply_text("🏓 Проверяю отклик...")
    api_latency_ms = round((time.perf_counter() - started_at) * 1000, 1)

    update_age_ms: float | None = None
    try:
        if update.effective_message and update.effective_message.date:
            update_age_ms = round((time.time() - update.effective_message.date.timestamp()) * 1000, 1)
    except Exception:
        update_age_ms = None

    lines = [
        "🏓 <b>Pong</b>",
        f"⏱️ Ответ Telegram API: {api_latency_ms} мс",
    ]
    if update_age_ms is not None and update_age_ms >= 0:
        lines.append(f"📥 Возраст входящей команды: {update_age_ms} мс")
    lines.append(f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    result_text = "\n".join(lines)

    try:
        await message.edit_text(result_text, parse_mode="HTML")
    except Exception as exc:
        LOGGER.warning("Не удалось отредактировать ответ /ping: %s", exc)
        await update.effective_message.reply_text(result_text, parse_mode="HTML")


# =============================================================================
# BRIGHTNESS & VOLUME COMMANDS
# =============================================================================

async def brightness_down_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, details = change_brightness(-config.brightness_step_percent)
    if success:
        await update.effective_message.reply_text(
            f"🔅 Яркость уменьшена.\n"
            f"Текущая яркость: {details}%"
        )
    else:
        await update.effective_message.reply_text(f"❌ Не удалось уменьшить яркость.\n{details}")


async def brightness_up_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, details = change_brightness(config.brightness_step_percent)
    if success:
        await update.effective_message.reply_text(
            f"🔆 Яркость увеличена.\n"
            f"Текущая яркость: {details}%"
        )
    else:
        await update.effective_message.reply_text(f"❌ Не удалось увеличить яркость.\n{details}")


async def volume_down_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, details = change_volume(config.volume_down_steps, VK_VOLUME_DOWN)
    if success:
        await update.effective_message.reply_text(f"🔉 Громкость уменьшена на {config.volume_down_steps} шаг(а).")
    else:
        await update.effective_message.reply_text(f"❌ Не удалось уменьшить громкость.\n{details}")


async def volume_up_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, details = change_volume(config.volume_down_steps, VK_VOLUME_UP)
    if success:
        await update.effective_message.reply_text(f"🔊 Громкость увеличена на {config.volume_down_steps} шаг(а).")
    else:
        await update.effective_message.reply_text(f"❌ Не удалось увеличить громкость.\n{details}")


async def mute_sound_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, details = mute_sound()
    if success:
        await update.effective_message.reply_text("🔇 Звук отключен.")
    else:
        await update.effective_message.reply_text(f"❌ Не удалось отключить звук.\n{details}")


# =============================================================================
# SCREENSHOT & WEBCAM COMMANDS
# =============================================================================

async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    status = await update.effective_message.reply_text("📷 Делаю скриншот...")
    
    success, result = take_screenshot()
    if success:
        await status.delete()
        await update.effective_message.reply_photo(
            photo=result,
            caption="📸 Скриншот готов.",
        )
    else:
        await status.edit_text(f"❌ Не удалось сделать скриншот.\n{result}")


async def webcam_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not config.allow_webcam:
        await update.effective_message.reply_text(
            "📹 Веб-камера отключена на этом ПК (ALLOW_WEBCAM=0)."
        )
        return

    status = await update.effective_message.reply_text("📹 Получаю доступ к веб-камере...")
    
    success, result = take_webcam_photo()
    if success:
        await status.delete()
        await update.effective_message.reply_photo(
            photo=result,
            caption="📹 Фото с веб-камеры готово.",
        )
    else:
        await status.edit_text(f"❌ {result}")


# =============================================================================
# MEDIA CONTROL COMMANDS
# =============================================================================

async def media_play_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, _ = media_play_pause()
    if success:
        await update.effective_message.reply_text("⏯️ Команда пуск/пауза отправлена.")
    else:
        await update.effective_message.reply_text("❌ Не удалось отправить мультимедийную клавишу.")


async def media_next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, _ = media_next_track()
    if success:
        await update.effective_message.reply_text("⏭️ Следующий трек.")
    else:
        await update.effective_message.reply_text("❌ Не удалось отправить мультимедийную клавишу.")


async def media_prev_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, _ = media_prev_track()
    if success:
        await update.effective_message.reply_text("⏮️ Предыдущий трек.")
    else:
        await update.effective_message.reply_text("❌ Не удалось отправить мультимедийную клавишу.")


# =============================================================================
# POWER COMMANDS
# =============================================================================

async def shutdown_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    await update.effective_message.reply_text(
        f"⏻ Компьютер будет выключен через {config.shutdown_delay_seconds} сек.\n"
        "Подтвердите действие:",
        reply_markup=build_power_action_markup(CALLBACK_CONFIRM_SHUTDOWN, "✅ Подтвердить выключение"),
    )


async def reboot_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    await update.effective_message.reply_text(
        f"🔄 Компьютер будет перезагружен через {config.shutdown_delay_seconds} сек.\n"
        "Подтвердите действие:",
        reply_markup=build_power_action_markup(CALLBACK_CONFIRM_REBOOT, "✅ Подтвердить перезагрузку"),
    )


async def lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, message = lock_workstation()
    if success:
        await update.effective_message.reply_text("🔒 Компьютер заблокирован.")
    else:
        await update.effective_message.reply_text(f"❌ {message}")


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    success, message = logout_user()
    if success:
        await update.effective_message.reply_text("👋 Выполняется выход из системы...")
    else:
        await update.effective_message.reply_text(f"❌ {message}")


# =============================================================================
# FILE COMMANDS
# =============================================================================

async def ls_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    path = " ".join(context.args) if context.args else "."
    success, result = list_directory(path)
    
    if success:
        await update.effective_message.reply_text(f"```\n{result}\n```", parse_mode="HTML")
    else:
        await update.effective_message.reply_text(f"❌ {result}")


async def get_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not context.args:
        await update.effective_message.reply_text("❌ Использование: /get &lt;путь_к_файлу&gt;")
        return

    path = " ".join(context.args)
    success, result, filename = get_file(path)
    
    if success:
        await update.effective_message.reply_document(
            document=result,
            filename=filename,
            caption=f"📄 {filename}",
        )
    else:
        await update.effective_message.reply_text(f"❌ {result}")


async def open_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not context.args:
        await update.effective_message.reply_text("❌ Использование: /open &lt;путь_к_файлу&gt;")
        return

    path = " ".join(context.args)
    success, message = open_file(path)
    
    if success:
        await update.effective_message.reply_text(f"✅ {message}")
    else:
        await update.effective_message.reply_text(f"❌ {message}")


# =============================================================================
# CLIPBOARD & HOTKEY COMMANDS
# =============================================================================

async def clipboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not context.args:
        await update.effective_message.reply_text("❌ Использование: /clip &lt;текст&gt;")
        return

    text = " ".join(context.args)
    success, message = copy_to_clipboard(text)
    
    if success:
        await update.effective_message.reply_text(f"📋 {message}\n\n<pre>{text[:200]}{'...' if len(text) > 200 else ''}</pre>", parse_mode="HTML")
    else:
        await update.effective_message.reply_text(f"❌ {message}")


async def hotkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not context.args:
        await update.effective_message.reply_text(
            "❌ Использование: /hotkey &lt;key1&gt; &lt;key2&gt; ...\n"
            "Пример: /hotkey ctrl c\n"
            "Клавиши: ctrl, alt, shift, win, f1-f12, enter, tab, esc и т.д."
        )
        return

    keys = context.args
    success, message = send_hotkey(*keys)
    
    if success:
        await update.effective_message.reply_text(f"⌨️ {message}")
    else:
        await update.effective_message.reply_text(f"❌ {message}")


# =============================================================================
# ACTIVITY COMMANDS
# =============================================================================

def _get_activity_tracker(context: ContextTypes.DEFAULT_TYPE) -> ActivityTracker | None:
    tracker: ActivityTracker | None = context.application.bot_data.get("activity_tracker")
    return tracker


async def apps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    tracker = _get_activity_tracker(context)
    if tracker is None or not tracker.enabled:
        await update.effective_message.reply_text(
            "⏱ Учёт активности отключён. Включите ACTIVITY_ENABLED=1 в .env и перезапустите бота."
        )
        return

    text = await tracker.build_apps_text()
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def topapps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    tracker = _get_activity_tracker(context)
    if tracker is None or not tracker.enabled:
        await update.effective_message.reply_text(
            "⏱ Учёт активности отключён. Включите ACTIVITY_ENABLED=1 в .env и перезапустите бота."
        )
        return

    period_map = {"today": 1, "7d": 7, "7": 7, "30d": 30, "30": 30}
    requested = context.args[0].lower() if context.args else "today"
    if requested not in period_map:
        await update.effective_message.reply_text(
            "❌ Использование: /topapps [today|7d|30d]\nПример: /topapps 7d"
        )
        return

    text = tracker.build_topapps_text(days=period_map[requested])
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def activity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    tracker = _get_activity_tracker(context)
    if tracker is None or not tracker.enabled:
        await update.effective_message.reply_text(
            "⏱ Учёт активности отключён. Включите ACTIVITY_ENABLED=1 в .env и перезапустите бота."
        )
        return

    date_text = context.args[0] if context.args else time.strftime("%Y-%m-%d")
    try:
        time.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        await update.effective_message.reply_text(
            "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД, например: /activity 2026-10-29"
        )
        return

    text = tracker.build_activity_text(date_text)
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def sites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    tracker = _get_activity_tracker(context)
    if tracker is None or not tracker.enabled:
        await update.effective_message.reply_text(
            "🌐 Учёт отключён. Включите ACTIVITY_ENABLED=1 и BROWSER_SITES_ENABLED=1 в .env."
        )
        return

    period_map = {"today": 1, "7d": 7, "7": 7, "30d": 30, "30": 30}
    requested = context.args[0].lower() if context.args else "today"
    if requested not in period_map:
        await update.effective_message.reply_text(
            "❌ Использование: /sites [today|7d|30d]\nПример: /sites 7d"
        )
        return

    text = tracker.build_sites_text(days=period_map[requested])
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def activity_tick_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    tracker = _get_activity_tracker(context)
    if tracker is not None:
        await tracker.tick()


async def activity_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    tracker = _get_activity_tracker(context)
    if tracker is None or not tracker.enabled or not config.allowed_chat_id:
        return
    if time.localtime().tm_hour < config.activity_summary_hour:
        return

    today = time.strftime("%Y-%m-%d")
    if tracker.is_summary_sent(today):
        return

    summary = tracker.build_daily_summary_text()
    try:
        await context.bot.send_message(config.allowed_chat_id, summary, parse_mode="HTML")
    except Exception as exc:
        LOGGER.error("Не удалось отправить сводку активности: %s", exc)
        return

    tracker.mark_summary_sent(today)
    await tracker.save()


# =============================================================================
# SCHEDULE COMMANDS
# =============================================================================

async def timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not await ensure_job_queue_available(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "❌ Использование: /timer &lt;минуты&gt; [shutdown|reboot|msg &lt;текст&gt;]\n"
            "Пример: /timer 30 shutdown\n"
            "         /timer 5 msg Не забудь выпить воды!"
        )
        return

    try:
        minutes = int(context.args[0])
        if minutes < 1:
            raise ValueError("Количество минут должно быть положительным")
    except ValueError:
        await update.effective_message.reply_text("❌ Неверное время. Укажите количество минут числом.")
        return

    action = context.args[1] if len(context.args) > 1 else "msg"
    message_text = " ".join(context.args[3:]) if len(context.args) > 3 else "Таймер завершен!"

    if action not in ["shutdown", "reboot", "msg"]:
        await update.effective_message.reply_text("❌ Неверное действие. Используйте: shutdown, reboot или msg")
        return

    task = ScheduledTask(
        task_type=action,
        execute_at=time.time() + minutes * 60,
        created_at=time.time(),
        message=message_text,
        chat_id=update.effective_chat.id,
    )
    add_scheduled_task(context, task)

    action_emoji = {"shutdown": "⏻", "reboot": "🔄", "msg": "💬"}
    action_name = {"shutdown": "выключение", "reboot": "перезагрузка", "msg": "сообщение"}
    await update.effective_message.reply_text(
        f"⏰ Таймер установлен!\n"
        f"{action_emoji.get(action, '⏰')} Действие: {action_name.get(action, action)}\n"
        f"⏱️ Через: {minutes} мин.\n"
        f"📅 Время: {time.strftime('%H:%M:%S', time.localtime(task.execute_at))}"
    )


async def alarm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    if not await ensure_job_queue_available(update, context):
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "❌ Использование: /alarm &lt;ЧЧ:ММ&gt; &lt;сообщение&gt;\n"
            "Пример: /alarm 14:30 Пора сделать перерыв!"
        )
        return

    time_str = context.args[0]
    message_text = " ".join(context.args[1:])

    try:
        hour, minute = map(int, time_str.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("Неверное время")
    except ValueError:
        await update.effective_message.reply_text("❌ Неверный формат времени. Используйте ЧЧ:ММ (24-часовой формат).")
        return

    # Calculate timestamp
    now = time.localtime()
    alarm_time = time.struct_time((
        now.tm_year, now.tm_mon, now.tm_mday,
        hour, minute, 0, now.tm_wday, now.tm_yday, now.tm_isdst
    ))
    execute_at = time.mktime(alarm_time)
    
    # If time has passed today, schedule for tomorrow
    if execute_at <= time.time():
        execute_at += 86400  # Add 24 hours

    task = ScheduledTask(
        task_type="alarm",
        execute_at=execute_at,
        created_at=time.time(),
        message=message_text,
        chat_id=update.effective_chat.id,
    )
    add_scheduled_task(context, task)

    await update.effective_message.reply_text(
        f"⏰ Будильник установлен!\n"
        f"📅 Время: {time_str}\n"
        f"💬 Сообщение: {message_text}"
    )


async def timers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    tasks = get_scheduled_tasks(context)
    
    if not tasks:
        await update.effective_message.reply_text("📭 Запланированных задач нет.")
        return

    lines = ["⏰ <b>Запланированные задачи</b>\n"]
    action_emoji = {"shutdown": "⏻", "reboot": "🔄", "msg": "💬", "alarm": "⏰"}
    action_name = {"shutdown": "выключение", "reboot": "перезагрузка", "msg": "сообщение", "alarm": "будильник"}
    
    for i, task in enumerate(tasks):
        remaining = task.execute_at - time.time()
        if remaining < 0:
            remaining = 0
        
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        
        emoji = action_emoji.get(task.task_type, "📌")
        lines.append(
            f"<b>{i}.</b> {emoji} {action_name.get(task.task_type, task.task_type)}\n"
            f"   ⏱️ Через: {minutes}м {seconds}с\n"
            f"   📅 Время: {time.strftime('%H:%M:%S', time.localtime(task.execute_at))}"
        )
        if task.message:
            lines.append(f"   💬 {task.message[:50]}{'...' if len(task.message) > 50 else ''}")
        lines.append("")

    lines.append("\n💡 Используйте /cancel &lt;id&gt; для отмены задачи")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    # Also cancel system shutdown
    success, _ = cancel_shutdown()

    if not context.args or context.args[0] == "all":
        count = clear_scheduled_tasks(context)
        await update.effective_message.reply_text(
            f"✅ Отменено задач: {count}.\n"
            f"{'✅ Выключение/перезагрузка системы отменены.' if success else ''}"
        )
        return

    try:
        task_id = int(context.args[0])
        if remove_scheduled_task(context, task_id):
            await update.effective_message.reply_text(
                f"✅ Задача {task_id} отменена.\n"
                f"{'✅ Выключение/перезагрузка системы отменены.' if success else ''}"
            )
        else:
            await update.effective_message.reply_text(f"❌ Задача {task_id} не найдена.")
    except ValueError:
        await update.effective_message.reply_text("❌ Использование: /cancel [all|&lt;task_id&gt;]")


# =============================================================================
# CALLBACK HANDLER
# =============================================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    query = update.callback_query
    if not query:
        return

    await query.answer()

    if not is_authorized(update, config):
        await query.edit_message_text("⛔ Доступ запрещен.")
        return

    if query.data == CALLBACK_CANCEL:
        await query.edit_message_text("❌ Действие отменено.")
        return

    if query.data == CALLBACK_CONFIRM_SHUTDOWN:
        success, details = schedule_power_action("/s", config.shutdown_delay_seconds)
        if success:
            await query.edit_message_text(
                f"⏻ Команда на выключение принята.\n"
                f"Windows выключится через {config.shutdown_delay_seconds} сек.\n"
                f"Используйте /cancel для отмены."
            )
        else:
            await query.edit_message_text(f"❌ Не удалось запланировать выключение.\n{details}")
        return

    if query.data == CALLBACK_CONFIRM_REBOOT:
        success, details = schedule_power_action("/r", config.shutdown_delay_seconds)
        if success:
            await query.edit_message_text(
                f"🔄 Команда на перезагрузку принята.\n"
                f"Windows перезагрузится через {config.shutdown_delay_seconds} сек.\n"
                f"Используйте /cancel для отмены."
            )
        else:
            await query.edit_message_text(f"❌ Не удалось запланировать перезагрузку.\n{details}")
        return

    await query.edit_message_text("❓ Неизвестное действие.")


# =============================================================================
# FALLBACK HANDLER
# =============================================================================

async def fallback_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    await update.effective_message.reply_text(
        "🤖 Неизвестная команда. Используйте /start, чтобы увидеть список команд.",
        reply_markup=MAIN_KEYBOARD,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует необработанные ошибки и предупреждает владельца."""
    LOGGER.error("Необработанная ошибка: %s", context.error, exc_info=context.error)
    try:
        config = get_config(context)
        if config.allowed_chat_id:
            await context.bot.send_message(
                config.allowed_chat_id,
                f"⚠️ Ошибка в ControlBot: {type(context.error).__name__}. Подробности в bot.log.",
            )
    except Exception:
        pass


# =============================================================================
# APPLICATION BUILDER
# =============================================================================

async def save_activity_on_shutdown(application: Application) -> None:
    tracker: ActivityTracker | None = application.bot_data.get("activity_tracker")
    if tracker is not None:
        await tracker.save()


# =============================================================================
# HELPER24: ОТЧЁТЫ И ОБНОВЛЕНИЯ
# =============================================================================


async def helper24_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправить срез работы ПК в Helper24 (08:00 и 21:00)."""
    if helper24_report is None:
        return
    config = get_config(context)
    since = time.time() - 12 * 3600
    try:
        await asyncio.to_thread(
            helper24_report.send_digest,
            APP_DIR,
            config.pc_name,
            config.club_name,
            APP_VERSION,
            since,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Отчёт Helper24 не отправлен: %s", exc)


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[.\-+_]", str(value or "").lstrip("vV")):
        match = re.match(r"(\d+)", chunk)
        parts.append(int(match.group(1)) if match else 0)
    return tuple(parts)


def _github_release() -> dict[str, Any] | None:
    """Последний релиз GitHub: версия, ссылка на exe, sha256."""
    repo = UPDATE_REPO
    if not repo:
        return None

    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ControlBot-updater",
            **({"Authorization": f"Bearer {UPDATE_TOKEN}"} if UPDATE_TOKEN else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        LOGGER.info("GitHub вернул %s при проверке релиза %s", exc.code, repo)
        return None
    except Exception as exc:  # noqa: BLE001
        LOGGER.info("GitHub недоступен: %s", exc)
        return None

    tag = str(data.get("tag_name") or "").strip()
    version = tag.lstrip("vV")
    if not version:
        return None

    asset_url = ""
    sha256 = ""
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.lower().endswith(".exe"):
            asset_url = str(asset.get("browser_download_url") or "")
            digest = str(asset.get("digest") or "")
            if digest.startswith("sha256:"):
                sha256 = digest.split(":", 1)[1].strip().lower()
            break

    if not asset_url:
        LOGGER.info("В релизе %s нет exe-файла", tag)
        return None

    return {
        "version": version,
        "url": asset_url,
        "sha256": sha256,
        "notes": str(data.get("body") or "")[:300],
    }


def check_for_update() -> dict[str, Any] | None:
    """Вернуть манифест новой версии или None."""
    if not getattr(sys, "frozen", False):
        return None

    use_github = UPDATE_SOURCE == "github" or (not UPDATE_SOURCE and bool(UPDATE_REPO))
    if use_github:
        data = _github_release()
    else:
        try:
            url = f"{UPDATE_MANIFEST_URL}?t={int(time.time())}"
            request = urllib.request.Request(
                url, headers={"User-Agent": "ControlBot-updater/1.0"}
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Проверка обновлений недоступна: %s", exc)
            return None

    if not data:
        return None

    latest = str(data.get("version") or "").strip()
    if not latest or not data.get("url"):
        return None
    if _version_tuple(latest) <= _version_tuple(APP_VERSION):
        return None
    return data


def download_and_apply_update(manifest: dict[str, Any]) -> bool:
    """Скачать новый exe и перезапустить программу через bat-обновлятор."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False

    current = Path(sys.executable).resolve()
    temp_dir = Path(tempfile.gettempdir())
    staged = temp_dir / "ControlBot-new.exe"
    updater = temp_dir / "controlbot_update.bat"

    try:
        with urllib.request.urlopen(str(manifest["url"]), timeout=180) as response:
            payload = response.read()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Не удалось скачать обновление: %s", exc)
        return False

    expected = str(manifest.get("sha256") or "").strip().lower()
    if expected and hashlib.sha256(payload).hexdigest().lower() != expected:
        LOGGER.warning("Обновление повреждено: не совпала контрольная сумма.")
        return False

    try:
        staged.write_bytes(payload)
    except OSError as exc:
        LOGGER.warning("Не удалось сохранить обновление: %s", exc)
        return False

    lines = [
        "@echo off",
        "setlocal",
        "timeout /t 4 /nobreak >nul",
        ":wait",
        'tasklist /FI "IMAGENAME eq ControlBot.exe" 2>nul | find /I "ControlBot.exe" >nul',
        "if not errorlevel 1 (",
        "  timeout /t 2 /nobreak >nul",
        "  goto wait",
        ")",
        f'copy /y "{staged}" "{current}" >nul',
        f'start "" "{current}"',
        'del "%~f0"',
    ]
    updater.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

    creationflags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(["cmd", "/c", str(updater)], creationflags=creationflags, close_fds=True)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Не удалось запустить обновлятор: %s", exc)
        return False

    LOGGER.info("Обновление до %s скачано, перезапускаюсь.", manifest.get("version"))
    return True


async def update_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фоновая проверка обновлений."""
    manifest = await asyncio.to_thread(check_for_update)
    if not manifest:
        return
    applied = await asyncio.to_thread(download_and_apply_update, manifest)
    if applied:
        await asyncio.sleep(1)
        os._exit(0)


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return

    manifest = await asyncio.to_thread(check_for_update)
    if not manifest:
        await update.effective_message.reply_text(
            f"✅ Обновлений нет. Версия: {APP_VERSION}"
        )
        return

    await update.effective_message.reply_text(
        f"⬇️ Найдена версия {manifest.get('version')}. Обновляюсь и перезапускаюсь..."
    )
    if not await asyncio.to_thread(download_and_apply_update, manifest):
        await update.effective_message.reply_text("❌ Обновление не удалось, смотрите bot.log.")
        return
    await asyncio.sleep(1)
    os._exit(0)


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Прислать отчёт в Helper24 прямо сейчас."""
    config = get_config(context)
    if not is_authorized(update, config):
        await send_unauthorized_message(update)
        return
    if helper24_report is None or not helper24_report.enabled():
        await update.effective_message.reply_text(
            "Отчёты Helper24 отключены: нет cryptography или ключа HELPER24_FERNET_KEY."
        )
        return

    await update.effective_message.reply_text("Собираю отчёт и отправляю…")
    ok = await asyncio.to_thread(
        helper24_report.send_digest,
        APP_DIR,
        config.pc_name,
        config.club_name,
        APP_VERSION,
        time.time() - 12 * 3600,
    )
    await update.effective_message.reply_text(
        "✅ Отчёт отправлен Helper24." if ok else "❌ Не удалось отправить, смотри bot.log."
    )


def build_application(config: BotConfig) -> Application:
    application = (
        ApplicationBuilder()
        .token(config.token)
        .post_shutdown(save_activity_on_shutdown)
        .build()
    )
    application.bot_data["config"] = config
    application.bot_data["scheduled_tasks"] = []
    application.add_error_handler(error_handler)

    owner_id = next(iter(config.allowed_chat_ids), config.allowed_chat_id)
    access_store.init_store(APP_DIR / "access_users.json", owner_id)

    tracker = ActivityTracker(
        data_path=APP_DIR / "activity_data.json",
        afk_threshold_seconds=config.activity_afk_seconds,
        track_titles=config.activity_track_titles,
        retention_days=config.activity_retention_days,
        enabled=config.activity_enabled,
        browser_sites=config.browser_sites_enabled,
        browser_sites_interval=config.browser_sites_interval,
    )
    application.bot_data["activity_tracker"] = tracker

    # Core commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("menu", start_command))
    application.add_handler(CommandHandler("id", id_command))
    
    # Status & Monitoring
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cpu", cpu_command))
    application.add_handler(CommandHandler("memory", memory_command))
    application.add_handler(CommandHandler("disk", disk_command))
    application.add_handler(CommandHandler("interfaces", interfaces_command))
    application.add_handler(CommandHandler("topcpu", topcpu_command))
    application.add_handler(CommandHandler("topmem", topmem_command))
    application.add_handler(CommandHandler("temps", temps_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("drives", drives_command))
    application.add_handler(CommandHandler("uptime", uptime_command))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("ip", ip_command))
    application.add_handler(CommandHandler("battery", battery_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("processes", processes_command))
    application.add_handler(CommandHandler("processes_all", processes_all_command))
    application.add_handler(CommandHandler("kill", kill_process_command))
    application.add_handler(CommandHandler("network", network_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("ping_me", report_command))
    application.add_handler(CommandHandler("update", update_command))
    application.add_handler(CommandHandler("access", access_command))
    application.add_handler(CommandHandler("grant", grant_command))
    application.add_handler(CommandHandler("revoke", revoke_command))
    application.add_handler(CallbackQueryHandler(access_callback, pattern=r"^access:"))
    application.add_handler(CommandHandler("services", services_command))
    
    # Brightness & Volume
    application.add_handler(CommandHandler("brightness_down", brightness_down_command))
    application.add_handler(CommandHandler("brightness_up", brightness_up_command))
    application.add_handler(CommandHandler("volume_down", volume_down_command))
    application.add_handler(CommandHandler("volume_up", volume_up_command))
    application.add_handler(CommandHandler("mute", mute_sound_command))
    application.add_handler(CommandHandler("mute_sound", mute_sound_command))
    
    # Capture
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("webcam", webcam_command))
    
    # Media Control
    application.add_handler(CommandHandler("play", media_play_command))
    application.add_handler(CommandHandler("next", media_next_command))
    application.add_handler(CommandHandler("prev", media_prev_command))
    
    # Power
    application.add_handler(CommandHandler("lock", lock_command))
    application.add_handler(CommandHandler("logout", logout_command))
    application.add_handler(CommandHandler("reboot", reboot_prompt))
    application.add_handler(CommandHandler("shutdown", shutdown_prompt))
    
    # Files
    application.add_handler(CommandHandler("ls", ls_command))
    application.add_handler(CommandHandler("get", get_file_command))
    application.add_handler(CommandHandler("open", open_file_command))
    
    # Clipboard & Hotkeys
    application.add_handler(CommandHandler("clip", clipboard_command))
    application.add_handler(CommandHandler("hotkey", hotkey_command))

    # Activity tracking
    application.add_handler(CommandHandler("apps", apps_command))
    application.add_handler(CommandHandler("topapps", topapps_command))
    application.add_handler(CommandHandler("activity", activity_command))
    application.add_handler(CommandHandler("sites", sites_command))
    
    # Scheduling
    application.add_handler(CommandHandler("timer", timer_command))
    application.add_handler(CommandHandler("alarm", alarm_command))
    application.add_handler(CommandHandler("timers", timers_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    
    # Callback handler
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    # Button handlers
    button_handlers = {
        BUTTON_VOLUME_DOWN: volume_down_command,
        BUTTON_VOLUME_UP: volume_up_command,
        BUTTON_MUTE_SOUND: mute_sound_command,
        BUTTON_SCREENSHOT: screenshot_command,
        BUTTON_WEBCAM: webcam_command,
        BUTTON_STATUS: status_command,
        BUTTON_PROCESSES: processes_command,
        BUTTON_ACTIVITY: apps_command,
        BUTTON_LOCK: lock_command,
        BUTTON_MEDIA_PLAY: media_play_command,
        BUTTON_MEDIA_NEXT: media_next_command,
        BUTTON_MEDIA_PREV: media_prev_command,
        BUTTON_RESTART: reboot_prompt,
        BUTTON_SHUTDOWN: shutdown_prompt,
    }
    
    for button_text, handler in button_handlers.items():
        application.add_handler(
            MessageHandler(
                filters.Regex(f"^{re.escape(button_text)}$"),
                handler,
            )
        )
    
    # Fallback
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text_handler)
    )
    
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            check_scheduled_tasks,
            interval=10,
            first=10,
        )

        if config.allowed_chat_id:
            application.job_queue.run_repeating(
                monitoring_alerts,
                interval=config.monitoring_interval,
                first=config.monitoring_interval,
            )

        if config.activity_enabled:
            application.job_queue.run_repeating(
                activity_tick_job,
                interval=15,
                first=15,
                name="activity_tick",
            )
            if config.allowed_chat_id and config.activity_summary_enabled:
                application.job_queue.run_repeating(
                    activity_summary_job,
                    interval=300,
                    first=120,
                    name="activity_summary",
                )

        if helper24_report is not None and helper24_report.enabled():
            for hour in (8, 21):
                application.job_queue.run_daily(
                    helper24_digest_job,
                    time=dt_time(hour=hour, minute=0),
                    name=f"helper24_digest_{hour}",
                )
            application.job_queue.run_once(
                helper24_digest_job, when=45, name="helper24_start"
            )
            LOGGER.info("Отчёты Helper24 включены: 08:00, 21:00 и сразу после старта.")

        if getattr(sys, "frozen", False):
            application.job_queue.run_once(update_job, when=60, name="update_start")
            application.job_queue.run_daily(
                update_job, time=dt_time(hour=10, minute=17), name="update_daily"
            )
    else:
        LOGGER.warning(
            "JobQueue недоступен. Таймеры, мониторинг и учёт активности отключены. "
            "Установите python-telegram-bot[job-queue], чтобы включить их."
        )
    
    return application


# =============================================================================
# MAIN
# =============================================================================

LOCK_FILE = APP_DIR / "controlbot_instance.lock"


def acquire_instance_lock() -> bool:
    """Не даёт запустить вторую копию бота на том же ПК (ошибка 409)."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0

        alive = False
        if pid:
            if HAS_PSUTIL:
                alive = psutil.pid_exists(pid)
            else:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False

        if alive and pid != os.getpid():
            LOGGER.error("ControlBot уже запущен (PID %s). Вторая копия не нужна.", pid)
            return False

    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Не удалось создать lock-файл: %s", exc)
        return True

    def _release() -> None:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_release)
    return True


SETUP_BANNER = """
============================================================
  ControlBot - первичная настройка
  Нужны две вещи: токен бота и название этого ПК.
  Остальное программа сделает сама.
============================================================
"""


def _tg_get_me(token: str) -> tuple[bool | None, str]:
    """Проверка токена через getMe. None в первом поле - сеть недоступна."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            return False, str(payload.get("description") or f"HTTP {exc.code}")
        except Exception:
            return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

    if data.get("ok"):
        return True, str((data.get("result") or {}).get("username") or "")
    return False, str(data.get("description") or "Telegram отклонил токен")


def _upsert_env(values: dict[str, str]) -> Path:
    """Записать ключи в .env, не трогая остальные строки."""
    path = APP_DIR / ".env"
    lines: list[str] = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []

    pending = set(values)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                out.append(f"{key}={values[key]}")
                pending.discard(key)
                continue
        out.append(line)
    for key in values:
        if key in pending:
            out.append(f"{key}={values[key]}")

    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return path


def _install_autostart() -> tuple[bool, str]:
    """Ярлык в автозагрузке: на exe или на pythonw + bot.py."""
    if sys.platform != "win32":
        return False, "автозапуск настраивается только в Windows"

    if getattr(sys, "frozen", False):
        target = str(Path(sys.executable).resolve())
        arguments = ""
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        target = str(pythonw if pythonw.exists() else Path(sys.executable))
        arguments = f'"{Path(__file__).resolve()}"'

    startup = (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft/Windows/Start Menu/Programs/Startup"
    )
    if not startup.parent.exists():
        return False, "не найдена папка автозагрузки"

    link = startup / "ControlBot.lnk"

    def esc(value: str) -> str:
        return value.replace("'", "''")

    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{esc(str(link))}'); "
        f"$s.TargetPath = '{esc(target)}'; "
        f"$s.Arguments = '{esc(arguments)}'; "
        f"$s.WorkingDirectory = '{esc(str(APP_DIR))}'; "
        "$s.WindowStyle = 7; $s.Description = 'ControlBot'; $s.Save()"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    if completed.returncode == 0:
        return True, str(link)
    detail = (completed.stderr or completed.stdout).strip()
    return False, detail or f"код {completed.returncode}"


def setup_wizard(force: bool = False) -> bool:
    """Первичная настройка: токен, имя ПК, .env и автозапуск."""
    print(SETUP_BANNER)
    if not force and os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return True

    token = ""
    while True:
        try:
            raw = getpass.getpass("Токен бота от @BotFather (ввод скрыт): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nНастройка прервана.")
            return False

        if not raw:
            print("  Токен не введён.")
            continue

        ok, info = _tg_get_me(raw)
        if ok:
            token = raw
            print(f"  Токен принят: @{info}")
            break
        if ok is None:
            print(f"  Не удалось проверить токен (сеть): {info}")
            if input("  Сохранить без проверки? [y/N]: ").strip().lower() == "y":
                token = raw
                break
            continue

        print(f"  Telegram не принял токен: {info}")

    default_pc = os.environ.get("COMPUTERNAME") or socket.gethostname()
    try:
        pc_name = input(f"Название ПК [{default_pc}]: ").strip() or default_pc
        club_name = input("Клуб / подразделение [без названия]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nНастройка прервана.")
        return False

    values = {
        "TELEGRAM_BOT_TOKEN": token,
        "PC_NAME": pc_name,
        "CLUB_NAME": club_name,
        "ALLOWED_CHAT_IDS": os.getenv("ALLOWED_CHAT_IDS", "").strip() or "2079052013",
        "ACTIVITY_ENABLED": "1",
        "ACTIVITY_TRACK_TITLES": "1",
        "ACTIVITY_SUMMARY_ENABLED": "1",
        "ACTIVITY_SUMMARY_HOUR": "21",
        "BROWSER_SITES_ENABLED": "1",
    }
    env_path = _upsert_env(values)
    os.environ.update(values)
    print(f"\n  Конфигурация сохранена: {env_path}")

    if sys.platform == "win32":
        try:
            answer = input("Добавить в автозапуск при входе? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "y", "yes", "д", "да"):
            ok, detail = _install_autostart()
            print(f"  Автозапуск: {'OK - ' + detail if ok else 'не настроен - ' + detail}")

    print("\nГотово. Запускаю бота...\n")
    return True


def run_gui_wizard() -> bool:
    """Оконная настройка (tkinter). False — окно недоступно или отменено."""
    try:
        from setup_gui import run_setup
    except Exception as exc:  # noqa: BLE001
        LOGGER.info("Оконная настройка недоступна: %s", exc)
        return False

    data = run_setup(
        default_pc=os.environ.get("COMPUTERNAME") or socket.gethostname(),
        default_club=os.getenv("CLUB_NAME", ""),
    )
    if not data:
        return False

    values = {
        "TELEGRAM_BOT_TOKEN": data["token"],
        "PC_NAME": data["pc_name"],
        "CLUB_NAME": data["club_name"],
        "ALLOWED_CHAT_IDS": os.getenv("ALLOWED_CHAT_IDS", "").strip() or "2079052013",
        "ACTIVITY_ENABLED": "1",
        "ACTIVITY_TRACK_TITLES": "1",
        "ACTIVITY_SUMMARY_ENABLED": "1",
        "ACTIVITY_SUMMARY_HOUR": "21",
        "BROWSER_SITES_ENABLED": "1",
    }
    _upsert_env(values)
    os.environ.update(values)

    if data.get("autostart") and sys.platform == "win32":
        ok, detail = _install_autostart()
        LOGGER.info("Автозапуск: %s (%s)", ok, detail)
    return True


def main() -> None:
    if "--no-setup" not in sys.argv:
        has_token = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
        if not has_token:
            load_env_file(APP_DIR / ".env")
            has_token = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())

        if "--setup" in sys.argv or not has_token:
            done = False
            if getattr(sys, "frozen", False) or "--gui" in sys.argv:
                done = run_gui_wizard()
            if not done:
                done = setup_wizard(force="--setup" in sys.argv)
            if not done:
                raise SystemExit("Не настроен токен бота - запуск остановлен.")

    config = load_config()
    if not acquire_instance_lock():
        return

    LOGGER.info(
        "Запуск ControlBot. Клуб: %s, ПК: %s, разрешённые chat id: %s",
        config.club_name or "-",
        config.pc_name or "-",
        list(config.allowed_chat_ids) or config.allowed_chat_id,
    )
    LOGGER.info("Опциональные модули - psutil: %s, pyperclip: %s, cv2: %s", 
                HAS_PSUTIL, HAS_PYPERCLIP, HAS_CV2)
    
    application = build_application(config)
    application.run_polling()


if __name__ == "__main__":
    main()
