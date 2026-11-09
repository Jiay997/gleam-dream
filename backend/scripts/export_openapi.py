"""OpenAPI 规范导出脚本（验收 · API 文档交付物）。

导出 FastAPI 应用完整的 OpenAPI 3.1 规范到 ``docs/openapi.json``，
供：离线查阅、前端代码生成（openapi-typescript）、接口回归 diff、
网关/测试工具导入。

用法::

    cd backend
    .venv\\Scripts\\python scripts\\export_openapi.py            # 默认输出 docs/openapi.json
    .venv\\Scripts\\python scripts\\export_openapi.py --out path  # 自定义输出路径
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 把 backend 根目录加入 sys.path（脚本直接运行时需要）
_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.main import app  # noqa: E402

# 输出根目录：仓库根 docs/（backend/ 上一级）
_REPO_ROOT = Path(_BACKEND_ROOT).parent


def main() -> None:
    """导出 OpenAPI JSON。

    Raises:
        SystemExit: 应用导入失败或写出失败时非 0 退出。
    """
    parser = argparse.ArgumentParser(description="导出 OpenAPI 规范")
    parser.add_argument("--out", default=str(_REPO_ROOT / "docs" / "openapi.json"),
                        help="输出文件路径（默认 <repo>/docs/openapi.json）")
    args = parser.parse_args()

    spec = app.openapi()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 汇总摘要
    paths = spec.get("paths", {})
    method_counts: dict[str, int] = {}
    for item in paths.values():
        for method in item:
            if method in ("get", "post", "put", "patch", "delete"):
                method_counts[method] = method_counts.get(method, 0) + 1
    total = sum(method_counts.values())
    print(f"OpenAPI 导出完成: {out_path}")
    print(f"路径数: {len(paths)} | 端点数: {total} | 方法分布: {method_counts}")


if __name__ == "__main__":
    main()
