"""安全配置升级：JWT_SECRET 强密钥 + ADMIN_TOKEN（一次性运维脚本）。

对 backend/.env 与 backend/.env.example：
- JWT_SECRET 长度不足 32 字节时替换为 48 字符 URL 安全随机串（修复
  InsecureKeyLengthWarning，对齐安全标准）；
- 追加 ADMIN_TOKEN（缺失时），用于埋点漏斗等管理端查询鉴权。
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def process(path: Path, *, is_example: bool) -> None:
    text = path.read_text(encoding="utf-8")
    changed = []

    # ---- JWT_SECRET ----
    m = re.search(r"^JWT_SECRET=(.*)$", text, flags=re.MULTILINE)
    if m and len(m.group(1).strip()) < 32:
        new_secret = secrets.token_urlsafe(48)
        if is_example:
            replacement = "JWT_SECRET=your-key-here-at-least-32-bytes-long"
        else:
            replacement = f"JWT_SECRET={new_secret}"
        text = text[: m.start()] + replacement + text[m.end():]
        changed.append("JWT_SECRET")
    elif m is None:
        text += f"\nJWT_SECRET={'your-key-here-at-least-32-bytes-long' if is_example else secrets.token_urlsafe(48)}\n"
        changed.append("JWT_SECRET(added)")

    # ---- ADMIN_TOKEN ----
    if "ADMIN_TOKEN=" not in text:
        value = "your-admin-token-here" if is_example else secrets.token_urlsafe(24)
        text += f"\n# ---- 管理端口令（埋点漏斗等平台级查询；空串表示禁用）----\nADMIN_TOKEN={value}\n"
        changed.append("ADMIN_TOKEN(added)")

    path.write_text(text, encoding="utf-8")
    print(f"{path.name}: {changed or 'no change'}")


def main() -> None:
    process(BACKEND_ROOT / ".env", is_example=False)
    process(BACKEND_ROOT / ".env.example", is_example=True)


if __name__ == "__main__":
    main()
