"""模型网关测试模块。

覆盖六组场景：
1. Stub 图片生成：object_key / 文件存在 / simulated 标记 / AiCallLog 记账；
2. Stub TTS：真实 WAV 可被 wave 模块读取且时长 > 0；
3. Stub 视频：JSON 元数据 + 首帧关键帧 poster_key；
4. 确定性路由：llm_text 优先命中 deepseek-flash；锁定 glm-5.3-flash 命中；
5. 真实 LLM 冒烟（marker ``llm_live``，默认执行）：DeepSeek 与 GLM 各一次，
   网络/密钥失败时 xfail 并输出响应摘要（连通性结论见测试报告）；
6. 失败路由：锁定不存在的模型 → BizError(80001)。

说明：测试直连开发库（ai_call_logs 只追加，不影响断言独立性）；生成产物
落入 backend/storage/stub/ 下，占位文件体积极小，不做清理。所有用例与其
他测试文件一致，统一挂在 pytest-asyncio 的 session 级 event loop 上
（全局 async_engine 连接池绑定首个建连的循环，跨循环复用会报
"attached to a different loop"，故不再自建独立 loop）。
"""

from __future__ import annotations

import json
import wave

import pytest
from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.infra.model_gateway import GenRequest, model_gateway
from app.infra.model_gateway.router import pick_model
from app.infra.storage import storage
from app.models import AiCallLog

#: 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_stub_image_generate_and_billing() -> None:
    """Stub 图片生成：产物落盘、simulated 标记、AiCallLog 记账 cost>0。"""
    result = await model_gateway.generate(
        "image",
        "text_to_image",
        GenRequest(
            capability="image",
            prompt="夕阳下的赛博朋克城市街道",
            params={"asset_name": "测试场景图"},
            # 锁定沙箱模型：避免命中真实百炼模型（测试不产生三方费用）
            model_hint="stub-image-hd",
        ),
    )
    assert result.ok, f"生成失败: {result.error}"
    assert result.object_key
    assert result.media_url == f"/files/{result.object_key}"
    assert result.meta["simulated"] is True
    assert result.meta["cost_credits"] > 0

    image_path = storage.object_path(result.object_key)
    assert image_path.exists(), "SVG 占位图文件未落盘"

    async with async_session_factory() as session:
        log_row = (
            await session.execute(
                select(AiCallLog).where(AiCallLog.trace_id == result.meta["trace_id"])
            )
        ).scalar_one_or_none()
    assert log_row is not None, "AiCallLog 未落库"
    assert log_row.status == "success"
    assert log_row.cost_credits > 0
    assert log_row.model_key in {"stub-image", "stub-image-hd"}
    # llm_text 之外的任务类型应原样落 call_type
    assert log_row.call_type == "text_to_image"


async def test_stub_tts_wav_readable() -> None:
    """Stub TTS：WAV 文件可被 wave 模块读取、时长 > 0。"""
    prompt = "这是一段用于测试语音合成时长的文本，共约二十个字符"
    result = await model_gateway.generate(
        "tts", "tts", GenRequest(capability="tts", prompt=prompt, model_hint="stub-tts")
    )
    assert result.ok, f"生成失败: {result.error}"
    assert result.object_key
    assert result.meta["simulated"] is True

    wav_path = storage.object_path(result.object_key)
    assert wav_path.exists()
    with wave.open(str(wav_path), "rb") as wav_file:
        frames = wav_file.getnframes()
        sample_rate = wav_file.getframerate()
        duration_seconds = frames / sample_rate
    assert duration_seconds > 0
    assert result.meta["duration_ms"] == int(duration_seconds * 1000)
    # 4 字/秒估算：约 21 字 → 6 秒，且不超过 30s 上限
    assert 1 <= duration_seconds <= 30


async def test_stub_video_metadata_and_poster() -> None:
    """Stub 视频：可播放 MP4 产物 + 首帧 SVG 关键帧 poster_key。"""
    result = await model_gateway.generate(
        "video",
        "text_to_video",
        GenRequest(capability="video", prompt="无人机航拍雪山日出", model_hint="stub-video"),
    )
    assert result.ok, f"生成失败: {result.error}"
    assert result.meta.get("placeholder") == "video"
    assert result.meta["simulated"] is True

    poster_key = result.meta["poster_key"]
    assert poster_key, "缺少首帧关键帧 object_key"
    assert storage.object_path(poster_key).exists(), "首帧 SVG 未落盘"

    # P0-3 修复后 stub 视频产出可播放 MP4，元数据保留在 result.meta
    video_path = storage.object_path(result.object_key)
    assert video_path.exists()
    assert video_path.read_bytes()[4:8] == b"ftyp", "stub 视频应为有效 MP4"
    assert 2000 <= result.meta["duration_ms"] <= 4000
    assert result.meta["fps"] > 0
    assert result.meta["frames"] > 0
    assert result.meta["poster_key"] == poster_key


async def test_pick_model_llm_text_and_locked() -> None:
    """确定性路由：llm_text 默认命中 deepseek-flash；锁定 glm 命中 glm。"""
    async with async_session_factory() as session:
        default_model = await pick_model(session, "llm", "llm_text")
    assert default_model.model_key == "deepseek-flash", (
        "llm_text 路由应优先命中 deepseek-flash（质量分 0.88 折 10 分制后 "
        "0.7*8.8-0.3=5.86 高于 glm 的 0.7*8.2-0=5.74）"
    )

    async with async_session_factory() as session:
        locked_model = await pick_model(
            session, "llm", "llm_text", user_locked_model="glm-5.3-flash"
        )
    assert locked_model.model_key == "glm-5.3-flash"


async def _xfail_llm_result(result, provider_name: str) -> None:
    """对真实 LLM 结果做连通性判定：失败时 xfail 并打印响应摘要。

    Args:
        result: 网关返回的 GenResult。
        provider_name: 提供商名（用于输出标识）。
    """
    if not result.ok:
        print(f"\n[{provider_name}] 连通失败摘要: {result.error}")
        pytest.xfail(f"{provider_name} 真实调用失败: {result.error}")


@pytest.mark.llm_live
async def test_llm_live_deepseek() -> None:
    """真实 DeepSeek 冒烟：ok=True、text 非空、tokens>0；失败 xfail。"""
    try:
        result = await model_gateway.generate(
            "llm",
            "llm_text",
            GenRequest(capability="llm", prompt="用一句话介绍你自己，20字以内"),
        )
    except BizError as exc:
        print(f"\n[DeepSeek] 路由/调用异常: code={exc.code} message={exc.message}")
        pytest.xfail(f"DeepSeek 调用异常: {exc.code} {exc.message}")
    await _xfail_llm_result(result, "DeepSeek")
    print(f"\n[DeepSeek] 响应摘要: text={result.text!r} meta={result.meta}")
    assert (result.text or "").strip(), "DeepSeek 返回文本为空"
    assert (result.meta.get("tokens_in") or 0) > 0, "DeepSeek usage 缺少输入 token"
    assert (result.meta.get("tokens_out") or 0) > 0, "DeepSeek usage 缺少输出 token"


@pytest.mark.llm_live
async def test_llm_live_glm() -> None:
    """真实 GLM 冒烟：锁定 glm-5.3-flash 调用，ok=True、text 非空、tokens>0。"""
    try:
        result = await model_gateway.generate(
            "llm",
            "llm_text",
            GenRequest(
                capability="llm",
                prompt="用一句话介绍你自己，20字以内",
                model_hint="glm-5.3-flash",
            ),
        )
    except BizError as exc:
        print(f"\n[GLM] 路由/调用异常: code={exc.code} message={exc.message}")
        pytest.xfail(f"GLM 调用异常: {exc.code} {exc.message}")
    await _xfail_llm_result(result, "GLM")
    print(f"\n[GLM] 响应摘要: text={result.text!r} meta={result.meta}")
    assert result.meta.get("model_key") == "glm-5.3-flash"
    assert (result.text or "").strip(), "GLM 返回文本为空"
    assert (result.meta.get("tokens_in") or 0) > 0, "GLM usage 缺少输入 token"
    assert (result.meta.get("tokens_out") or 0) > 0, "GLM usage 缺少输出 token"


async def test_pick_model_locked_not_found() -> None:
    """失败路由：锁定不存在的模型 → BizError(80001)。"""
    async with async_session_factory() as session:
        with pytest.raises(BizError) as exc_info:
            await pick_model(session, "llm", "llm_text", user_locked_model="no-such-model")
    assert exc_info.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert "no-such-model" in exc_info.value.message
