"""画面采集适配包注册中心。

一个适配包 = screen_adapters/<id>/adapter.json + adapter.c + CMakeLists.txt。
adapter.json 里必须声明 adapter.c 里 wrap 的符号名，且 verified 只有在真实工程
实测通过后才允许置 true —— 未实测的适配包不出现在可勾选列表（开发文档 8.18）。
"""
from __future__ import annotations

import json

import config
import log_setup

log = log_setup.get_logger("adapters")


def load_all() -> list[dict]:
    out = []
    if not config.ADAPTER_DIR.exists():
        return out
    for d in sorted(config.ADAPTER_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = d / "adapter.json"
        if not meta.exists():
            log.warning("适配包 %s 缺 adapter.json，跳过", d.name)
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("适配包 %s 的 adapter.json 解析失败：%s", d.name, e)
            continue
        data.setdefault("id", d.name)
        data.setdefault("symbols", [])
        data.setdefault("headers", [])
        data.setdefault("verified", False)
        data["dir"] = str(d)
        out.append(data)
    return out


def list_adapters() -> list[dict]:
    """只返回 verified 的适配包。"""
    return [a for a in load_all() if a.get("verified")]


def list_pending() -> list[dict]:
    return [a for a in load_all() if not a.get("verified")]


def resolve(scan: dict, manual_symbol: str | None = None) -> list[str]:
    """按静态扫描结果返回可注入的适配包 id；空列表表示画面采集不可用。"""
    if manual_symbol:
        return [f"manual:{manual_symbol}"]
    ids = []
    for a in list_adapters():
        if any(s in (scan.get("symbols_seen") or []) for s in a["symbols"]):
            ids.append(a["id"])
        elif any(h in (scan.get("headers_seen") or []) for h in a["headers"]):
            ids.append(a["id"])
    return ids
