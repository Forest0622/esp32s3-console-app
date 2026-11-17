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
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import app_data_cleanup            # noqa: E402
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


def test_cleanup() -> None:
    print("[7] 拔板清理缓存与旧工程")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "ESP32-S3-Console-App"
        home = base / "data"
        workspace = home / "workspace" / "chip1"
        imported = home / "projects_imported" / "old1"
        legacy = base / "projects_imported" / "legacy1"
        backup = home / "backups"
        for d in (workspace, imported, legacy, backup):
            d.mkdir(parents=True, exist_ok=True)
        (workspace / "build.bin").write_bytes(b"x" * 4096)
        (imported / "main.c").write_text("// old project", encoding="utf-8")
        (legacy / "old.txt").write_text("legacy", encoding="utf-8")
        (backup / "keep.bin").write_bytes(b"backup")
        (backup / "index.json").write_text('{"boards":{}}', encoding="utf-8")
        (backup / ".chunk_1.tmp").write_bytes(b"temp")

        got = app_data_cleanup.purge_caches(home)
        check("清掉构建副本", got["workspace"] == 1, str(got))
        check("清掉导入工程", got["projects"] == 1, str(got))
        check("清掉旧版目录", got["legacy_projects"] == 1, str(got))
        check("清掉分块临时文件", got["temp_files"] == 1, str(got))
        check("释放字节数大于 0", got["bytes"] > 0, str(got))
        check("workspace 已空", not any((home / "workspace").iterdir()))
        check("导入工程已空", not any((home / "projects_imported").iterdir()))
        check("旧版目录已空", not any((base / "projects_imported").iterdir()))
        check("正式备份镜像保留", (backup / "keep.bin").is_file())
        check("备份索引保留", (backup / "index.json").is_file())

        # 重新生成缓存，验证 1 小时计时：插着不删，拔出未满 1 小时不删，满 1 小时删。
        (home / "workspace" / "chip2").mkdir(parents=True)
        (home / "workspace" / "chip2" / "build.bin").write_bytes(b"y" * 1024)
        (home / "projects_imported" / "new1").mkdir(parents=True)
        check("插着板不清理",
              app_data_cleanup.maybe_cleanup(True, home, now=1000) is None)
        check("拔出未满 1 小时不清理",
              app_data_cleanup.maybe_cleanup(False, home, now=1000 + 59) is None)
        check("拔出满 1 小时清理",
              isinstance(app_data_cleanup.maybe_cleanup(False, home, now=1000 + 3601), dict))
        check("构建副本已清空", not any((home / "workspace").iterdir()))
        check("导入工程已清空", not any((home / "projects_imported").iterdir()))
        check("同一轮不会重复清理",
              app_data_cleanup.maybe_cleanup(False, home, now=1000 + 7201) is None)
        check("清理截止时间已结束",
              app_data_cleanup.deadline(home) is None)


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
               test_importer, test_env, test_cleanup):
        fn()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
