"""认证与用户资料 DTO 模块（Task 3 · 认证与计费域）。

定义认证域全部请求体验证模型（Pydantic v2）：
- 短信验证码：发送请求 / 响应数据；
- 注册 / 登录 / 微信 Stub 登录 / 刷新令牌；
- 用户资料更新（PATCH /users/me，供新手引导 P02 偏好存储）。

响应统一走 :func:`app.core.errors.success_envelope` 包络，
data 字段由服务层构造 dict，此处仅负责请求体校验。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: 中国大陆手机号（11 位，1 开头第二位 3-9），自动去除首尾空白
PhoneStr = Annotated[str, StringConstraints(pattern=r"^1[3-9]\d{9}$", strip_whitespace=True)]


class SmsCodeRequest(BaseModel):
    """发送短信验证码请求体。

    Attributes:
        phone: 接收验证码的手机号。
    """

    model_config = ConfigDict(extra="forbid")

    phone: PhoneStr = Field(description="接收验证码的手机号")


class SmsCodeData(BaseModel):
    """发送短信验证码响应数据。

    Attributes:
        expires_in: 验证码有效期（秒）。
        dev_code: 开发固定验证码（仅 SANDBOX_MODE 返回，供自动化测试）。
    """

    expires_in: int = Field(description="验证码有效期（秒）")
    dev_code: str | None = Field(default=None, description="沙箱固定验证码（仅 sandbox 返回）")


class RegisterRequest(BaseModel):
    """手机号 + 验证码注册请求体。

    Attributes:
        phone: 注册手机号。
        sms_code: 短信验证码（4~8 位数字）。
        password: 可选登录密码（6~64 位，未提供则仅支持短信登录）。
        nickname: 可选昵称（缺省按手机号尾号生成）。
    """

    model_config = ConfigDict(extra="forbid")

    phone: PhoneStr = Field(description="注册手机号")
    sms_code: Annotated[str, StringConstraints(min_length=4, max_length=8, strip_whitespace=True)] = Field(
        description="短信验证码"
    )
    password: Annotated[str | None, StringConstraints(min_length=6, max_length=64)] = Field(
        default=None, description="可选登录密码（6~64 位）"
    )
    nickname: Annotated[str | None, StringConstraints(min_length=1, max_length=64)] = Field(
        default=None, description="可选昵称"
    )


class LoginRequest(BaseModel):
    """登录请求体（密码 / 短信双通道）。

    Attributes:
        login_type: 登录方式，password 或 sms。
        phone: 登录手机号。
        password: 密码（login_type=password 时必填）。
        sms_code: 短信验证码（login_type=sms 时必填）。
    """

    model_config = ConfigDict(extra="forbid")

    login_type: str = Field(pattern=r"^(password|sms)$", description="登录方式：password / sms")
    phone: PhoneStr = Field(description="登录手机号")
    password: str | None = Field(default=None, description="密码（password 登录时必填）")
    sms_code: Annotated[str | None, StringConstraints(min_length=4, max_length=8, strip_whitespace=True)] = Field(
        default=None, description="短信验证码（sms 登录时必填）"
    )


class WechatLoginRequest(BaseModel):
    """微信登录 Stub 请求体（沙箱模拟，覆盖 AC-F1-02）。

    Attributes:
        mock_unionid: 模拟微信 unionid；缺省时自动生成新身份（走注册分支）。
    """

    model_config = ConfigDict(extra="forbid")

    mock_unionid: str | None = Field(default=None, max_length=128, description="模拟微信 unionid")


class RefreshRequest(BaseModel):
    """刷新访问令牌请求体。

    Attributes:
        refresh_token: 登录时签发的 refresh JWT。
    """

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=16, description="刷新令牌")


class UserUpdateRequest(BaseModel):
    """更新个人资料请求体（PATCH /users/me）。

    三个字段均可选；preferences 与库中 JSONB 做浅合并（新增键覆盖同名键）。

    Attributes:
        nickname: 新昵称（1~64 字符）。
        avatar_url: 新头像 URL（最长 512）。
        preferences: 偏好设置键值对（新手引导 P02 采集结果）。
    """

    model_config = ConfigDict(extra="forbid")

    nickname: Annotated[str | None, StringConstraints(min_length=1, max_length=64)] = Field(
        default=None, description="新昵称"
    )
    avatar_url: Annotated[str | None, StringConstraints(min_length=1, max_length=512)] = Field(
        default=None, description="新头像 URL"
    )
    preferences: dict | None = Field(default=None, description="偏好设置（JSONB 浅合并）")
