"""异步数据库引擎与会话模块。

- ``async_engine``：全局异步引擎（惰性建连，启动时不占用连接）；
- ``async_session_factory``：会话工厂（expire_on_commit=False，提交后对象
  属性仍可访问，适配 API 序列化场景）；
- ``Base``：ORM 声明式基类，后续模型任务中的所有表模型继承于此；
- ``get_db``：FastAPI 依赖，按请求粒度提供会话并在请求结束时关闭。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# 全局异步引擎：首次真正建连发生在首次使用时（惰性），启动阶段不阻塞
# 连接池容量：pool_size + max_overflow 为并发上限（默认 20+30=50），
# 覆盖单实例并发读场景（03 文档 NFR：读接口 P95 ≤ 300/500ms）；
# 超过容量的请求在池上排队——容量按压测校准（scripts/perf_smoke.py）。
async_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,   # 取连接前探活，规避 PostgreSQL 断连后拿到失效连接
    pool_recycle=1800,    # 连接最长复用 30 分钟
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
)

# 异步会话工厂
async_session_factory = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# 约束/索引命名约定（对齐《02 数据库设计文档》§4.1：
# 索引 idx_<表>_<字段>、唯一 uq_<表>_<字段>、检查 ck_<表>_<字段>、外键 fk_...；
# 多列约束在模型中显式传完整命名，仅未显式命名的对象走此约定兜底）
NAMING_CONVENTION: dict[str, str] = {
    "ix": "idx_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """ORM 声明式基类。

    后续模型任务中的全部表模型继承本类；Alembic 迁移将基于
    ``Base.metadata`` 自动生成。metadata 挂载统一命名约定，保证
    autogenerate 产生的约束名与设计文档一致。

    Attributes:
        metadata: 携带命名约定的 MetaData 实例。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：提供一个请求级异步数据库会话。

    Yields:
        AsyncSession 会话实例；请求结束时自动提交上下文并关闭连接。
    """
    async with async_session_factory() as session:
        yield session
