"""文档解析域路由模块（/documents、/parse-confidence-items）。

提供上传剧本文档的解析状态查询、字段级置信度列表与人工校正端点。
所有响应统一 ``success_envelope`` 包络；鉴权经
:func:`app.core.deps.require_project_acl`（按资源反查项目做 ACL）。

路由变量 ``router = APIRouter(tags=["script"])``（路径自带完整前缀，
由主控在 ``app/api/__init__.py`` 聚合）。

端点清单：
- GET   /documents/{document_id}                 解析状态 + 整体置信度
- GET   /documents/{document_id}/confidence-items 字段级置信度列表（分页）
- PATCH /parse-confidence-items/{item_id}        人工校正值
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_project_acl
from app.core.errors import BizError, ErrorCode, success_envelope
from app.repositories.script_repo import ScriptRepository
from app.schemas.script import ConfidenceItemPatchRequest, confidence_item_out, document_out
from app.services import script_service

#: 文档解析域路由（路径自带完整前缀；主控聚合：include_router(documents.router)）
router = APIRouter(tags=["script"])


@router.get("/documents/{document_id}")
async def get_document_detail(
    document_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """查询上传文档的解析状态与整体置信度。

    Args:
        document_id: 文档 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含 {parse_status, parse_error, overall_confidence,
        manual_pending_count}。

    Raises:
        BizError: 10002 文档不存在；20003 无权限。
    """
    repo = ScriptRepository(session)
    document = await repo.get_document(document_id)
    if document is None:
        raise BizError(ErrorCode.NOT_FOUND, "文档不存在")
    await require_project_acl(int(document.project_id), user_id, session, "view")
    overall = await repo.overall_confidence(document_id)
    pending = await repo.manual_pending_count(document_id)
    return success_envelope(
        document_out(document, overall_confidence=overall, manual_pending_count=pending)
    )


@router.get("/documents/{document_id}/confidence-items")
async def get_confidence_items(
    document_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    needs_manual: Annotated[
        bool | None, Query(description="按是否待人工过滤（缺省不过滤）")
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="页大小")] = 20,
) -> dict[str, Any]:
    """分页查询文档的字段级置信度项（如 episode_1.scene_2.speaker）。

    Args:
        document_id: 文档 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        needs_manual: 可选过滤（true 仅待人工项 / false 仅已处理项）。
        page: 页码（≥1）。
        page_size: 页大小（1~100）。

    Returns:
        分页结构 {items, page, page_size, total}（外层再包 success_envelope）。

    Raises:
        BizError: 10002 文档不存在；20003 无权限。
    """
    repo = ScriptRepository(session)
    document = await repo.get_document(document_id)
    if document is None:
        raise BizError(ErrorCode.NOT_FOUND, "文档不存在")
    await require_project_acl(int(document.project_id), user_id, session, "view")
    items, total = await repo.confidence_page(
        document_id, page=page, page_size=page_size, needs_manual=needs_manual
    )
    return success_envelope(
        {
            "items": [confidence_item_out(item) for item in items],
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    )


@router.patch("/parse-confidence-items/{item_id}")
async def patch_confidence_item(
    item_id: int,
    body: ConfidenceItemPatchRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """人工校正经解析置信项（needs_manual 置否）。

    当该文档全部低置信项处理完毕时，关联剧本自动从 needs_manual_fix
    推进到 pending_confirm。

    Args:
        item_id: 置信度项 ID。
        body: 校正请求体（corrected_value 必填）。
        user_id: 当前登录用户 ID（记录校正人）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含 {item, script_status}。

    Raises:
        BizError: 10002 置信项不存在；20003 无权限。
    """
    repo = ScriptRepository(session)
    item = await repo.get_confidence_item(item_id)
    if item is None or item.document_id is None:
        raise BizError(ErrorCode.NOT_FOUND, "置信度项不存在")
    document = await repo.get_document(int(item.document_id))
    if document is None:
        raise BizError(ErrorCode.NOT_FOUND, "置信度项所属文档不存在")
    await require_project_acl(int(document.project_id), user_id, session, "edit")
    result = await script_service.patch_confidence_item(
        session,
        item_id=item_id,
        corrected_value=body.corrected_value,
        user_id=int(user_id),
    )
    return success_envelope(result)
