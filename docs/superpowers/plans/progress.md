# 实施进度 ledger — plan: docs/superpowers/plans/2026-10-27-shot-composition-frames.md

> 项目目录非 Git 仓库，故以本文件替代 SDD 的 git ledger（记录变更与验证证据）。

- [x] Task 1 完成：`shot_frame_spec.py`（构图契约 + 规则推导）+ 17 项单测通过
- [x] Task 2 完成：`shots.composition_spec` JSONB 迁移（`f2a3b4c5d6e7`）已 upgrade head + 列校验测试通过
- [x] Task 3 完成：`frame_composer.py`（ffmpeg 抠像叠加 + 覆盖率检测）+ 5 项集成测试通过
  - 偏差：`colorkey` 前必须 `format=rgba`（PNG 常为 rgb24，否则 ffmpeg 退出 -5）
  - 偏差：`metadata=print` 输出在 stderr（按版本可能 stdout），解析需合并两路
- [x] Task 4 完成：`shot_frame_service.py`（取图层→合成→MediaFile→绑首帧）+ 4 项单测通过
  - 偏差：FakeSession 自增首帧 ID 为 901，测试断言据实调整
- [x] Task 5 完成：`DirectorShot` 扩展 `prop_names`/`composition` + `_match_prop_assets` + 生成时写 `composition_spec` + 8 项契约单测通过
- [x] Task 6 完成：`redraw` 双模式 + 批量合成接口 + `shot_out` 要素清单 + 11 项契约单测通过 + `import app.main` 正常
- [x] Task 7 完成：`build_input_hash` 纳入首帧 + 管线版本升 `image-driven-v5-20261027` + 4 项哈希单测通过
- [x] Task 8 完成：`frame_qc_service` 五检门禁 + `GET /storyboards/{id}/frames/qc` 路由 + 6 项单测通过
- [x] Task 9 完成：前端 `composeStoryboardFrames`/`getStoryboardFramesQc` + 双模式按钮 + `npm run typecheck` 通过
- [x] Task 10 完成（端到端验收）：
  - 845 整板合成 10/10 成功、0 降级、全部 720×1280、亮度跨度 175–185
  - QC `passed=true`，issues=0（elements 1.0 / duplicates 0 / variety 4 / speaker 1.0 / degraded 0）
  - 渲染 10/10 由合成首帧驱动（`poster_key == first_frame`），`actual_cost=0`
  - 时间线 10/10 指向当前镜头版本（修复后），成片 720×1280 / 34.17s / H.264+AAC 可播放
  - 报告：`测试报告/深夜的暖_分镜整幅画面复测_20261027.md`

## 验收后追加修复（复测中发现）

- 合成器：抠像相似度 0.14→0.40；新增人物宽度上限（主 46%/次 42%/道具 28%）；双人位置 0.34/0.66→0.28/0.72
  - 依据：`测试报告/artifacts/exp/` 对照实验（A_soft 双重曝光 / B_hard 可用 / C_card 贴纸感 / D_sim0.55 抠穿人脸）
- `post_service.compose`：video 轨有效性判定从"数量一致"改为"每个片段 source_id == 镜头当前版本"，修复"重渲染后成片画面不变"

## 既有失败（非本次引入，已在报告中披露）

- `tests/test_v4_features.py` 29 项：`.env` 中 `SANDBOX_MODE=false` 时 `/auth/sms-code` 不返回 `dev_code`
- `tests/test_asset_storyboard.py` 1 项：断言"无锚点锁定应 60005"的前提不成立（生成时已自动绑定锚点）

## 追加任务：「单镜重出」按钮交互修复（2026-10-27）

问题：`StoryboardCanvasView.vue` 的「单镜重出」按钮只调用 `openFocusDrawer()` 打开抽屉，不重出任何画面；无 loading/失败/防重复；后端 `redraw_shot_frame` 不校验锁定态。

改动：
- 前端：新增 `redrawing` 状态、`canRedrawShot`、`redrawHint`、`redrawFocusShot()`；按钮改为直接重出当前焦点镜首帧（compose，0 积分），loading 时转圈 + 文案「重出中…」+ 禁用；成功后就地更新 `media_url` 再 `loadCurrentDetail()` 兜底；失败保留旧画面并复位。
- 后端：`redraw_shot_frame` 入口新增锁定校验（按**分镜板状态**判断，因为锁定后镜头 status 已被改写为 rendered），双保险兼容镜头级 locked。

验证：
- `npm run typecheck` 通过
- `tests/test_shot_frame_contracts.py` **13 passed**（新增 2 项锁定拒绝用例）
- 真实接口：锁定分镜 → `code=40001「分镜已锁定…」`；未锁定分镜 → `code=0`，返回新 `media_url`，`redraft_count` 仍 0（不消耗额度）
- 脚本：`测试报告/scripts/_verify_redraw_button.py`

