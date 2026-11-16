"""IDF 编译调度（文档 3.3 / 3.7 / 7 章 IDF-Build-Scheduler）。

在工作区副本内编译；流式解析 idf.py 输出做进度；失败保留最后 200 行。
"""

from __future__ import annotations

import os
import re
import subprocess

import cancel_token
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import flash_controller
import build_env_checker

ACTIVATE_SCRIPT = "/Users/xiaoyi/.espressif/tools/activate_idf_v5.5.6.sh"
LOG_TAIL_LINES = 200
TIMEOUT = 900

_PROGRESS = re.compile(r"\[\s*(\d+)/(\d+)\]\s")


@dataclass
class BuildResult:
    ok: bool = False
    bins: list[tuple[str, str]] = field(default_factory=list)
    log_tail: str = ""
    workspace: str = ""
    duration: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "bins": [{"offset": o, "path": p} for o, p in self.bins],
            "log_tail": self.log_tail,
            "workspace": self.workspace,
            "duration": round(self.duration, 1),
            "error": self.error,
        }


def _build_env(idf_path: str, extra_dirs: list[str] | None) -> dict:
    env = dict(os.environ)
    env["IDF_PATH"] = idf_path
    env["IDF_TARGET"] = "esp32s3"
    if extra_dirs:
        # 文档 3.5.1：只用 EXTRA_COMPONENT_DIRS 注入，不写用户 CMakeLists
        env["EXTRA_COMPONENT_DIRS"] = ";".join(str(d) for d in extra_dirs)
    return env


def build_project(work_copy: str,
                  progress_cb: Callable[[float, str], None] | None = None,
                  extra_dirs: list[str] | None = None,
                  idf_path: str | None = None) -> BuildResult:
    res = BuildResult(workspace=work_copy)
    root = Path(work_copy)
    if not root.is_dir():
        res.error = f"工作区副本不存在：{work_copy}"
        return res

    idf_path = idf_path or build_env_checker.find_idf()
    if not idf_path:
        res.error = "未检测到 ESP-IDF 环境"
        return res

    env = _build_env(idf_path, extra_dirs)
    # 组件目录必须用命令行 -D 传：EXTRA_COMPONENT_DIRS 只在工程 CMakeLists 里
    # 被读取，放在 env 里首次 CMake 配置阶段解析不到，表现为 4 秒内 rc=2 失败。
    defs = ""
    if extra_dirs:
        joined = ";".join(str(d) for d in extra_dirs)
        defs = f' -DEXTRA_COMPONENT_DIRS="{joined}"'
    cmd = ["/bin/bash", "-c",
           f'. "{ACTIVATE_SCRIPT}" >/dev/null 2>&1 && cd "{root}" && idf.py{defs} -DIDF_TARGET=esp32s3 build']

    started = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env, bufsize=1)
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            if len(tail) > LOG_TAIL_LINES:
                tail.pop(0)
            m = _PROGRESS.search(line)
            if m and progress_cb:
                done, total = int(m[1]), int(m[2])
                if total > 0:
                    progress_cb(min(done / total, 0.99), f"编译 {done}/{total}")
            if cancel_token.is_cancelled():
                proc.kill()
                res.error = "已停止编译"
                break
            if time.time() - started > TIMEOUT:
                proc.kill()
                res.error = "编译超时（15 分钟）"
                break
    finally:
        proc.wait(timeout=30)
    res.duration = time.time() - started
    res.log_tail = "\n".join(tail)

    if proc.returncode != 0:
        res.ok = False
        res.error = res.error or f"idf.py build 失败（返回码 {proc.returncode}）"
        return res

    bins = flash_controller.parse_flasher_args(str(root / "build"))
    if not bins:
        res.ok = False
        res.error = "编译成功但未解析到烧录清单（flasher_args.json / flash_args 均缺失）"
        return res

    res.ok = True
    res.bins = bins
    return res


if __name__ == "__main__":
    import json
    import sys
    r = build_project(sys.argv[1], progress_cb=lambda f, m: print(f"[{f:.0%}] {m}"))
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
