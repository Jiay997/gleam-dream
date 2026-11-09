"""阿里云百炼（AI 漫剧）媒体模型提供商模块。

对接百炼专属推理端点的三类媒体生成能力（真实调用，非沙箱占位）：

- **tts**（``qwen-audio-3.1-tts-plus``）：DashScope WebSocket 协议
  （run-task / continue-task / finish-task），服务端逐帧回传音频二进制，
  汇总为 WAV 落本地存储；
- **image**（``qwen-image-3.0``）：DashScope HTTP 多模态生成
  （``POST /api/v1/services/aigc/multimodal-generation/generation``），
  同步返回图片 URL，下载后落本地存储；
- **video**（``wan3.0-video``）：DashScope 异步视频合成
  （``POST /api/v1/services/aigc/video-generation/video-synthesis`` +
  ``X-DashScope-Async: enable``），返回 task_id 后轮询
  ``GET /api/v1/tasks/{task_id}`` 至 SUCCEEDED，下载 video_url 落盘。

所有产物与 StubProvider 一致落 :class:`~app.infra.storage.LocalStorage`，
返回 ``object_key`` / ``media_url``；``meta`` 不含任何密钥。失败以
``ok=False`` 表达（不抛异常），与网关契约一致。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx
import websockets

from app.core.config import settings
from app.core.ids import new_request_id
from app.infra.model_gateway.base import (
    CAPABILITY_IMAGE,
    CAPABILITY_TTS,
    CAPABILITY_VIDEO,
    GenRequest,
    GenResult,
    ModelProvider,
)

if TYPE_CHECKING:
    from app.infra.storage import LocalStorage
    from app.models import ModelRegistry

logger = logging.getLogger("gleamdream.gateway")

# 百炼提供商支持的能力集合（bgm 仍由沙箱承担）
_BAILIAN_CAPABILITIES = {CAPABILITY_IMAGE, CAPABILITY_VIDEO, CAPABILITY_TTS}

# HTTP 调用超时（秒）：图片同步生成可能较慢
_HTTP_TIMEOUT: float = 120.0
# WebSocket TTS 会话总超时（秒）
_TTS_WS_TIMEOUT: float = 90.0
# 视频下载超时（秒）
_DOWNLOAD_TIMEOUT: float = 300.0


class BailianProvider(ModelProvider):
    """阿里云百炼媒体模型提供商（TTS / 图片 / 视频真实调用）。

    构造参数全部可选：缺省时读取 ``settings``（应用级默认），模型配置页
    可传入行级覆盖值——网关每次调用按 ``model_registry`` 行构造实例，
    因此配置修改后下一次生成立即生效，无需重启。

    Attributes:
        _storage: 本地存储客户端（产物落盘）。
        _api_key: 百炼 API Key（仅内存持有，绝不入日志/快照）。
        _base_url: HTTP 推理基址。
        _ws_url: WebSocket 推理地址（TTS）。
        _tts_model: TTS 模型名。
        _image_model: 文生图模型名。
        _video_model: 视频生成模型名。
        _default_voice: TTS 默认音色。
        _capabilities: 本实例声明支持的能力集合。
    """

    def __init__(
        self,
        storage: LocalStorage,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        ws_url: str | None = None,
        tts_model: str | None = None,
        image_model: str | None = None,
        video_model: str | None = None,
        default_voice: str | None = None,
        capabilities: set[str] | None = None,
    ) -> None:
        """构造百炼提供商。

        Args:
            storage: 本地存储客户端实例。
            api_key: API Key；缺省取 ``settings.BAILIAN_API_KEY``。
            base_url: HTTP 基址；缺省取 ``settings.BAILIAN_BASE_URL``。
            ws_url: TTS WebSocket 地址；缺省取 ``settings.BAILIAN_WS_URL``。
            tts_model: TTS 模型名；缺省取 ``settings.BAILIAN_TTS_MODEL``。
            image_model: 图片模型名；缺省取 ``settings.BAILIAN_IMAGE_MODEL``。
            video_model: 视频模型名；缺省取 ``settings.BAILIAN_VIDEO_MODEL``。
            default_voice: TTS 默认音色；缺省取 ``settings.BAILIAN_TTS_VOICE``。
            capabilities: 支持能力集合；缺省 TTS/图片/视频三选。

        Raises:
            无（参数均为内存赋值）。
        """
        self._storage = storage
        self._api_key = (api_key or settings.BAILIAN_API_KEY).strip()
        self._base_url = (base_url or settings.BAILIAN_BASE_URL).strip().rstrip("/")
        self._ws_url = (ws_url or settings.BAILIAN_WS_URL).strip()
        self._tts_model = tts_model or settings.BAILIAN_TTS_MODEL
        self._image_model = image_model or settings.BAILIAN_IMAGE_MODEL
        self._video_model = video_model or settings.BAILIAN_VIDEO_MODEL
        self._default_voice = default_voice or settings.BAILIAN_TTS_VOICE
        self._capabilities = capabilities or set(_BAILIAN_CAPABILITIES)

    def supports(self, capability: str) -> bool:
        """判断是否支持指定能力。

        Args:
            capability: 能力名。

        Returns:
            能力在本实例声明集合内返回 True，其余（含 llm/bgm）False。
        """
        return capability in self._capabilities

    async def generate(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """按能力分发生成调用。

        Args:
            request: 统一生成请求。
            model_spec: 路由命中的模型注册行（model_key 即百炼模型名）。

        Returns:
            成功：ok=True + object_key/media_url；失败：ok=False + error。
        """
        try:
            if request.capability == CAPABILITY_TTS:
                return await self._generate_tts(request, model_spec)
            if request.capability == CAPABILITY_IMAGE:
                return await self._generate_image(request, model_spec)
            if request.capability == CAPABILITY_VIDEO:
                return await self._generate_video(request, model_spec)
            return GenResult(
                ok=False,
                error=f"百炼提供商不支持能力: {request.capability}",
                meta={"provider": "bailian", "model_key": model_spec.model_key},
            )
        except Exception as exc:  # noqa: BLE001 网络等意外统一转失败结果
            logger.exception("百炼生成失败 capability=%s", request.capability)
            return GenResult(
                ok=False,
                error=f"百炼生成失败: {type(exc).__name__}: {str(exc)[:200]}",
                meta={"provider": "bailian", "model_key": model_spec.model_key},
            )

    # ------------------------------------------------------------------
    # TTS（WebSocket）
    # ------------------------------------------------------------------

    async def _generate_tts(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """DashScope WebSocket 语音合成（qwen-audio-3.1-tts-plus）。

        流程：run-task → task-started → continue-task(文本) → finish-task
        → 持续收集二进制音频帧 → task-finished → 落盘 WAV。

        Args:
            request: 统一生成请求（prompt 为待合成文本；params.voice 可选音色）。
            model_spec: 模型注册行。

        Returns:
            成功结果：object_key 指向 WAV，meta.duration_ms 为估算时长。
        """
        text = (request.prompt or "").strip()
        if not text:
            return GenResult(
                ok=False,
                error="TTS 文本为空",
                meta={"provider": "bailian", "model_key": model_spec.model_key},
            )
        voice = str(request.params.get("voice") or request.params.get("voice_name")
                    or self._default_voice)
        started = time.monotonic()
        audio_chunks: list[bytes] = []
        finished = False
        error_message: str | None = None

        headers = {
            "Authorization": f"bearer {self._api_key}",
            "user-agent": "gleamdream-studio/1.0",
        }
        async with websockets.connect(
            self._ws_url, additional_headers=headers, max_size=16 * 1024 * 1024,
        ) as ws:
            task_id = uuid.uuid4().hex

            async def _send(payload: dict[str, Any]) -> None:
                """发送一条 JSON 指令帧。

                参数:
                    payload: DashScope 协议指令载荷。
                """
                await ws.send(json.dumps(payload, ensure_ascii=False))

            # 1) run-task（cosyvoice 引擎 duplex 模式：文本经 continue-task 流式提交）
            await _send({
                "header": {
                    "action": "run-task",
                    "task_id": task_id,
                    "streaming": "duplex",
                },
                "payload": {
                    "model": self._tts_model,
                    "task_group": "audio",
                    "task": "tts",
                    "function": "SpeechSynthesizer",
                    "parameters": {
                        "text_type": "PlainText",
                        "voice": voice,
                        "format": "wav",
                        "sample_rate": 16000,
                        "volume": 60,
                        "rate": 1.0,
                        "pitch": 1.0,
                    },
                    "input": {},
                },
            })

            # 2) 事件循环：started → 发文本与 finish；收集音频帧
            deadline = asyncio.get_event_loop().time() + _TTS_WS_TIMEOUT
            text_sent = False
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    error_message = f"TTS WebSocket 会话超时（{_TTS_WS_TIMEOUT}s）"
                    break
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30))
                except asyncio.TimeoutError:
                    continue
                if isinstance(message, (bytes, bytearray)):
                    audio_chunks.append(bytes(message))
                    continue
                try:
                    frame = json.loads(message)
                except (TypeError, ValueError):
                    continue
                header = frame.get("header") or {}
                event = str(header.get("event") or "")
                if event == "task-started":
                    # duplex 模式：文本经 continue-task 流式提交后 finish-task 收口
                    await _send({
                        "header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
                        "payload": {"input": {"text": text}},
                    })
                    await _send({
                        "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                        "payload": {"input": {}},
                    })
                    text_sent = True
                elif event == "task-finished":
                    finished = True
                    break
                elif event == "task-failed":
                    error_message = str(
                        (frame.get("payload") or {}).get("message")
                        or header.get("error_message")
                        or "task-failed"
                    )
                    break
            if not text_sent and error_message is None:
                error_message = "TTS 会话未进入 task-started（服务端无响应）"

        if not finished or not audio_chunks:
            logger.warning("百炼 TTS 失败 model=%s err=%s", model_spec.model_key, error_message)
            return GenResult(
                ok=False,
                error=error_message or "TTS 未返回音频数据",
                meta={"provider": "bailian", "model_key": model_spec.model_key},
            )

        audio = b"".join(audio_chunks)
        object_key = self._storage.save_bytes(
            f"bailian/audio/{new_request_id().lower()}.wav", audio
        )
        duration_ms = _estimate_wav_duration_ms(audio)
        return GenResult(
            ok=True,
            object_key=object_key,
            media_url=f"/files/{object_key}",
            meta={
                "provider": "bailian",
                "model_key": model_spec.model_key,
                "mime_type": "audio/wav",
                "duration_ms": duration_ms,
                "sample_rate": 16000,
                "channels": 1,
                "voice": voice,
                "bytes": len(audio),
                "wall_time_ms": int((time.monotonic() - started) * 1000),
            },
        )

    # ------------------------------------------------------------------
    # 图片（HTTP 同步）
    # ------------------------------------------------------------------

    async def _generate_image(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """DashScope 多模态生成（qwen-image-3.0 文生图）。

        同步返回图片 URL，下载后落本地存储。

        Args:
            request: 统一生成请求（params.size 如 "1024x1024"）。
            model_spec: 模型注册行。

        Returns:
            成功结果：object_key 指向图片文件，meta 含 width/height。
        """
        prompt = (request.prompt or "").strip()
        if not prompt:
            return GenResult(
                ok=False,
                error="图片提示词为空",
                meta={"provider": "bailian", "model_key": model_spec.model_key},
            )
        width, height = _parse_size(request.params)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._image_model,
            "input": {
                "messages": [
                    {"role": "user", "content": [{"text": prompt}]},
                ]
            },
            "parameters": {
                "size": f"{width}*{height}",
                "n": 1,
            },
        }
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            data = await _post_with_retry(
                client,
                f"{self._base_url}/api/v1/services/aigc/multimodal-generation/generation",
                headers=headers,
                body=body,
            )
            image_url = _extract_image_url(data)
            if not image_url:
                logger.warning(
                    "百炼图片响应缺 image 字段 model=%s resp=%s", model_spec.model_key, str(data)[:400],
                )
                return GenResult(
                    ok=False,
                    error="百炼图片响应未包含图片 URL",
                    meta={"provider": "bailian", "model_key": model_spec.model_key},
                )
            image_bytes, mime_type = await _download_media(client, image_url)

        ext = "png" if "png" in (mime_type or "") else "jpg"
        object_key = self._storage.save_bytes(
            f"bailian/images/{new_request_id().lower()}.{ext}", image_bytes
        )
        return GenResult(
            ok=True,
            object_key=object_key,
            media_url=f"/files/{object_key}",
            meta={
                "provider": "bailian",
                "model_key": model_spec.model_key,
                "mime_type": mime_type or f"image/{ext}",
                "width": width,
                "height": height,
                "bytes": len(image_bytes),
                "remote_url": image_url,
                "wall_time_ms": int((time.monotonic() - started) * 1000),
            },
        )

    # ------------------------------------------------------------------
    # 视频（HTTP 异步任务轮询）
    # ------------------------------------------------------------------

    async def _generate_video(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """DashScope 异步视频合成（wan3.0-video）。

        提交任务（X-DashScope-Async: enable）→ 轮询 task 状态至
        SUCCEEDED → 下载 video_url 落本地存储。

        Args:
            request: 统一生成请求（params.duration_ms / img_url 可选图生视频）。
            model_spec: 模型注册行。

        Returns:
            成功结果：object_key 指向 MP4 文件，meta 含 duration_ms。
        """
        prompt = (request.prompt or "").strip()
        if not prompt:
            return GenResult(
                ok=False,
                error="视频提示词为空",
                meta={"provider": "bailian", "model_key": model_spec.model_key},
            )
        img_url = request.params.get("img_url") or request.params.get("image_url")
        duration_ms = request.params.get("duration_ms")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        input_body: dict[str, Any] = {"prompt": prompt}
        if img_url:
            # 支持公网 URL 与 data URI（executor 会把首帧/尾帧编码为 data URI，
            # 本地存储没有公网地址也能走真实 I2V，不再退化成文生视频）。
            input_body["img_url"] = str(img_url)
        parameters: dict[str, Any] = {
            # wan2.6-i2v-flash：720P/1080P 档位；竖屏由首帧比例决定；关闭水印，开启提示词扩写
            "resolution": str(request.params.get("resolution") or "720P"),
            "watermark": False,
            "prompt_extend": True,
        }
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            # wan2.6 支持 2–15 秒；4–5 秒可覆盖 3–5 秒镜头（成片时间线会裁剪）
            parameters["duration"] = max(2, min(15, round(duration_ms / 1000)))
        negative_prompt = str(request.params.get("negative_prompt") or "").strip()
        if negative_prompt:
            parameters["negative_prompt"] = negative_prompt[:500]
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            data = await _post_with_retry(
                client,
                f"{self._base_url}/api/v1/services/aigc/video-generation/video-synthesis",
                headers=headers,
                body={
                    "model": self._video_model,
                    "input": input_body,
                    "parameters": parameters,
                },
            )
            task_id = str(((data.get("output") or {}).get("task_id")) or "")
            if not task_id:
                logger.warning(
                    "百炼视频提交无 task_id model=%s resp=%s", model_spec.model_key, str(data)[:400],
                )
                return GenResult(
                    ok=False,
                    error="百炼视频任务提交失败（无 task_id）",
                    meta={"provider": "bailian", "model_key": model_spec.model_key},
                )

            # 配置页"测试连接"：任务被受理即证明凭据/地址/模型有效，
            # 不再轮询等待成片（避免测试长时间占用与额外产物下载）。
            if request.params.get("test_only"):
                return GenResult(
                    ok=True,
                    meta={
                        "provider": "bailian",
                        "model_key": model_spec.model_key,
                        "api_model": self._video_model,
                        "task_id": task_id,
                        "test_only": True,
                        "wall_time_ms": int((time.monotonic() - started) * 1000),
                    },
                )

            # 轮询任务状态
            poll_headers = {"Authorization": f"Bearer {self._api_key}"}
            deadline = time.monotonic() + settings.BAILIAN_VIDEO_POLL_TIMEOUT
            video_url: str | None = None
            while time.monotonic() < deadline:
                await asyncio.sleep(settings.BAILIAN_VIDEO_POLL_INTERVAL)
                task_resp = await client.get(
                    f"{self._base_url}/api/v1/tasks/{task_id}", headers=poll_headers,
                )
                task_data = _parse_response(task_resp)
                output = task_data.get("output") or {}
                status = str(output.get("task_status") or "")
                if status == "SUCCEEDED":
                    video_url = output.get("video_url")
                    break
                if status in ("FAILED", "CANCELED", "UNKNOWN"):
                    return GenResult(
                        ok=False,
                        error=f"百炼视频任务失败 status={status}: {str(output.get('message') or '')[:160]}",
                        meta={
                            "provider": "bailian",
                            "model_key": model_spec.model_key,
                            "task_id": task_id,
                        },
                    )
            if not video_url:
                return GenResult(
                    ok=False,
                    error=f"百炼视频任务超时（>{settings.BAILIAN_VIDEO_POLL_TIMEOUT}s）task_id={task_id}",
                    meta={"provider": "bailian", "model_key": model_spec.model_key, "task_id": task_id},
                )
            video_bytes, mime_type = await _download_media(client, video_url)

        object_key = self._storage.save_bytes(
            f"bailian/videos/{new_request_id().lower()}.mp4", video_bytes
        )
        # 以 ffprobe 实测元数据为准（请求时长只是档位提示，实际可能被模型
        # 取整；QC 的时长覆盖判定与成片元数据都应读真实值）。
        probed = _probe_video_info(self._storage.object_path(object_key))
        duration_ms_out = _int_or_zero(probed.get("duration_ms"))
        if duration_ms_out <= 0 and isinstance(duration_ms, (int, float)) and duration_ms > 0:
            duration_ms_out = int(duration_ms)
        return GenResult(
            ok=True,
            object_key=object_key,
            media_url=f"/files/{object_key}",
            meta={
                "provider": "bailian",
                "model_key": model_spec.model_key,
                "mime_type": mime_type or "video/mp4",
                "duration_ms": duration_ms_out,
                "width": _int_or_zero(probed.get("width")) or None,
                "height": _int_or_zero(probed.get("height")) or None,
                "fps": probed.get("fps"),
                "bytes": len(video_bytes),
                "file_size_bytes": len(video_bytes),
                "task_id": task_id,
                "remote_url": video_url,
                "image_driven": bool(img_url),
                "wall_time_ms": int((time.monotonic() - started) * 1000),
            },
        )


# ----------------------------------------------------------------------
# 模块级工具函数
# ----------------------------------------------------------------------


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    attempts: int = 3,
) -> dict[str, Any]:
    """提交百炼 HTTP 请求，并对 429 限流做指数退避重试。

    百炼免费额度存在短窗口 QPS 限制，单次 429 并不代表凭据或模型无效；
    这里按 1s → 2s 退避重试，仍失败则由 :func:`_parse_response` 抛错，
    由上层转换为 ``GenResult(ok=False)``。

    Args:
        client: 复用的 httpx 异步客户端。
        url: 完整请求地址。
        headers: 请求头（含 Bearer 凭据，不落日志）。
        body: JSON 请求体。
        attempts: 最大尝试次数（含首次）。

    Returns:
        解析后的 JSON 响应字典。

    Raises:
        RuntimeError: 非 2xx 且非可重试状态，或重试耗尽（由响应片段构成）。
    """
    delay = 1.0
    resp: httpx.Response | None = None
    for attempt in range(attempts):
        try:
            resp = await client.post(url, headers=headers, json=body)
        except httpx.TransportError as exc:
            # 网络瞬时故障（连接失败/超时/TLS）不应立刻判定为生成失败：
            # 先退避重试，避免一次网络抖动直接触发模型熔断。
            if attempt < attempts - 1:
                logger.warning(
                    "百炼网络异常 %s，%.1fs 后重试（第 %d 次）",
                    type(exc).__name__, delay, attempt + 1,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(
                f"百炼网络连接失败（{type(exc).__name__}）：{str(exc) or '无法连接上游服务'}"
            ) from exc
        if resp.status_code == 429 and attempt < attempts - 1:
            logger.warning("百炼限流 429，%.1fs 后重试（第 %d 次）", delay, attempt + 1)
            await asyncio.sleep(delay)
            delay *= 2
            continue
        break
    assert resp is not None
    return _parse_response(resp)


def _parse_response(resp: httpx.Response) -> dict[str, Any]:
    """解析百炼 HTTP 响应为 JSON；非 2xx 抛 RuntimeError。

    Args:
        resp: httpx 响应对象。

    Returns:
        JSON 字典。

    Raises:
        RuntimeError: 非 2xx 状态（携带响应片段，不含密钥）。
    """
    if resp.status_code in (401, 403):
        # 鉴权失败给用户可执行的指引，避免只看到原始 InvalidApiKey JSON。
        raise RuntimeError(
            f"百炼鉴权失败 HTTP {resp.status_code}：API Key 无效或无权限。"
            "请在「模型配置」更新百炼 API Key，或切换图片模型后重试。"
            f"（响应：{resp.text[:120]}）"
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"百炼 HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"百炼响应非 JSON: {resp.text[:200]}") from exc


def _extract_image_url(data: dict[str, Any]) -> str | None:
    """从多模态生成响应中提取图片 URL。

    兼容两种返回形态：``output.choices[0].message.content[*].image`` 与
    ``output.results[*].url``。

    Args:
        data: 响应 JSON。

    Returns:
        图片 URL；未找到返回 None。
    """
    output = data.get("output") or {}
    choices = output.get("choices") or []
    if choices:
        content = ((choices[0].get("message") or {}).get("content")) or []
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("image"):
                return str(item["image"])
    results = output.get("results") or []
    for item in results:
        if isinstance(item, dict) and item.get("url"):
            return str(item["url"])
    return None


async def _download_media(client: httpx.AsyncClient, url: str) -> tuple[bytes, str | None]:
    """下载远端媒体为字节并识别 MIME 类型。

    Args:
        client: 已配置的 httpx 客户端。
        url: 媒体 URL。

    Returns:
        (字节数据, MIME 类型)。

    Raises:
        RuntimeError: 下载失败或内容为空。
    """
    resp = await client.get(url, timeout=_DOWNLOAD_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"媒体下载失败 HTTP {resp.status_code}: {url[:120]}")
    content = resp.content
    if not content:
        raise RuntimeError("媒体下载内容为空")
    mime_type = resp.headers.get("Content-Type")
    return content, (mime_type.split(";")[0].strip() if mime_type else None)


def _parse_size(params: dict[str, Any]) -> tuple[int, int]:
    """从请求参数解析图片宽高（默认 1024x1024）。

    Args:
        params: 能力参数字典（size "1024x1024" 或 width/height）。

    Returns:
        (width, height)。
    """
    width, height = 1024, 1024
    size = params.get("size")
    if isinstance(size, str) and ("x" in size.lower() or "*" in size):
        normalized = size.lower().replace("*", "x")
        left, _, right = normalized.partition("x")
        try:
            width, height = int(left), int(right)
        except ValueError:
            pass
    return max(256, min(2048, width)), max(256, min(2048, height))


def _int_or_zero(value: Any) -> int:
    """把任意值安全转换为整数。

    Args:
        value: 待转换值（int/float/数字字符串）。

    Returns:
        转换结果；失败返回 0。
    """
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _probe_video_info(path: Any) -> dict[str, Any]:
    """用 ffprobe 读取视频真实宽高、帧率与时长。

    Args:
        path: 视频文件绝对路径（Path 或字符串）。

    Returns:
        ``{width, height, fps, duration_ms}``；ffprobe 不可用/解析失败返回空字典。
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,avg_frame_rate,duration",
                "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        data = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, OSError, ValueError, json.JSONDecodeError):
        return {}
    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    duration_s = 0.0
    try:
        duration_s = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration_s = 0.0
    if duration_s <= 0:
        try:
            duration_s = float(stream.get("duration") or 0)
        except (TypeError, ValueError):
            duration_s = 0.0
    fps = None
    rate = str(stream.get("avg_frame_rate") or "")
    if "/" in rate:
        left, _, right = rate.partition("/")
        try:
            denominator = float(right)
            fps = round(float(left) / denominator, 2) if denominator else None
        except (TypeError, ValueError):
            fps = None
    return {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "fps": fps,
        "duration_ms": int(round(duration_s * 1000)) if duration_s > 0 else 0,
    }


def _estimate_wav_duration_ms(audio: bytes) -> int:
    """估算 WAV 音频时长（毫秒）。

    按标准 44 字节头 + 16kHz/16bit/单声道 推算；解析失败时按
    字节数粗估（每秒约 32KB）。

    Args:
        audio: WAV 文件字节。

    Returns:
        时长（毫秒，≥100）。
    """
    try:
        import io
        import wave

        with wave.open(io.BytesIO(audio), "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate() or 16000
            return max(100, int(frames / rate * 1000))
    except Exception:  # noqa: BLE001 解析失败按字节粗估
        return max(100, len(audio) // 32)
