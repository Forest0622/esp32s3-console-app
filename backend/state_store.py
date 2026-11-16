"""本机状态存储（按芯片 ID 隔离的备份记录、板子首见时间、引脚映射）。

只做一件事：原子读写 state.json，避免半截文件导致下次启动解析失败。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading

import config

_lock = threading.RLock()
_DEFAULT = {"boards": {}, "pins": {}}


def load() -> dict:
    with _lock:
        try:
            if config.STATE_FILE.exists():
                data = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("boards", {})
                    data.setdefault("pins", {})
                    return data
        except Exception:
            pass
        return json.loads(json.dumps(_DEFAULT))


def save(data: dict) -> None:
    with _lock:
        config.ensure_dirs()
        fd, tmp = tempfile.mkstemp(dir=str(config.APP_DIR), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, config.STATE_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def board(chip_id: str) -> dict:
    data = load()
    return data["boards"].get(chip_id, {})


def set_board_field(chip_id: str, **fields) -> dict:
    with _lock:
        data = load()
        rec = data["boards"].setdefault(chip_id, {})
        rec.update(fields)
        save(data)
        return rec


def pins() -> dict:
    return load().get("pins", {})


def set_pins(mapping: dict) -> None:
    with _lock:
        data = load()
        data["pins"] = mapping
        save(data)
