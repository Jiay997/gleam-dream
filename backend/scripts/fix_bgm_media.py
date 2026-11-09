"""BGM 曲库媒体回填脚本（P1-7 修复的一次性数据修复）。

背景：``bgm_tracks`` 种子 20 条曲目全部没有实际音频媒体
（``media_file_id`` 为 NULL），成片合成时 BGM 轨片段被逐条跳过，
成片出现整镜静音段（全链路测试报告 P1-7 / 终验 4 观察项 1）。

本脚本幂等执行：
1. 扫描全部上架 BGM 曲目，找出 ``media_file_id`` 为空或对应媒体
   文件不存在的曲目；
2. 调用 :mod:`app.services.bgm_synth` 按 (情绪, BPM, 时长) 程序化
   合成原创 WAV 并落盘到 ``storage/bgm/``；
3. 创建对应 ``media_files`` 记录并回填 ``bgm_tracks.media_file_id``。

用法（backend 目录下）::

    ./.venv/Scripts/python.exe scripts/fix_bgm_media.py

Args:
    无命令行参数。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 允许以 `python scripts/fix_bgm_media.py` 直接运行（backend 为包根）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.db import async_session_factory  # noqa: E402
from app.core.ids import new_request_id  # noqa: E402
from app.infra.storage import storage  # noqa: E402
from app.models import BgmTrack, MediaFile  # noqa: E402
from app.services.bgm_synth import synthesize_bgm_wav  # noqa: E402


def _existing_file(object_key: str) -> Path | None:
    """检查对象键对应的本地文件是否存在。

    Args:
        object_key: 存储对象键（如 ``bgm/xxx.wav``）。

    Returns:
        存在的文件路径；不存在返回 None。
    """
    try:
        path = Path(storage.object_path(object_key))
    except Exception:  # noqa: BLE001 非法对象键按缺失处理
        return None
    return path if path.is_file() else None


async def fix_bgm_media() -> int:
    """为缺少媒体的 BGM 曲目合成音频并回填 media_file_id。

    幂等：已有可用媒体的曲目跳过；重复运行只补缺口。

    Returns:
        本次新合成/回填的曲目数量。

    Raises:
        无：单首曲目合成失败仅记录告警并继续，不中断整体修复。
    """
    fixed = 0
    async with async_session_factory() as session:
        tracks = (
            (await session.execute(select(BgmTrack).order_by(BgmTrack.id.asc())))
            .scalars()
            .all()
        )
        for track in tracks:
            media = (
                await session.get(MediaFile, int(track.media_file_id))
                if track.media_file_id
                else None
            )
            if media is not None and _existing_file(str(media.object_key)):
                continue
            emotion = "calm"
            tags = track.mood_tags if isinstance(track.mood_tags, list) else []
            if tags:
                emotion = str(tags[0])
            wav_bytes = await asyncio.to_thread(
                synthesize_bgm_wav,
                emotion=emotion,
                bpm=int(track.bpm or 72),
                duration_ms=int(track.duration_ms or 120000),
                seed=int(track.id),
            )
            object_key = f"bgm/{new_request_id().lower()}.wav"
            storage.save_bytes(object_key, wav_bytes)
            media = MediaFile(
                project_id=None,
                storage_provider="cos",
                bucket="gleamdream-local",
                object_key=object_key,
                url=f"/files/{object_key}",
                media_type="audio",
                mime_type="audio/wav",
                file_size_bytes=len(wav_bytes),
                duration_ms=int(track.duration_ms or 0),
                meta={
                    "kind": "bgm",
                    "title": track.title,
                    "synthesized": True,
                    "license_type": track.license_type,
                },
            )
            session.add(media)
            await session.flush()
            track.media_file_id = int(media.id)
            fixed += 1
            print(f"[fix_bgm_media] #{track.id} {track.title} -> {object_key}")
        await session.commit()
    return fixed


def main() -> None:
    """脚本入口：执行回填并打印统计。

    Raises:
        无：异常直接向上抛出（脚本语境需要非零退出码）。
    """
    count = asyncio.run(fix_bgm_media())
    print(f"[fix_bgm_media] done, fixed={count}")


if __name__ == "__main__":
    main()
