"""构建前环境预检（文档 3.6 / 7 章 Build-Env-Checker）。

四项阻断 + 两项告警。任一 block 存在即不允许进入备份与烧录。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent

# 本机已知的 IDF 安装点（文档未规定路径，按真实环境探测）
_CANDIDATE_IDF = (
    "/Users/xiaoyi/esp/v5.5.6/esp-idf",
    os.path.expanduser("~/esp/esp-idf"),
    os.path.expanduser("~/esp/v5.5.6/esp-idf"),
)
_CANDIDATE_VENV = (
    os.path.expanduser("~/.espressif/tools/python/v5.5.6/venv/bin/python"),
    os.path.expanduser("~/.espressif/python_env/idf5.5_py3.12_env/bin/python"),
    os.path.expanduser("~/.espressif/python_env/idf5.5_py3.9_env/bin/python"),
)


@dataclass
class Block:
    code: str
    message: str          # 中文，直接给前端展示
    action: str = ""      # 可执行建议

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "action": self.action}


@dataclass
class EnvReport:
    ok: bool = True
    blocks: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    idf_path: str = ""
    idf_py: str = ""
    idf_version: str = ""
    venv_python: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "blocks": [b.to_dict() for b in self.blocks],
            "warnings": self.warnings,
            "idf_path": self.idf_path,
            "idf_py": self.idf_py,
            "idf_version": self.idf_version,
            "venv_python": self.venv_python,
        }


def find_idf() -> str:
    env = os.environ.get("IDF_PATH", "")
    if env and Path(env, "tools", "idf.py").is_file():
        return env
    for cand in _CANDIDATE_IDF:
        if Path(cand, "tools", "idf.py").is_file():
            return cand
    return ""


def find_venv_python(idf_path: str) -> str:
    env = os.environ.get("IDF_PYTHON_ENV_PATH", "")
    if env and Path(env, "bin", "python").is_file():
        return str(Path(env, "bin", "python"))
    for cand in _CANDIDATE_VENV:
        if Path(cand).is_file():
            return cand
    for cand in sorted(glob.glob(os.path.expanduser("~/.espressif/python_env/idf5.5_py*/bin/python"))):
        if Path(cand).is_file():
            return cand
    return ""


def _read_version(idf_path: str) -> str:
    vf = Path(idf_path, "version.txt")
    try:
        return vf.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def idf_version(idf_py: str, venv: str, idf_path: str) -> tuple[str, str]:
    """返回 (版本字符串, 原始报错)。"""
    text = _read_version(idf_path)
    if text:
        return text, ""
    if not venv:
        return "", "未找到 IDF python 虚拟环境"
    try:
        out = subprocess.run([venv, idf_py, "--version"], capture_output=True,
                             text=True, timeout=90).stdout
        m = re.search(r"v[\d.]+", out)
        return (m[0] if m else out.strip()[:80]), ""
    except Exception as exc:  # noqa: BLE001
        return "", f"idf.py --version 执行失败：{exc}"


def is_offline() -> bool:
    for host, port in (("127.0.0.1", 53), ("components.espressif.com", 443)):
        try:
            socket.create_connection((host, port), timeout=2).close()
            if host != "127.0.0.1":
                return False
        except OSError:
            if host != "127.0.0.1":
                return True
    return True


def check(project, offline: bool | None = None) -> EnvReport:
    """project: project_importer.ImportedProject"""
    rep = EnvReport()

    if not project.ok or project.kind != "idf":
        rep.blocks.append(Block("E_PROJECT", project.error or "工程不可用"))
        rep.ok = False
        return rep

    idf_path = find_idf()
    if not idf_path:
        rep.blocks.append(Block(
            "E_NO_IDF", "未检测到 ESP-IDF 环境，请先安装或使用「直烧 bin」模式",
            "安装 ESP-IDF 5.x 后设置 IDF_PATH，或改用单 bin 直烧模式"))
        rep.ok = False
        return rep
    rep.idf_path = idf_path
    rep.idf_py = str(Path(idf_path, "tools", "idf.py"))
    rep.venv_python = find_venv_python(idf_path)
    rep.idf_version, verr = idf_version(rep.idf_py, rep.venv_python, idf_path)
    if verr:
        rep.blocks.append(Block("E_IDF_BROKEN", f"ESP-IDF 环境不可用：{verr}"))
        rep.ok = False
        return rep

    if project.target and project.target != "esp32s3":
        rep.blocks.append(Block(
            "E_TARGET", "目标芯片不匹配（当前 esp32s3）",
            f"该工程 target={project.target}，本工具只支持 esp32s3"))
    if not project.target:
        rep.warnings.append("工程未声明 target，将在编译阶段由 ESP-IDF 决定")

    if offline is None:
        offline = is_offline()
    if project.has_component_yml and offline:
        rep.blocks.append(Block(
            "E_OFFLINE_DEPS", "该工程需要联网拉取组件依赖，请联网后重试",
            "离线状态下无法下载 managed components"))

    need = 2 * 1024 * 1024 * 1024
    free = shutil.disk_usage(APP_ROOT).free
    if free < need:
        rep.blocks.append(Block(
            "E_DISK", f"磁盘空间不足（需 2 GB，当前 {free / 1024 ** 3:.1f} GB）"))

    if project.idf_version and rep.idf_version:
        a = re.findall(r"\d+", project.idf_version)
        b = re.findall(r"\d+", rep.idf_version)
        if a and b and a[:2] != b[:2]:
            rep.warnings.append(
                f"工程 IDF 版本 {project.idf_version} 与本机 {rep.idf_version} 不一致，"
                "可能出现 API 不兼容，编译失败会回显原始报错（不自动改工程配置）")

    rep.ok = not rep.blocks
    return rep


if __name__ == "__main__":
    import json as _json
    import sys
    sys.path.insert(0, str(APP_ROOT / "backend"))
    from project_importer import import_project
    p = import_project(sys.argv[1])
    print(_json.dumps(check(p).to_dict(), ensure_ascii=False, indent=2))
