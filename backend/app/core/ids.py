"""分布式 ID 与哈希工具模块。

提供请求追踪所需的 ULID 请求 ID、通用 UUID、以及幂等键/指纹所需的
SHA-256 摘要工具。
"""

from __future__ import annotations

import hashlib
import uuid

import ulid


def new_request_id() -> str:
    """生成 ULID 格式的请求 ID。

    按契约 §8.1，request_id 每请求生成（ULID），贯穿日志与 Agent trace。
    兼容 python-ulid 4.x（default_generator.generate()）与 2.x（ulid.new()）。

    Returns:
        26 位大写 ULID 字符串（字典序即时间序）。

    Raises:
        无（两种版本的 API 均已覆盖）。
    """
    generator = getattr(ulid, "default_generator", None)
    if generator is not None:  # python-ulid >= 4.0
        return str(generator.generate())
    return str(ulid.new())  # python-ulid 2.x/3.x


def new_uuid() -> str:
    """生成 UUID v4 字符串。

    用于幂等键（契约 §8.4）、普通业务主键等场景。

    Returns:
        标准带连字符的 36 位 UUID 字符串。
    """
    return str(uuid.uuid4())


def sha256_hash(s: str) -> str:
    """计算字符串的 SHA-256 十六进制摘要。

    用于幂等键指纹（如 `sha256(project_id + node + input_hash)`）、
    文件指纹等场景。

    Args:
        s: 原始字符串（UTF-8 编码后计算）。

    Returns:
        64 位十六进制小写摘要字符串。
    """
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
