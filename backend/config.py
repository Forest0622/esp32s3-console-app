"""全局配置与路径常量。所有模块只从这里读取配置，不各自硬编码。"""
from __future__ import annotations

import os
import pathlib

APP_NAME = "ESP32-S3 Console"

APP_DIR = pathlib.Path.home() / "Library" / "Application Support" / APP_NAME
BACKUP_DIR = APP_DIR / "backups"
IMPORT_DIR = APP_DIR / "projects_imported"
WORKSPACE_DIR = APP_DIR / "workspace"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "console.log"
PIN_CONFIG_FILE = APP_DIR / "pins.json"

# 项目根 = backend/ 的上一级
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT_DIR = PROJECT_ROOT / "esp32_components"
ADAPTER_DIR = COMPONENT_DIR / "screen_adapters"
UI_DIR = PROJECT_ROOT / "frontend-ui"

HTTP_HOST = "127.0.0.1"
HTTP_PORT = int(os.environ.get("CONSOLE_HTTP_PORT", "8781"))

# 串口参数
SERIAL_BAUD = 921600          # 上报口；画面帧需要这个带宽
HANDSHAKE_BAUD_FALLBACK = 115200
FLASH_BAUD = 460800
BACKUP_BAUD = 115200
BACKUP_TOTAL = 0x200000       # 2 MB
BACKUP_CHUNK = 0x20000        # 128 KB 分块（整段读必 corrupt）
BACKUP_RETRIES = 3
APP_OFFSET = "0x10000"

# 画面
FRAME_W_DEFAULT = 320
FRAME_H_DEFAULT = 240
FRAME_MAX_CHUNKS = 128
TARGET_CHIP = "esp32s3"

PORT_PATTERNS = (
    "usbserial", "wchusbserial", "usbmodem", "SLAB_USBtoUART", "CH340",
    "cu.wchusbserial", "cu.usbserial",
)

# 无画面 3 秒判定
NO_FRAME_TIMEOUT = 3.0
PING_INTERVAL = 0.2
PING_TIMEOUT = 0.5


def ensure_dirs() -> None:
    for d in (APP_DIR, BACKUP_DIR, IMPORT_DIR, WORKSPACE_DIR):
        d.mkdir(parents=True, exist_ok=True)
