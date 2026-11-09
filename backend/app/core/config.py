"""应用配置模块。

使用 pydantic-settings 从 backend/.env 加载环境变量，并提供全局单例
``settings`` 供所有模块使用。STORAGE_DIR 等相对路径统一以 backend/ 根
目录为锚点解析，避免受启动时工作目录影响。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 根目录（本文件位于 backend/app/core/config.py，向上三级）
BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """全局应用配置。

    从 backend/.env 读取环境变量；字段名与环境变量名不区分大小写，
    未提供的字段使用代码内默认值。

    Attributes:
        APP_NAME: 应用名称。
        ENV: 运行环境（dev/test/prod）。
        API_V1_PREFIX: API 路由统一前缀。
        DATABASE_URL: PostgreSQL 连接串（已含 asyncpg 驱动前缀）。
        DB_POOL_SIZE: 连接池常驻连接数（默认 20）。
        DB_MAX_OVERFLOW: 连接池可超发连接数（默认 30）。
        JWT_SECRET: JWT 签名密钥（HS256）。
        JWT_ACCESS_EXPIRE_MINUTES: 访问令牌有效期（分钟）。
        JWT_REFRESH_EXPIRE_DAYS: 刷新令牌有效期（天）。
        DEEPSEEK_API_KEY: DeepSeek 平台 API Key。
        DEEPSEEK_BASE_URL: DeepSeek API 基础地址。
        DEEPSEEK_MODEL: DeepSeek 默认模型名。
        GLM_API_KEY: 智谱开放平台 API Key。
        GLM_BASE_URL: 智谱 GLM Chat Completions 地址。
        GLM_MODEL: GLM 默认模型名。
        ADMIN_TOKEN: 管理端口令（埋点漏斗等平台级查询用；空则管理端点禁用）。
        STORAGE_DIR: 本地磁盘存储根目录（相对路径锚定 backend/）。
        SANDBOX_MODE: 沙箱开关（true 时不产生真实扣费/三方副作用）。
        DEV_FIXED_SMS_CODE: 开发环境固定短信验证码。
    """

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 应用基础 ----
    APP_NAME: str = "GleamDream Studio"
    ENV: str = "dev"
    API_V1_PREFIX: str = "/api/v1"

    # ---- 数据库 ----
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/gleam-dream"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 30

    # ---- JWT 认证 ----
    JWT_SECRET: str = "gd-dev-secret-change-me"
    JWT_ACCESS_EXPIRE_MINUTES: int = 120
    JWT_REFRESH_EXPIRE_DAYS: int = 30

    # ---- DeepSeek 模型 ----
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-flash"

    # ---- 智谱 GLM 模型 ----
    GLM_API_KEY: str = ""
    GLM_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    GLM_MODEL: str = "GLM-5.3-Flash"

    # ---- 阿里云百炼（AI 漫剧）媒体模型 ----
    BAILIAN_API_KEY: str = ""
    BAILIAN_BASE_URL: str = "https://ws-8pvjeij4qw980ad2.cn-beijing.maas.aliyuncs.com"
    BAILIAN_WS_URL: str = (
        "wss://ws-8pvjeij4qw980ad2.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )
    BAILIAN_TTS_MODEL: str = "qwen-audio-3.1-tts-plus"
    BAILIAN_TTS_VOICE: str = "Cherry"
    BAILIAN_IMAGE_MODEL: str = "qwen-image-3.0"
    BAILIAN_VIDEO_MODEL: str = "wan3.0-video"
    #: 视频生成任务轮询超时（秒）与间隔
    BAILIAN_VIDEO_POLL_TIMEOUT: int = 600
    BAILIAN_VIDEO_POLL_INTERVAL: int = 5

    # ---- Agnes AI（图片/视频媒体模型，OpenAI 风格兼容）----
    AGNES_API_KEY: str = ""
    AGNES_BASE_URL: str = "https://apihub.agnes-ai.com/v1"
    AGNES_IMAGE_MODEL: str = "agnes-image-2.5-flash"
    AGNES_VIDEO_MODEL: str = "agnes-video-2.5-flash"
    #: 视频任务轮询超时（秒）与间隔（文档建议 1~2 秒）
    AGNES_VIDEO_POLL_TIMEOUT: int = 600
    AGNES_VIDEO_POLL_INTERVAL: int = 3

    # ---- 千问 AI · Qwen3-TTS（HTTP 同步语音合成）----
    QWEN_TTS_API_KEY: str = ""
    QWEN_TTS_BASE_URL: str = "https://dashscope.aliyuncs.com"
    QWEN_TTS_MODEL: str = "qwen3-tts-flash"
    QWEN_TTS_VOICE: str = "Cherry"

    # ---- 管理端口令（平台级查询 / 模型配置写操作；空串表示管理端点禁用）----
    ADMIN_TOKEN: str = ""

    # ---- 支付回调密钥（非沙箱环境必填；null/空时非沙箱回调一律拒绝）----
    PAYMENT_WEBHOOK_SECRET: str = ""

    # ---- CORS 允许来源（逗号分隔；生产禁止使用通配）----
    ALLOWED_ORIGINS: str = (
        "http://127.0.0.1:5175,http://localhost:5175,http://127.0.0.1:5173,http://localhost:5173"
    )

    # ---- 静态文件鉴权（true 时 /files 需 Bearer 或 ?access_token=）----
    # 默认关闭以兼容既有前端 <img src> 直链；生产环境建议开启并适配媒体 URL。
    FILES_REQUIRE_AUTH: bool = False

    # ---- 本地存储 ----
    STORAGE_DIR: str = "./storage"

    # ---- 沙箱开关 ----
    SANDBOX_MODE: bool = True
    #: TTS 是否允许在 SANDBOX_MODE=true 时仍走真实提供商。
    #: 图片/视频在沙箱下仍走真实路由，而配音此前被全局沙箱强制 stub，
    #: 导致“画面真实、配音占位”的混合产物（Q-003）。
    TTS_ALLOW_REAL: bool = False

    # ---- 开发环境固定短信验证码 ----
    DEV_FIXED_SMS_CODE: str = "123456"

    @property
    def allowed_origins(self) -> list[str]:
        """解析 CORS 允许来源列表。

        Returns:
            去空白后的来源列表；包含 ``*`` 时由调用方关闭凭证。
        """
        return [item.strip() for item in self.ALLOWED_ORIGINS.split(",") if item.strip()]

    @property
    def database_url_masked(self) -> str:
        """获取脱敏后的数据库连接串。

        Returns:
            隐藏密码后的 DATABASE_URL，用于日志输出。
        """
        url = self.DATABASE_URL
        if "@" not in url:
            return url
        cred, _, host_part = url.partition("@")
        scheme, _, _ = cred.partition("://")
        return f"{scheme}://***@{host_part}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例。

    Returns:
        Settings 实例（进程内缓存，仅首次读取 .env）。
    """
    return Settings()


# 全局配置单例
settings: Settings = get_settings()
