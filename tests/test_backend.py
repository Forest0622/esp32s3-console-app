"""后端自测（不依赖真机）。

覆盖：CRC、混合流解析（JSON 行 + 二进制帧）、烧录清单解析、源注入、
工程导入、编译环境检查。
运行：IDF venv python  backend 同级执行
    /Users/xiaoyi/.espressif/tools/python/v5.5.6/venv/bin/python tests/test_backend.py
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import flash_controller              # noqa: E402
import project_importer              # noqa: E402
import serial_data_parser            # noqa: E402
import source_injector               # noqa: E402
import build_env_checker             # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def make_frame(payload: bytes) -> bytes:
    crc = serial_data_parser.crc16_ccitt(payload)
    return b"\xa5\x5a" + struct.pack("<I", len(payload)) + struct.pack("<H", crc) + payload


def test_crc() -> None:
    print("[1] CRC16-CCITT")
    # 标准测试向量："123456789" → 0x29B1
    check("crc 123456789 == 0x29B1",
          serial_data_parser.crc16_ccitt(b"123456789") == 0x29B1)


def test_parser() -> None:
    print("[2] 混合流解析")
    got: dict = {"frame": None, "tel": []}
    p = serial_data_parser.SerialDataParser(
        on_frame=lambda fb: got.__setitem__("frame", fb),
        on_telemetry=lambda o: got["tel"].append(o),
    )

    line = (json.dumps({"type": "led_status", "data": {"pins": [{"pin": "GPIO1", "level": 1, "pwm": False}]}}) + "\n").encode()
    p.feed(line)
    check("JSON 行被解析", len(got["tel"]) == 1 and got["tel"][0]["type"] == "led_status")

    # 一个 PART 块：idx=0, 1024 像素，全 0x1234
    blk = 0x1234.to_bytes(2, "little") * 1024
    payload = bytes([0x05, 0x00]) + struct.pack("<H", 1024) + blk + bytes([0x00])
    p.feed(make_frame(payload))
    fb = got["frame"]
    check("二进制帧回调触发", fb is not None)
    if fb:
        check("PART 块写入位置正确", fb[0:2] == 0x1234.to_bytes(2, "little"), fb[0:2].hex())
        check("快照长度 = 320*240*2", len(fb) == 320 * 240 * 2, str(len(fb)))

    # 坏 CRC 必须被拒
    bad = bytearray(make_frame(payload))
    bad[6] ^= 0xFF
    frames_before = p.stats.frames
    p.feed(bytes(bad))
    check("坏 CRC 计数+1 且不写 framebuffer",
          p.stats.bad_crc == 1 and p.stats.frames == frames_before + 1)

    # 未知 op 整帧作废
    weird = bytes([0x77, 0x00])
    p.feed(make_frame(weird))
    check("未知 op 记 bad_len", p.stats.bad_len >= 1)


def test_flasher_args() -> None:
    print("[3] 烧录清单解析")
    build = ROOT / "tests" / "fixture_project" / "build"
    bins = flash_controller.parse_flasher_args(str(build))
    if not build.is_dir():
        print("  skip：fixture 未编译")
        return
    check("解析到 >=3 个镜像", len(bins) >= 3, str(bins))
    offsets = [o for o, _ in bins]
    check("包含 0x0 / 0x8000 / 0x10000",
          {"0x0", "0x8000", "0x10000"}.issubset(set(offsets)), str(offsets))
    check("所有路径存在", all(Path(p).is_file() for _, p in bins))


def test_injector() -> None:
    print("[4] 源注入参数")
    dirs = source_injector.resolve_component_dirs(["display", "led", "key"])
    check("解析出 4 个目录（含 console_protocol）", len(dirs) == 4, str(dirs))
    args = source_injector.inject_args(dirs)
    check("产出 EXTRA_COMPONENT_DIRS", "EXTRA_COMPONENT_DIRS=" in args, args)
    dirs2 = source_injector.resolve_component_dirs(["display", "led"])
    check("未勾选的 key 组件不会被注入",
          not any("key_read_driver" in d for d in dirs2), str(dirs2))
    try:
        source_injector.resolve_component_dirs(["bogus"])
        check("未知组件必须抛错", False)
    except ValueError:
        check("未知组件必须抛错", True)

    snap = source_injector.snapshot_key_files(str(ROOT / "tests" / "fixture_project"))
    check("快照到用户 CMakeLists", len(snap) >= 2, str(snap))
    check("未改动时复核为空",
          source_injector.verify_no_touch(str(ROOT / "tests" / "fixture_project"), snap) == [])


def test_importer() -> None:
    print("[5] 工程导入")
    fixture = ROOT / "tests" / "fixture_project"
    root, hits = project_importer.find_project_root(str(fixture))
    check("识别出工程根", root == fixture or (root is not None and str(root).endswith("fixture_project")),
          f"root={root} hits={hits}")

    scan = project_importer.scan_screen_path(str(fixture))
    check("扫描结果种类合法", scan.path in ("esp_lcd", "known_driver", "unknown"), scan.path)

    adapters = project_importer.load_adapters(verified_only=True)
    check("未实测适配包不出现在可勾选列表", adapters == [], str(adapters))
    adapters_all = project_importer.load_adapters(verified_only=False)
    check("全部适配包可读出（含 verified:false）", len(adapters_all) >= 1, str(adapters_all))


def test_env() -> None:
    print("[6] 编译环境检查")
    idf = build_env_checker.find_idf()
    check("找到 IDF_PATH", bool(idf), str(idf))
    proj = project_importer.import_project(str(ROOT / "tests" / "fixture_project"))
    check("夹具被识别为 IDF 工程", proj.kind == "idf", f"{proj.kind} {proj.error}")
    rep = build_env_checker.check(proj)
    check("预检 PASS", rep.ok, json.dumps(rep.to_dict(), ensure_ascii=False)[:300])


def main() -> int:
    for fn in (test_crc, test_parser, test_flasher_args, test_injector,
               test_importer, test_env):
        fn()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
