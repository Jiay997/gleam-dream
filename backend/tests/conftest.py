"""pytest 全局夹具：把媒体模型调用切换到进程内 stub，避免真实三方调用。

背景：项目已接入阿里云百炼与 Agnes AI（图片/视频/TTS 为真实调用），而集成
测试并不校验三方真实生成质量，只校验任务编排、冻结/退款、重试与事件。
真实调用会受网络与队列/限流影响，导致 60 秒级超时与随机失败。

测试环境说明：当前用例直接连接 ``backend/.env`` 指向的开发库，未做独立
测试库/事务回滚隔离。CI 或无外网环境请设置 ``GD_SKIP_LLM_LIVE=1``，
并执行 ``pytest -m "not llm_live"`` 跳过真实 LLM 用例。

实现方式：**只在测试进程内**替换 ``ModelGateway.build_provider``——媒体能力
（image/video/tts/bgm）一律返回内置 ``StubProvider``，LLM 仍走真实配置。
这样不修改数据库、不存在测试中断后残留配置的风险（此前曾发生测试中断把
``model_registry.protocol`` 留成 stub 的事故）。
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import pytest
import pytest_asyncio
from _pytest.monkeypatch import MonkeyPatch

from app.infra.model_gateway.gateway import ModelGateway


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """按环境变量跳过真实 LLM 用例（CI/离线环境使用 ``GD_SKIP_LLM_LIVE=1``）。

    Args:
        config: pytest 配置对象。
        items: 已收集的测试项列表。

    Returns:
        None。
    """
    if os.environ.get("GD_SKIP_LLM_LIVE", "").lower() not in {"1", "true", "yes"}:
        return
    skip_marker = pytest.mark.skip(reason="GD_SKIP_LLM_LIVE=1：跳过真实 LLM 用例")
    for item in items:
        if "llm_live" in item.keywords:
            item.add_marker(skip_marker)


#: 需要 stub 化的能力（LLM 保持真实，以覆盖密钥与协议校验）
_MEDIA_CAPABILITIES = frozenset({"image", "video", "tts", "bgm"})


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def stub_media_models() -> AsyncIterator[None]:
    """session 级夹具：测试期间媒体能力统一走进程内 StubProvider。

    Yields:
        无（仅提供测试期间的稳定媒体生成环境）。

    Raises:
        无。
    """
    monkeypatch = MonkeyPatch()
    original_build_provider = ModelGateway.build_provider

    def _build_provider(self: ModelGateway, registry):  # type: ignore[no-untyped-def]
        """测试专用提供商构建：媒体能力返回 stub，LLM 沿用原逻辑。

        Args:
            self: 网关实例。
            registry: 模型注册行。

        Returns:
            提供商实例。
        """
        if registry.capability in _MEDIA_CAPABILITIES:
            return self.providers["stub"]
        return original_build_provider(self, registry)

    monkeypatch.setattr(ModelGateway, "build_provider", _build_provider)
    try:
        yield
    finally:
        monkeypatch.undo()
