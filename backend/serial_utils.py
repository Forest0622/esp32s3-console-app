"""串口工具：统一开关串口，保证 fd 释放（macOS 上残留独占锁会导致后续 esptool 报 port busy）。"""
from __future__ import annotations

import contextlib
import time

import serial
import serial.tools.list_ports

import config


class PortLease:
    """with PortLease(port, baud) as s: ... 退出时必定关闭。"""

    def __init__(self, port: str, baud: int, timeout: float = 0.12):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None

    def __enter__(self) -> serial.Serial:
        self._ser = open_port(self.port, self.baud, self.timeout)
        return self._ser

    def __exit__(self, *exc) -> None:
        close_port(self._ser)
        self._ser = None


def open_port(port: str, baud: int, timeout: float = 0.12) -> serial.Serial:
    """打开并做与已知可用流程一致的复位序列：dtr=False / rts 脉冲 / 清缓冲。"""
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = int(baud)
    ser.timeout = timeout
    ser.write_timeout = 2.0
    ser.open()
    try:
        ser.dtr = False
        ser.rts = True
        time.sleep(0.05)
        ser.rts = False
        time.sleep(0.35)
    except Exception:
        # 某些板子上 dtr/rts 不可控，不影响后续通讯
        pass
    ser.reset_input_buffer()
    return ser


def close_port(ser) -> None:
    if ser is None:
        return
    try:
        with contextlib.suppress(Exception):
            ser.reset_input_buffer()
        with contextlib.suppress(Exception):
            ser.reset_output_buffer()
        with contextlib.suppress(Exception):
            ser.close()
    finally:
        # 给 macOS 一点时间释放独占锁，紧随其后的 esptool 才不会报 port busy
        time.sleep(0.35)


def list_candidate_ports() -> list[str]:
    """只返回名字像 USB 串口的设备（按 config.PORT_PATTERNS 过滤）。"""
    out = []
    for p in serial.tools.list_ports.comports():
        name = (p.device or "").lower()
        if any(k.lower() in name for k in config.PORT_PATTERNS):
            out.append(p.device)
    return sorted(out)

def hard_release(device: str = "", timeout: float = 2.5) -> bool:
    """把所有指向该端口的 Serial 对象真的关掉，并等到端口能再次被独占。

    实测坑：`ConsoleLink.stop()` 只 close 自己那个对象，读取线程和外层异常栈
    里残留的 Serial 仍握着 fd，macOS 的独占 flock 不会释放 —— 于是下一次
    esptool 读芯片 ID 直接拿不到（表现为 chip_id 为空、备份信息变 null）。
    """
    import gc
    deadline = time.time() + timeout
    while True:
        gc.collect()
        closed = False
        for obj in gc.get_objects():
            if isinstance(obj, serial.Serial):
                try:
                    if obj.is_open:
                        obj.close()
                        closed = True
                except Exception:
                    pass
        if closed:
            time.sleep(0.2)
        if not device:
            return True
        try:
            probe = serial.Serial()
            probe.port = device
            probe.baudrate = 115200
            probe.timeout = 0.1
            probe.open()
            probe.close()
            return True
        except Exception:
            if time.time() > deadline:
                return False
            time.sleep(0.25)
