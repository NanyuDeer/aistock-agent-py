# Tasks：功能链路说明页 v2（真实节点版）

## [x] T1 重写 HTML 骨架与阅读说明

- 文件：`docs/智慧大脑-功能对照与链路说明.html`
- 动作：
  - 重写页面头部：标题、v2、设计图 v1.3 / 契约 v0.5 / 代码核对 2026-11-22。
  - 新增 `01 怎么读`：统一骨架条、边颜色图例、可折叠节点字典（47 节点）、五功能矩阵表。
  - 保留 sticky TOC，更新锚点。
- 验收：
  - 节点字典包含 T1-T4、O1、P1-P4、R1-R6、J1-J4、C1-C3、E1-E4、U1-U5、M1-M6、TOOL。
  - 矩阵表五行：首页 / 对话 / 个股 / 选股 / 提醒。

## [x] T2 实现 SVG 渲染器

- 文件：`docs/智慧大脑-功能对照与链路说明.html`
- 动作：
  - 内联 JS：`LANES`、`NODE`、`ORDER`、`SPECS`、`renderGraph()`。
  - 固定 7 条泳道 + GM 记忆带 + GX 工具带；`viewBox 0 0 1420 568`。
  - 边类型：`main / orth / side-up / side-down / side-bi / jump / cal / mem / memread / memlong`。
  - 节点右上角记忆徽标（1 / 1·2 / 1·2·5）。
  - 隐藏 SVG 定义箭头 marker（蓝 / 青 / 紫 / 灰）。
- 验收：
  - 任意一张图渲染出泳道、节点、边、M 框、GX 框。
  - 未参与节点不出现。
  - 五张图泳道和 M 槽坐标一致。

## [x] T3 写首页 · 早点听

- 文件：`docs/智慧大脑-功能对照与链路说明.html`
- 动作：
  - 代码核实框：`MorningContent.vue` 四宫格四接口 + `getBriefing(pre/mid/post)`。
  - 改前 / 改后手机 mockup：四宫格 + 今日专属播报；功能不变，只改为同源。
  - 图：`T1/T3→O1→P1/P2/P3→M1→R2/R3/R5/R6→M2→J1-J4→M3→E1→U1/U4`。
  - 步骤 + delta。
- 验收：
  - 风口洞见 = `getWindLeaders(20)` 取前 3；消息洞见 = `getEventList`；市场洞见 = `getMarketTraceReview`；节奏洞见 = `getRhythmMasterCalendar`。
  - 图中出现 R2/R3/R5/R6，不出现 R1/R4/R6 以外的无关研究节点。

## [x] T4 写 AI 对话

- 文件：`docs/智慧大脑-功能对照与链路说明.html`
- 动作：
  - 代码核实框：`pages-sub-app/chat/index.vue` 原有问答 / 卡片 / 推理 / 深度分析 / 追问；当前 `supervisor→intent_router` 10 个可路由 worker，一次一问只走一个。
  - 改前 / 改后手机 mockup：保留对话形态，新增“引用 judgment / evidence + 失效条件”。
  - 图：`T3→O1→P1-P4→M1→R1-R6→M2→J1-J4→M3→E4→U5`，`M5→R4`。
  - 明确“科普 / 纯概念可只走 E4 轻档”。
- 验收：
  - 不出现“固定四段式回答”的旧写法。
  - 图中出现 P4、M5。

## [x] T5 写个股详情

- 文件：`docs/智慧大脑-功能对照与链路说明.html`
- 动作：
  - 代码核实框：`detail.vue` 多接口 + `overallDecision` / `decisionInsight` 前端 computed + `getStockAnalysis` + 事件展示。
  - 改前 / 改后手机 mockup：综合洞见从“前端规则拼接”变为“唯一 judgment + 证据附件”。
  - 图：`T3→O1→P1/P2/P3→M1→R1/R2/R3/R5/R4→M2→J1-J4→M3→E2→U2`，`M5→R4`。
- 验收：
  - 明确写出“综合洞见不是后端接口，是前端 computed”。
  - 不出现“三个接口各下结论”的旧写法。

## [x] T6 写选股

- 文件：`docs/智慧大脑-功能对照与链路说明.html`
- 动作：
  - 代码核实框：四个 Tab 的真实 API / 排序 / 字段（AI 帮我选、趋势评分榜、热门爆发共振、盈利预测 + 业绩报告）。
  - 改前 / 改后手机 mockup：四个 Tab 保留，理由改为同一套 claim 口径。
  - 图：`T3→O1→P1/P2/P3→M1→R1/R2/R5/R4→M2→J1-J4→M3`；GO 画虚线缺口框 `U? 选股页（目标图未定义）`。
  - 明确缺口：设计图没有选股出口，不能硬套 U1。
- 验收：
  - 机构热门写 `getHotBurstHistory` 近 3 日共振；趋势写 `trendScoreApi.getTop(30)`；业绩预测写双视图。
  - 图中不出现 E/U 节点（缺口框除外）。

## [x] T7 写提醒

- 文件：`docs/智慧大脑-功能对照与链路说明.html`
- 动作：
  - 代码核实框：`AlertContent.vue` 个股情报（`getFavoritesNews` / `getTrendEvents` + `ai_impact` 三档 + 过滤中性）+ 自选股洞察（`stockTraceApi.list` + 低置信过滤 + 同日同股聚合 + 主因三段式）。
  - 改前 / 改后手机 mockup：两个模块保留；情报加“是否改变结论”；洞察主因分层。
  - 图：`T1/T2/T3→O1→P1/P2→M1→R2/R3/R5→M2→J1-J4→M3→E3→U4`。
  - 明确：页面读 M3 / M1 投影；E3 只推变化 / 失效 / 高置信机会。
- 验收：
  - 不出现“逐条新闻推送”的旧写法。
  - 明确 `primary_cause` 三段式保留。

## [x] T8 一天传导与修正清单

- 文件：`docs/智慧大脑-功能对照与链路说明.html`
- 动作：
  - `07 一天传导`：T1 盘前 → T2 盘中 → T3 页面 / 对话 → T4 次日验证，全部引用真实节点。
  - `08 修正与缺口`：列 2.2 的 9 条修正 + 选股出口缺口 + 提醒列表 vs E3 推送。
- 验收：
  - 时间线每个节点都能在五张图里找到。
  - 修正清单与 spec 2.2 一致。

## [x] T9 验证与记录

- 文件：
  - `docs/智慧大脑-功能对照与链路说明.html`
  - `changelog-pending.md`
- 动作：
  - 标签闭合检查：`section` / `svg` / `div` / `text` 配对。
  - 本地 `python -m http.server 8767 --bind 127.0.0.1`（cwd `docs`）。
  - 浏览器验收：5 张图渲染、TOC 跳转、无文字重叠、SVG 可横向滚动。
  - 验收后停止服务。
  - `changelog-pending.md` 追加 v2 修正记录。
- 验收：
  - 浏览器截图 / 文本检查 PASS。
  - changelog 记录“真实节点图 + 9 条描述修正 + 2 个缺口”。
