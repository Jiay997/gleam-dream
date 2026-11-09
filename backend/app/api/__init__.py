"""API 路由聚合器。

集中挂载全部子路由，main.py 以 ``settings.API_V1_PREFIX``（/api/v1）
统一前缀引入。各业务域路由由对应任务产出后在此注册。
"""

from fastapi import APIRouter

from app.api import analytics
from app.api import assistant
from app.api import auth
from app.api import assets
from app.api import billing
from app.api import composition
from app.api import continuity
from app.api import anchors
from app.api import drama_qc
from app.api import documents
from app.api import events
from app.api import export
from app.api import health
from app.api import lines
from app.api import model_configs
from app.api import notifications
from app.api import orchestration
from app.api import params
from app.api import photo_characters
from app.api import post
from app.api import projects
from app.api import render
from app.api import scripts
from app.api import storyboards
from app.api import teams
from app.api import users
from app.api import voice

#: v1 版本聚合路由
api_router = APIRouter()

# 健康检查（无鉴权）
api_router.include_router(health.router, tags=["health"])

# 认证与用户域（Task 3）
api_router.include_router(auth.router)
api_router.include_router(users.router)

# 计费域（Task 3）
api_router.include_router(billing.router)

# 项目域与团队域（Task 4）
api_router.include_router(projects.router)
api_router.include_router(teams.router)

# 编排域（Task 6：图触发/审批决策）
api_router.include_router(orchestration.router)

# 剧本域（Task 7a：剧本结构/文档解析/合规送审）
api_router.include_router(scripts.router)
api_router.include_router(documents.router)

# 资产域与分镜域（Task 7b）
api_router.include_router(assets.router)
api_router.include_router(storyboards.router)

# 生成执行域（Task 8）
api_router.include_router(render.router)

# 后期域与导出分发域（Task 9）
api_router.include_router(post.router)
api_router.include_router(export.router)
# 成片多维质检（rubric 评分 Agent，独立于生成流程）
api_router.include_router(drama_qc.router)

# 通知域（通知中心/顶栏未读徽标）
api_router.include_router(notifications.router)

# 埋点域（验收报告 7.1 P0 埋点最小集：客户端上报 + 管理端漏斗）
api_router.include_router(analytics.router)

# 模型配置域（四类模型接入参数维护 / 测试连接 / 默认路由绑定，即时生效）
api_router.include_router(model_configs.router)

# 全局 SSE 事件流（D22 修复：AppShell 聚合订阅用户全部可见项目）
api_router.include_router(events.router)

# V6 增量：F2.12 台词清单 + F4.13 台词绑定 + F9 配音轨（V6.0）
api_router.include_router(lines.router)
api_router.include_router(voice.router)

# V6 增量：F10 合成编排（V6.1）
api_router.include_router(composition.router)

# V6 增量：F11 参数中心（V6.2）
api_router.include_router(params.router)

# V6 增量：5A Story Bible/连续性/反馈 + F2.13 逻辑规则 + F3.14 照片合成（V6.3）
api_router.include_router(continuity.router)
api_router.include_router(photo_characters.router)

# V6 增量：F3.13 多维锚点包
api_router.include_router(anchors.router)

# AI 创作助手（PRD V6.4 第 6 章多智能体架构的统一对话入口）
api_router.include_router(assistant.router)
