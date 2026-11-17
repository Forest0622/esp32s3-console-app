"""开发板拔出满 1 小时后清理构建缓存与旧工程。

清理范围只限 App 自己的数据目录：
- data/workspace/ 下的编译副本
- data/projects_imported/ 下的解压工程
- 旧版本留在 Application Support 根目录的 projects_imported/
- backups/ 下的遗留分块临时文件

不动用户原始工程，不动 backups 里的正式镜像与索引，不动端口记录。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

GRACE = 3600
STATE_NAME = "cleanup_state.json"


def _home(data_home: Path | str | None = None) -> Path:
    if data_home is not None:
        return Path(data_home)
    env = os.environ.get("CONSOLE_APP_HOME")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def _legacy_imports(home: Path) -> Path | None:
    parent = home.parent
    if parent.name == "ESP32-S3-Console-App" and home.name == "data":
        return parent / "projects_imported"
    return None


def _size(path: Path) -> int:
    total = 0
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).lstat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _remove(path: Path) -> tuple[bool, int]:
    size = _size(path)
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            return False, 0
        return True, size
    except OSError:
        return False, 0


def _clear_children(directory: Path) -> tuple[int, int]:
    removed = 0
    freed = 0
    if not directory.is_dir():
        return removed, freed
    for child in list(directory.iterdir()):
        ok, size = _remove(child)
        if ok:
            removed += 1
            freed += size
    return removed, freed


def purge_caches(data_home: Path | str | None = None) -> dict:
    """删除缓存与旧工程，返回删除数量与释放字节数。"""
    home = _home(data_home)
    result = {
        "workspace": 0,
        "projects": 0,
        "legacy_projects": 0,
        "temp_files": 0,
        "bytes": 0,
    }

    for key, directory in (
        ("workspace", home / "workspace"),
        ("projects", home / "projects_imported"),
        ("legacy_projects", _legacy_imports(home)),
    ):
        if directory is None:
            continue
        count, freed = _clear_children(directory)
        result[key] += count
        result["bytes"] += freed

    backup_dir = home / "backups"
    if backup_dir.is_dir():
        for path in list(backup_dir.glob(".chunk_*.tmp*")):
            ok, size = _remove(path)
            if ok:
                result["temp_files"] += 1
                result["bytes"] += size

    return result


def _state_path(home: Path) -> Path:
    return home / STATE_NAME


def _load_state(home: Path) -> dict:
    try:
        return json.loads(_state_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(home: Path, state: dict) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        path = _state_path(home)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def deadline(data_home: Path | str | None = None) -> float | None:
    """返回本轮拔板清理的到期时间；已清过或没有拔板记录时返回 None。"""
    home = _home(data_home)
    state = _load_state(home)
    last_present = float(state.get("last_present_at") or 0)
    if last_present <= 0:
        return None
    due = last_present + GRACE
    if float(state.get("last_cleanup_at") or 0) >= due:
        return None
    return due


def maybe_cleanup(present: bool, data_home: Path | str | None = None,
                  now: float | None = None) -> dict | None:
    """记录插拔状态；板子不在且满 1 小时时执行一次清理。

    present 由调用方按系统里是否存在任意串口设备判定。只要板子插着，
    就不断刷新 last_present_at，不会误删。
    """
    home = _home(data_home)
    state = _load_state(home)
    now = time.time() if now is None else float(now)

    if present:
        state["last_present_at"] = now
        state["cleanup_due_at"] = None
        _save_state(home, state)
        return None

    last_present = float(state.get("last_present_at") or 0)
    if last_present <= 0:
        # 第一次运行且没插板：从本次启动开始计时，不立刻清空旧数据。
        state["last_present_at"] = now
        state["cleanup_due_at"] = now + GRACE
        _save_state(home, state)
        return None

    due = last_present + GRACE
    state["cleanup_due_at"] = due
    if now < due:
        _save_state(home, state)
        return None
    if float(state.get("last_cleanup_at") or 0) >= due:
        _save_state(home, state)
        return None

    result = purge_caches(home)
    state["last_cleanup_at"] = now
    state["cleanup_due_at"] = None
    state["last_result"] = result
    _save_state(home, state)
    return result
