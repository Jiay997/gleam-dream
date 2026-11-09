"""临时诊断脚本：查询项目 90 审批单与状态（用后即删）。"""

import asyncio

from sqlalchemy import select

from app.core.db import async_session_factory
from app.models import ApprovalRequest, Project, Storyboard


async def main() -> None:
    """打印项目 90 状态、审批单与分镜板状态。"""
    async with async_session_factory() as s:
        p = (await s.execute(select(Project).where(Project.id == 90))).scalar_one()
        print(f"Project 90 status={p.status} config={p.config}")
        aps = (
            await s.execute(select(ApprovalRequest).where(ApprovalRequest.project_id == 90))
        ).scalars().all()
        for a in aps:
            print(f"Approval #{a.id} type={a.approval_type} status={a.status} decision={a.decision} target={a.target_type}/{a.target_id}")
        sbs = (
            await s.execute(select(Storyboard).where(Storyboard.project_id == 90))
        ).scalars().all()
        for sb in sbs:
            print(f"Storyboard #{sb.id} ep={sb.episode_no} status={sb.status}")


asyncio.run(main())
