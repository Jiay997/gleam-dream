"""结构化日志模块。

在标准库 logging 之上提供 JSON 行格式化器（timestamp/level/logger/
message/request_id），并通过 contextvars 在请求上下文中传递 request_id。
``setup_logging`` 会同时接管 uvicorn 系列日志，使启动与访问日志同样
输出为结构化 JSON。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime

# 请求级日志上下文：默认占位符 "-"（非请求上下文时）
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def set_request_id(request_id: str) -> contextvars.Token[str]:
    """设置当前请求上下文的 request_id。

    Args:
        request_id: 请求 ID（ULID）。

    Returns:
        contextvars Token，供 finally 中调用 :func:`reset_request_id` 复原。
    """
    return request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token[str]) -> None:
    """将 request_id 上下文复原为进入请求前的值。

    Args:
        token: :func:`set_request_id` 返回的 Token。
    """
    request_id_var.reset(token)


def get_request_id() -> str:
    """读取当前上下文的 request_id。

    Returns:
        当前请求 ID；非请求上下文返回 "-"。
    """
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    """JSON 行结构化日志格式化器。

    输出字段：timestamp（本地时区 ISO8601）、level、logger、message、
    request_id；异常时附加 exc_info 堆栈字符串。
    """

    def format(self, record: logging.LogRecord) -> str:
        """将日志记录格式化为单行 JSON。

        Args:
            record: 标准库日志记录。

        Returns:
            单行 JSON 字符串（中文不转义）。
        """
        payload: dict[str, str] = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    """初始化全局日志并接管 uvicorn 日志。

    清空根 logger 既有 handler 后挂载 JSON 格式 stdout handler，
    并将 uvicorn/uvicorn.error/uvicorn.access 的日志统一交给根
    logger 传播输出，保证全部日志为结构化 JSON。

    Args:
        level: 全局日志级别，默认 INFO。
    """
    formatter = JsonFormatter()

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    # uvicorn 各子 logger 移除自带 handler，统一向根 logger 传播
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
