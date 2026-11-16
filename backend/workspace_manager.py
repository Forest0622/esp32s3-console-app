"""工作区管理（文档 3.3 / 7 章 Workspace-Manager）。

原始工程目录只读；每次烧录用全新副本，不复用旧 build/。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

APP_ROOT = Path(os.environ.get("CONSOLE_APP_HOME")
                or Path(__file__).resolve().parent.parent)
WORKSPACE = APP_ROOT / "workspace"

_IGNORE = shutil.ignore_patterns(
    "build", ".git", "managed_components", ".vscode", "__pycache__", "*.o",
)


def _safe(name: str) -> str:
    keep = [c for c in name if c.isalnum() or c in "-_."]
    out = "".join(keep)[:40].strip("._")
    return out or "project"


def prepare_copy(project_root: str, chip_id: str) -> str:
    """把工程复制到 workspace/<chip_id>/<工程名>_<时间戳>/，返回副本路径。"""
    src = Path(project_root)
    if not src.is_dir():
        raise ValueError(f"工程根不存在：{project_root}")
    if chip_id is None:
        chip_id = "unknown"
    safe_chip = _safe(chip_id.replace(":", "").lower())
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest_parent = WORKSPACE / safe_chip
    dest = dest_parent / f"{_safe(src.name)}_{stamp}"
    dest_parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    def _copy(s: str, d: str) -> None:
        try:
            shutil.copy2(s, d)
        except (OSError, shutil.SameFileError):
            pass

    shutil.copytree(src, dest, ignore=_IGNORE, symlinks=False, copy_function=_copy)
    return str(dest)


def list_workspaces() -> list[dict]:
    out: list[dict] = []
    if not WORKSPACE.is_dir():
        return out
    for chip_dir in sorted(WORKSPACE.iterdir()):
        if not chip_dir.is_dir():
            continue
        for d in sorted(chip_dir.iterdir()):
            if d.is_dir():
                out.append({
                    "path": str(d),
                    "chip_id": chip_dir.name,
                    "size": _size(d),
                    "mtime": d.stat().st_mtime,
                })
    return out


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def cleanup(max_gb: float = 10.0) -> int:
    """按时间从旧到新删，直到占用低于 max_gb。返回释放字节数。"""
    items = list_workspaces()
    total = sum(i["size"] for i in items)
    if total <= max_gb * 1024 ** 3:
        return 0
    freed = 0
    for item in sorted(items, key=lambda i: i["mtime"]):
        if total - freed <= max_gb * 1024 ** 3:
            break
        try:
            shutil.rmtree(item["path"], ignore_errors=True)
            freed += item["size"]
        except OSError:
            pass
    return freed


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) >= 3:
        print(prepare_copy(sys.argv[1], sys.argv[2]))
    else:
        print(json.dumps(list_workspaces(), ensure_ascii=False, indent=2))
