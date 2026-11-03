#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_gui.py — окно первичной настройки ControlBot (tkinter).

Поля: токен бота (скрытый ввод + показать), название ПК, клуб.
Кнопки: «Проверить токен», «Сохранить и запустить», «Отмена».
Возвращает словарь с настройками или None.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request

import tkinter as tk
from tkinter import messagebox, ttk


def _get_me(token: str) -> tuple[bool | None, str]:
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


def run_setup(default_pc: str = "", default_club: str = "") -> dict | None:
    """Показать окно настройки. Вернуть настройки или None при отмене."""
    result: dict | None = None

    root = tk.Tk()
    root.title("ControlBot — первичная настройка")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass

    frame = ttk.Frame(root, padding=18)
    frame.grid(row=0, column=0, sticky="nsew")

    ttk.Label(
        frame,
        text="Настройка ControlBot",
        font=("Segoe UI", 14, "bold"),
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

    ttk.Label(
        frame,
        text="Нужны токен бота и название этого ПК. Остальное программа сделает сама.",
        wraplength=460,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 14))

    # --- токен -----------------------------------------------------------
    ttk.Label(frame, text="Токен бота (от @BotFather):").grid(
        row=2, column=0, columnspan=3, sticky="w"
    )
    token_var = tk.StringVar()
    token_entry = ttk.Entry(frame, textvariable=token_var, width=52, show="•")
    token_entry.grid(row=3, column=0, columnspan=2, sticky="we", pady=(4, 6))

    show_var = tk.BooleanVar(value=False)

    def toggle_token() -> None:
        token_entry.configure(show="" if show_var.get() else "•")

    ttk.Checkbutton(
        frame, text="Показать", variable=show_var, command=toggle_token
    ).grid(row=3, column=2, sticky="w", padx=(8, 0))

    # --- имя ПК и клуб ----------------------------------------------------
    ttk.Label(frame, text="Название ПК:").grid(row=4, column=0, sticky="w", pady=(6, 0))
    pc_var = tk.StringVar(value=default_pc or os.environ.get("COMPUTERNAME") or socket.gethostname())
    ttk.Entry(frame, textvariable=pc_var, width=30).grid(
        row=5, column=0, sticky="w", pady=(4, 6)
    )

    ttk.Label(frame, text="Клуб / подразделение:").grid(
        row=4, column=1, columnspan=2, sticky="w", pady=(6, 0)
    )
    club_var = tk.StringVar(value=default_club)
    ttk.Entry(frame, textvariable=club_var, width=30).grid(
        row=5, column=1, columnspan=2, sticky="w", pady=(4, 6)
    )

    autostart_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        frame,
        text="Добавить в автозапуск при входе в Windows",
        variable=autostart_var,
    ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 10))

    # --- статус -----------------------------------------------------------
    status_var = tk.StringVar(value="Вставьте токен и нажмите «Проверить токен».")
    status = ttk.Label(frame, textvariable=status_var, foreground="#555555", wraplength=460)
    status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 10))

    # --- кнопки -----------------------------------------------------------
    buttons = ttk.Frame(frame)
    buttons.grid(row=8, column=0, columnspan=3, sticky="e")

    check_btn = ttk.Button(buttons, text="Проверить токен")
    check_btn.grid(row=0, column=0, padx=(0, 8))

    start_btn = ttk.Button(buttons, text="Сохранить и запустить")
    start_btn.grid(row=0, column=1, padx=(0, 8))

    cancel_btn = ttk.Button(buttons, text="Отмена")
    cancel_btn.grid(row=0, column=2)

    state = {"token_ok": False, "busy": False}

    def set_status(text: str, color: str = "#555555") -> None:
        status_var.set(text)
        status.configure(foreground=color)

    def check_token_async() -> None:
        if state["busy"]:
            return
        token = token_var.get().strip()
        if not token:
            set_status("Введите токен бота.", "#b00020")
            return

        state["busy"] = True
        check_btn.configure(state="disabled")
        start_btn.configure(state="disabled")
        set_status("Проверяю токен…", "#555555")

        def worker() -> None:
            ok, info = _get_me(token)
            root.after(0, lambda: finish_check(ok, info))

        threading.Thread(target=worker, daemon=True).start()

    def finish_check(ok: bool | None, info: str) -> None:
        state["busy"] = False
        check_btn.configure(state="normal")
        start_btn.configure(state="normal")
        if ok:
            state["token_ok"] = True
            set_status(f"Токен принят: @{info}", "#0a7a28")
        elif ok is None:
            state["token_ok"] = False
            set_status(f"Не удалось проверить (сеть): {info}", "#b06a00")
        else:
            state["token_ok"] = False
            set_status(f"Telegram не принял токен: {info}", "#b00020")

    def save() -> None:
        nonlocal result
        token = token_var.get().strip()
        pc = pc_var.get().strip()
        club = club_var.get().strip()

        if not token:
            set_status("Введите токен бота.", "#b00020")
            token_entry.focus_set()
            return
        if not pc:
            set_status("Введите название ПК.", "#b00020")
            return

        if not state["token_ok"]:
            if not messagebox.askyesno(
                "Токен не проверен",
                "Токен ещё не проверен. Сохранить без проверки?",
                parent=root,
            ):
                return

        result = {
            "token": token,
            "pc_name": pc,
            "club_name": club,
            "autostart": bool(autostart_var.get()),
        }
        root.destroy()

    def cancel() -> None:
        root.destroy()

    check_btn.configure(command=check_token_async)
    start_btn.configure(command=save)
    cancel_btn.configure(command=cancel)
    root.bind("<Return>", lambda _event: save())
    root.bind("<Escape>", lambda _event: cancel())

    token_entry.focus_set()
    root.update_idletasks()
    width = root.winfo_width()
    height = root.winfo_height()
    x = (root.winfo_screenwidth() - width) // 2
    y = (root.winfo_screenheight() - height) // 3
    root.geometry(f"+{x}+{y}")

    root.mainloop()
    return result
