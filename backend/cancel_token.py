"""任务取消令牌（停止按钮 / 任何长任务的统一中断入口）。

为什么单独一个模块：备份、烧录、编译三条链路分散在三个文件里，取消必须
是「一处置位、三处都能看到」。用模块级 Event 而不是给每个函数加参数，
是因为 esptool 子进程、idf.py 子进程都是别人拉起来的，只能靠轮询。
"""

from __future__ import annotations

import threading

_event = threading.Event()


def request() -> None:
    """请求取消当前任务（幂等）。"""
    _event.set()


def clear() -> None:
    """新任务开始前清掉上一次的取消标记。"""
    _event.clear()


def is_cancelled() -> bool:
    return _event.is_set()
