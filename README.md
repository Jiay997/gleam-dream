# 浮光造梦 · GleamDream Studio

面向短剧创作者的一站式 AI 创作工具：从一句话创意出发，自动完成**剧本 → 角色资产 → 分镜 → 视频生成 → 配音/字幕/配乐 → 成片导出**的全流程，把"一部短剧"的生产压缩到"单人操作数小时"。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3.4（`<script setup>` + TS strict）、Vite 5、Pinia、Vue Router、Tailwind CSS、Konva（vue-konva 分镜画布/时间线） |
| 后端 | FastAPI（async）、SQLAlchemy 2.0、Pydantic v2、Alembic |
| 编排 | LangGraph（状态机 + HITL interrupt + Postgres Checkpoint 断点续跑） |
| 数据库 | PostgreSQL 16（本地 `localhost:5432`，库名 `gleam-dream`） |
| 大模型 | DeepSeek（`deepseek-flash`）与 GLM（`GLM-5.3-Flash`），OpenAI 兼容协议，经统一模型网关接入 |
| 媒体 | 图像/视频/TTS/BGM 为本地 Stub Provider（生成占位媒体），接口预留真实供应商扩展点 |

## 一键启动 / 停止

项目根目录的 `gleam-dream控制台.ps1`（首次使用会自动复制到桌面）：

```powershell
powershell -ExecutionPolicy Bypass -File .\gleam-dream控制台.ps1 start    # 启动全部服务并打开浏览器
powershell -ExecutionPolicy Bypass -File .\gleam-dream控制台.ps1 status   # 查看运行状态
powershell -ExecutionPolicy Bypass -File .\gleam-dream控制台.ps1 stop     # 停止全部服务（无残留进程）
```

`start` 会按序执行：环境检查 → 虚拟环境/依赖安装（幂等）→ 数据库初始化（建库/迁移/种子，幂等）→ 后端 `:8100` → 前端 `:5175` → 打开浏览器。

| 服务 | 地址 |
|---|---|
| 前端界面 | http://127.0.0.1:5175 |
| 后端 API | http://127.0.0.1:8100/api/v1/health |
| API 文档（Swagger） | http://127.0.0.1:8100/docs |

> 端口说明：本机 8000/5173/5174 若被其他项目占用，本项目固定使用 **8100/5175**（可在 `frontend/vite.config.ts` 与脚本顶部调整）。

## 手动启动（开发模式）

```powershell
# 后端（首次需先 python scripts/init_db.py 初始化数据库）
cd backend
.venv\Scripts\python -m uvicorn app.main:app --port 8100 --loop app.loop_factory:selector_loop_factory

# 前端
cd frontend
npm install
npm run dev
```

> **Windows 注意**：uvicorn 0.36+ 在 Windows 上默认 ProactorEventLoop，会导致 psycopg 异步池（LangGraph Checkpointer）无法建连，必须带 `--loop app.loop_factory:selector_loop_factory` 参数。

## 演示旅程（注册即赠 500 积分）

1. **P01 登录/注册**：手机号 + 验证码（开发模式验证码固定 `123456`，会自动回填）
2. **P04 创建项目**：题材/画风/画幅/集数/预算
3. **P06 剧本编辑器**：一句话生成（可实时看到 write_todos 计划与进度）→ **P08 剧本确认**（HITL 关口一）
4. **P09 资产卡片墙**：智能规划角色/场景/道具 → 生成主形象/三视图/表情集（含一致性质检）→ 确认锁定（HITL 关口二）
5. **P12 分镜画布**：AI 拆解镜头（Konva 画布拖拽排序）→ **P14 草稿预览**（低成本静帧样片）→ 锁定（HITL 关口三）
6. **P15 任务中心**：批量视频生成（SSE 实时进度、失败自动重试、成本结算透明）
7. **P16/P17 后期与成片确认**：TTS 配音 → 字幕 → BGM → 合成 → 确认（HITL 关口四）
8. **P18 导出分发**：母版导出（权益校验）、元数据 AI 生成、一键分发（演示）
9. **P20 计费中心**：冻结-结算-返还全链路流水可查、充值与套餐（模拟支付）

## 后端测试

```powershell
cd backend
.venv\Scripts\python -m pytest tests/ -q
# 无外网 / CI：跳过真实 LLM 用例
.venv\Scripts\python -m pytest tests/ -q -m "not llm_live"
```

覆盖：认证与计费（冻结/结算/返还/支付幂等）、项目状态机、剧本生成与解析、资产 QC、分镜锁定、批量渲染（幂等/取消/重试/恢复）、后期合成与导出权益、编排 HITL 全链路、模型网关强化（空内容自适应重试）、埋点漏斗聚合等 **182** 个用例。

依赖复现：`backend/requirements.txt` 为版本区间约束，`backend/requirements.lock` 为当前环境实际解析版本（`pip install -r requirements.lock` 可精确复现）。

端到端冒烟：`.venv\Scripts\python scripts\e2e_smoke.py`（支持 `--from <步骤>` 断点续跑）。

## 运维诊断脚本

| 脚本 | 用途 |
|---|---|
| `backend/scripts/probe_llm.py` | DeepSeek / GLM 真实连通性探测（密钥/端点/耗时/token 计量） |
| `backend/scripts/perf_smoke.py` | 性能冒烟：P50/P95/P99、RPS、错误率（NFR-P1 口径自动判定） |
| `backend/scripts/export_openapi.py` | 导出 OpenAPI 规范到 `docs/openapi.json` |
| `backend/scripts/check_migrations.py` | 校验 Alembic 版本与关键表/约束 |
| `backend/scripts/harden_env.py` | JWT_SECRET 强密钥与 ADMIN_TOKEN 配置升级 |

## 目录结构

```
backend/                 # FastAPI 后端
  app/api/               #   路由层（27 个业务域，247 个端点）
  app/services/          #   业务服务（计费/编排/一致性/后期/导出/埋点…）
  app/graphs/            #   LangGraph 编排（状态/契约/提示词/节点/编剧子图）
  app/infra/             #   模型网关（DeepSeek/GLM/Stub）/事件总线(SSE)/存储
  app/tasks/             #   异步任务执行器（模拟 Celery 队列语义）
  alembic/               #   数据库迁移（56 张业务表 + 埋点表）
  scripts/               #   init_db / e2e_smoke / probe_llm / perf_smoke / export_openapi
  tests/                 #   pytest 测试
frontend/                # Vue3 前端
  src/views/             #   P01–P22 全部页面
  src/components/        #   AppShell 布局 + 通用组件库
  src/api/               #   统一 http 层（包络解包/幂等键/错误码策略）
docs/openapi.json        # 导出的 OpenAPI 3.1 规范（247 端点）
gleam-dream控制台.ps1     # 一键启停脚本
项目开发文档/             # 00-09 设计文档（实现依据）
```

## 演示环境说明（Sandbox）

- **媒体生成**：图像/视频/TTS/BGM 由 Stub Provider 生成占位文件（SVG/WAV），前端展示 `simulated` 产物；接入真实供应商只需在 `app/infra/model_gateway/` 增加对应 Provider 并更新 `model_registry`。
- **支付/短信/微信登录/平台分发**：均为 Sandbox Stub（模拟回调、固定验证码、Mock OAuth）。
- **密钥配置**：`backend/.env`（数据库连接、JWT、DeepSeek/GLM API Key），`.env.example` 为占位模板，请勿提交真实密钥到版本库。
- **积分体系**：注册赠 500 积分；每次生成经"预估 → 冻结 → 按实际结算 / 失败全额返还"的确定性计费节点，全部变动可在 P20 计费中心追溯。

## 生产部署安全清单（必读）

> 本地 Sandbox 演示模式与生产模式共用代码，以下配置决定是否关闭后门。

| 配置项 | 生产要求 | 说明 |
|---|---|---|
| `SANDBOX_MODE` | `false` | 关闭后：`POST /billing/subscriptions`（跳过支付开通）与 `/auth/wechat` Stub 登录一律返回 20003；支付回调不再接受固定签名 |
| `PAYMENT_WEBHOOK_SECRET` | **必填强随机** | 回调签名 = `HMAC-SHA256(out_trade_no + "\|" + status, secret)`；未配置且非沙箱时回调一律拒绝 |
| `ADMIN_TOKEN` | **必填强随机** | 模型配置读/写管理端口令（列表/新增/编辑/删除/测试/设默认）；前端「模型配置」页右上角可录入（保存在本地 `gd_admin_token`） |
| `ALLOWED_ORIGINS` | 前端真实域名（逗号分隔） | 禁止使用 `*` + 凭证组合；默认仅允许本地 5173/5175 |
| `FILES_REQUIRE_AUTH` | `true` | 开启后 `/files/*` 需 `Authorization: Bearer` 或 `?access_token=`；需前端媒体 URL 同步适配 |
| `JWT_SECRET` | ≥32 字节强随机 | 已实现登出 jti 黑名单与 refresh 轮换；黑名单为进程内实现，多实例需换 Redis |
| 单实例部署 | 必须 | EventBus、任务队列、验证码、并发信号量、注销黑名单均为进程内存实现；**禁止 uvicorn 多 worker / 多副本** |

- **密钥轮换**：如果 `.env` 曾进入任何共享包/版本库，请立即轮换数据库密码、JWT_SECRET、ADMIN_TOKEN、PAYMENT_WEBHOOK_SECRET 及全部模型 API Key（本仓库不提供自动轮换）。
- **数据与媒体**：`backend/storage/` 为用户素材/成片目录，`frontend/public/derived/` 为演示素材；交付前请清理测试账号与用户数据（根 `.gitignore` 已忽略这两类路径）。
- **测试**：`pytest tests/ -q -m "not llm_live"` 可跳过真实 LLM 用例，适合无外网/CI 环境。

## 设计规范

- 契约基线：`项目开发文档/00_文档总览与术语表.md`（术语/枚举/命名/错误码唯一事实来源）
- 设计系统：浮光紫主色 `#6D5AE6`，浅色主题，`--gd-*` CSS 变量（见 `frontend/src/assets/styles/tokens.css`）
- 状态五态：生成中（蓝）/ 待确认（黄）/ 已锁定（绿）/ 失败（红）/ 审核拦截（品红），见 `src/constants/statusTone.ts`
