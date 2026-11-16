"""串口探测与握手（文档 2.1 / 6 章 / 7 章 Serial-Detect-Handshake）。

- 按端口名特征枚举 ESP32-S3 串口（CH340 / CP210x / 原生 USB）
- 打开后按既定时序拉 DTR/RTS 让板子复位，再发 ping
- 心跳：200 ms 一次 ping，500 ms 无 pong 判为未部署组件
"""

from __future__ import annotations

import glob
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import serial
from serial.tools import list_ports

PORT_PATTERNS = (
    "usbserial", "wchusbserial", "usbmodem", "SLAB_USBtoUART",
    "CH340", "cu.usbserial", "cu.wchusbserial",
)
BAUD = 921600
PING_INTERVAL = 0.2
PONG_TIMEOUT = 0.5


def list_candidate_ports() -> list[dict]:
    out: list[dict] = []
    for p in list_ports.comports():
        name = p.device or ""
        hay = f"{name} {p.description or ''} {p.hwid or ''}".lower()
        if any(pat.lower() in hay for pat in PORT_PATTERNS):
            out.append({
                "device": name,
                "description": p.description or "",
                "hwid": p.hwid or "",
                "serial": p.serial_number or "",
            })
    if not out:
        for g in ("/dev/cu.usbserial*", "/dev/cu.wchusbserial*", "/dev/cu.usbmodem*"):
            for dev in sorted(glob.glob(g)):
                out.append({"device": dev, "description": "", "hwid": "", "serial": ""})
    return out


def open_port(device: str, baud: int = BAUD, reset: bool = True) -> serial.Serial:
    """按既定时序打开：DTR=False / RTS=True(复位) → 释放 → 清空输入缓冲。"""
    ser = serial.Serial()
    ser.port = device
    ser.baudrate = baud
    ser.timeout = 0.05
    ser.write_timeout = 1.0
    ser.open()
    try:
        if reset:
            ser.dtr = False
            ser.rts = True
            time.sleep(0.05)
            ser.rts = False
            time.sleep(0.35)
        ser.reset_input_buffer()
    except (serial.SerialException, OSError):
        pass
    return ser


@dataclass
class ConsoleLink:
    """一条打开的串口连接 + 读取线程。"""

    device: str
    parser: object                     # SerialDataParser
    ser: serial.Serial | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _last_rx: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    on_disconnect: Callable[[], None] | None = None
    bytes_total: int = 0

    def start(self, reset: bool = True) -> bool:
        try:
            self.ser = open_port(self.device, reset=reset)
        except (serial.SerialException, OSError) as exc:
            self.ser = None
            self.last_error = str(exc)
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._rx_loop, name="console-rx", daemon=True)
        self._thread.start()
        return True

    # ------------------------------------------------------------- 读

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ser = self.ser
                if ser is None:
                    break
                chunk = ser.read(4096)
                if chunk:
                    self._last_rx = time.time()
                    self.bytes_total += len(chunk)
                    self.parser.feed(chunk)
            except (serial.SerialException, OSError, TypeError):
                break
        if not self._stop.is_set() and self.on_disconnect:
            self.on_disconnect()

    # ------------------------------------------------------------- 写

    def write_line(self, obj: dict) -> bool:
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        return self.write_raw(line)

    def write_raw(self, data: bytes) -> bool:
        with self._lock:
            ser = self.ser
            if ser is None:
                return False
            try:
                ser.write(data)
                return True
            except (serial.SerialException, OSError):
                return False

    def send_key(self, index: int) -> bool:
        return self.write_line({"cmd": "key", "idx": int(index)})

    def send_ping(self) -> bool:
        # ping 只证明链路活着；组件清单必须靠 console_info 主动问，
        # 固件只在 console_proto_init 里发一次 console_status，开端口晚一点就永久错过。
        ok = self.write_line({"cmd": "ping"})
        self.write_line({"cmd": "console_info"})
        return ok

    def send_report_cfg(self, display: bool, led: bool, key: bool) -> bool:
        return self.write_line({"cmd": "set_report", "display": bool(display),
                                "led": bool(led), "key": bool(key)})

    def send_console_info(self) -> bool:
        return self.write_line({"cmd": "console_info"})

    # ------------------------------------------------------------- 活

    def alive(self, window: float = 8.0) -> bool:
        return (time.time() - self._last_rx) < window

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                except (serial.SerialException, OSError):
                    pass
                self.ser = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)


def probe_mirror(device: str, timeout: float = 2.5) -> dict:
    """短探测：发 ping，看是否收到 pong / console_status。"""
    result = {"has_components": False, "pong": False, "console_status": None,
              "bytes": 0, "error": ""}
    seen: dict = {}

    def on_tel(obj: dict) -> None:
        if obj.get("resp") == "pong":
            result["pong"] = True
        if obj.get("type") == "console_status":
            result["console_status"] = obj.get("data", {})
            result["has_components"] = True

    from serial_data_parser import SerialDataParser
    parser = SerialDataParser(on_telemetry=on_tel)
    try:
        ser = open_port(device, reset=True)
    except (serial.SerialException, OSError) as exc:
        result["error"] = str(exc)
        return result

    try:
        end = time.time() + timeout
        next_ping = time.time()
        while time.time() < end:
            if time.time() >= next_ping:
                ser.write((json.dumps({"cmd": "ping"}) + "\n").encode())
                ser.write((json.dumps({"cmd": "console_info"}) + "\n").encode())
                next_ping = time.time() + PING_INTERVAL
            data = ser.read(4096)
            if data:
                result["bytes"] += len(data)
                parser.feed(data)
            else:
                time.sleep(0.01)
    finally:
        try:
            ser.close()
        except (serial.SerialException, OSError):
            pass
    return result


if __name__ == "__main__":
    print(json.dumps(list_candidate_ports(), ensure_ascii=False, indent=2))
