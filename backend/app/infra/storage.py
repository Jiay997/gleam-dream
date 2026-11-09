"""本地磁盘存储模块：以本地文件系统模拟 OSS 对象存储。

对象键（object_key）即存储根目录下的相对路径（POSIX 风格）。main.py 会将
``/files`` 前缀通过 StaticFiles 挂载到存储根目录，客户端可直接通过
``/files/{object_key}`` 下载对象。所有磁盘 IO 异常统一转换为契约业务码
（路径非法 50004 / 不存在 10002 / 读写失败 80003）。
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from app.core.config import settings
from app.core.errors import BizError, ErrorCode


class LocalStorage:
    """本地磁盘存储客户端。

    以 backend/storage/（由 settings.STORAGE_DIR 解析）为根目录，
    提供 bytes 落盘、按对象键取路径、按对象键打开文件等能力。

    Attributes:
        _root: 存储根目录绝对路径。
    """

    def __init__(self, root: Path) -> None:
        """构造存储客户端并确保根目录存在。

        Args:
            root: 存储根目录。

        Raises:
            BizError: 80003 根目录创建失败（如磁盘不可写）。
        """
        self._root = root
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BizError(ErrorCode.STORAGE_ERROR, f"存储根目录创建失败: {exc}") from exc

    @property
    def root_path(self) -> Path:
        """获取存储根目录。

        Returns:
            根目录 Path（绝对路径）。
        """
        return self._root

    def _resolve(self, object_key: str) -> Path:
        """将对象键解析为根目录内的绝对路径，并防御路径穿越。

        Args:
            object_key: 对象键（根目录内相对路径，支持 / 或 \\ 分隔）。

        Returns:
            目标文件绝对路径。

        Raises:
            BizError: 50004 对象键越出存储根目录（疑似路径穿越）。
        """
        target = (self._root / object_key).resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError as exc:
            raise BizError(ErrorCode.UPLOAD_FILE_INVALID, "非法的存储路径") from exc
        return target

    def save_bytes(self, rel_path: str, data: bytes) -> str:
        """将二进制内容写入相对路径并返回对象键。

        Args:
            rel_path: 存储根目录内的相对路径（同时作为对象键）。
            data: 待写入的二进制内容。

        Returns:
            POSIX 风格对象键（可直接用于 /files/{object_key} 下载）。

        Raises:
            BizError: 50004 路径非法；80003 磁盘写入失败。
        """
        target = self._resolve(rel_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise BizError(ErrorCode.STORAGE_ERROR, f"文件写入失败: {exc}") from exc
        return rel_path.replace("\\", "/")

    def object_path(self, object_key: str) -> Path:
        """获取对象键对应的磁盘绝对路径。

        Args:
            object_key: 对象键。

        Returns:
            目标文件绝对路径。

        Raises:
            BizError: 50004 路径非法。
        """
        return self._resolve(object_key)

    def size_of(self, object_key: str | None) -> int:
        """获取对象文件字节数（用于 media_files.file_size_bytes 回填）。

        Args:
            object_key: 对象键（可空）。

        Returns:
            文件字节数；对象键为空、路径非法或文件不存在时返回 0。
        """
        if not object_key:
            return 0
        try:
            path = self._resolve(str(object_key))
        except BizError:
            return 0
        try:
            return int(path.stat().st_size) if path.is_file() else 0
        except OSError:
            return 0

    def open(self, object_key: str) -> BinaryIO:
        """以只读二进制模式打开对象。

        Args:
            object_key: 对象键。

        Returns:
            二进制文件句柄（调用方负责 close）。

        Raises:
            BizError: 10002 文件不存在；80003 打开失败；50004 路径非法。
        """
        target = self._resolve(object_key)
        try:
            return target.open("rb")
        except FileNotFoundError as exc:
            raise BizError(ErrorCode.NOT_FOUND, "文件不存在") from exc
        except OSError as exc:
            raise BizError(ErrorCode.STORAGE_ERROR, f"文件打开失败: {exc}") from exc


def _build_storage_root() -> Path:
    """根据 settings.STORAGE_DIR 解析最终存储根目录。

    相对路径以 backend/ 根目录为锚点解析，避免受启动工作目录影响。

    Returns:
        存储根目录绝对路径。
    """
    configured = Path(settings.STORAGE_DIR)
    if not configured.is_absolute():
        configured = Path(__file__).resolve().parents[2] / configured
    return configured.resolve()


# 进程级存储单例
storage = LocalStorage(_build_storage_root())
