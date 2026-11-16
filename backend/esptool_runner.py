"""在进程内调用 esptool（避免打包后找不到可执行文件），并捕获其输出。

注意：esptool 的所有 argv 必须全部是 str，混入 int 会在其参数处理里抛
`'int' object has no attribute 'startswith'`。
"""
from __future__ import annotations

import contextlib
import io
import re
import time

import esptool

import log_setup

log = log_setup.get_logger("esptool")

_PORT_BUSY_PAT = re.compile(r"could not (?:exclusively )?(?:open|lock) port|serial port busy|multiple access on port", re.I)
_MAC_PAT = re.compile(r"MAC:\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")


class EsptoolError(RuntimeError):
    pass


class _Tee(io.TextIOBase):
    """把 esptool 输出同时写入缓冲和逐行回调（用于给 UI 报进度）。"""

    def __init__(self, buf: io.StringIO, cb):
        self._buf = buf
        self._cb = cb
        self._tail = ""

    def write(self, s: str) -> int:
        self._buf.write(s)
        if self._cb:
            self._tail += s
            while True:
                idx = [i for i in (self._tail.find("\r"), self._tail.find("\n")) if i >= 0]
                if not idx:
                    break
                cut = min(idx)
                line = self._tail[:cut].strip()
                self._tail = self._tail[cut + 1:]
                if line:
                    try:
                        self._cb(line)
                    except Exception:
                        pass
        return len(s)

    def flush(self) -> None:
        pass


def run(argv: list[str], retries: int = 3, retry_delay: float = 2.0, line_cb=None) -> str:
    """执行一次 esptool 命令，返回捕获到的输出文本。

    argv 例如 ["--port", "/dev/cu.usbserial-140", "--baud", "460800",
              "write_flash", "0x10000", "app.bin"]
    """
    argv = [str(a) for a in argv]
    last_text = ""
    for attempt in range(1, retries + 1):
        buf = io.StringIO()
        rc = 0
        try:
            tee = _Tee(buf, line_cb)
            with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                esptool.main(argv)
        except SystemExit as e:           # esptool 失败时会 sys.exit(1/2)
            rc = int(getattr(e, "code", 1) or 0)
        except Exception as e:            # FatalError 等
            rc = 1
            buf.write(f"\n{type(e).__name__}: {e}\n")
        text = buf.getvalue()
        last_text = text
        if rc == 0 and not _has_fatal(text):
            return text
        if _PORT_BUSY_PAT.search(text) and attempt < retries:
            log.warning("port busy，%ss 后重试（第 %d 次）", retry_delay, attempt)
            time.sleep(retry_delay)
            continue
        if "chip stopped responding" in text.lower() and attempt < retries:
            log.warning("芯片停止响应，3s 后重试（第 %d 次）", attempt)
            time.sleep(3)
            continue
        raise EsptoolError(_short(text) or f"esptool 失败（退出码 {rc}）")
    raise EsptoolError(_short(last_text) or "esptool 重试后仍失败")


def read_mac(port: str, baud: int = 115200) -> str | None:
    """读取芯片 MAC，作为设备唯一 ID。读不到返回 None（上层降级处理）。"""
    try:
        text = run(["--port", port, "--baud", str(baud), "--connect-attempts", "3", "read_mac"])
    except EsptoolError as e:
        log.warning("读取 MAC 失败：%s", e)
        return None
    m = _MAC_PAT.search(text)
    if not m:
        log.warning("MAC 输出解析失败：%s", _short(text))
        return None
    mac = m.group(1).lower()
    return mac.replace(":", "")


def _has_fatal(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in ("a fatal error occurred", "fatal error:", "failed to connect", "invalid head of packet"))


def _short(text: str, limit: int = 400) -> str:
    """取最后几行有信息量的内容，避免把整段进度条塞进 UI。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    tail = "\n".join(lines[-6:])
    return tail[-limit:]
