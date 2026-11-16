"""串口数据解析（文档 6 章 / 7 章 Serial-Data-Parser）。

混合流设计（修订后，详见文档 6.1 说明）：
- 首字节 0xA5 → 二进制帧：0xA5 0x5A | u32 len(LE) | u16 crc16 | payload
- 其它 → 文本行，以 \n 结尾，JSON
画面只能走二进制（base64@115200 实测 0.06 fps，不可用）。
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Callable

FRAME_MAGIC = b"\xa5\x5a"

OP_END = 0x00
OP_SKIP = 0x01
OP_RLE = 0x02
OP_STATE = 0x03
OP_RAW = 0x04
OP_PART = 0x05

PART_PIXELS = 1024
DEFAULT_W = 320
DEFAULT_H = 240
MAX_PAYLOAD = 1 << 20        # 单帧负载上限，防异常长度把内存吃穿


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass
class ParserStats:
    frames: int = 0
    parts: int = 0
    bad_json: int = 0
    bad_crc: int = 0
    bad_len: int = 0
    dropped_display: int = 0


@dataclass
class SerialDataParser:
    width: int = DEFAULT_W
    height: int = DEFAULT_H
    on_frame: Callable[[bytes], None] | None = None
    on_telemetry: Callable[[dict], None] | None = None
    stats: ParserStats = field(default_factory=ParserStats)

    def __post_init__(self) -> None:
        self._fb = bytearray(self.width * self.height * 2)
        self._buf = bytearray()      # 待解析二进制缓冲（含已收到的头）
        self._line = bytearray()     # 文本行缓冲
        self._in_frame = False
        self._need = 0

    # ---------------------------------------------------------- 入口

    def feed(self, data: bytes) -> None:
        i = 0
        n = len(data)
        while i < n:
            if self._in_frame:
                need = self._need - len(self._buf)
                take = min(need, n - i)
                self._buf += data[i:i + take]
                i += take

                # 先攒满 8 字节头，再按头里的长度等负载：
                # 不能一拿到 8 字节就当整帧结束（那样负载永远收不到）
                if self._need == 8 and len(self._buf) >= 8:
                    length = struct.unpack_from("<I", self._buf, 2)[0]
                    if length == 0 or length > MAX_PAYLOAD:
                        self.stats.bad_len += 1
                        self._buf = bytearray()
                        self._in_frame = False
                        self._need = 0
                        continue
                    self._need = 8 + length
                    continue

                if self._need > 8 and len(self._buf) >= self._need:
                    self._handle_frame(bytes(self._buf))
                    self._buf = bytearray()
                    self._in_frame = False
                    self._need = 0
                continue

            b = data[i]
            if b == 0xA5:
                if i + 1 < n and data[i + 1] == 0x5A:
                    self._buf = bytearray(data[i:i + 2])
                    self._in_frame = True
                    self._need = 8          # 等 magic + len + crc
                    i += 2
                    continue
                if i + 1 >= n:              # 半个 magic，留到下批
                    self._buf = bytearray(data[i:i + 1])
                    self._in_frame = True
                    self._need = 8
                    i += 1
                    continue
                i += 1                      # 单字节 0xA5 无 0x5A，丢弃
                continue

            if b == 0x0A:
                self._handle_line(bytes(self._line))
                self._line = bytearray()
                i += 1
                continue
            if b == 0x0D:
                i += 1
                continue
            if len(self._line) < 8192:
                self._line.append(b)
            i += 1

    # ---------------------------------------------------------- 二进制帧

    def _handle_frame(self, raw: bytes) -> None:
        self.stats.frames += 1
        if len(raw) < 8:
            self.stats.bad_len += 1
            return
        length = struct.unpack_from("<I", raw, 2)[0]
        crc = struct.unpack_from("<H", raw, 6)[0]
        payload = raw[8:8 + length]
        if len(payload) != length:
            self.stats.bad_len += 1
            return
        if crc16_ccitt(payload) != crc:
            self.stats.bad_crc += 1
            return
        self._apply_ops(payload)

    def _apply_ops(self, payload: bytes) -> None:
        i = 0
        while i < len(payload):
            op = payload[i]
            if op == OP_END:
                if self.on_frame:
                    self.on_frame(bytes(self._fb))
                return
            if op == OP_PART:
                if i + 3 > len(payload):
                    self.stats.bad_len += 1
                    return
                idx = payload[i + 1]
                cnt = struct.unpack_from("<H", payload, i + 2)[0]
                start = i + 4
                data = payload[start:start + cnt * 2]
                if len(data) != cnt * 2:
                    self.stats.bad_len += 1
                    return
                off = idx * PART_PIXELS * 2
                end = min(off + len(data), len(self._fb))
                if off < len(self._fb):
                    self._fb[off:end] = data[:end - off]
                self.stats.parts += 1
                i = start + cnt * 2
                continue
            if op == OP_RAW:
                if i + 1 > len(payload):
                    self.stats.bad_len += 1
                    return
                data = payload[i + 1:]
                self._fb[0:min(len(data), len(self._fb))] = data[:len(self._fb)]
                i = len(payload)
                continue
            if op == OP_SKIP:
                i += 3
                continue
            if op == OP_RLE:
                i += 5
                continue
            if op == OP_STATE:
                i += 5
                continue
            # 未知 op：本帧作废，避免错位继续写坏 framebuffer
            self.stats.bad_len += 1
            return

    # ---------------------------------------------------------- 文本行

    def _handle_line(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            obj = json.loads(text.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.stats.bad_json += 1
            return
        if isinstance(obj, dict) and self.on_telemetry:
            self.on_telemetry(obj)
