# 第 5 阶段 Playwright 回归测试

覆盖测试报告（AI短剧全链路测试报告_20261103.md）P0-P2 修复后的核心路径与 1.2 链路保真度断言。

## 前置条件

1. 后端已启动：`backend/` 目录 `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8100`
2. 前端已启动：`frontend/` 目录 `npm run dev`（端口 5175）
3. Playwright 依赖随 frontend/node_modules 提供（无需额外安装）

## 运行

```powershell
# 在 frontend/ 目录下执行
cd frontend

# 快速回归（核心路径 UI 冒烟 + 保真度断言，约 1 分钟）
npx playwright test -c ../tests/regression/playwright.config.ts

# 仅保真度断言
npx playwright test -c ../tests/regression/playwright.config.ts fidelity

# 仅 UI 冒烟
npx playwright test -c ../tests/regression/playwright.config.ts core-path

# 完整链路（真实调用模型，约 20 分钟，含 Q-014 并发生成 40001 验证）
$env:FULL_CHAIN='1'; npx playwright test -c ../tests/regression/playwright.config.ts full-chain
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| FRONT_BASE | http://localhost:5175 | 前端地址 |
| API_BASE | http://localhost:8100/api/v1 | 后端地址 |
| QA_PHONE | 13900009903 | 测试账号 |
| QA_PASSWORD | E2ePass123456 | 测试账号密码 |
| REG_PROJECT | 1410 | 保真度断言的基线项目 ID |
| FULL_CHAIN | 未设置 | 置 1 启用 full-chain.spec |

## 覆盖的修复项

- **core-path.spec.ts**：登录→项目列表→工作台进入（Q-015 状态标签、Q-020 字体本地化后无外链阻塞）
- **fidelity.spec.ts**：Q-012（script_anchor/scene_id/prop_asset_ids 输出）、1.2 保真度（final_prompt 人物造型锁定）、Q-013（镜头视频地址优先）、Q-010（preview_url 真实渲染）
- **full-chain.spec.ts**：Q-005（剧本重试）、Q-014（资产并发生成 40001）、Q-018（超时看门狗路径）
