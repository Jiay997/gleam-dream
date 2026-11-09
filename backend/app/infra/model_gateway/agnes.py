"""Agnes AI 媒体提供商（图片 / 视频）。

对接 Agnes AI（``https://apihub.agnes-ai.com/v1``）的媒体生成能力：

- **图片**：``POST /v1/images/generations``（OpenAI 兼容），同步返回
  ``data[0].url`` 或 ``data[0].b64_json``；尺寸使用档位（1K/2K/3K/4K）+
  宽高比（9:16 等），不再传历史的精确像素写法；
- **视频**：``POST /v1/videos`` 创建异步任务，``GET /agnesapi?video_id=...``
  轮询至 ``status=completed``，从顶层 ``url`` 下载 MP4。

设计说明：
- 语音（TTS）不在本提供商内实现——当前 API Key 分组没有可用 TTS 模型，
  保持由百炼 ``dashscope_tts`` 承担；
- 与 :class:`~app.infra.model_gateway.bailian.BailianProvider` 一致，失败以
  ``GenResult(ok=False)`` 表达，不向调用方抛异常；
- 所有远端媒体统一下载后落本地存储，返回 ``/files/{object_key}``。

参考文档：
- https://www.agnes-ai.com/zh-Hans/docs/agnes-image-25-flash
- https://www.agnes-ai.com/zh-Hans/docs/agnes-video-25-flash
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.core.config import settings
from app.core.ids import new_request_id
from app.infra.model_gateway.base import (
    CAPABILITY_IMAGE,
    CAPABILITY_VIDEO,
    GenRequest,
    GenResult,
    ModelProvider,
)

if TYPE_CHECKING:
    from app.infra.storage import LocalStorage
    from app.models import ModelRegistry

logger = logging.getLogger("gleamdream.gateway.agnes")

#: 支持的媒体能力（TTS 由百炼承担）
_AGNES_CAPABILITIES = {CAPABILITY_IMAGE, CAPABILITY_VIDEO}
#: 图片请求超时（秒）
_HTTP_TIMEOUT = 180.0
#: 媒体下载超时（秒）
_DOWNLOAD_TIMEOUT = 120.0
#: 是否读取系统代理（trust_env）。
#: Windows 上 httpx 会通过 urllib 读取注册表里的系统代理（如 127.0.0.1:7890 的
#: Clash/代理客户端）；代理未就绪时 TLS 握手直接抛 ConnectError，表现为
#: "Agnes 生成失败 ConnectError"、连续失败还会触发熔断切模型。
#: Agnes 接口可直连（实测直连 200、走系统代理 ConnectError），因此关闭代理探测。
_AGNES_TRUST_ENV = False
#: 图片档位 → 短边像素（用于把精确尺寸归一到档位）
_SIZE_TIERS = (("4K", 3072), ("3K", 2048), ("2K", 1024), ("1K", 0))
#: 支持的宽高比（文档列举）
_SUPPORTED_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "2:3", "3:2", "9:16")
#: 档位 → 输出倍数（以 1K 基准尺寸线性放大）
_TIER_MULTIPLIER = {"1K": 1, "2K": 2, "3K": 3, "4K": 4}
#: 1K 档各画幅的基准像素（对齐官方尺寸参考表）
_TIER_BASE_PIXELS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "3:4": (864, 1152),
    "4:3": (1152, 864),
    "16:9": (1312, 736),
    "9:16": (736, 1312),
    "2:3": (832, 1248),
    "3:2": (1248, 832),
    "21:9": (1568, 672),
}


def _ratio_for(width: int, height: int) -> str:
    """按宽高选出最接近的受支持画幅。

    Args:
        width: 目标宽度（像素）。
        height: 目标高度（像素）。

    Returns:
        受支持的宽高比字符串（如 ``9:16``）。
    """
    if width <= 0 or height <= 0:
        return "16:9"
    target = width / height
    best = "16:9"
    best_diff = float("inf")
    for ratio in _SUPPORTED_RATIOS:
        left, _, right = ratio.partition(":")
        try:
            value = int(left) / int(right)
        except (ValueError, ZeroDivisionError):
            continue
        diff = abs(value - target)
        if diff < best_diff:
            best, best_diff = ratio, diff
    return best


def _tier_for(width: int, height: int) -> str:
    """按长边把精确尺寸归一到 Agnes 输出档位。

    Args:
        width: 目标宽度（像素）。
        height: 目标高度（像素）。

    Returns:
        档位字符串 1K/2K/3K/4K。
    """
    longest = max(width, height)
    for tier, threshold in _SIZE_TIERS:
        if longest > threshold:
            return tier
    return "1K"


def _tier_pixels(tier: str, ratio: str) -> tuple[int, int]:
    """按档位与画幅推算实际输出像素。

    Args:
        tier: 输出档位（1K/2K/3K/4K）。
        ratio: 宽高比（如 9:16）。

    Returns:
        (width, height)；未知画幅回退 1:1。
    """
    base_width, base_height = _TIER_BASE_PIXELS.get(ratio, (1024, 1024))
    multiplier = _TIER_MULTIPLIER.get(tier, 1)
    return base_width * multiplier, base_height * multiplier


def _parse_size(params: dict[str, Any]) -> tuple[int, int]:
    """解析请求中的目标尺寸。

    Args:
        params: 能力参数字典（``size`` 形如 ``1024x1792``，或 width/height）。

    Returns:
        (width, height)，缺省 1024x1024。
    """
    width = int(params.get("width") or 0)
    height = int(params.get("height") or 0)
    size = params.get("size")
    if isinstance(size, str) and ("x" in size.lower() or "*" in size):
        normalized = size.lower().replace("*", "x")
        left, _, right = normalized.partition("x")
        try:
            width, height = int(left), int(right)
        except ValueError:
            pass
    if width <= 0:
        width = 1024
    if height <= 0:
        height = 1024
    return width, height


async def _download_media(client: httpx.AsyncClient, url: str) -> tuple[bytes, str | None]:
    """下载远端媒体为字节并识别 MIME。

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
        raise RuntimeError(f"Agnes 媒体下载失败 HTTP {resp.status_code}: {url[:120]}")
    content = resp.content
    if not content:
        raise RuntimeError("Agnes 媒体下载内容为空")
    mime = resp.headers.get("Content-Type")
    return content, (mime.split(";")[0].strip() if mime else None)


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    attempts: int = 3,
) -> dict[str, Any]:
    """提交 JSON 请求并在 429 / 503 时指数退避重试。

    Agnes 免费额度存在短窗口限流，视频队列满时返回 503 ``video_queue_full``；
    两者都不是凭据错误，重试后仍失败才交由上层转为失败结果。

    Args:
        client: 复用的 httpx 客户端。
        url: 完整请求地址。
        headers: 请求头。
        body: JSON 请求体。
        attempts: 最大尝试次数（含首次）。

    Returns:
        解析后的 JSON 响应。

    Raises:
        RuntimeError: 非可重试状态码，或重试耗尽。
    """
    delay = 1.5
    resp: httpx.Response | None = None
    for attempt in range(attempts):
        try:
            resp = await client.post(url, headers=headers, json=body)
        except httpx.TransportError as exc:
            # 连接失败/超时等瞬时网络问题先退避重试：此前 5 次 ConnectError
            # 会直接触发熔断并把请求切到备用模型，造成“突然换模型/报鉴权错”。
            if attempt < attempts - 1:
                logger.warning(
                    "Agnes 网络异常 %s，%.1fs 后重试（第 %d 次）",
                    type(exc).__name__, delay, attempt + 1,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(
                f"Agnes 网络连接失败（{type(exc).__name__}）：{str(exc) or '无法连接上游服务'}"
            ) from exc
        if resp.status_code in (429, 503) and attempt < attempts - 1:
            logger.warning("Agnes 限流/队列满 %s，%.1fs 后重试", resp.status_code, delay)
            await asyncio.sleep(delay)
            delay *= 2
            continue
        break
    assert resp is not None
    if resp.status_code in (401, 403):
        raise RuntimeError(
            "Agnes 鉴权失败：API Key 无效或无权限。"
            "请在「模型配置」更新 Agnes API Key，或切换图片模型后重试。"
            f"（响应：{resp.text[:120]}）"
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Agnes HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Agnes 响应非 JSON: {resp.text[:200]}") from exc


class AgnesProvider(ModelProvider):
    """Agnes AI 媒体提供商（图片同步、视频异步）。

    与百炼提供商共用统一网关契约；由 ``protocol=agnes_image / agnes_video``
    的模型注册行实时构建。
    """

    def __init__(
        self,
        storage: LocalStorage,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        image_model: str | None = None,
        video_model: str | None = None,
        capabilities: set[str] | None = None,
    ) -> None:
        """初始化提供商。

        Args:
            storage: 本地存储（媒体落盘）。
            api_key: API Key；缺省取 ``settings.AGNES_API_KEY``。
            base_url: API 基址；缺省取 ``settings.AGNES_BASE_URL``。
            image_model: 图片模型名；缺省取 ``settings.AGNES_IMAGE_MODEL``。
            video_model: 视频模型名；缺省取 ``settings.AGNES_VIDEO_MODEL``。
            capabilities: 声明支持的能力集合；缺省图片 + 视频。

        Raises:
            无（参数均为内存赋值）。
        """
        self._storage = storage
        self._api_key = (api_key or settings.AGNES_API_KEY).strip()
        self._base_url = (base_url or settings.AGNES_BASE_URL).strip().rstrip("/")
        self._image_model = image_model or settings.AGNES_IMAGE_MODEL
        self._video_model = video_model or settings.AGNES_VIDEO_MODEL
        self._capabilities = capabilities or set(_AGNES_CAPABILITIES)

    def supports(self, capability: str) -> bool:
        """判断是否支持指定能力。

        Args:
            capability: 能力名（llm/image/video/tts/bgm）。

        Returns:
            在声明集合内返回 True。
        """
        return capability in self._capabilities

    async def generate(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """按能力分发生成调用。

        Args:
            request: 统一生成请求。
            model_spec: 路由命中的模型注册行。

        Returns:
            成功：ok=True + object_key/media_url；失败：ok=False + error。
        """
        try:
            if request.capability == CAPABILITY_IMAGE:
                return await self._generate_image(request, model_spec)
            if request.capability == CAPABILITY_VIDEO:
                return await self._generate_video(request, model_spec)
            return GenResult(
                ok=False,
                error=f"Agnes 提供商不支持能力: {request.capability}",
                meta={"provider": "agnes", "model_key": model_spec.model_key},
            )
        except Exception as exc:  # noqa: BLE001 网络等意外统一转失败结果
            logger.exception("Agnes 生成失败 capability=%s", request.capability)
            return GenResult(
                ok=False,
                error=f"Agnes 生成失败: {type(exc).__name__}: {str(exc)[:200]}",
                meta={"provider": "agnes", "model_key": model_spec.model_key},
            )

    def _headers(self) -> dict[str, str]:
        """构造认证与内容类型请求头。

        Returns:
            HTTP 请求头字典（含 Bearer 凭据，不落日志）。
        """
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _generate_image(
        self, request: GenRequest, model_spec: ModelRegistry
    ) -> GenResult:
        """文生图 / 图生图（OpenAI Images 兼容）。

        Args:
            request: 统一生成请求（prompt、params.size / image(s)）。
            model_spec: 模型注册行。

        Returns:
            成功结果：object_key 指向图片，meta 含档位与画幅。
        """
        prompt = (request.prompt or "").strip()
        if not prompt:
            return GenResult(
                ok=False,
                error="图片提示词为空",
                meta={"provider": "agnes", "model_key": model_spec.model_key},
            )
        width, height = _parse_size(request.params)
        ratio = str(request.params.get("aspect_ratio") or _ratio_for(width, height))
        tier = _tier_for(width, height)
        body: dict[str, Any] = {
            "model": self._image_model,
            "prompt": prompt,
            "size": tier,
            "ratio": ratio,
        }
        refs = request.params.get("images") or request.params.get("image")
        if isinstance(refs, str):
            refs = [refs]
        if refs:
            body["image"] = [str(item) for item in refs][:10]

        started = time.monotonic()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=_AGNES_TRUST_ENV) as client:
            data = await _post_json(
                client,
                f"{self._base_url}/images/generations",
                headers=self._headers(),
                body=body,
            )
            items = data.get("data") or []
            first = items[0] if items and isinstance(items[0], dict) else {}
            image_url = str(first.get("url") or "")
            b64_json = first.get("b64_json")
            if image_url:
                image_bytes, mime_type = await _download_media(client, image_url)
            elif isinstance(b64_json, str) and b64_json:
                image_bytes = base64.b64decode(b64_json)
                mime_type = "image/png"
            else:
                logger.warning(
                    "Agnes 图片响应缺少 url/b64_json model=%s resp=%s",
                    model_spec.model_key,
                    str(data)[:400],
                )
                return GenResult(
                    ok=False,
                    error="Agnes 图片响应未包含图片地址",
                    meta={"provider": "agnes", "model_key": model_spec.model_key},
                )

        ext = "png" if "png" in (mime_type or "") or not image_url else "jpg"
        object_key = self._storage.save_bytes(
            f"agnes/images/{new_request_id().lower()}.{ext}", image_bytes
        )
        # Agnes 按档位 + 画幅输出，meta 记录实际像素，避免沿用请求的
        # 精确尺寸造成记账/预览误判。
        actual_width, actual_height = _tier_pixels(tier, ratio)
        return GenResult(
            ok=True,
            object_key=object_key,
            media_url=f"/files/{object_key}",
            meta={
                "provider": "agnes",
                "model_key": model_spec.model_key,
                "mime_type": mime_type or f"image/{ext}",
                "width": actual_width,
                "height": actual_height,
                "requested_width": width,
                "requested_height": height,
                "size_tier": tier,
                "aspect_ratio": ratio,
                "bytes": len(image_bytes),
                "remote_url": image_url or None,
                "wall_time_ms": int((time.monotonic() - started) * 1000),
            },
        )

    async def _generate_video(
        self, request: GenRequest, model_spec: ModelRegistry
    ) -> GenResult:
        """文生视频 / 首尾帧 / 参考图模式（OpenAI Videos 兼容异步任务）。

        Args:
            request: 统一生成请求（prompt、params.duration_ms / first_frame /
                last_frame / images / aspect_ratio / test_only）。
            model_spec: 模型注册行。

        Returns:
            成功结果：object_key 指向 MP4，meta 含 task_id 与时长。
        """
        prompt = (request.prompt or "").strip()
        if not prompt:
            return GenResult(
                ok=False,
                error="视频提示词为空",
                meta={"provider": "agnes", "model_key": model_spec.model_key},
            )
        first_frame = request.params.get("first_frame")
        last_frame = request.params.get("last_frame")
        refs = request.params.get("images") or request.params.get("image")
        if isinstance(refs, str):
            refs = [refs]
        if first_frame or last_frame:
            mode = "keyframe"
        elif refs:
            mode = "reference"
        else:
            mode = "text"

        duration_ms = request.params.get("duration_ms")
        seconds = 5
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            seconds = max(4, min(12, round(duration_ms / 1000)))
        width, height = _parse_size(request.params)
        aspect_ratio = str(
            request.params.get("aspect_ratio") or _ratio_for(width, height)
        )
        body: dict[str, Any] = {
            "model": self._video_model,
            "prompt": prompt,
            "mode": mode,
            "seconds": str(seconds),
            "size": "720P",
            "aspect_ratio": aspect_ratio,
        }
        if mode == "keyframe":
            if first_frame:
                body["first_frame"] = str(first_frame)
            if last_frame:
                body["last_frame"] = str(last_frame)
        if mode == "reference" and refs:
            body["images"] = [str(item) for item in refs][:5]

        started = time.monotonic()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=_AGNES_TRUST_ENV) as client:
            data = await _post_json(
                client,
                f"{self._base_url}/videos",
                headers=self._headers(),
                body=body,
            )
            video_id = str(
                data.get("video_id") or data.get("task_id") or data.get("id") or ""
            )
            if not video_id:
                logger.warning(
                    "Agnes 视频提交无 video_id model=%s resp=%s",
                    model_spec.model_key,
                    str(data)[:400],
                )
                return GenResult(
                    ok=False,
                    error="Agnes 视频任务提交失败（无 video_id）",
                    meta={"provider": "agnes", "model_key": model_spec.model_key},
                )

            # 配置页“测试连接”：任务受理即证明凭据/地址/模型有效，不等待成片
            if request.params.get("test_only"):
                return GenResult(
                    ok=True,
                    meta={
                        "provider": "agnes",
                        "model_key": model_spec.model_key,
                        "api_model": self._video_model,
                        "task_id": video_id,
                        "test_only": True,
                        "wall_time_ms": int((time.monotonic() - started) * 1000),
                    },
                )

            origin = self._base_url[: -len("/v1")] if self._base_url.endswith("/v1") else self._base_url
            poll_headers = {"Authorization": f"Bearer {self._api_key}"}
            deadline = time.monotonic() + settings.AGNES_VIDEO_POLL_TIMEOUT
            video_url: str | None = None
            last_status = "pending"
            while time.monotonic() < deadline:
                await asyncio.sleep(settings.AGNES_VIDEO_POLL_INTERVAL)
                resp = await client.get(
                    f"{origin}/agnesapi",
                    params={"video_id": video_id, "model_name": self._video_model},
                    headers=poll_headers,
                )
                if resp.status_code >= 400:
                    # 轮询接口偶发 5xx 不应立即判失败，继续等待到超时
                    last_status = f"http_{resp.status_code}"
                    continue
                task = resp.json()
                status = str(task.get("status") or "").lower()
                last_status = status or last_status
                if status == "completed":
                    video_url = str(task.get("url") or "")
                    break
                if status == "failed":
                    return GenResult(
                        ok=False,
                        error=f"Agnes 视频任务失败: {str(task.get('error') or '')[:160]}",
                        meta={
                            "provider": "agnes",
                            "model_key": model_spec.model_key,
                            "task_id": video_id,
                        },
                    )
            if not video_url:
                return GenResult(
                    ok=False,
                    error=(
                        f"Agnes 视频任务超时（>{settings.AGNES_VIDEO_POLL_TIMEOUT}s）"
                        f" video_id={video_id} last_status={last_status}"
                    ),
                    meta={
                        "provider": "agnes",
                        "model_key": model_spec.model_key,
                        "task_id": video_id,
                    },
                )
            video_bytes, mime_type = await _download_media(client, video_url)

        object_key = self._storage.save_bytes(
            f"agnes/videos/{new_request_id().lower()}.mp4", video_bytes
        )
        return GenResult(
            ok=True,
            object_key=object_key,
            media_url=f"/files/{object_key}",
            meta={
                "provider": "agnes",
                "model_key": model_spec.model_key,
                "mime_type": mime_type or "video/mp4",
                "duration_ms": seconds * 1000,
                "mode": mode,
                "aspect_ratio": aspect_ratio,
                "bytes": len(video_bytes),
                "task_id": video_id,
                "remote_url": video_url,
                "wall_time_ms": int((time.monotonic() - started) * 1000),
            },
        )
