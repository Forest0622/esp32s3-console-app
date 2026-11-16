"""日志：文件 + 标准输出。文件用 RotatingFileHandler，避免长期运行撑爆磁盘。"""
from __future__ import annotations

import logging
import logging.handlers

import config

_configured = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    global _configured
    config.ensure_dirs()
    logger = logging.getLogger("console")
    if _configured:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(level)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(level)
    logger.addHandler(sh)

    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str = "console") -> logging.Logger:
    return logging.getLogger(name)
