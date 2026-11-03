#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""access_store.py — выдача доступа к боту без правки .env.

Хранит owner / allowed / pending в JSON рядом с exe. Владелец — первый
chat_id из ALLOWED_CHAT_IDS. Новый пользователь пишет боту → попадает в
pending, владельцу приходит заявка с кнопками ✅/❌.

Команды: /access (список), /grant <id>, /revoke <id>.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("controlbot.access")

_STORE: "AccessStore | None" = None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class AccessStore:
    def __init__(self, path: Path, owner_id: int | None) -> None:
        self.path = path
        self.data: dict[str, Any] = {"owner": owner_id, "admins": [], "allowed": {}, "pending": {}, "revoked": {}}
        self._load()
        if owner_id:
            self.data["owner"] = int(owner_id)
            key = str(owner_id)
            if key not in self.data["allowed"]:
                self.data["allowed"][key] = {
                    "name": "Владелец",
                    "username": "",
                    "granted_at": _now(),
                    "granted_by": None,
                }
                self._save()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Файл доступов повреждён, создаю заново.")
            return
        if isinstance(raw, dict):
            self.data = {
                "owner": raw.get("owner"),
                "admins": [int(x) for x in (raw.get("admins") or []) if str(x).lstrip("-").isdigit()],
                "allowed": {str(k): v for k, v in (raw.get("allowed") or {}).items()},
                "pending": {str(k): v for k, v in (raw.get("pending") or {}).items()},
                "revoked": {str(k): v for k, v in (raw.get("revoked") or {}).items()},
            }

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            LOGGER.warning("Не удалось сохранить доступы: %s", exc)

    @property
    def owner_id(self) -> int | None:
        try:
            return int(self.data.get("owner")) if self.data.get("owner") else None
        except (TypeError, ValueError):
            return None

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id == self.owner_id

    def is_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return self.is_owner(user_id) or int(user_id) in self.data.get("admins", [])

    def is_allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        if self.is_admin(user_id):
            return True
        return str(user_id) in self.data["allowed"]

    def make_admin(self, user_id: int) -> bool:
        if self.is_owner(user_id):
            return False
        if not self.is_allowed(user_id):
            self.grant(user_id)
        if user_id not in self.data.setdefault("admins", []):
            self.data["admins"].append(user_id)
            self._save()
            return True
        return False

    def demote_admin(self, user_id: int) -> bool:
        admins = self.data.setdefault("admins", [])
        if user_id in admins and not self.is_owner(user_id):
            admins.remove(user_id)
            self._save()
            return True
        return False

    def stats(self) -> dict[str, int]:
        return {
            "admins": len(self.data.get("admins", [])),
            "allowed": len(self.data.get("allowed", {})),
            "pending": len(self.data.get("pending", {})),
            "revoked": len(self.data.get("revoked", {})),
        }

    def is_pending(self, user_id: int) -> bool:
        return str(user_id) in self.data["pending"]

    def add_pending(self, user_id: int, username: str = "", name: str = "") -> bool:
        key = str(user_id)
        if self.is_allowed(user_id) or key in self.data["pending"]:
            return False
        self.data["pending"][key] = {"username": username, "name": name, "at": _now()}
        self._save()
        return True

    def grant(self, user_id: int, by: int | None = None, name: str = "") -> bool:
        key = str(user_id)
        if key in self.data["allowed"]:
            return False
        info = self.data["pending"].pop(key, {})
        self.data["allowed"][key] = {
            "name": name or info.get("name", ""),
            "username": info.get("username", ""),
            "granted_at": _now(),
            "granted_by": by,
        }
        self._save()
        return True

    def revoke(self, user_id: int) -> bool:
        key = str(user_id)
        if self.is_owner(user_id) or key not in self.data["allowed"]:
            return False
        info = self.data["allowed"].pop(key)
        self.data.setdefault("revoked", {})[key] = {**info, "revoked_at": _now()}
        if user_id in self.data.get("admins", []):
            self.data["admins"].remove(user_id)
        self._save()
        return True

    def reject(self, user_id: int) -> bool:
        return self.data["pending"].pop(str(user_id), None) is not None and self._save() is None

    def allowed_items(self) -> list[tuple[int, dict[str, Any]]]:
        items: list[tuple[int, dict[str, Any]]] = []
        for key, info in self.data["allowed"].items():
            try:
                items.append((int(key), info))
            except ValueError:
                continue
        return sorted(items, key=lambda kv: (not self.is_owner(kv[0]), kv[0]))

    def pending_items(self) -> list[tuple[int, dict[str, Any]]]:
        items: list[tuple[int, dict[str, Any]]] = []
        for key, info in self.data["pending"].items():
            try:
                items.append((int(key), info))
            except ValueError:
                continue
        return items


def init_store(path: Path, owner_id: int | None) -> AccessStore:
    global _STORE
    _STORE = AccessStore(path, owner_id)
    return _STORE


def get_store() -> AccessStore | None:
    return _STORE
