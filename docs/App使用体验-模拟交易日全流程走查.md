# 洞见 App 使用体验走查 —— 模拟一个完整交易日

> **文档目的**：以「一个交易日从盘前到盘后」为主线，逐场景模拟用户操作，核对 App 前端**是否提供解决方案**、**哪里用得不顺**、**哪里不够准确**，并给出分级整改建议。
>
> **评估视角**：**仅前端（aistock-app-frontend）**的页面、交互与数据时效；后端能力只在必要时作为参照，不做后端实现评估。
>
> **方法**：静态代码走查（页面 / 组件 / API / 交易时间工具），每条结论附 `文件:行号` 证据。未运行真机，故「运行时接口是否真的返回数据」不在此报告结论范围内。
>
> **时间线假设**：一个正常交易日（周一~周五、非节假日）。盘前 08:00–09:15 → 集合竞价 09:15–09:25 → 早盘 09:30–11:30 → 午间 11:30–13:00 → 午后 13:00–15:00 → 盘后 15:00 起 → 夜间 20:30 后。
>
> **更新记录（2026-10-11）**：首轮走查发现的问题中，**已修复的部分已从本文件删除**（交易日历对齐、自选/选股/提醒盘中刷新、立即检测、H5 异动实时、关注事件假成功、文案与日期口径、死代码与写死值、提醒 Tab 双实现等；commit `12b286a7`）。本文件当前**只保留仍未解决的问题**（P0-1 / P0-4 / P1-2 / P1-6 / P2-9）。

---

## 1. 一页速览（当前遗留结论）

| 维度 | 结论 |
|------|------|
| **能解决的场景** | 盘前资讯（晨报/风口/事件/恐贪/节奏/选股）、午间报、盘中自选与个股行情刷新、盘后复盘（晚报/大盘归因/板块四环/预测验证/业绩报告）、AI 问答（流式） |
| **遗留缺口** | **「AI 帮我选」是写死假数据且无演示标识（还是选股 Tab 默认项）**；**集合竞价（09:15–09:25）整段无内容**；**个股详情无分时主图、无盘口五档**；**持仓陪伴 / 交易复盘 / 动态估值 三个页面为空占位** |
| **遗留体感问题** | 底部双层导航（3 Tab + 全局对话栏）拥挤；09:30–12:00 首页仍是"晨报"语境，无盘中内容位；个股详情看分时仍需去自选列表 |

> **一句话结论**：App 的定位是「**盘前读懂 + 盘中盯自选 + 盘后复盘 + 随时提问**」的辅助决策工具（非交易终端）。**当前最影响信任的是选股默认入口的"AI 帮我选"是假数据**；**盘中体验的最大短板是个股详情没有分时图与盘口**。

---

## 2. 走查前提：这个 App 是什么 / 不是什么

**信息架构（前端）**

- 底部 3 个 Tab：**早点听 / 选股 / 提醒**（`src/shared/components/MainTabs.vue:87-94`），各自内容为 `MorningContent / StockContent / AlertContent`。
- 底部**还有一条常驻的全局对话栏**：`报告 | AI 对话胶囊 | 自选`（`src/shared/components/GlobalChatBar.vue:6-55`）。
- **全 App 没有任何下单 / 成交 / 持仓录入能力**——「持仓陪伴 / 交易复盘 / 动态估值」三个页面是空占位（见 §4 P0-4）。

**由此可直接推断**：所有「交易」相关场景，App 的定位都是**信息与解读**，而非执行。走查时应按"辅助决策工具"而非"交易终端"评估。

---

## 3. 全天时间轴走查（仅列仍有问题的环节）

每节结构：**用户此刻想干什么 → App 现有方案（含证据）→ 判定 / 遗留问题**。

### 3.1 盘前 08:00–09:15

盘前是覆盖最完整的时间段：晨报（`MorningContent.vue:5-62`）、风口洞见（`MorningContent.vue:109-131`）、消息洞见（`MorningContent.vue:90-107`）、未来事件时间线（`chat/pages/event/timeline.vue:134-142`）、恐贪指数（`MainTabs.vue:25`）、节奏洞见（`MorningContent.vue:133-153`）、选股 Tab（`StockContent.vue:32-33`）**都在首页或一级 Tab 可达**。**判定：✅ 覆盖完整。**

**遗留问题**
- 🔴 **「AI 帮我选」是硬编码假数据**（P0-1）：`AiPickContent.vue:3,18-24` 写死 5 只股票，**无任何 API、也没有"演示数据"角标**；它又是「选股」Tab 的默认项（`StockContent.vue:33`），用户第一眼就会当成 AI 真实推荐。
- 🟡 **09:30–12:00 首页仍是"晨报"语境**：`shared/utils/useBriefingCard.ts:39-54` 的切换规则是「15:30 后→晚报、12:00–15:30→午间报、其余→晨报」，上午盘中首页没有"盘中"专属内容位。

### 3.2 集合竞价 09:15–09:25 —— **整段缺失**（P1-6）

- 前端**有**判定函数：`isCallAuction()`（09:15–09:25）与 `getMarketStatus()`（返回「集合竞价」，`src/shared/utils/tradingTime.ts:85-102`）。其中 `getMarketStatus` 只被用作**一句状态文案**（自选股异动页 `favorites/pages/monitor.vue:19` 的"市场状态"、市场 store `shared/store/modules/market.ts:10,17`），`isCallAuction` 仅被 `getMarketStatus` 内部调用——**没有任何页面渲染"竞价"专属内容**（无竞价异动、无竞价高开/低开榜、无自选股竞价表现），首页/自选列表此刻也不会提示"正在集合竞价"。

**判定：❌ 仍缺失。** 竞价是 A 股短线用户最关注的 10 分钟。

### 3.3 早盘 09:30–11:30

| 用户需求 | App 现有方案 | 证据 | 判定 |
|---|---|---|---|
| 看自选股涨跌幅并自动刷新 | 交易时段内每 60s 原地刷新行情（默认视图即可，无需开分时模式） | `modules/favorites/pages/favorites.vue`（`syncQuoteRefresh`） | ✅ |
| 看自选股盘中分时走势 | **仍需手动开启分时模式**（表头"三横线"），开启后每 60s 轮询分时 | `favorites.vue:291-299, 341-351` | 🟡 门槛仍在 |
| 看单只个股实时行情 | 个股详情页 **60s 轮询**（最新价/涨跌/量比/换手/涨跌停价…） | `favorites/pages/detail.vue:2125-2142` | ✅ |
| **看个股分时图** | **详情页 K 线只有 日/周/月，无分时**（分时仅出现在自选列表 / 多股同列的 MiniKLine） | `detail.vue:919`；`KLineChart.vue:139-143` | 🔴 缺口 |
| **看盘口/五档** | **无盘口**（只有涨停/跌停/均价内联展示） | `detail.vue:47-53` | 🔴 缺口 |
| 盯异动 | App 端 WebSocket 实时；H5/小程序盘中 60s 轮询替代 | `favorites/pages/monitor.vue` | ✅ |
| 让 AI 解读某股异动 | AI 异动解读页：**SSE 流式**，进页触发 + 当日缓存 | `market/pages/alert-analysis.vue:247-255`；`market/utils/useAlertSSE.ts:141, 174-196` | ✅（按需） |
| 盘中看板块/大盘 | 风口龙头/板块详情/板块四环均为**进页拉一次、无轮询**；板块四环明确"20:30 前主因未产出" | `leaders.vue:676-680`；`sector-loop.vue:65, 389` | 🟡 盘中无当日数据 |

**判定：🟡 盘中"盯自选"已可用，但"看个股踩点"仍弱。**

**遗留问题**
- 🔴 **个股详情无分时主图、无盘口五档**（P1-2）：盘中想判断"这只票现在是拉升还是回落"，详情页给不出分时走势与买卖盘口，只能退回自选列表看 MiniKLine。
- 🟡 **分时图仍需手动开启**：默认列表已自动刷新数字，但"看分时走势"仍要用户先切到分时模式（多一步操作）。
- 🟡 自选页顶部"平均涨幅 / 加自选后"为简单算术均值口径（`favorites.vue`），未说明是否按仓位加权，容易被误读为组合收益。
- 🟡 板块/大盘类页面盘中无当日数据（依赖盘后产出，见 3.6），盘中想看"今日板块强弱"只能靠风口的涨跌幅标签。

### 3.4 午间休市 11:30–13:00

**午间报**（`briefing/index.vue:35-130`；`shared/utils/middayReport.ts`）：summary + 分段结论 + 「机会/风险」双栏对位 + 风险提示，音频可选。**判定：✅ 满足，是 App 独有亮点。**

**遗留问题**
- 🟡 午间报音频为**可选**（后端有才渲染音频条），无音频时只出文字——用户无法预期"是否有得听"。

### 3.5 午后 13:00–15:00 —— 同 §3.3

- 与早盘同源：个股详情 60s 轮询 + 自选列表 60s 轮询（默认模式即可，分时模式为手动开启）；**无盘口、无分时主图、无盘中异动排行**（同 P1-2）。
- 尾盘（14:30–15:00）**没有针对性功能**（无尾盘异动/尾盘资金/打板相关），`getTradingTimeSlot` 也无法区分尾盘（只有 pre/intraday/post 三档）。

**判定：🟡 与早盘同源缺口，尾盘为空档。**

### 3.6 盘后 15:00 起

"看市场"一侧很完整：晚报（`useBriefingCard.ts:43-45`；`briefing/index.vue:133-138, 207-220`）、大盘归因链（`analytics/pages/traceability.vue:37-80`）、板块四环/板块详情、预测验证（`analytics/pages/prediction-history.vue:12-90`）、业绩报告/财报详情、节奏大师（`rhythm/pages/index.vue:62-72`）。**判定：✅。**

**遗留问题**
- 🔴 **盘后最刚需的"我的交易复盘 / 持仓"三个页面都是空壳**（P0-4）：`pages-sub-app/review/index.vue`、`portfolio/index.vue`、`valuation/index.vue` 均只有"页面开发中..."，且路由与导航可达（`pages.json:423,431,439`）——**点击即失望**；"看自己"完全缺失。
- 🟡 市场洞见页有 **60s 前台常驻轮询**（`traceability.vue:255-262`）用于自动跨 15:30/20:30 切日，对耗电不友好。

### 3.7 夜间（20:30 后）/ 非交易日

- 板块四环明确"**收盘后数据更全（20:30 前主因未产出属正常）**"（`sector-loop.vue:65`）；市场洞见 20:30 后落预判。**判定：✅ 有语义提示。**
- 非交易日降级：早点听**有**向前回退（最多 7 个自然日）并横幅标注（`briefing/index.vue:11-15, 356, 714-741`）——**✅**；其余模块基本**没有非交易日降级**（交易日判定已改为以后端日历为准，但页面内容本身不做回退）。

---

## 4. 遗留问题清单（分级 + 证据）

### P0 —— 影响信任 / 正确性

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| P0-1 | **「AI 帮我选」为写死假数据且无演示标识，还是选股 Tab 默认项** | `AiPickContent.vue:3,18-24`；`StockContent.vue:32-33` | 用户把假推荐当真，直接损害产品信任 |
| P0-4 | **持仓陪伴 / 交易复盘 / 动态估值三页为空占位**且导航可达 | `pages-sub-app/portfolio/index.vue`、`review/index.vue`、`valuation/index.vue`；`pages.json:423,431,439`（另：`agent.ts` 的 `getValuation` 后端亦未实现） | 盘后"看自己"全缺，点击即失望 |

### P1 —— 盘中体验 / 功能可用性

| # | 问题 | 证据 |
|---|---|---|
| P1-2 | 个股详情**无分时主图、无盘口五档** | `favorites/pages/detail.vue:919`、`shared/components/KLineChart.vue:139-143`、`detail.vue:47-53` |
| P1-6 | 集合竞价（09:15–09:25）无任何专属内容位 | `shared/utils/tradingTime.ts:85-102`（`isCallAuction` 仅被 `getMarketStatus` 内部调用；后者只作状态文案，见 `monitor.vue:19`） |

### P2 —— 一致性 / 体验细节

| # | 问题 | 证据 |
|---|---|---|
| P2-9 | 底部双层导航（3 Tab + 全局对话栏 `报告/AI/自选`）信息架构拥挤 | `MainTabs.vue:41-43`；`GlobalChatBar.vue:6-55` |

> 上表为**已分级**的问题；§3 各时段还记录了若干**未分级的体感问题**（09:30–12:00 首页无盘中语境、看分时需手动切换、自选"平均涨幅"口径未说明、午间报音频可选不可预期、市场洞见页前台常驻轮询），一并列入 §6 建议。

---

## 5. 数据时效性矩阵（当前前端实际刷新行为）

| 模块 / 页面 | 数据时效 | 刷新机制 | 证据 |
|---|---|---|---|
| 个股详情（行情） | 盘中准实时 | **60s 轮询** | `detail.vue:2125-2142` |
| 自选列表（默认模式） | 盘中准实时 | **60s 轮询**（交易时段内，原地刷新行情） | `favorites.vue`（`syncQuoteRefresh`） |
| 自选列表（分时模式） | 盘中准实时 | **60s 轮询**（需手动开启分时模式） | `favorites.vue:341-351` |
| 自选股异动 monitor | 盘中准实时 | App **WebSocket**（`movement.created/updated`）；H5/小程序 **60s 轮询** | `monitor.vue` |
| 「提醒」Tab（AlertContent） | 盘中准实时 | 切到该 Tab 立即刷新 + 盘中 **60s 轮询** | `favorites/components/AlertContent.vue`、`MainTabs.vue` |
| AI 异动解读 | 按需流式 | **SSE**（进页触发 + 当日缓存） | `useAlertSSE.ts:141, 174-196` |
| AI 投顾对话 | 实时流式 | **WebSocket** | `shared/utils/useChatStream.ts`、`agent.ts` |
| 市场洞见 traceability | 盘后 | **60s 轮询**（自动切日） | `traceability.vue:255-262` |
| 板块龙/风口/板块四环/板块详情 | 盘后 | 进页一次 | `leaders.vue:676-680`、`sector-loop.vue:389` |
| 选股 Tab 四子 Tab | 切换时刷新 | 挂载拉一次 + **切 Tab 时 `refresh()`** | `StockContent.vue`、`TrendStockInsightContent.vue` 等 |
| 机构热门股 hot-burst | 2 分钟本地缓存 | 无网络轮询 | `hot-burst.vue:110-118` |
| 早点听 晨/午/晚报 | 按时点生成 | 手动切 Tab / 进页 | `briefing/index.vue:594-601` |
| 恐贪指数 | 由独立实时服务计算 | `onShow`（自然日门控） | `fear-greed/pages/index.vue:765, 781-792` |
| 节奏大师 | 三时点版本（盘前/午间/收盘） | `onShow` 重拉 | `rhythm/pages/index.vue:121-125` |
| 预测验证 / 业绩报告 / 财报 | 盘后 | 进页一次 | `prediction-history.vue:208-210`、`reports.vue:581` |
| 交易日判定 | 以后端 `trading_calendar` 为准 | App 启动/回前台拉取并缓存（跨天刷新） | `shared/utils/tradingCalendar.ts`、`App.vue` |

> 读表要点：**盘中准实时的入口已有 5 处**（个股详情、自选默认、自选分时、App 端异动、提醒 Tab）；板块/大盘/事件类仍为"进页快照或盘后产出"。

---

## 6. 剩余改进建议（按优先级）

### 6.1 优先（决定信任与盘中可用性）

1. **给「AI 帮我选」加显式占位标识**（P0-1）
   - 最小改动：列表头加"演示数据 · 后续接入选股 Agent"；或在真实接口就绪前把默认子 Tab 改为「趋势股」（真数据）。
2. **个股详情补分时图**（P1-2）：K 线周期加"分时"，复用现有 `getKLine(period:'minute')` 分钟链路（自选列表 MiniKLine 已在用）。
3. **持仓陪伴 / 交易复盘 / 动态估值**（P0-4）：短期先**隐藏入口**（避免点击即"开发中"），或明确排期。

### 6.2 中等（盘中体验补强）

4. **集合竞价内容位**（P1-6）：盘前 09:15–09:25 在首页「今日专属」位置展示竞价状态与自选股竞价表现（复用 `isCallAuction/getMarketStatus`）。
5. **首页盘中语境**：09:30–12:00 增加"盘中"卡片（自选异动 + 大盘快照），避免全天只切晨报/午间报。
6. **盘口/五档**（P1-2）：如需支撑"看盘"，详情页增加五档买卖盘（依赖后端行情字段）。
7. **尾盘场景**：14:30–15:00 的尾盘异动/资金（`getTradingTimeSlot` 目前无法区分尾盘，需扩展时段定义）。

### 6.3 细节

8. **底部双层导航**（P2-9）：评估是否把 `GlobalChatBar` 的「报告/自选」并入 Tab 或收纳，减轻底部拥挤。
9. **分时模式门槛**：考虑把"看分时"做成行内展开或自动分时，减少一次手动切换。
10. **自选平均涨幅口径**：在 UI 上标注统计口径（等权/是否含未开盘），避免误读为组合收益。

---

## 7. 附录：主要证据索引

- 信息架构：`src/shared/components/MainTabs.vue`、`AppBottomBar.vue`、`GlobalChatBar.vue`、`src/pages.json`
- 交易时间/日历：`src/shared/utils/tradingTime.ts`、`src/shared/utils/tradingCalendar.ts`、`src/App.vue`
- 首页：`src/modules/home/components/MorningContent.vue`、`StockContent.vue`、`AiPickContent.vue`、`shared/utils/useBriefingCard.ts`
- 早点听：`src/pages-sub-app/briefing/index.vue`、`shared/utils/middayReport.ts`
- 自选/提醒：`src/modules/favorites/pages/favorites.vue`、`detail.vue`、`monitor.vue`、`components/AlertContent.vue`
- 行情：`src/modules/market/pages/*.vue`、`utils/useAlertSSE.ts`
- 洞察/分析：`src/modules/analytics/pages/*.vue`
- 情绪/节奏：`src/modules/fear-greed/pages/index.vue`、`src/modules/rhythm/pages/index.vue`
- 空占位页：`src/pages-sub-app/portfolio/index.vue`、`review/index.vue`、`valuation/index.vue`
- 事件：`src/modules/chat/pages/event/*.vue`、`modules/chat/event/api/eventApi.ts`

---

> 本报告仅覆盖前端页面与交互层；接口是否有真实数据、后端生成时刻是否准确，需结合 `aistock-app-api` 与 `aistock-agent-py` 另做验证。
