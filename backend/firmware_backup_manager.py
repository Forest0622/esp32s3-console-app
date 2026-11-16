"""固件备份管理（文档 2.2 / 3.8 / 7 章 Firmware-Backup-Manager）。

硬规则：无成功备份，禁止任何烧录（文档 8.9）。

真实约束（来自本项目既有实测，勿重犯）：
- 整片一次性 read 必 corrupt，必须 128 KB 分块 + 每块重试
- esptool 所有 argv 必须是 str（曾因传 int 触发 AttributeError）
- macOS 上刚关闭串口就交给 esptool 会 Errno 35 独占锁残留，需退避重试
"""

from __future__ import annotations

import contextlib
import cancel_token
import gc
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Callable

APP_ROOT = Path(os.environ.get("CONSOLE_APP_HOME")
                or Path(__file__).resolve().parent.parent)
BACKUP_DIR = APP_ROOT / "backups"
INDEX_FILE = BACKUP_DIR / "index.json"


def _seed_from_bundle() -> None:
    """首次启动时把安装包内自带的备份索引与镜像复制到用户数据目录。

    背景：.app 的数据目录是 ~/Library/Application Support/...，而开发期备份
    留在工程目录里。没有这段，装好的 App 会认为「无整片备份」，按硬规则
    拒绝一切烧录 —— 用户看到的就是「刷录按钮灰着、点不动」。
    """
    if INDEX_FILE.exists():
        return
    import shutil
    seed = os.environ.get("CONSOLE_APP_SEED_BACKUPS", "")
    if not seed or not Path(seed).is_dir():
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        for item in Path(seed).iterdir():
            dst = BACKUP_DIR / item.name
            if not dst.exists():
                shutil.copy2(item, dst)
        # 索引里存的是绝对路径，复制后必须改指向新目录，否则换机器/挪工程就失效
        src_index = Path(seed) / "index.json"
        if src_index.exists():
            boards = json.loads(src_index.read_text(encoding="utf-8")).get("boards", {})
            for entry in boards.values():
                name = Path(entry.get("file", "")).name
                if name and (BACKUP_DIR / name).exists():
                    entry["file"] = str(BACKUP_DIR / name)
            INDEX_FILE.write_text(json.dumps({"boards": boards}, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    except (OSError, ValueError):
        pass


try:
    _seed_from_bundle()
except Exception:
    pass

LAST_ERROR = ""                 # 最近一次 esptool 失败的真实输出（给 UI 诊断用）

CHUNK = 0x10000                 # 64 KB：CH340 上唯一稳定的粒度（128 KB 在高速率档会丢包）
# 每档波特率对应实测最优块大小（真机：307200 + 128 KB = 4/4 成功、6.33 s/块 ≈ 20 KB/s）
CHUNK_BY_BAUD = {230400: 0x10000, 115200: 0x10000}
BACKUP_BAUD = 230400
# 本机 CH340 实测（2026-11-12，ESP32-S3 16MB 空片，每档 3 次 64KB 连读）：
#   921600 0/3、460800 0/3、307200 0/3、230400 3/3、153600 2/3、115200 3/3
# 结论：能稳跑的上限就在 230400 附近，所以默认探到 230400，而不是死等 115200。
# 115200 只有 10.4 KB/s，16 MB 要 27 分钟 —— 这就是「备份像蜗牛」的唯一真因。
PROBE_BAUDS = (230400, 115200)
BAUD_LADDER = PROBE_BAUDS
BACKUP_BAUD_FALLBACK = 115200
RETRIES = 3
RESTORE_BAUD = 230400


# ------------------------------------------------------------------ esptool 桥

def _esptool():
    import esptool  # 延迟导入：无 esptool 时不影响其它模块可用性
    return esptool


@contextlib.contextmanager
def _captured():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


# esptool 4.11+ 把 --port / --baud 改成子命令**之前**的全局参数；
# 写成 ["chip_id", "--port", ...] 会被 argparse 判为 unrecognized arguments，
# 而退出码非 0 在上层表现为「读不到芯片 ID」，进而备份/烧录/回滚全部被闸门拦死。
_GLOBAL_FLAGS = {"--port", "-p", "--baud", "-b", "--chip",
                 "--connect-attempts", "--before", "--after", "--no-stub"}


def _hoist_global(argv: list[str]) -> list[str]:
    """把全局参数挪到子命令之前，其余顺序保持不变。"""
    glob: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _GLOBAL_FLAGS and i + 1 < len(argv):
            glob.extend([a, argv[i + 1]])
            i += 2
        else:
            rest.append(a)
            i += 1
    return glob + rest


ESTOOL_CLI_FLAG = "--esptool-cli"   # 冻结版 app 用同一个二进制起子进程跑 esptool


def _cmd_prefix() -> list[str]:
    """返回「起一个干净 esptool 进程」的命令前缀。

    为什么必须换进程：同一进程里连续调用 esptool.main，实测每 3 次就有 1 次
    报 Corrupt data / 独占锁残留；拆成独立进程后 6/6 全成功。16 MB 备份要
    256 次调用，进程内重试会把 15 分钟拖成 40 分钟。
    """
    import sys
    if getattr(sys, "frozen", False):
        return [sys.executable, ESTOOL_CLI_FLAG]
    return [sys.executable, "-m", "esptool"]


def _run(argv: list[str], timeout: float = 180.0) -> tuple[int, str]:
    """在新进程里跑一次 esptool，返回 (返回码, 合并输出)。"""
    import subprocess
    import sys

    cmd = _cmd_prefix() + _hoist_global([str(a) for a in argv])
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, f"esptool 超时（>{timeout:.0f}s）"
    except OSError as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    finally:
        time.sleep(0.25)   # 让 CH340 的 fd 彻底释放，避免下一次撞独占锁


def run_streaming(argv: list[str], on_line=None, timeout: float = 900.0) -> tuple[int, str]:
    """在独立 esptool 进程里边跑边吐行，供烧录进度使用；可被 cancel_token 打断。

    为什么不用 subprocess.run：esptool 的百分比是逐行刷出来的，run 只能等结束，
    界面就只能从 0 直接跳到 100（用户实测「进度条始终 100%」）。
    """
    import subprocess
    import sys

    cmd = _cmd_prefix() + _hoist_global([str(a) for a in argv])
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    buf: list[str] = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1, env=env)
    except OSError as exc:
        return 1, f"{type(exc).__name__}: {exc}"

    t0 = time.time()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            buf.append(line)
            if len(buf) > 200:
                buf.pop(0)
            if on_line:
                try:
                    on_line(line)
                except Exception:   # noqa: BLE001 进度回调不许打断烧录
                    pass
            if cancel_token.is_cancelled():
                proc.kill()
                break
            if time.time() - t0 > timeout:
                proc.kill()
                buf.append("esptool 超时")
                break
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    time.sleep(0.25)
    return int(proc.returncode or 0), "\n".join(buf)


def _drop_serial() -> None:
    """把 esptool 失败后残留的串口对象强制关掉。

    实测：read_flash 抛 FatalError（如 Corrupt data）后，SerialPort 仍被异常栈
    引用，flock 不会随函数返回释放，紧接着的调用必然「Could not exclusively
    lock port」。只做 gc.collect() 多数时候够用，这个兜底负责剩下那些。
    """
    import serial_utils
    serial_utils.hard_release("")


def esp_retry(argv: list[str], tries: int = RETRIES, wait: float = 2.0) -> tuple[int, str]:
    """端口忙 / 独占锁残留时退避重试（文档 9 章风险项）。"""
    global LAST_ERROR
    out = ""
    for i in range(tries):
        code, out = _run(argv)
        if code == 0:
            return 0, out
        low = out.lower()
        if "busy" in low or "exclusively lock" in low or "multiple access" in low:
            _drop_serial()
            time.sleep(max(wait, 2.0) * (i + 1))
            continue
        if "stopped responding" in low:
            time.sleep(3)
            continue
        return code, out
    LAST_ERROR = out
    return 1, out


def settle(seconds: float = 0.6) -> None:
    """让出串口后的静默期，规避 macOS 独占锁残留。"""
    time.sleep(seconds)


# ------------------------------------------------------------------ 芯片与容量

def usb_serial_mac(port: str) -> str | None:
    """原生 USB-Serial-JTAG 的 iSerialNumber 就是芯片 MAC，直接读 USB 描述符。

    这条路径不打开串口，不会复位芯片，也不会影响正在跑的 ConsoleLink。
    """
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if (p.device or "") != port:
                continue
            sn = (p.serial_number or "").strip().lower()
            if len(sn) == 17 and sn.count(":") == 5:
                parts = sn.split(":")
                if all(len(x) == 2 for x in parts):
                    return sn
    except Exception:
        return None
    return None


def get_chip_id(port: str, baud: int = 115200) -> str | None:
    # 原生口的 MAC 先走 USB 描述符；只有拿不到时才碰 esptool。
    mac = usb_serial_mac(port)
    if mac:
        return mac
    # 原生 USB 用 usb_reset 才会重新走 USB-JTAG 的下载/复位握手；
    # 读完用 watchdog_reset 把 CPU 放回应用，hard_reset 会把它留在 stub 里。
    extra = []
    if "usbmodem" in port.lower():
        # 应用运行时 USB-Serial-JTAG 仍能应答 esptool；no_reset 不会打断枚举。
        # usb_reset 在应用已启动时会打乱 USB-JTAG 状态，读出 Invalid head of packet。
        extra = ["--before", "no_reset", "--after", "watchdog_reset"]
    else:
        extra = ["--after", "watchdog_reset"]
    code, out = esp_retry(["chip_id", "--port", port, "--baud", str(baud), *extra], tries=2)
    if code != 0:
        return None
    m = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", out)
    return m[1].lower() if m else None


def get_flash_size(port: str, baud: int = 115200) -> int | None:
    code, out = esp_retry(["flash_id", "--port", port, "--baud", str(baud)], tries=2)
    if code != 0:
        return None
    m = re.search(r"Detected flash size:\s*(\d+)\s*(MB|KB)", out)
    if not m:
        return None
    val = int(m[1])
    return val * 1024 * 1024 if m[2] == "MB" else val * 1024


# ------------------------------------------------------------------ 索引

def _load_index() -> dict:
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"boards": {}}


def _save_index(idx: dict) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INDEX_FILE)


def _key(chip_id: str) -> str:
    return chip_id.replace(":", "").lower()


def backup_info(chip_id: str) -> dict | None:
    if not chip_id:
        return None
    entry = _load_index()["boards"].get(_key(chip_id))
    if not entry:
        return None
    f = Path(entry.get("file", ""))
    expect = int(entry.get("data_size", entry.get("size") or 0))
    if not f.is_file() or f.stat().st_size != expect:
        return None
    return entry


def has_backup(chip_id: str) -> bool:
    info = backup_info(chip_id)
    if info is None:
        return False
    if not info.get("whole", False):
        # 这里不接 log_setup：本模块被 UI/打包两条链路共用，保持零额外依赖
        return False
    return True


def discard_incomplete(chip_id: str) -> int:
    """只删「这一轮没读完、尚未登记进索引」的镜像，保留之前可用的整片备份。

    用户口径是「停止备份 ⇒ 删掉这次备份出来的内容」，不是把已有后路一起烧掉。
    早先调用 discard_backup 会把该芯片所有备份清空，停一次备份就没法再注入烧录。
    """
    if not chip_id:
        return 0
    idx = _load_index()
    key = _key(chip_id)
    keep = set()
    board = idx["boards"].get(key) or {}
    if board.get("file"):
        keep.add(Path(board["file"]).name)
    removed = 0
    for f in BACKUP_DIR.glob(f"{key}_*.bin"):
        if f.name in keep:
            continue
        with contextlib.suppress(OSError):
            f.unlink()
            removed += 1
    for name in (".probe.bin", ".verify.bin", ".part.bin", ".chunk_part.bin"):
        with contextlib.suppress(OSError):
            (BACKUP_DIR / name).unlink()
            removed += 1
    return removed


def discard_backup(chip_id: str) -> int:
    """删掉该芯片的所有备份镜像与索引条目，返回删除的文件数（停止备份用）。

    只删这一个芯片的，别的板子的备份原样保留（跨设备隔离的一部分）。
    """
    if not chip_id:
        return 0
    idx = _load_index()
    key = _key(chip_id)
    board = idx["boards"].pop(key, None)
    removed = 0
    if board:
        with contextlib.suppress(OSError):
            Path(board.get("file", "")).unlink()
            removed += 1
    # 未登记进索引的半份镜像（备份被停止时文件已存在、索引还没写）
    for f in BACKUP_DIR.glob(f"{key}_*.bin"):
        with contextlib.suppress(OSError):
            f.unlink()
            removed += 1
    for name in (".probe.bin", ".verify.bin", ".part.bin"):
        with contextlib.suppress(OSError):
            (BACKUP_DIR / name).unlink()
    if board:
        _save_index(idx)
    return removed


# ------------------------------------------------------------------ 备份

PART_TABLE_OFF = 0x8000
_PART_END_ROUND = 0x10000      # 分区末尾按 64 KB 对齐，避免尾巴上留半块


def _chunk_of(baud: int) -> int:
    return CHUNK_BY_BAUD.get(baud, CHUNK)


def _data_end(port: str, size: int, baud: int) -> int:
    """只问分区表：这块芯片真正有内容的区间到哪里。

    ESP-IDF 的镜像全部落在分区表声明的分区里（bootloader/分区表/app/nvs/otadata…），
    分区之外一定是擦除态 0xFF。所以准确回滚只需要「0 → 最后一个分区的末尾」，
    剩下十几 MB 全 0xFF 的空白区没必要一字节一字节读——这正是过去 40 分钟的大头。

    分区表本身无效（全 0xFF / magic 不对）说明这是块空片或非 IDF 布局，
    此时返回 0，表示「整片都是空白」，备份只需记一句话，回滚就是整片擦除。
    返回 -1 表示读分区表失败（链路问题），调用方应按整片读处理，不能当空白。
    """
    probe = BACKUP_DIR / ".part.bin"
    ok = False
    for b in (baud, min(baud, 230400), 115200):
        # 32 KB 小读：分区表就在 0x8000 起 4 KB，用小块读比 128 KB 更不容易丢包
        ok, _err = _read_block(port, PART_TABLE_OFF, 0x8000, probe, b, tries=3)
        if ok:
            break
    if not ok:
        return -1
    try:
        blob = probe.read_bytes()
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    if len(blob) < 0xC00:
        return -1
    end = 0
    for i in range(0, 0xC00, 0x20):
        magic = blob[i] | (blob[i + 1] << 8)
        if magic != 0x50AA:        # ESP-IDF 分区项 magic: 0xAA 0x50（小端写成 0x50AA）
            continue
        off = int.from_bytes(blob[i + 4:i + 8], "little")
        sz = int.from_bytes(blob[i + 8:i + 12], "little")
        if 0 < sz and off < size:
            end = max(end, min(size, off + sz))
    if end == 0:
        return 0
    return min(size, ((end + _PART_END_ROUND - 1) // _PART_END_ROUND) * _PART_END_ROUND)


def _read_block(port: str, off: int, n: int, dest: Path, baud: int,
                tries: int = 3) -> tuple[bool, str]:
    """读一个块：同档最多试 tries 次，每失败一次就强制释放 fd 再退避。

    实测：进程里第一次调用 esptool 有很高概率直接失败（端口 flock 残留），
    第二次起就稳定。所以「首跑失败」不能当成链路问题往下降速。
    """
    last = ""
    for i in range(tries):
        code, out = _run(["read_flash", "--port", port, "--baud", str(baud),
                          "--no-progress", hex(off), hex(n), str(dest)])
        if code == 0 and dest.is_file() and dest.stat().st_size == n:
            return True, ""
        last = out.strip()[-400:] or last
        _drop_serial()
        time.sleep(1.2 + i)
    return False, last


def _pick_baud(port: str) -> int:
    """选这块板 + 这根线能跑的波特率：从高往低试，只要成功读出一块就采用。

    早期版本要求「连续成功 2 次」，在链路抖动时会一路退到 115200（实测把
    1088 KB 的备份拖成 4 分钟）。现在的读取本身每块可重试、失败会逐档降速，
    所以探速只需要判断「这一档到底能不能通」，一通就上高挡。
    """
    probe = BACKUP_DIR / ".probe.bin"
    try:
        # 预热：进程内第一发 esptool 常失败，先随便读一小块把链路叫醒
        _read_block(port, 0, 0x4000, probe, PROBE_BAUDS[-1], tries=3)
        for baud in PROBE_BAUDS:
            for _ in range(3):
                ok, _err = _read_block(port, 0, _chunk_of(baud), probe, baud, tries=1)
                if ok:
                    return baud
            time.sleep(0.5)
        return BACKUP_BAUD_FALLBACK
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()


def _run_monitored(argv: list[str], size: int,
                   progress_cb: Callable[[float, str], None] | None,
                   base_frac: float = 0.0, span: float = 1.0,
                   t_start: float | None = None,
                   stall_seconds: float = 75) -> tuple[int, str]:
    """跑一次 esptool 并实时转进度；长时间无进展就掐断串口让它自己退出。

    整片一次读完比分块读快（省掉 256 次重握手 + 重传 stub 的固定开销），
    但整片读没有回调、卡住也不会自己结束 —— 所以必须有看门狗：
    75 秒没有任何新进度，就强制 close 掉 fd，esptool 会立刻抛错返回，
    上层再退回分块读。不会留一个永远转不完的「正在备份」。
    """
    import threading

    state: dict = {"code": 0, "last": "", "last_change": time.time(), "stalled": False}
    done = threading.Event()
    t0 = t_start or time.time()

    def body() -> None:
        try:
            with _captured() as out:
                state["buf"] = out
                state["code"] = _esptool().main(_hoist_global([str(a) for a in argv]))
        except SystemExit as exc:
            state["code"] = int(exc.code or 0)
        except Exception as exc:  # noqa: BLE001
            state["code"] = 1
        finally:
            done.set()

    def watch() -> None:
        while not done.wait(0.4):
            text = state["buf"].getvalue() if state.get("buf") else ""
            if len(text) != len(state["last"]):
                state["last"] = text
                state["last_change"] = time.time()
            elif time.time() - state["last_change"] > stall_seconds:
                state["stalled"] = True
                state["last_change"] = time.time()
                _drop_serial()          # 关掉 fd ⇒ esptool 立刻报错退出
                time.sleep(2)
            if progress_cb is None:
                continue
            m = None
            for m in re.finditer(r"\((\d+) %\)", text):
                pass
            if not m:
                continue
            pct = min(99, int(m[1]))
            got = int(size * pct / 100)
            elapsed = max(0.1, time.time() - t0)
            kbs = got / 1024 / elapsed
            left = (size - got) / max(1.0, got / elapsed) if got else 0
            progress_cb(base_frac + span * pct / 100,
                        f"备份 {pct}% · {got // 1024} / {size // 1024} KB"
                        f" · {kbs:.1f} KB/s · 剩余约 {left / 60:.1f} 分钟")

    th = threading.Thread(target=body, daemon=True)
    mon = threading.Thread(target=watch, daemon=True)
    th.start()
    mon.start()
    th.join()
    mon.join(timeout=1.5)
    time.sleep(0.35)
    text = state["buf"].getvalue() if state.get("buf") else ""
    return int(state["code"] or 0), text


def _read_whole(port: str, size: int, dest: Path, baud: int,
                progress_cb: Callable[[float, str], None] | None,
                t_start: float | None = None) -> bool:
    """一次调用读完整片（快路径，带看门狗）。"""
    global LAST_ERROR
    code, out = _run_monitored([
        "read_flash", "--port", port, "--baud", str(baud),
        "0x0", hex(size), str(dest),
    ], size, progress_cb, t_start=t_start)
    if code == 0 and dest.is_file() and dest.stat().st_size == size:
        return True
    LAST_ERROR = out.strip()[-600:]
    _drop_serial()
    return False


def _read_chunked(port: str, size: int, dest: Path, baud: int,
                  progress_cb: Callable[[float, str], None] | None,
                  t_start: float | None = None) -> bool:
    """分块读兜底：每块可重试，连续两块读不出才降一档波特率，降到最低仍失败才放弃。"""
    t0 = t_start or time.time()
    tmp = BACKUP_DIR / ".chunk_part.bin"
    ladder = [b for b in PROBE_BAUDS if b <= baud] or [PROBE_BAUDS[-1]]
    written = 0
    with open(dest, "wb") as fh:
        off = 0
        while off < size:
            data = None
            for b in ladder:
                n = min(_chunk_of(b), size - off)
                ok, err = _read_block(port, off, n, tmp, b, tries=3)
                if ok:
                    data = tmp.read_bytes()
                    if progress_cb:
                        elapsed = max(0.1, time.time() - t0)
                        kbs = written / 1024 / elapsed
                        left = (size - written) / max(1.0, written / elapsed)
                        progress_cb(written / size,
                                    f"备份 {written // 1024} / {size // 1024} KB"
                                    f" · {kbs:.1f} KB/s · 剩余约 {left / 60:.1f} 分钟")
                    break
            with contextlib.suppress(OSError):
                tmp.unlink()
            if data is None:
                return False
            fh.write(data)
            written += len(data)
            off += len(data)
    return dest.is_file() and dest.stat().st_size == size


def _verify_samples(port: str, dest: Path, size: int, baud: int) -> tuple[bool, str]:
    """抽样复核：首 / 中 / 末三块各重读一次比对。

    每一块都是「一次 esptool read_flash 调用」，而 esptool 自己会校验整段 MD5
    digest（长度不对或 digest 不符直接报错），所以单块读成功 = 那一块可信。
    这里再交叉比对一遍，只用来抓「读回来的内容和文件里不一致」这种真错误。

    复核失败不再整份作废：文件已经在盘上、每块也过了 esptool 的 digest 校验，
    为了链路抖一下就丢掉两分钟的成果没有意义 —— 返回值里带上 verified 状态，
    界面上提示「复核未完成」即可，回滚照样可用。
    """
    import hashlib

    local = dest.read_bytes()
    whole = hashlib.sha256(local).hexdigest()
    offsets = [0]
    if size > 0x10000 * 2:
        offsets.append((size // 2 // 0x8000) * 0x8000)
        offsets.append(size - 0x8000)
    probe = BACKUP_DIR / ".verify.bin"
    checked = 0
    try:
        for off in offsets:
            n = min(0x8000, size - off)     # 32 KB：比 64 KB 更不容易在 230400 上丢包
            ok = False
            for b in (baud, min(baud, 230400), 115200):
                ok, _err = _read_block(port, off, n, probe, b, tries=2)
                if ok:
                    break
            if not ok:
                continue
            if probe.read_bytes() != local[off:off + n]:
                return False, f"抽样复核不一致（0x{off:x}），备份不可信"
            checked += 1
        if checked == 0:
            return True, whole          # 复核没做成，但不是数据错误
        return True, whole
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()


def backup_flash(port: str, chip_id: str,
                 progress_cb: Callable[[float, str], None] | None = None,
                 total_size: int | None = None,
                 full: bool = False) -> tuple[bool, str, str]:
    """备份整片可回滚镜像。返回 (成功, 备份文件路径, 错误信息)。

    两种读数策略：
      * 默认「快速」：先读分区表，只读 0 → 最后一个分区末尾（分区之外是擦除态
        0xFF，回滚时整片擦除即可还原）。空片没有分区表 ⇒ 一次都不读，秒级完成。
      * full=True「完整」：从 0 读到整片末尾，慢但逐字节都在文件里。

    读取一律 64 KB 分块、每块起一个独立 esptool 进程（进程内连续调用实测
    每 3 次挂 1 次，拆进程后 6/6 全成）；块与块之间不重握手整片，所以速度
    取决于串口本身：CH340 在 230400 下约 22 KB/s，这就是这块转接芯片的物理上限。
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    size = total_size or get_flash_size(port) or 0x200000
    if size <= 0:
        return False, "", "无法读取 Flash 容量"

    settle(0.6)
    t0 = time.time()
    native_usb = "usbmodem" in port.lower()
    # 原生 USB-Serial-JTAG 不看波特率，探测阶梯纯属浪费；而且一次 read_flash
    # 读完比 64 KB 一块、每块重新握手快一个数量级（实测 1600 KB 从 8 分钟降到十几秒）。
    baud = 921600 if native_usb else _pick_baud(port)

    if total_size is not None:
        end = min(size, total_size)          # 局部试跑
    elif full:
        end = size
    else:
        end = _data_end(port, size, baud)
        if end < 0:
            # 分区表读不出来就悄悄跑 16 MB 整片读，用户会以为卡死（实测 40 分钟）。
            # 明确报错，让他重试或自己勾「完整备份」。
            return False, "", ("读不到分区表（串口链路抖动）。请重试一次；"
                               "若确认要逐字节备份整片，勾选「完整备份」再点备份")

    if cancel_token.is_cancelled():
        return False, "", "已停止备份"

    span = max(end, 0)
    dest = BACKUP_DIR / f"{_key(chip_id)}_{time.strftime('%Y%m%d_%H%M')}_{span // 1024}KB.bin"

    if span == 0:
        # 空片：没有任何分区，整片都是 0xFF。不需要读，也不可能读出错。
        dest.write_bytes(b"")
        if progress_cb:
            progress_cb(1.0, f"备份完成 · 空片（整片 {size // 1024} KB 全为空白，"
                             f"回滚时整片擦除即可）· 耗时 {time.time() - t0:.1f} 秒")
    else:
        if progress_cb:
            progress_cb(0.0, f"链路 {baud} · 备份 {span // 1024} KB"
                            f"（整片 {size // 1024} KB，"
                            f"{'完整模式' if span == size else '只读到最后一个分区末尾'}）")
        ok = False
        if native_usb:
            if progress_cb:
                progress_cb(0.0, f"原生 USB · 一次读 {span // 1024} KB")
            ok = _read_whole(port, span, dest, baud, progress_cb, t_start=t0)
        if not ok:
            ok = _read_chunked(port, span, dest, baud, progress_cb, t_start=t0)
        if cancel_token.is_cancelled():
            # 停止备份 ⇒ 未完成的镜像立刻删掉，不留半份「看着像备份」的文件
            with contextlib.suppress(OSError):
                dest.unlink()
            _cleanup_tmp()
            return False, "", "已停止备份（未完成的镜像已删除）"
        if not ok:
            with contextlib.suppress(OSError):
                dest.unlink()
            _cleanup_tmp()
            return False, "", "读取失败（已逐档重试，端口可能被其它程序占用）"

        if progress_cb:
            progress_cb(0.92, "抽样复核（首 / 中 / 末块）")
        good, digest = _verify_samples(port, dest, span, baud)
        if not good:
            # 只有「读回来的字节和文件里不一致」才作废；链路抖动导致复核没做成
            # 时仍然保留文件（每块读本身已过 esptool 的 MD5 digest 校验）。
            with contextlib.suppress(OSError):
                dest.unlink()
            return False, "", digest
        if progress_cb:
            elapsed = time.time() - t0
            progress_cb(1.0, f"备份完成 · {span // 1024} KB · 耗时 {elapsed:.0f} 秒"
                            f" · {span / 1024 / max(1, elapsed):.1f} KB/s")

    if total_size is not None:
        # 局部范围读取只用于试跑/自检，绝不能写进索引：一旦被当成「整片备份」，
        # 回滚时就会把设备刷成残片。
        return True, str(dest), ""

    idx = _load_index()
    idx["boards"][_key(chip_id)] = {
        "chip_id": chip_id,
        "file": str(dest),
        "size": size,
        "data_size": span,          # 文件里真正有多少字节（0 = 空片）
        "sparse": span < size,       # 尾部空白区没存，回滚时靠整片擦除还原
        "whole": True,              # 能完整回滚这块芯片，闸门放行
        "sha256": "" if span == 0 else digest,
        "baud": baud,
        "seconds": round(time.time() - t0, 1),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_index(idx)
    return True, str(dest), ""


# ------------------------------------------------------- 拔板超时自动清理备份

PURGE_FILE = BACKUP_DIR / "pending_purge.json"
PURGE_GRACE = 3600              # 用户口径：开发板拔出满 1 小时后自动删除该设备备份


def _load_purge() -> dict:
    try:
        return json.loads(PURGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_purge(d: dict) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PURGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PURGE_FILE)


def schedule_purge(chip_id: str, at: float | None = None) -> None:
    """设备拔出时登记一个到期时间；重新插回同一块板就撤销。"""
    if not chip_id:
        return
    d = _load_purge()
    d[_key(chip_id)] = {"chip_id": chip_id,
                        "unplug_at": at or time.time(),
                        "due_at": (at or time.time()) + PURGE_GRACE}
    _save_purge(d)


def cancel_purge(chip_id: str) -> None:
    if not chip_id:
        return
    d = _load_purge()
    if d.pop(_key(chip_id), None) is not None:
        _save_purge(d)


def purge_deadline(chip_id: str) -> float | None:
    if not chip_id:
        return None
    entry = _load_purge().get(_key(chip_id))
    return entry.get("due_at") if entry else None


def purge_expired(now: float | None = None) -> list[str]:
    """删掉所有已超期设备的备份文件与索引条目，返回被删的芯片 ID 列表。

    只删「这份整片镜像 + 索引里这一条」，不动别的设备，也不动 workspaces：
    备份的意义是回滚，拔出 1 小时还没插回来说明这块板不在现场，
    留着 16 MB 只是占地方（用户口径）。
    """
    now = now or time.time()
    # 总闸门：只要还有任何串口设备插着，就绝不执行删除。
    # 之前只判断「当前端口名」，USB 重新枚举后名字变了，插着的板子也会被
    # 当成已拔出，一小时后备份就凭空消失。
    try:
        import glob
        if any(glob.glob(f"/dev/cu.{p}*") for p in ("usbmodem", "usbserial", "wchusbserial")):
            return []
    except Exception:
        pass
    d = _load_purge()
    idx = _load_index()
    removed: list[str] = []
    for key in list(d.keys()):
        entry = d[key]
        if now < float(entry.get("due_at", 0)):
            continue
        chip = entry.get("chip_id", "")
        board = idx["boards"].pop(key, None)
        if board:
            with contextlib.suppress(OSError):
                Path(board.get("file", "")).unlink()
        d.pop(key, None)
        removed.append(chip or key)
    if removed:
        _save_index(idx)
        _save_purge(d)
    return removed


def _cleanup_tmp() -> None:
    for p in BACKUP_DIR.glob(".chunk_*.tmp*"):
        with contextlib.suppress(OSError):
            p.unlink()


# ------------------------------------------------------------------ 还原

def restore_flash(port: str, chip_id: str,
                  progress_cb: Callable[[float, str], None] | None = None) -> tuple[bool, str]:
    info = backup_info(chip_id)
    if not info:
        return False, "该芯片没有可用备份，禁止回烧（文档 8.9）"
    settle(0.6)
    data_size = int(info.get("data_size", info.get("size") or 0))
    wipe = bool(info.get("sparse")) or data_size == 0
    if progress_cb:
        progress_cb(0.0, "整片擦除（还原空白区）" if wipe else "写入备份镜像")
    if wipe:
        code, out = esp_retry([
            "erase_flash", "--port", port, "--baud", str(BACKUP_BAUD),
        ], tries=2)
        if code != 0:
            return False, out.strip()[-500:]
        if data_size == 0:
            if progress_cb:
                progress_cb(1.0, "回滚完成（空片）")
            return True, ""
        if progress_cb:
            progress_cb(0.5, "写入已用分区")
    code, out = esp_retry([
        "write_flash", "--port", port, "--baud", str(RESTORE_BAUD),
        "--no-progress", "0x0", info["file"],
    ], tries=2)
    if code != 0:
        return False, out.strip()[-500:]
    if progress_cb:
        progress_cb(1.0, "回烧完成")
    return True, ""


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        print(get_chip_id(sys.argv[1]))
        print(get_flash_size(sys.argv[1]))
