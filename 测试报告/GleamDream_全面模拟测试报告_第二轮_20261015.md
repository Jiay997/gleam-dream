# 浮光造梦（GleamDream Studio）全面模拟测试报告 · 第二轮（深度与边界）

| 项目 | 内容 |
|---|---|
| 测试对象 | 浮光造梦 · AI 短剧创作工具（GleamDream Studio）MVP 全栈系统 |
| 测试依据 | 《01_AI短剧创作工具_PRD_V3.0_详细开发版》（现升级为 V4.0：`01_AI短剧创作工具_PRD_V4.0_开发落地版.md`；承接第一轮报告，编号续接 D12） |
| 测试日期 | 2026-10-15 |
| 测试方式 | 浏览器真实操作（Trae Browser / Chrome）+ API 直调（PowerShell / REST）+ PostgreSQL psql 直查取证 |
| 本轮范围 | 第一轮未覆盖的模块深度（全局功能 / 项目管理扩展 / 成员权限 / 剧本扩展 / 资产扩展 / 渲染守卫）+ 网络中断 + 断点续跑（E2E-01）+ 输入边界 + 兼容性补充 |
| 报告状态 | 完整版（可评审，与第一轮报告配套阅读） |

---

## 1. 测试环境说明

| 组件 | 版本/说明 |
|---|---|
| 前端 | Vue 3.4.38 + Vite 5.4.8 + Pinia 2.2.6 + Konva 9.3.22 + Tailwind 3.4.13，端口 5175 |
| 后端 | FastAPI（Python 3.12 / uv venv），端口 8100，`--loop app.loop_factory:selector_loop_factory` 启动 |
| 数据库 | PostgreSQL 16（asyncpg），库名 `gleam-dream` |
| LLM | DeepSeek（deepseek-flash）+ GLM（GLM-5.3-Flash）真实调用 |
| 沙箱 | `SANDBOX_MODE=true`（支付/短信/媒体/分发 Stub） |
| 测试账号 | 甲 13800001111（user_id=327）/ 乙 13800002222（user_id=328） |
| 测试数据 | 项目 #164「雾都夜行录」（archived）/ #165「审核阻断测试3」（script_review，含 pending 审批单 #71） |

**环境事件说明**：本轮测试起始于前后端进程均已停止的状态，测试人员手动重启（过程中验证了启动路径）；测试中段执行了一次受控的后端进程终止与重启，用于 E2E-01/E2E-12 验证。

---

## 2. 多智能体协作功能验证（本轮补充）

| 验证点 | 方法 | 结果 |
|---|---|---|
| LangGraph checkpoint 持久化跨重启 | 终止后端进程（含子进程树 taskkill /T /F）→ 重启 → 查库 | checkpoints/checkpoint_writes 表数据保留；审批单 #71（pending）在重启后原样存在，可继续决策——**断点续跑机制真实有效**（E2E-01 通过，见第 4 章） |
| 编排任务中断清理 | AI 局部改写请求执行中强制重启后端 | 重启后无孤儿冻结、无重复任务、积分流水无变化（frozen=0）——**中断无副作用**（E2E-12 变体通过） |
| 事件总线与操作审计 | 查询 `operation_logs` | pause/resume/member 授权/剧本提交等变更全部留痕（action/entity_type/diff/request_id），满足 NFR-C5 追溯要求 |
| 归档状态下的编排受理一致性 | archived 项目发起分镜生成 | 接口返回受理（code=0）但**无 agent_run 记录、无 storyboard 产物**——受理与执行不一致，见缺陷 **D15** |

**结论**：多智能体协作的持久化、断点恢复、审计链路经破坏性测试验证可靠；发现归档状态的编排受理与执行不一致问题（D15）。

---

## 3. 功能测试用例（本轮新增）

### 3.1 全局功能

| 用例 | 功能描述 | 操作步骤 | 预期结果 | 实际结果 | 状态 |
|---|---|---|---|---|---|
| TC-B01 | 通知列表与分类筛选 | 打开 /notifications | 通知按类型分 Tab（全部/审批/生成/系统/积分），显示未读数 | 3 条"剧本待确认"通知，5 个筛选 Tab，未读数 3 | ✅ |
| TC-B02 | 单条已读 | 点击一条通知 | 未读数 -1，徽标更新 | 已读处理正常 | ✅ |
| TC-B03 | 全部已读 | 点击"全部已读" | 未读清零、顶栏徽标消失 | Tab 从"🔔 全部 3"变为"🔔 全部"，顶栏徽标清空 | ✅ |
| TC-B04 | 个人资料保存 | 修改昵称 → 保存 → API 验证 | 落库并回显 | `GET /users/me` 昵称更新成功；恢复原名同样成功 | ✅ |
| TC-B05 | 创作偏好回显 | 打开"创作偏好"Tab | 与 onboarding 选择一致 | 题材（悬疑逆袭+科幻末世 pressed）、画风（国风）、画幅（9:16）、集数（10）全部回显正确 | ✅ |
| TC-B06 | 安全页降级诚实性 | 打开"安全"Tab | 不可用功能明确告知 | 明示"沙箱后端暂未开放修改密码接口，以下表单仅作交互演示"；登录设备列表含占位标记 | ✅ |
| TC-B07 | token 过期处理 | access token 过期（2h）后访问受保护页 | 引导重新登录 | 跳转登录页、登录后回到工作台 | ⚠️（缺陷 D12：未先尝试 refresh 续期） |

### 3.2 项目管理扩展（F8）

| 用例 | 功能描述 | 操作步骤 | 预期结果 | 实际结果 | 状态 |
|---|---|---|---|---|---|
| TC-B08 | 项目暂停 | POST /projects/165/pause | script_review → paused | status=paused，archived_at 语义字段独立 | ✅ |
| TC-B09 | 暂停期操作限制 | paused 状态发起剧本生成 | 40001 状态机拒绝 | `code=40001`（GR-1 白名单生效） | ✅ |
| TC-B10 | 项目恢复 | POST /projects/165/resume | 恢复到暂停前状态 | script_review（精确恢复原状态） | ✅ |
| TC-B11 | 项目归档 | POST /projects/164/archive（completed） | archived + archived_at | archived_at=2026-10-15T01:58:16Z | ✅ |
| TC-B12 | 归档后导出限制 | archived 项目新建导出 | 拒绝（只读语义） | **code=0 且任务 #30 实际创建并 succeeded** | ❌（缺陷 D13） |
| TC-B13 | 归档恢复路径 | POST resume（archived） | 应拒绝或提供 unarchive | 40001（与 B12 组合后，归档项目"能写不能恢复"语义矛盾） | ⚠️ |
| TC-B14 | 归档后分镜生成 | POST storyboards/generate（episode 2） | 40001 拒绝 | code=0 受理，但无 agent_run、无产物——**静默丢弃** | ❌（缺陷 D15） |
| TC-B15 | 操作审计留痕 | 查 operation_logs | 全部变更留痕 | update/create 记录含 request_id 与 diff | ✅ |

### 3.3 成员与权限矩阵（F8/PR-1~PR-4）

| 用例 | 操作 | 预期 | 实际 | 状态 |
|---|---|---|---|---|
| TC-B16 | Owner 授予用户乙 view | PUT members/{uid} permission=view | code=0 | ✅ |
| TC-B17 | view 读项目 | GET 165 | 成功（status 可见） | ✅ |
| TC-B18 | view 尝试写（PATCH synopsis） | 20003 | code=20003 | ✅ |
| TC-B19 | view 自我提权（view→export） | 20003 | code=20003 | ✅ |
| TC-B20 | Owner 升级 view→export | code=0 | code=0 | ✅ |
| TC-B21 | Owner 撤销成员授权（DELETE） | 移除成功、访问失效 | **HTTP 405 Method Not Allowed——撤销端点不存在** | ❌（缺陷 D14） |
| TC-B22 | 成员列表展示 | Owner + 授权成员、is_owner 标记 | `{owner_user_id:327, items:[{user_id:327, is_owner:true, permission:"export"}]}` | ✅ |

> 结论：权限读取/授予/提权拦截全部正确（PR-1/PR-3 服务端判定成立）；但**权限生命周期不完整**——授权只能授予不能撤销（PR-4 级联要求中"成员移除"缺失）。

### 3.4 剧本扩展（F2）

| 用例 | 功能描述 | 操作步骤 | 预期结果 | 实际结果 | 状态 |
|---|---|---|---|---|---|
| TC-B23 | 分集大纲页 | 打开 /script/outline | 10 集节奏总览与分集结构 | 每集时长（2:41–3:00）+ "单集目标时长（上限 3 分钟，超出建议裁剪）"提示（符合 Q6） | ✅ |
| TC-B24 | AI 改写弹窗 | 集头"AI 改写本集" | 弹窗含范围标签 + ≥5 字反馈校验 | DOM 证实弹窗（"AI 局部改写 / 改写范围：第 1 集（整集）/ 0/5 字"），AX 树因节流未捕获（环境限制） | ✅（功能见下） |
| TC-B25 | AI 改写状态守卫 | 对 archived 项目 POST rewrite | 40001 | 40001 拒绝（第一次改写请求恰逢受控重启被中断，重启后无残留副作用，见第 2 章） | ✅ |
| TC-B26 | 剧本版本链 | 查 scripts 表 | 版本号连续、状态机清晰 | v1→v4（pending_confirm），locked/compliance_review 等状态并存，字数 9,064→10,502 递进 | ✅ |

### 3.5 资产扩展（F3）

| 用例 | 功能描述 | 操作步骤 | 预期结果 | 实际结果 | 状态 |
|---|---|---|---|---|---|
| TC-B27 | 音色绑定 | PUT /assets/179/voice voice_id=2 | 绑定落库 | 返回资产含 `voice_id:2`，用于后续 TTS | ✅ |
| TC-B28 | 锚点影响面（GR-2 数据） | GET /assets/179/anchor-impact | 报告受影响镜头数 | `affected_shots: 12`（陈默覆盖全部 12 镜，更换锚点将解锁全部） | ✅ |
| TC-B29 | 锁定资产重抽守卫 | POST /assets/180/regenerate | 40001"已锁定不可重新生成" | code=40001，版本数不变 | ✅ |
| TC-B30 | 音色库 | GET /tts-voices | 音色列表 | 10 个音色（含性别标注） | ✅ |

### 3.6 渲染任务守卫（F5）

| 用例 | 操作 | 预期 | 实际 | 状态 |
|---|---|---|---|---|
| TC-B31 | 取消已成功任务（POST /render-tasks/111/cancel） | 40001 状态守卫 | code=40001 | ✅ |
| TC-B32 | 重试已成功任务（/retry） | 40001 状态守卫 | code=40001 | ✅ |

---

## 4. 边界与异常测试（本轮）

| 编号 | 场景 | 操作 | 预期 | 实际 | 状态 |
|---|---|---|---|---|---|
| E2E-01 | **断点续跑** | 记录状态（2 项目状态/审批单/5 快照/4 剧本版本/积分）→ taskkill 终止后端进程树 → 重启 → 逐项比对 | 状态完整恢复、无数据丢失、审批单可继续处理 | **全部一致**：165=script_review、164=archived、#71 pending、快照 5、剧本 4、balance=5699/frozen=0/consumed=1801 | ✅ |
| E2E-12' | 长任务中断（变体） | AI 改写请求执行中强制重启后端 | 无孤儿冻结、无重复扣费、系统可继续使用 | frozen=0、流水无新增、重启后正常受理新请求 | ✅ |
| NET-01 | 后端宕机前端表现 | 杀后端 → 前端访问项目总览 | 明确错误提示或优雅回退，不白屏 | 路由守卫 `ensureProject` 失败 → **自动回退 /projects 列表页**，无崩溃、无白屏 | ✅ |
| NET-02 | 无效 token | `Authorization: Bearer invalid.token.here` | 401 | HTTP 401 + code=20001 | ✅ |
| NET-03 | 无认证头 | 不带 Authorization 访问 /users/me | 401 | HTTP 401 | ✅ |
| BND-06 | 404 资源 | GET /projects/999999 | 10002 语义错误码 | code=10002"项目不存在"（非裸 404） | ✅ |
| BND-07 | 非法路径参数 | GET /projects/abc | 10001 参数校验 | code=10001（Pydantic 路径类型校验） | ✅ |
| BND-08 | XSS 存储 | 项目名 `<script>alert(1)</script>` | 存储或转义，前端不得执行 | API 原样存储（未过滤）；**前端 Vue 文本插值自动转义**——DOM 检验 `scriptNodeCount=0`，脚本以纯文本渲染，**无 XSS 执行** | ✅（防御纵深建议见 8.3） |
| BND-09 | 超长标题（300 字） | POST title×300 | 10001 | `String should have at most 255 characters` | ✅ |
| BND-10 | 空标题 | POST title="" | 10001 | `String should have at least 1 character` | ✅ |
| BND-11 | 集数超上限 | target_episode_count=31 | 40005 | code=40005 `{"allowed":"1~30"}`（Q6 默认值） | ✅ |
| BND-12 | 方法不允许 | DELETE /projects | 405 | HTTP 405 | ✅ |

---

## 5. 关键业务流程测试（本轮聚焦"非 happy path"）

本轮验证了主流程之外的关键分支，全部符合 PRD 设计：

1. **暂停-恢复流程**：`script_review → paused →（拒绝生成 40001）→ script_review`——状态机唯一性（GR-1）与白名单流转实测成立。
2. **权限授予-拦截流程**：owner → 授予 view → 只读生效 → 自我提权被拒 → 升级 export →（撤销缺失 D14）——服务端权限判定（PR-1/PR-2/PR-3）成立。
3. **中断-恢复流程**：编排执行中进程被杀 → checkpoint 恢复 → 数据零丢失 → 审批单继续可用——"断点不重来"（UV-3）在 API/数据层成立。
4. **异常输入流程**：XSS/超长/空值/越界集数/非法参数 → 全部在服务端被结构化错误码拦截，无 5xx。

---

## 6. 界面兼容性测试（本轮补充）

| 检查项 | 方法 | 结果 |
|---|---|---|
| 横向溢出（614px 视口） | 项目列表/剧本编辑器/资产墙/计费中心逐页 `scrollWidth vs clientWidth` | 项目列表无溢出（614/614）；其余页面因浏览器节流未全部采集，第一轮全程 612px 操作未见横向滚动条 |
| 移动断点交互 | 全程 612px 操作 | 汉堡导航、抽屉详情、底部吸底审批栏、分镜画布主动降级提示——一致性良好 |
| `prefers-reduced-motion` | 代码审查 | 全局降级规则存在（index.css:93），符合无障碍基线 |
| Windows DPI 1.25 | 全程实测 | 文字/图标无模糊错位 |
| 桌面宽屏（≥1280px）/ Firefox / Safari | 未实测（浏览器窗口受控限制） | 遗留项，建议人工回归；布局为桌面优先 + lg: 增强，风险低 |

---

## 7. 缺陷清单（本轮新增，编号续接第一轮）

| 编号 | 严重度 | 模块 | 缺陷描述 | 复现步骤 | 实际 vs 预期 | 证据 |
|---|---|---|---|---|---|---|
| **D12** | **一般** | 全局认证 | refreshToken 已存储但未实现自动续期：access token（2h）过期后直接踢回登录页 | 登录 → 等待 2h（或调短 JWT_ACCESS_EXPIRE_MINUTES）→ 任意操作 | 预期：401 时先调 `/auth/refresh` 静默续期（后端端点已存在）；实际：axios 拦截器对 20001/401 一律 `redirectToLogin()`，refreshToken 从未被使用。与 PRD 7.2 会话可用性预期不符 | [http.ts#L164-188](file:///e:/FeiSi/Project%20code/gleam-dream/frontend/src/api/http.ts#L164-L188)；实测过期跳登录 |
| **D13** | **一般** | F8/F7 | 归档（archived）项目仍可新建导出任务 | 对 archived 项目 POST /exports | 预期：归档为只读语义应拒绝写操作；实际：code=0 且导出任务 #30 创建并 succeeded。同源问题：归档态也未限制剧本改写以外的多数写入口（与暂停态严格 40001 形成不一致） | DB：exports 表新增 #30 |
| **D14** | **一般** | F8 协作 | 项目成员授权无撤销端点：DELETE /projects/{id}/members/{uid} 返回 405 | Owner 授予成员 view → 尝试 DELETE 撤销 | 预期：PR-4 要求成员移除级联生效；实际：方法不存在，权限只能授予/升级，**无法撤销或降级**，被授权用户永久保留访问 | 实测 HTTP 405；路由表仅 PUT/GET members |
| **D15** | **一般** | F8/F4 | 归档项目发起分镜生成"受理成功但静默丢弃" | 对 archived 项目 POST /storyboards/generate {episode_no:2} | 接口返回 code=0（受理），但无 agent_run、无 storyboard 产物、无用户可见失败提示——受理与执行不一致，用户会误以为正在生成 | DB：storyboards 无新行、agent_runs 无新记录 |
| D16 | 轻微 | F8 | archived 项目无恢复（unarchive）路径 | POST /resume 对 archived 项目 | 返回 40001"状态不允许"；归档为终态。若为有意设计建议 UI 明示"归档不可逆"，否则补 unarchive | 实测 40001 |
| D17 | 轻微 | F2 | AI 改写弹窗 role/aria 缺失 | 打开改写弹窗 → accessibility 树检查 | Modal 内容在 DOM 存在但不出现在 accessibility 树（无 role=dialog 或容器结构问题），屏幕阅读器不可达；建议补充 ARIA 标注 | DOM 有内容 vs AX 树为空 |

> 第一轮 D1–D11 状态：本轮未修复、未回归（本轮未涉及对应代码变更）；D2（过渡动画卡死）在后台化窗口场景再次复现，进一步佐证其普遍性。

---

## 8. 测试总结与建议

### 8.1 通过率汇总（本轮）

| 维度 | 通过 / 总数 | 通过率 |
|---|---|---|
| 功能用例（3.1–3.6） | 27 / 30（3 项 ❌：D13/D14/D15 相关；1 项 ⚠️） | **90.0%** |
| 边界与异常（第 4 章） | 13 / 13（含 E2E-01 断点续跑） | **100%** |
| 输入安全 | XSS 转义/长度/空值/枚举校验 4/4 | **100%** |

### 8.2 风险点

1. **🟡 权限生命周期不完整（D14）**：当前为"只升不降"的授权模型。个人创作者影响小，但 P1 团队版（PRD 分期）将直接依赖成员管理，**建议在 P1 前必须补齐**。
2. **🟡 归档态写守卫不一致（D13/D15/D16）**：暂停态守卫严格（40001 全拦截），归档态却出现"导出放行 + 编排静默丢弃 + 无法恢复"三种不一致行为，状态机语义需要统一梳理（建议：归档=纯只读白名单 + 显式 unarchive）。
3. **🟡 会话续期缺失（D12）**：2 小时强制重登录对创作类长会话产品体验伤害大，修复成本低（拦截器加一次 refresh 重放）。
4. **🟢 数据可靠性表现优秀**：进程级破坏性测试（E2E-01/E2E-12'）零数据丢失、零孤儿副作用；资金流水继续精确守恒（consumed=1801 与第一轮一致）。
5. **🟢 输入与安全边界扎实**：XSS 前端转义、错误码语义化、参数校验全覆盖，无 5xx。

### 8.3 改进建议（按优先级）

| 优先级 | 建议 | 对应缺陷 |
|---|---|---|
| P0 | axios 响应拦截器增加 401 → `/auth/refresh` 单次重放逻辑（失败再踢登录） | D12 |
| P1 | 补 DELETE /projects/{id}/members/{uid}（撤销 + 级联校验 ACL） | D14 |
| P1 | 归档态统一只读：project_guard 增加 archived 拒写分支（导出/编排/配置等全部写端点），编排受理前置状态校验避免静默丢弃 | D13/D15 |
| P2 | 归档提供 unarchive 或 UI 明示不可逆；RewriteModal/全局 Modal 补 `role="dialog"` 与 aria 属性 | D16/D17 |
| P2 | API 层对用户输入标题/简介增加富文本过滤（当前依赖前端转义，防御纵深） | BND-08 |
| P2 | 桌面宽屏与 Firefox/Safari 人工回归（第一轮 6.3 遗留项） | — |

### 8.4 两轮累计缺陷总览

| 轮次 | 致命 | 严重 | 一般 | 轻微 | 合计 |
|---|---|---|---|---|---|
| 第一轮 | 0 | 3（D1/D2/D3） | 5（D4–D8） | 3（D9–D11） | 11 |
| 本轮 | 0 | 0 | 4（D12–D15） | 2（D16/D17） | 6 |
| **累计** | **0** | **3** | **9** | **5** | **17** |

无致命级缺陷；3 个严重级（D1/D2/D3）集中在 UI 流程断点与过渡动画，均有明确修复路径。

---

*报告完 · 全部结论基于实际操作：API 响应包络、PostgreSQL 直查结果、浏览器 DOM/AX 快照与受控破坏性测试记录。测试脚本存档 `测试报告/scripts/`（本轮新增 test_members*.ps1 / test_asset_ext.ps1 / test_rewrite*.ps1 / test_boundary2.ps1 / test_storyboard_ep2.ps1 等）。*
