# 分镜整幅要素画面（Shot Composition Frame）设计

- 日期：2026-10-27
- 状态：待评审（方案 C：本地合成打底 + 模型精修预留）
- 关联项目：845「深夜的暖」
- 关联文档：`测试报告/深夜的暖_全链路测试报告_20261027.md`

---

## 1. 背景与审核结论

### 1.1 问题陈述

当前分镜生成只产出"要素绑定 + 一段长 prompt"，画面由渲染层**从三张资产图中择一**充当
（`executor._resolve_shot_image_key`），`final_prompt` 在出图环节完全不参与。
结果是：数据层要素齐全，表达层退化为"单资产图 + 慢推"的动态 PPT。

项目 845 十镜实测画面来源：

| 镜号 | 画面来源 | 叙事问题 |
| --- | --- | --- |
| 1、2 | 同一张街道场景图 | 两张同图，无空间推进 |
| 3、8 | 便利店场景图 | 主角林晚不在画面 |
| 4、6、9 | 同一张林晚白墙定妆照 | 无场景、无道具，三镜同图 |
| 5、7 | 同一张陈姨白墙定妆照 | 无场景、无道具 |
| 10 | 纸条道具图 | AI 生成乱码文字 + 水印 |

**0/10 镜同时包含"角色 + 场景 + 道具"**。

### 1.2 导演视角审核标准与 845 得分

| # | 标准 | 判断依据 | 合格线 | 845 得分 |
| --- | --- | --- | --- | --- |
| S1 | 叙事连贯性 | 相邻镜能否回答"谁/在哪/做什么/为什么"；轴线与动作方向延续 | ≥9/10，无相邻同图 | 4/10 ✗ |
| S2 | 要素关联一致性 | 每镜具备「主体角色 + 场景基底 + 叙事道具」，且存在接触/视线/朝向关系 | 要素齐备率 ≥90% | 0/10 ✗ |
| S3 | 画面与情节匹配度 | 画面主体 = 该镜台词的叙事主体；景别服务情绪 | ≥9/10 | 5/10 ✗ |
| S4 | 镜头语言规范 | 景别递进与变化、单镜运动合理、无连续静态 | 景别 ≥3 种，连续静态 ≤2 镜 | 3/10 ✗ |
| S5 | 成片完整性 | 时长/音画/字幕/可播放/无黑帧/无水印乱码 | 全部通过 | 通过（镜 10 乱码除外） |

结论：**技术链路通过，表达链路不合格**。

### 1.3 根因

1. **缺构图层**：分镜与画面之间没有 `composition` 环节，要素绑定无法落到像素。
2. **渲染择一语义**：`_resolve_shot_image_key` 在"角色图 / 场景图 / 道具图"中二选一，
   结构上最多只能出现一个要素。
3. **QC 与被审对象无因果**：一致性校验用 `task_id:round` 种子构造伪向量，
   不校验要素是否出现，产生假阳性。

---

## 2. 目标与非目标

### 2.1 目标

1. 每镜产出一张 **720×1280（9:16）整幅要素帧**：场景为基底，角色按景别比例与站位置入，
   叙事道具置入并归属到具体角色，形成可读的空间关系。
2. 该帧写入 `shots.first_frame_media_id`，渲染层**自动以它驱动** stub 视频，
   换帧即换视频（不新增渲染分支）。
3. 增加**导演 QC 门禁**：要素齐备率、相邻重复帧、景别多样性、台词主体匹配，
   不达标即阻断并给出补镜建议。
4. 第一版 **0 成本**（本地 ffmpeg），同时在接口层预留"模型精修"适配器。

### 2.2 非目标

- 不做 3D 透视校正与人脸重绘（不改变角色长相，仅复用定妆资产）。
- 不做画面内文字渲染（规避 AI 乱码与水印风险）。
- 不修改时间线、配音、字幕、导出链路。
- 不替换 `composition_service`（该服务是编排图/渲染计划，语义完全不同，禁止混用命名）。

---

## 3. 架构

### 3.1 新增单元（职责单一、可独立测试）

| 单元 | 文件 | 职责 | 依赖 |
| --- | --- | --- | --- |
| `CompositionSpec` 契约 | `app/services/shot_frame_spec.py` | Pydantic 模型 + `derive_composition_spec()` 纯函数：由镜头字段与资产推导构图 | 无（纯函数） |
| `frame_composer` | `app/services/frame_composer.py` | `compose_frame()`：把 spec + 资产图渲染为 PNG 字节；`LocalFrameComposer` 与预留 `ModelFrameComposer` | ffmpeg、LocalStorage |
| `shot_frame_service` | `app/services/shot_frame_service.py` | 编排：取资产 → 推导 spec → 调合成器 → 写 MediaFile → 绑定首帧 → 记录操作 | 上述两者、DB |
| `frame_qc_service` | `app/services/frame_qc_service.py` | `evaluate_storyboard_frames()`：四项门禁与建议 | DB、spec |

### 3.2 改造点

| 位置 | 改动 |
| --- | --- |
| `storyboard_service.DirectorShot` | 新增可选字段 `prop_names` / `composition`，并扩展系统提示词输出契约 |
| `storyboard_service._run_storyboard_generation` | 生成时调用 `derive_composition_spec` 写入 `shots.composition_spec`；道具优先用 LLM 输出，文本匹配降级为兜底 |
| `storyboard_service.redraw_shot_frame` | 新增 `mode="compose"|"generate"`，默认 compose 走 `shot_frame_service` |
| `storyboard_service.shot_out` | 输出 `composition_spec` 与 `frame_elements`（要素名 + 是否置入） |
| `render_service.build_input_hash` | 新增 `frame_media_id` 参与摘要；`RENDER_PIPELINE_VERSION` 升为 `image-driven-v5-20261027` |
| `StoryboardV4Panel.vue` | 镜头卡显示要素徽标与首帧缩略图；「重出」拆为「重出整幅画面」「纯生成」；顶部批量合成按钮 |

### 3.3 分层理由

- **spec 推导是纯函数**：不碰 IO，可用单测覆盖全部景别/角色数/道具组合，是质量的第一道闸。
- **合成器是接口**：`LocalFrameComposer` 与 `ModelFrameComposer` 同签名，
  切换只改配置，不动调用方——这是"预留 B"的落地方式，而非注释承诺。
- **service 只做编排**：事务、落库、操作记录集中一处，便于回滚与审计。

---

## 4. 数据模型

### 4.1 `shots.composition_spec`（新增 JSONB，可空）

迁移：`backend/alembic/versions/f2a3b4c5d6e7_shot_composition_spec.py`，
`down_revision = "e1f2a3b4c5d6"`（当前链头）。

```jsonc
{
  "version": 1,                       // 结构版本，便于后续演进
  "aspect_ratio": "9:16",             // 画幅
  "canvas": {"w": 720, "h": 1280},    // 输出尺寸
  "setting": {"asset_id": 501, "name": "便利店夜内景", "crop": "center"},
  "shot_size": "medium",              // 与 shots.shot_size 对齐
  "camera": {"angle": "eye_level", "move": "static"},
  "subjects": [                       // 置入的角色，按从远到近排序
    {
      "asset_id": 311,                // 角色资产 ID
      "name": "林晚",
      "source": "portrait",           // portrait=定妆照
      "height_ratio": 0.65,           // 屏幕高度占比
      "anchor": "bottom",             // 底部对齐地面
      "x": 0.34,                      // 中心点横坐标（0~1）
      "facing": "right",              // left/right/camera/away
      "depth": "mid"                  // near/mid/far，决定图层与虚化
    }
  ],
  "props": [
    {
      "asset_id": 402,
      "name": "手写纸条",
      "owner": "林晚",                // 归属角色；空表示独立静物
      "height_ratio": 0.18,
      "x": 0.62,
      "y": 0.78,
      "depth": "near"
    }
  ],
  "focus": "character",               // character/prop/scene，决定视觉重心
  "safe_area": {"top": 0.08, "bottom": 0.18},  // 字幕安全区（仅避让，不渲染文字）
  "source": "derived"                 // derived/llm/manual，记录 spec 来源
}
```

### 4.2 契约扩展（`DirectorShot`）

```python
class DirectorShot(BaseModel):
    """LLM 导演输出的单镜头契约（本设计新增道具与构图提示）。

    Attributes:
        prop_names: 该镜出现的道具名（须为项目资产名），默认空数组。
        composition: 构图提示（可选）；缺省时由 derive_composition_spec 规则推导。
    """

    # ... 既有字段保持不变 ...
    prop_names: list[str] = []          # 参数注释：道具名列表，用于精确绑定
    composition: dict[str, Any] | None = None  # 参数注释：可选构图提示，含 subjects/props 位置
```

提示词输出契约追加（仅扩字段，不改既有键，保证向后兼容）：

```text
"prop_names":["手写纸条"],"composition":{"focus":"character",
"subjects":[{"name":"林晚","x":0.34,"facing":"right"}],
"props":[{"name":"手写纸条","owner":"林晚"}]}
```

### 4.3 为什么位置参数由规则推导而非全靠 LLM

LLM 输出的坐标不稳定且无法校验；规则推导具备确定性、可单测、可复现。
LLM 提示只作为**可选覆盖**（`source="llm"`），缺省与非法输入一律回落规则推导
（`source="derived"`），保证任何情况下 spec 都合法。

---

## 5. 合成算法（`LocalFrameComposer`）

### 5.1 滤镜链（单次 ffmpeg filter_complex，输出单帧 PNG）

```text
[0:v] scale=720:1280:force_original_aspect_ratio=increase, crop=720:1280,
      eq=saturation=0.92:brightness=-0.015, gblur=sigma=1.2 [bg]      // 场景基底 + 轻虚化
[1:v] colorkey=0xFFFFFF:0.14:0.06, scale=-1:{H}, format=rgba [fg1]   // 角色抠白底
[2:v] colorkey=0xFFFFFF:0.14:0.06, scale=-1:{h}, format=rgba [fg2]   // 道具抠白底
[bg][fg1] overlay=x={X1}:y={Y1} [t1]
[t1][fg2] overlay=x={X2}:y={Y2} [t2]
[t2] vignette=PI/5, format=rgb24 [out]
```

- `{H}` = `canvas.h * height_ratio`，`{X}` = `canvas.w * x - fg_w/2`，
  `{Y}` = `canvas.h * (1 - safe_bottom)` − `fg_h`（底部落地对齐）。
- 图层顺序：场景 → 远角色 → 近角色 → 道具（`depth` 决定先后）。
- 背景虚化只作用于基底，角色保持锐利，形成浅景深观感。
- 输出走 `storage.save_bytes(f"frames/{project_id}/{uuid}.png", png)`。

### 5.2 比例与透视的近似策略

不做 3D 校正，用三条规则保证"看起来对"：

1. **景别 → 身高比**：`extreme_close_up 1.10 / close_up 0.95 / medium 0.65 /
   over_shoulder 0.80 / wide 0.38 / extreme_wide 0.22`（超出 1 时裁切上半身，符合特写直觉）。
2. **底部对齐**：所有角色按脚底落于 `h * (1 - safe_bottom)`，避免"漂浮"。
3. **双人对望**：两名角色分列 `x=0.32 / 0.68`，`facing` 互为对方；
   单人 `x=0.50` 且 `facing="camera"`；说话人优先居中（形成"谁说话给谁"）。

### 5.3 兜底矩阵

| 缺失情形 | 处理 |
| --- | --- |
| 无场景资产 | 生成深灰渐变底（`gradients`），并标记 `degraded=no_setting` |
| 无角色资产 | 只出场景 + 道具，标记 `degraded=no_subject` |
| 抠像后前景覆盖率 < 5%（非白底） | 降级为"圆角卡片置入"（带 8px 阴影描边），标记 `degraded=card_layout` |
| 道具无归属角色 | 置于右下 0.86/0.90（羽化边静物图，无相框），标记 `degraded=orphan_prop` |
| ffmpeg 不可用 | 抛 `BizError(MODEL_UNAVAILABLE)`，由调用方回落 `text_to_image` |
| spec 全部为空 | 回退既有 `text_to_image` 通道（`mode="generate"`） |

**降级必须显式记录**，禁止静默产出，否则又回到"看起来生成成功、实际不达标"的老路。

---

## 6. 数据流

```text
剧本行
  └─ 分镜生成（LLM）→ DirectorShot{description, character_names, scene_name, prop_names}
       └─ 资产匹配 → character_asset_ids / scene_asset_id / prop_asset_ids
            └─ derive_composition_spec() → shots.composition_spec
                 └─ compose_frame() → PNG → MediaFile → shots.first_frame_media_id
                      └─ 渲染（stub-video）→ image_key = 首帧 → ffmpeg 图片驱动 MP4
                           └─ 四轨时间线 → compose → 导出
```

关键点：**渲染主流程不改**。`_resolve_shot_image_key` 已实现"首帧优先"，
合成帧写入 `first_frame_media_id` 后自动生效。

---

## 7. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PATCH | `/shots/{shot_id}/frame/redraw` | 增加 query `mode`（`compose` / `generate`），默认 compose；compose 不消耗重出次数（本地 0 成本），generate 沿用 ≤3 次限制 |
| POST | `/storyboards/{storyboard_id}/frames/compose` | 批量合成整板首帧，body `{shot_ids?: number[]}`，返回逐镜 `{shot_id, status, media_url, degraded[]}` |
| GET | `/storyboards/{storyboard_id}/frames/qc` | 返回四项门禁与逐镜明细 |

响应中 `frame_elements` 结构：

```jsonc
{
  "setting": {"name": "便利店夜内景", "present": true},
  "subjects": [{"name": "林晚", "present": true}],
  "props": [{"name": "手写纸条", "present": true}],
  "degraded": [],
  "spec_source": "derived"
}
```

---

## 8. 错误处理

| 场景 | 错误码 | 行为 |
| --- | --- | --- |
| 镜头不属于该项目 | 10002 | 拒绝 |
| 项目无任何资产图 | 10002 | 返回"无可用素材，请先上传资产" |
| ffmpeg 执行失败 | 80003 | 记录 stderr 前 500 字，标记任务失败，不写首帧 |
| 批量中部分失败 | — | 逐镜返回状态，整体 200，前端高亮失败镜 |
| 合成期间镜头被锁 | 40001 | 拒绝并提示先解锁 |

---

## 9. 导演 QC 门禁（`frame_qc_service`）

| 检查项 | 判定 | 阈值 | 阻断 |
| --- | --- | --- | --- |
| 要素齐备率 | 具备 setting + 至少 1 subject + 应有道具（`focus="prop"` 或有 `prop_names`） | ≥90% | 是 |
| 相邻重复帧 | 相邻镜 `first_frame_media_id` 相同，或来源资产图相同 | 0 | 是 |
| 景别多样性 | 整板 `shot_size` 去重数 | ≥3 | 是 |
| 台词主体匹配 | 每镜说话人 ∈ `subjects`；无对白镜不检查 | ≥90% | 警告 |
| 降级占比 | `degraded` 非空镜头占比 | ≤20% | 警告 |

未通过时返回逐镜问题与建议（如"镜 3 缺林晚 → 建议将镜 3 的 character_names 补入林晚并重新合成"）。

---

## 10. 测试策略

### 10.1 单元测试（纯函数，无 IO）

- `derive_composition_spec`：6 种景别 × 单/双/无角色 × 有/无道具 的坐标与比例断言。
- 非法 spec 归一化：越界 x、负数 height_ratio、未知 facing 全部回落合法值。
- `frame_qc_service`：构造 4 类不合格数据，断言问题码与阻断结论。

### 10.2 集成测试（真实 ffmpeg，本地临时目录）

- 用 3 张测试图（白底人像、场景、白底道具）合成 720×1280 PNG，断言：
  尺寸、非纯色（方差阈值）、前景区域像素差 > 背景，验证图层确实叠加。
- 抠像失败场景（非白底输入）断言降级为 `card_layout` 而非崩溃。

### 10.3 端到端（项目 845）

1. 批量合成 10 镜首帧 → 逐镜检查 S2/S3。
2. 重渲染（`image-driven-v5`）→ 四轨合成 → 导出 720p。
3. 输出前后对比帧与 S1–S5 评分表。

---

## 11. 验收标准

| 编号 | 验收项 | 通过条件 |
| --- | --- | --- |
| AC-1 | 整幅要素帧 | 10 镜全部产出 720×1280 PNG，且每镜含 setting + subject（有角色镜） |
| AC-2 | 要素齐备率 | ≥9/10 镜同时含角色与场景；含道具镜道具可见 |
| AC-3 | 相邻重复帧 | 0（镜 1/2、镜 4/6/9 不再同图） |
| AC-4 | 渲染联动 | 换帧后重渲染产物 `image_driven=true` 且画面含置入要素 |
| AC-5 | 成片可播放 | 导出 MP4 可播放、音画同步、字幕覆盖 |
| AC-6 | 成本 | 合成环节 0 积分；整链新增成本 = 0 |
| AC-7 | 降级可见 | 任一降级在接口与前端均可见，无静默降级 |

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 定妆照背景非纯白，抠像残缺 | 人物边缘毛刺 | 相似度可调 + 覆盖率检测 + `card_layout` 降级；必要时先跑资产"白底化" |
| 人物比例与场景透视不匹配 | 拼贴感 | 景别身高比 + 底部落地 + 背景虚化；关键镜可用 B 精修 |
| 道具归属错误 | 叙事误导 | `prop_names` 由 LLM 显式输出；无 owner 时置右下并标记 |
| 批量合成耗时 | 10 镜约 10–30 秒 | 后台任务 + 进度返回；单镜合成 < 2s |
| 幂等复用旧视频 | 换帧不生效 | `build_input_hash` 加入 `frame_media_id` + 管线版本升 v5 |
| 与 `composition_service` 命名冲突 | 维护混乱 | 新模块统一 `shot_frame_*` / `frame_*` 前缀 |

---

## 13. 分批实施计划

| 批次 | 内容 | 可独立验证 |
| --- | --- | --- |
| B1 | `shot_frame_spec.py`（契约 + 推导）与单测 | 纯函数单测通过 |
| B2 | `frame_composer.py`（ffmpeg 合成）与集成测试 | 产出正确尺寸与叠加的 PNG |
| B3 | 迁移 + `shot_frame_service` + `redraw`/批量接口 + `shot_out` 扩展 | 845 单镜可出整幅帧 |
| B4 | `build_input_hash` + 管线版本 + 渲染联动验证 | 换帧后视频画面变化 |
| B5 | `frame_qc_service` + 前端要素徽标/批量按钮 | 门禁可阻断、前端可见 |
| B6 | 845 全量复测 + 前后对比报告 | AC-1~AC-7 全部通过 |

---

## 14. 待确认事项（评审时请一并确认）

1. 合成帧的**视觉取向**：写实拼贴（默认）还是"分镜排版"（带要素标签与画框）？
   默认写实拼贴，理由：成片要过观众关，排版感会暴露"PPT 分镜"。
2. `generate` 通道是否保留？默认保留（作为 B 精修与人工兜底入口）。
3. 道具是否允许"无归属角色"独立出现？默认允许但标记 `orphan_prop`。
