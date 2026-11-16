"""烧录控制器（文档 3.7 / 7 章 Flash-Controller）。

三种模式：编译产物（多 bin）、备份镜像回滚、单 bin 直烧。
所有串口操作结束必须释放 fd（文档 8.8）。
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path

import cancel_token
from firmware_backup_manager import esp_retry, get_chip_id, run_streaming, settle

FLASH_BAUD = 460800
BAUD_FALLBACK = 115200


def _percent_of(line: str) -> int | None:
    """从 esptool 的 `Writing at 0x.... ( 37 %)` 取百分比。"""
    m = re.search(r"\(\s*(\d+)\s*%\s*\)", line)
    return int(m[1]) if m else None


def flash_bins(port: str, bins: list[tuple[str, str]], baud: int = FLASH_BAUD,
               progress_cb=None) -> tuple[bool, str]:
    """bins 为 [(offset, path)]；按文档 3.7 的 flasher_args 结果逐个写。

    progress_cb：按文件大小加权，把 esptool 的逐文件百分比折算成整体进度。
    （以前传 --no-progress 又只在上层写「烧录中」，界面就一直是 100%。）
    """
    if not bins:
        return False, "没有可烧录的镜像"
    for off, path in bins:
        if not Path(path).is_file():
            return False, f"镜像不存在：{path}"
        if not str(off).startswith("0x"):
            return False, f"偏移格式必须是 0x 开头：{off}"

    settle(0.6)
    sizes = [max(1, Path(path).stat().st_size) for _off, path in bins]
    total = float(sum(sizes))

    ladder = (baud, 230400, BAUD_FALLBACK)
    last_out = ""
    for attempt, cur in enumerate(ladder, start=1):
        argv: list[str] = ["write_flash", "--port", port, "--baud", str(cur)]
        for off, path in bins:
            argv += [str(off), str(path)]

        state = {"done": 0.0, "idx": 0}

        def on_line(line: str, _sizes=sizes, _total=total, _st=state) -> None:
            pct = _percent_of(line)
            if pct is not None and progress_cb:
                cur_size = _sizes[min(_st["idx"], len(_sizes) - 1)]
                frac = (_st["done"] + cur_size * pct / 100.0) / _total
                name = Path(bins[min(_st["idx"], len(bins) - 1)][1]).name
                progress_cb(min(0.99, frac),
                            f"烧录 {int(min(0.99, frac) * 100)}% · {name}")
                return
            if line.startswith("Wrote ") and _st["idx"] < len(_sizes):
                _st["done"] += _sizes[_st["idx"]]
                _st["idx"] += 1
                if progress_cb and _st["idx"] < len(bins):
                    progress_cb(min(0.99, _st["done"] / _total),
                                f"烧录 {int(min(0.99, _st['done'] / _total) * 100)}%"
                                f" · 准备写入 {Path(bins[_st['idx']][1]).name}")

        code, out = run_streaming(argv, on_line=on_line, timeout=600)
        if cancel_token.is_cancelled():
            return False, "已停止烧录（芯片已被清空，可从备份回滚）"
        if code == 0:
            if progress_cb:
                progress_cb(1.0, "烧录完成，正在校验芯片")
            return True, ""
        if code == 0:
            return True, ""
        last_out = out.strip()[-800:]
        if attempt == len(ladder):
            break
        time.sleep(2)
    return False, last_out or "烧录失败"


def flash_image(port: str, bin_path: str, offset: str) -> tuple[bool, str]:
    """单 bin 直烧，offset 必填（文档 3.4：不自动猜测偏移）。"""
    if not offset or not re.match(r"0x[0-9a-fA-F]+$", offset.strip()):
        return False, "直烧模式必须填写合法偏移（如 0x0）"
    return flash_bins(port, [(offset.strip(), bin_path)])


def erase_flash(port: str, baud: int = FLASH_BAUD) -> tuple[bool, str]:
    """整片擦除（原样烧录走这条路：用户要的就是覆盖，不再要求先备份）。

    ESP32-S3 整片擦 16 MB 约 20~40 秒，比读一整片快得多。擦除后再写，
    避免旧固件残留的分区表 / NVS 与新工程打架。
    """
    settle(0.6)
    last = ""
    if cancel_token.is_cancelled():
        return False, "已停止"
    for cur in (baud, 230400, BAUD_FALLBACK):
        code, out = esp_retry(["erase_flash", "--port", port, "--baud", str(cur)],
                              tries=2, wait=1.5)
        if code == 0:
            return True, ""
        last = out.strip()[-400:]
        time.sleep(1.5)
    return False, last or "擦除失败"


def verify_same_chip(port: str, expected_chip_id: str) -> tuple[bool, str]:
    """烧录完成二次读芯片 ID，防止烧错设备（文档 8.13）。"""
    actual = get_chip_id(port)
    if actual is None:
        return False, "烧录后无法读取芯片 ID，无法确认设备身份"
    if actual.lower() != expected_chip_id.lower():
        return False, f"芯片 ID 不匹配：期望 {expected_chip_id}，实际 {actual}"
    return True, ""


def parse_flasher_args(build_dir: str) -> list[tuple[str, str]]:
    """按文档 3.7 优先级解析烧录清单：flasher_args.json → flash_args → 标准偏移。"""
    import json
    build = Path(build_dir)
    result: list[tuple[str, str]] = []

    fa = build / "flasher_args.json"
    if fa.is_file():
        try:
            data = json.loads(fa.read_text(encoding="utf-8"))
            for off, item in (data.get("flash_files") or {}).items():
                fname = str(item)
                p = build / fname if not Path(fname).is_absolute() else Path(fname)
                if p.is_file():
                    result.append((off, str(p)))
            if result:
                return result
        except (OSError, json.JSONDecodeError):
            pass

    args = build / "flash_args"
    if args.is_file():
        try:
            for line in args.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("0x"):
                    p = build / parts[1]
                    if p.is_file():
                        result.append((parts[0], str(p)))
            if result:
                return result
        except OSError:
            pass

    fallback = [
        ("0x0", build / "bootloader" / "bootloader.bin"),
        ("0x8000", build / "partition_table" / "partition-table.bin"),
        ("0x10000", build / "app.bin"),
    ]
    for off, p in fallback:
        if p.is_file():
            result.append((off, str(p)))
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2:
        print(parse_flasher_args(sys.argv[1]))
