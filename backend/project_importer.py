"""项目导入：解压、zip slip 防护、工程根识别、工程类型判定、画面采集路径静态扫描。

对应开发文档：3.1 / 3.2 / 3.5.5.3 / 7章 Project-Importer

设计约束（文档 8 章）：
- 原始目录只读；本模块只读取，不写入用户工程
- 解压产物必须落在 projects_imported/ 内，越界即拒绝
- 静态扫描为启发式，允许误判，运行时由 3.5.5.4 校验兜底
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
# 解压目录必须在「可写」的数据目录里：以前写在 .app 的 Resources 下，
# 用户直接把 App 从 dmg 里双击运行时，卷是只读的，zip 解压必然失败
# （表现就是「zip 导入没反应 / 无法烧录压缩包」）。开发态同样落数据目录。
DATA_ROOT = Path(os.environ.get("CONSOLE_APP_HOME")
                 or (Path.home() / "Library" / "Application Support"
                     / "ESP32-S3-Console-App"))
IMPORT_DIR = DATA_ROOT / "projects_imported"
ADAPTER_DIR = APP_ROOT / "esp32_components" / "screen_adapters"

# 工程类型特征
IDF_ROOT_HINTS = ("include($ENV{IDF_PATH}", "project.cmake", "project(")
ARDUINO_HINTS = (".ino",)
PLATFORMIO_HINTS = ("platformio.ini",)

# 画面采集路径特征（文档 3.5.5.3 三档判定）
ESP_LCD_SYMBOLS = (
    "esp_lcd_panel_draw_bitmap",
    "esp_lcd_panel_io_tx_color",
    "esp_lcd_panel_draw_bitmap(",
    "esp_lcd_panel_io_tx_color(",
    "esp_lcd_panel_ops.h",
    "esp_lcd_panel_io.h",
)
SPI_WRITE_HINTS = (
    "spi_device_polling_transmit",
    "spi_device_transmit",
    "spi_device_queue_trans",
    "SPI1_WriteData",
    "spi_flash_",
)

SOURCE_SUFFIX = {".c", ".h", ".cc", ".cpp", ".hpp"}
SKIP_DIRS = {".git", "build", "managed_components", ".vscode", "__pycache__"}


@dataclass
class ScreenScan:
    path: str = "unknown"                # esp_lcd | known_driver | unknown
    evidence: list[str] = field(default_factory=list)
    matched_adapters: list[str] = field(default_factory=list)
    spi_write_hint: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "evidence": self.evidence,
            "matched_adapters": self.matched_adapters,
            "spi_write_hint": self.spi_write_hint,
        }


@dataclass
class ImportedProject:
    source_path: str
    extract_path: str
    kind: str = "unknown"                # idf | arduino | platformio | bin | unknown
    project_root: str = ""
    target: str = ""
    idf_version: str = ""
    has_component_yml: bool = False
    has_sdkconfig: bool = False
    screen_scan: ScreenScan = field(default_factory=ScreenScan)
    error: str = ""
    tree_preview: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return {
            "source_path": self.source_path,
            "extract_path": self.extract_path,
            "kind": self.kind,
            "project_root": self.project_root,
            "target": self.target,
            "idf_version": self.idf_version,
            "has_component_yml": self.has_component_yml,
            "has_sdkconfig": self.has_sdkconfig,
            "screen_scan": self.screen_scan.to_dict(),
            "error": self.error,
            "tree_preview": self.tree_preview,
        }


# ---------------------------------------------------------------- 适配包注册表


def load_adapters(verified_only: bool = True) -> list[dict]:
    """读取 esp32_components/screen_adapters/*/adapter.json。"""
    out: list[dict] = []
    if not ADAPTER_DIR.is_dir():
        return out
    for d in sorted(ADAPTER_DIR.iterdir()):
        meta = d / "adapter.json"
        if not meta.is_file():
            continue
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if verified_only and not info.get("verified", False):
            continue
        info["_dir"] = str(d)
        info.setdefault("id", d.name)
        out.append(info)
    return out


# ---------------------------------------------------------------- 解压


def _safe_dest(name: str) -> str:
    return re.sub(r"[^\w\-.一-鿿]", "_", name)[:60] or "project"


def _under(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _extract_zip(src: Path, dest: Path) -> None:
    # ditto 对中文文件名兼容最好；失败回落 Python zipfile（先做名字校验）
    if shutil.which("ditto"):
        subprocess.run(["ditto", "-x", "-k", "--rsrc", str(src), str(dest)],
                       check=True, capture_output=True)
        return
    with zipfile.ZipFile(src) as zf:
        for member in zf.namelist():
            if member.startswith("/") or ".." in Path(member).parts:
                raise ValueError(f"zip 内含越界路径：{member}")
        zf.extractall(dest)


def _extract_tar(src: Path, dest: Path) -> None:
    with tarfile.open(src) as tf:
        for member in tf.getmembers():
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise ValueError(f"tar 内含越界路径：{member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"tar 内含链接，拒绝解压：{member.name}")
        # Python 3.12+ 的 data 过滤器会再拦一次
        try:
            tf.extractall(dest, filter="data")
        except TypeError:
            tf.extractall(dest)


def _check_escapes(dest: Path) -> None:
    """解压后复核：不许出现越界符号链接（文档 8.16）。"""
    for root, dirs, files in os.walk(dest, followlinks=False):
        for d in dirs:
            p = Path(root) / d
            if p.is_symlink() and not _under(p, dest):
                raise ValueError(f"符号链接越界：{p}")


def extract_source(src: Path) -> tuple[Path, str]:
    """返回 (解压目录, 错误)。文件夹不复制，bin 不解压。"""
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    lower = src.name.lower()

    if src.is_dir():
        return src, ""
    if lower.endswith(".bin"):
        return src, ""
    if not src.is_file():
        return src, "路径不存在"

    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = IMPORT_DIR / f"{stamp}_{_safe_dest(src.stem)}"
    if dest.exists():
        dest = IMPORT_DIR / f"{stamp}_{_safe_dest(src.stem)}_{os.getpid()}"

    try:
        if lower.endswith(".zip"):
            _extract_zip(src, dest)
        elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
            _extract_tar(src, dest)
        else:
            return src, f"不支持该文件类型：{src.name}"
    except Exception as exc:  # noqa: BLE001 - 解压异常一律转成可展示错误
        shutil.rmtree(dest, ignore_errors=True)
        return src, f"解压失败：{exc}"

    try:
        _check_escapes(dest)
    except ValueError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        return src, str(exc)

    if not any(dest.iterdir()):
        shutil.rmtree(dest, ignore_errors=True)
        return src, "压缩包内没有文件"
    return dest, ""


# ---------------------------------------------------------------- 工程根识别


def _looks_like_idf(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    has_proj = any(h in text for h in IDF_ROOT_HINTS)
    has_reg = "idf_component_register" in text
    return has_proj or has_reg


def find_project_root(base: Path | str) -> tuple[Path | None, list[Path]]:
    """广度优先找 CMakeLists.txt，返回 (最浅命中, 全部命中)。"""
    base = Path(base)
    hits: list[Path] = []
    level = [base]
    while level:
        nxt: list[Path] = []
        for d in level:
            try:
                if (d / "CMakeLists.txt").is_file() and _looks_like_idf(d / "CMakeLists.txt"):
                    hits.append(d)
            except OSError:
                continue
            try:
                for sub in d.iterdir():
                    if sub.is_dir() and sub.name not in SKIP_DIRS and not sub.is_symlink():
                        nxt.append(sub)
            except OSError:
                continue
        if hits:                      # 命中即止：保证取最浅层
            return hits[0], hits
        level = nxt
    return None, hits


def _tree_preview(base: Path, depth: int = 3, limit: int = 60) -> list[str]:
    out: list[str] = []
    base_depth = len(base.parts)
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel_depth = len(Path(root).parts) - base_depth
        if rel_depth >= depth:
            dirs[:] = []
            continue
        for name in sorted(dirs)[:12] + sorted(files)[:12]:
            out.append(str(Path(root, name).relative_to(base)))
            if len(out) >= limit:
                return out
    return out


def _read_target(root: Path) -> tuple[str, str]:
    """返回 (target, idf_version)，来源 sdkconfig > sdkconfig.defaults > CMakeLists。"""
    target, ver = "", ""
    for name in ("sdkconfig", "sdkconfig.defaults", "sdkconfig.defaults.esp32s3"):
        f = root / name
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = re.search(r'^CONFIG_IDF_TARGET="([^"]+)"', text, re.M)
        if m and not target:
            target = m[1]
        m = re.search(r'^CONFIG_IDF_INIT_VERSION="([^"]+)"', text, re.M)
        if m and not ver:
            ver = m[1]
        if target and ver:
            break
    if not target:
        try:
            cm = (root / "CMakeLists.txt").read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"set\s*\(\s*IDF_TARGET\s+([\w]+)", cm)
            if m:
                target = m[1]
        except OSError:
            pass
    return target, ver


# ---------------------------------------------------------------- 画面采集扫描


def scan_screen_path(root: Path | str) -> ScreenScan:
    """按文档 3.5.5.3 判三档：esp_lcd / known_driver / unknown。"""
    root = Path(root)
    scan = ScreenScan()
    adapters = load_adapters()
    adapter_hits: dict[str, list[str]] = {}
    found_esp_lcd = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if Path(fn).suffix.lower() not in SOURCE_SUFFIX:
                continue
            fp = Path(dirpath) / fn
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            if not found_esp_lcd:
                for sym in ESP_LCD_SYMBOLS:
                    if sym in text:
                        found_esp_lcd = True
                        scan.evidence.append(f"{fp.relative_to(root)}: {sym}")
                        break

            for hint in SPI_WRITE_HINTS:
                if hint in text:
                    scan.spi_write_hint = True
                    break

            for info in adapters:
                for sym in info.get("symbols", []):
                    if sym in text:
                        adapter_hits.setdefault(info["id"], []).append(
                            f"{fp.relative_to(root)}: {sym}")

    if found_esp_lcd:
        scan.path = "esp_lcd"
    elif adapter_hits:
        scan.path = "known_driver"
        scan.matched_adapters = sorted(adapter_hits)
        for aid, ev in adapter_hits.items():
            scan.evidence.extend(ev[:2])
    else:
        scan.path = "unknown"
    return scan


# ---------------------------------------------------------------- 主入口


def import_project(path: str) -> ImportedProject:
    src = Path(path).expanduser()
    proj = ImportedProject(source_path=str(src), extract_path="")

    if src.is_file() and src.name.lower().endswith(".bin"):
        proj.kind = "bin"
        proj.extract_path = str(src)
        proj.target = "esp32s3"
        return proj

    dest, err = extract_source(src)
    proj.extract_path = str(dest)
    if err:
        proj.error = err
        proj.kind = "unknown"
        proj.tree_preview = _tree_preview(dest) if dest.is_dir() else []
        return proj

    root, hits = find_project_root(dest)
    if root is None:
        # 判定是不是已知的非 IDF 工程
        if any(dest.rglob("*.ino")):
            proj.kind = "arduino"
            proj.error = "暂不支持该工程类型（Arduino / PlatformIO）"
        elif any(dest.rglob("platformio.ini")):
            proj.kind = "platformio"
            proj.error = "暂不支持该工程类型（Arduino / PlatformIO）"
        else:
            proj.kind = "unknown"
            proj.error = "未找到 ESP-IDF 工程（缺少 CMakeLists.txt）"
        proj.tree_preview = _tree_preview(dest)
        return proj

    if len(hits) > 1:
        proj.error = "工程根不唯一，请指定：" + "、".join(
            str(h.relative_to(dest)) for h in hits[:5])

    proj.kind = "idf"
    proj.project_root = str(root)
    proj.target, proj.idf_version = _read_target(root)
    proj.has_sdkconfig = (root / "sdkconfig").is_file()
    proj.has_component_yml = any(
        (root / p).is_file() for p in ("main/idf_component.yml", "idf_component.yml"))
    proj.screen_scan = scan_screen_path(root)
    return proj


if __name__ == "__main__":  # 手工冒烟
    import sys
    for arg in sys.argv[1:]:
        r = import_project(arg)
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
