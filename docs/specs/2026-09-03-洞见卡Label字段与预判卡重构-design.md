# 2026-09-03 洞见卡 Label 字段与预判卡重构 Design

> 状态：组件库 + agent-py schema/prompt 已改；app-frontend 与服务器同步待做
> 关联：2026-09-02-板块四环前端展示、2026-09-03-动态档位-影响时长分流

## 1. 背景

板块四环洞见卡的"预判部分"视觉混乱（金色软底 + 多色混用），且条件化预判
缺少一眼可读的"路径名"。组长验收稿（ASCII）要求三行结构 + 基准行右对齐。

## 2. 展示结构（洞见整卡 = 外卡 + 两个同构子卡）

```
头部(标签+时间) → 结论一句话
┌ 溯源子卡：冷雾蓝底 + 标题"溯源" + 正文   （无图标）
┌ 预判子卡：浅中性底 + 标题"预判" + [验证 pill]（无图标）
   ├ 期段 seg（短/中/长，选中=浅蓝底蓝字）
   ├ 基准行：<方向徽> 基准 · {horizon.label}   置信{高/中/低}  {remaining}   ← "基准 · xxxx" 不加粗
   ├ 路径①：<方向徽> label(加粗)  → chips 行(keywords) → scenario 行(幅度弱化/触发染方向色)
   ├ 或（分隔线）
   ├ 路径②：同结构
   └ 触发态：命中路径 border+底色染方向色、右上实心徽"条件成立"（四字）；未触发支 opacity .42
```

- 颜色：去金全中性。红=看多、绿=看空、蓝=选中/交互；幅度段灰；pending pill 琥珀、hit pill 实心绿"已验证"。
- 两子卡同构（仅底色区分：溯源=冷雾蓝 #f4f8fe / 预判=浅中性 #f7f8fb）。

## 3. 新增输出字段（agent-py schema，防遗忘）

| 字段 | 位置 | 规则 |
|---|---|---|
| `PredictionHorizon.label` | horizons[] | 基准走势短语，**4~6 字**（如 恐慌出清为主 / 震荡磨底）；基准行"基准 · {label}"；旧记录缺省空 → 前端不渲染中间文本 |
| `PredictionCondition.label` | conditions[] | 路径短语名，固定两段式 **"{市场状态/触发条件} · {触发后走势}"**：`·`前≈4 字(≤6)、`·`后≈4 字(≤6)，总长 ≤15 字（含分隔符）；如 恐慌出清 · 下跌中继、缩量企稳 · 平台修复 |

- 默认值 `""`（旧记录兼容，不 break 校验）。
- prompt 已在 PREDICTION_PROMPT/PREDICTION_CHAT_PROMPT 两版增加 label 生成约束
  （两段各 ≈4 字硬上限 6 字、禁止形容词堆砌/与 keywords/scenario 大段重复；horizon label 禁止整句）。

## 4. 前端渲染规则（ConditionalForecastBlock / InsightCard）

- **tags 模式**且 `label` 存在：行1 = 方向徽 + label（加粗 24rpx）；行2 = keywords chips（量化细节）；行3 = scenario 原文。
- `label` 缺（旧数据）：行1 = 方向徽 + `condMain(condition)` 长句主干（加粗）；不再重复 chips。
- **sentence 模式**（预测详情页）：恒显示长句主干（忽略 label/chips）。
- 基准行：`<方向徽> 基准 · {label}` 不加粗（label 缺省则不渲染文本段）；右侧弱字 置信 + remaining。
- `conditionDisplay` 两模式逻辑见组件（组件库 ConditionalForecastBlock.vue）。

## 5. 组件库 dev 预览修复

- rpx 转换由 vw 改为 **px(0.5)**（vite.config `rpxToPx`）：字号不随视口等比放大，容器 max-width 500px 居中。
- dev 数据含双卡对照（pending/hit + label/chips/met）。
- InsightCard/CFB 类型已镜像 label/horizon.label/keywords。

## 6. 待办（进度）

- [x] 组件库 InsightCard / ConditionalForecastBlock 样式与结构重构（去金全中性 + C 互斥路径）
- [x] agent-py schemas/prediction.py + prompts/workers/prediction.py 加 label 字段
- [ ] app-frontend mirror（InsightCard / CFB / SectorInsight 组装 / prediction-detail sentence）同步 label 与结构样式
- [ ] agent-py 相关测试补 label 断言 + commit + 服务器部署（prediction.py/schema）
- [x] 组件库/前端 commit（组件库 preview 效果组长已确认）

---

## 7. 预判展示改造：只显示已验证结论（2026-09-16 决议）

> 需求来源：2026-09-04 项目周会（王岢）——"不给用户看预判过程，直接给结论；只显示已验证/条件已确认的预判，未验证的不显示。"本小节为该项已完成的技术/范围决议。

### 7.1 总体决策

| 维度 | 决议 |
|---|---|
| 展示形态 | **溯源与预判分区独立显示**；预判只显示**已成立分支**，未满足分支**彻底隐藏、不显示任何提示** |
| 判定口径 | **条件触发即展示**（`met=true` 分支亮起即出结论） |
| 验证语义 | **两步阶段**：① 条件触发→预判结论亮起（状态=待验证）；② 到验证时间→回填 已命中 / 未命中 |
| 期段切换 | 保留三档（短/中/长） |
| 依据详情 | 溯源区保留"依据详情"展开入口（编码前先核实现有组件是否已有该按钮；有则沿用，无则补） |
| 覆盖范围 | 大盘 + 板块 + 个股三粒度统一 |
| **边界（2026-09-16 补）** | 预判卡（CFB）只服务**有预判记录**的板块（风口榜单，对应链式溯源 v3 轨道 A）；市场洞见的**溯源卡不走预判卡形态**（不渲染 CFB），只出溯源内容 |

**本次不纳入**（独立任务）：板块行业/概念范围调整、个股异动阈值 ≥5%、PDF 完整报告下载。

### 7.2 预判展示结构（改造后）

```
溯源子卡（独立区）：
   标题"溯源" + [依据 pill]
   ├ 溯源结论句（事件发酵 + 板块联动，主力资金持续流入）
   ├ 归因 chips（概念联动 · 种植业）
   ├ 事件胶囊（可选；链式溯源的事件节点，点开跳事件原文/时间线）
   └ 依据详情 ▾（展开显示板块自身链 stages：现象 → 触发 → 传导 → 定价；
                  与大盘溯源同源结构，对应总纲 §3.1）

预判子卡（独立区）：
   标题"预判" + [验证标识]
   ├ 期段 seg（短/中/长，选中高亮）
   └ 仅已成立分支：
      <方向徽> label(加粗) + 结论句
      验证锚点 chips（threshold/metric）
   ── 未满足分支：不渲染，无提示 ──
   （条件都未成立 → 预判区显示"条件未成立 · 暂无已验证结论"）
```

**边界（2026-09-16 补）**：无分支级 `met` 数据（后端未回填 `condition_met`）时，结论模式按**当前期段**自动降级为全量渲染，避免假空态；仅当**当前期段**确实含布尔 `met` 数据时才严格只显已成立分支。**已接线调用点**（板块三页 sector-detail / sector-loop / traceability）在后端回填后自然生效，大盘/个股/Web 需另行接开关。

### 7.3 验证标识（两步阶段）

- **待验证**：条件已触发、结论已亮起，但未到验证时间 → 琥珀 `待验证`
- **已命中 / 未命中**：到验证时间回填 → 绿 `已验证` / 灰 `验证未中`

### 7.4 落地涉及

- 组件库 `ConditionalForecastBlock.vue`：加"结论模式/过滤已成立分支 + 两阶段验证标识 + 溯源预判分区"。
- 大盘 / 板块 / 个股页面：同步传入模式开关。
- 复用现有 `InsightCard.structured` 数据结构，不新增 schema 重设计（`conditions[].met`、`verification` 已具备）。
- 溯源子卡的事件胶囊 / 板块链：复用链式溯源（v3 §3/§7）产出；`InsightCard.traceStructured` 做**加性扩展**（新增 `events[]`、可展开 `stages[]`），不改现有字段语义与调用方。

### 7.5 待办（进度）

- [x] 核实结论（2026-09-16）：现有 `InsightCard` / `ConditionalForecastBlock` 均**无溯源"依据详情"入口**（CFB 的「详情 ▾」只作用于预判分支 scenario）→ 需在 `InsightCard` 溯源子卡新增；与链式溯源 v3（`2026-09-03-链式溯源与事件驱动板块预判-design.md`）的 P3' 前端一并落地
- [x] 组件库"结论模式"落地：只渲染 `met===true` 分支 + 两阶段验证标识 + 溯源预判分区（2026-09-16 落地：CFB `displayMode` + `resolveDisplayMode` 无 met 降级、InsightCard 溯源「依据详情」+ `traceStructured.stages` 预留、组件库 dev 预览样例；两副本逐字一致，仅 CFB helper 定义块差异）
- [ ] 大盘 / 板块 / 个股页面接入模式开关 + 跨端（app-frontend / aistock-frontend）同步检查 —— 现状（2026-09-16）：板块已接（sector-detail / sector-loop / traceability）；大盘无分支级 met → 保持 full（T5 取证）；个股与 Web 端未接（另行排期）
- [ ] 后端 prediction_validator 回填 conditions[].met（condition_met 落地）后：大盘 MarketTracePrediction 补 :display-mode="conclusion"；个股/Web 接入另行排期
