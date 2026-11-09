"""GleamDream Studio 后端应用包。

多智能体 AI 短剧创作 SaaS 的 FastAPI 后端，按分层架构组织：
- api/          路由层（HTTP 端点）
- core/         框架基础设施（配置、数据库、错误、安全、日志）
- schemas/      Pydantic 请求/响应模型
- models/       SQLAlchemy ORM 模型
- repositories/ 数据访问层
- services/     业务服务层
- graphs/       LangGraph 工作流编排
- agents/       智能体实现
- tools/        Agent 工具集
- tasks/        后台/内联任务
- infra/        基础设施适配（本地存储、三方客户端等）
"""

__version__ = "1.0.0"
