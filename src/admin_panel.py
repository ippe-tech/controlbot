#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""admin_panel.py — inline-панель администратора ControlBot.

/admin (или /panel) — панель: заявки, пользователи, выдача по ID, статистика.
"""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import access_store

LOGGER = logging.getLogger("controlbot.panel")


def _store():
    return access_store.get_store()


def _esc(value: Any) -> str:
    return escape(str(value if value is not None else ""))


def _kb_back(target: str = "admin:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=target)]])


def _panel_text(store) -> str:
    st = store.stats()
    return (
        "🛠 <b>Панель управления доступом</b>\n\n"
        f"👑 Администраторов: <b>{st['admins']}</b>\n"
        f"✅ С доступом: <b>{st['allowed']}</b>\n"
        f"🕐 Заявок: <b>{st['pending']}</b>\n"
        f"🚫 Отозвано: <b>{st['revoked']}</b>\n\n"
        "Выберите раздел 👇"
    )


def _kb_main(store) -> InlineKeyboardMarkup:
    st = store.stats()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"🕐 Заявки ({st['pending']})", callback_data="admin:req"),
                InlineKeyboardButton(f"👥 Пользователи ({st['allowed']})", callback_data="admin:users"),
            ],
            [
                InlineKeyboardButton("➕ Выдать по ID", callback_data="admin:howto"),
                InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
            ],
            [InlineKeyboardButton("🔄 Обновить", callback_data="admin:main")],
        ]
    )


def _users_view(store) -> tuple[str, InlineKeyboardMarkup]:
    items = store.allowed_items()
    if not items:
        return "👥 Пользователей с доступом пока нет.", _kb_back()
    rows = []
    for uid, info in items:
        mark = "👑" if store.is_owner(uid) else ("🛡" if store.is_admin(uid) else "✅")
        label = info.get("name") or info.get("username") or str(uid)
        if info.get("username"):
            label = f"{label} @{info['username']}"
        rows.append([InlineKeyboardButton(f"{mark} {label}"[:60], callback_data=f"admin:u:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:main")])
    return f"👥 <b>Пользователи ({len(items)})</b>\n\nНажмите, чтобы управлять.", InlineKeyboardMarkup(rows)


def _user_view(store, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    info = store.data["allowed"].get(str(uid))
    if not info:
        return f"❓ Пользователь <code>{uid}</code> больше не имеет доступа.", _kb_back("admin:users")

    lines = ["👤 <b>Карточка пользователя</b>", "", f"🆔 ID: <code>{uid}</code>"]
    if info.get("name"):
        lines.append(f"📝 Имя: {_esc(info['name'])}")
    if info.get("username"):
        lines.append(f"🔗 Ник: @{_esc(info['username'])}")
    lines.append(f"🕒 Выдан: {_esc(info.get('granted_at', '—'))}")
    if store.is_owner(uid):
        lines.append("\n👑 Владелец — права неизменяемы.")
    elif store.is_admin(uid):
        lines.append("\n🛡 Администратор.")

    rows: list[list[InlineKeyboardButton]] = []
    if not store.is_owner(uid):
        if store.is_admin(uid):
            rows.append([InlineKeyboardButton("⬇️ Снять админа", callback_data=f"admin:dm:{uid}")])
        else:
            rows.append([InlineKeyboardButton("🛡 Сделать админом", callback_data=f"admin:mk:{uid}")])
        rows.append([InlineKeyboardButton("🚫 Отозвать доступ", callback_data=f"admin:rv:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:users")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _requests_view(store) -> tuple[str, InlineKeyboardMarkup]:
    pending = store.pending_items()
    if not pending:
        return "🕐 Новых заявок нет.", _kb_back()
    rows = []
    for uid, info in pending:
        label = info.get("name") or str(uid)
        if info.get("username"):
            label = f"{label} @{info['username']}"
        rows.append([InlineKeyboardButton(f"🆕 {label}"[:60], callback_data=f"admin:r:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:main")])
    return f"🕐 <b>Заявки ({len(pending)})</b>", InlineKeyboardMarkup(rows)


def _request_view(store, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    info = store.data["pending"].get(str(uid))
    if not info:
        return f"❓ Заявка от <code>{uid}</code> уже обработана.", _kb_back("admin:req")
    lines = ["🆕 <b>Заявка на доступ</b>", "", f"🆔 ID: <code>{uid}</code>"]
    if info.get("name"):
        lines.append(f"📝 Имя: {_esc(info['name'])}")
    if info.get("username"):
        lines.append(f"🔗 Ник: @{_esc(info['username'])}")
    lines.append(f"🕒 Отправлена: {_esc(info.get('at', '—'))}")
    rows = [
        [
            InlineKeyboardButton("✅ Выдать доступ", callback_data=f"admin:ap:{uid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:dj:{uid}"),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin:req")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


_HOWTO = (
    "➕ <b>Как выдать доступ</b>\n\n"
    "1️⃣ Человек пишет боту /start — вам приходит заявка с кнопками ✅/❌.\n\n"
    "2️⃣ Или выдайте сразу по ID:\n"
    "<code>/grant 123456789</code>\n\n"
    "ID человек узнаёт командой /id в боте."
)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = _store()
    user = update.effective_user
    message = update.effective_message
    if store is None or user is None or message is None:
        return
    if not store.is_admin(user.id):
        await message.reply_text("⛔ Панель доступна только администраторам.")
        return
    await message.reply_html(_panel_text(store), reply_markup=_kb_main(store))


async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    store = _store()
    if query is None or user is None or store is None:
        return
    if not store.is_admin(user.id):
        await query.answer("⛔ Только для администраторов.", show_alert=True)
        return

    try:
        await query.answer()
    except TelegramError:
        pass

    parts = (query.data or "").split(":")
    route = parts[1] if len(parts) > 1 else "main"
    arg = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

    try:
        if route == "users":
            text, kb = _users_view(store)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif route == "u" and arg is not None:
            text, kb = _user_view(store, arg)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif route == "req":
            text, kb = _requests_view(store)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif route == "r" and arg is not None:
            text, kb = _request_view(store, arg)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif route == "ap" and arg is not None:
            store.grant(arg, by=user.id)
            await query.edit_message_text(
                f"✅ Доступ выдан: <code>{arg}</code>", parse_mode="HTML",
                reply_markup=_kb_back("admin:req"),
            )
            try:
                await context.bot.send_message(arg, "✅ Вам выдали доступ к ControlBot. Отправьте /start.")
            except TelegramError:
                pass
        elif route == "dj" and arg is not None:
            store.reject(arg)
            await query.edit_message_text(
                f"❌ Заявка отклонена: <code>{arg}</code>", parse_mode="HTML",
                reply_markup=_kb_back("admin:req"),
            )
        elif route == "rv" and arg is not None:
            ok = store.revoke(arg)
            text = f"🚫 Доступ отозван: <code>{arg}</code>" if ok else f"❓ Нет доступа: <code>{arg}</code>"
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=_kb_back("admin:users"))
        elif route == "mk" and arg is not None:
            store.make_admin(arg)
            await query.edit_message_text(
                f"🛡 Назначен администратором: <code>{arg}</code>", parse_mode="HTML",
                reply_markup=_kb_back("admin:users"),
            )
        elif route == "dm" and arg is not None:
            store.demote_admin(arg)
            await query.edit_message_text(
                f"⬇️ Снят администратор: <code>{arg}</code>", parse_mode="HTML",
                reply_markup=_kb_back("admin:users"),
            )
        elif route == "stats":
            st = store.stats()
            await query.edit_message_text(
                "📊 <b>Статистика</b>\n\n"
                f"👑 Админов: {st['admins']}\n✅ С доступом: {st['allowed']}\n"
                f"🕐 Заявок: {st['pending']}\n🚫 Отозвано: {st['revoked']}",
                parse_mode="HTML", reply_markup=_kb_back(),
            )
        elif route == "howto":
            await query.edit_message_text(_HOWTO, parse_mode="HTML", reply_markup=_kb_back())
        else:
            await query.edit_message_text(
                _panel_text(store), parse_mode="HTML", reply_markup=_kb_main(store)
            )
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            LOGGER.warning("Ошибка панели: %s", exc)


def register(application) -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler

    application.add_handler(CommandHandler(["admin", "panel"], cmd_admin))
    application.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^admin:"))
