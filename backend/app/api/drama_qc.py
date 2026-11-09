"""成片多维质检 API（P0 质检模块优化）。

提供项目级 rubric 质检端点：按 7 大维度（剧本/分镜/节奏/画面/配音/
剪辑/合规）对成片量化评分，返回维度得分、逐镜头评分表与不合格项
（定位到具体环节 + 修复建议）。质检独立于生成流程，可随时对任意
项目调用，用于验收与回归。

路由变量 ``router``（prefix=/projects, tags=[drama-qc]）由
``app/api/__init__.py`` 聚合引入。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, project_guard
from app.core.errors import success_envelope
from app.models.project import Project
from app.services import drama_qc_service

#: 成片质检路由（主控聚合使用：prefix=/projects, tags=[drama-qc]）
router = APIRouter(prefix="/projects", tags=["drama-qc"])


@router.get("/{project_id}/drama-qc")
async def get_drama_qc(
    project: Annotated[Project, Depends(project_guard("view"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """执行成片多维质检并返回 rubric 报告（只读，不落库）。

    Args:
        project: 项目 ORM（project_guard 依赖注入，含 ACL 校验）。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 为质检报告（total_score/dimensions/shots/
        failures）。

    Raises:
        BizError: 20001 未登录；40001 无项目权限。
    """
    report = await drama_qc_service.evaluate_project(session, project)
    return success_envelope(report)
