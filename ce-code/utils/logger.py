"""统一日志配置 —— 服务/编排共用（承旧 server.py 的 logging.basicConfig）。

提供 ``get_logger(name)``：首次调用时按统一格式初始化 root logger（幂等），返回命名 logger。
知识服务（service/knowledge_api）与构建编排（service/build_service）共用，避免各处重复
basicConfig 导致格式漂移。
"""
from __future__ import annotations

import logging

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_CONFIGURED = False


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """返回命名 logger（首次调用幂等初始化 root 格式）。

    参数：
        name (str): logger 名（如 "ce-code.service"）。
        level (int): 根日志级别（默认 INFO）。
    返回：
        logging.Logger: 命名 logger。
    """
    global _CONFIGURED
    if not _CONFIGURED:
        logging.basicConfig(level=level, format=_FORMAT)
        _CONFIGURED = True
    return logging.getLogger(name)
