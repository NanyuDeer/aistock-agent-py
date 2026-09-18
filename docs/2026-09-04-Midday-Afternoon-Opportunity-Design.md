# 午间报「午后前瞻 · 机会提示」逻辑修正设计

> 日期：2026-09-04
> 状态：**待评审（接入数据源需先解绑 H6 硬约束，属跨仓改动）**
> 关联：监督组「机会提示全是下跌」问题；design-debate 已完成 2 轮对抗验证（正方/反方 subagent）。
> 交付物：本次先出 **实现 spec**，评审通过后再进入 writing-plans / 编码。

***

## 1. 问题陈述（已直连服务器核实）

用户反馈：2026-09-04 午间报「午后前瞻 · 机会提示」列举的板块**当日实际全部下跌**，根本不是机会。

直连服务器（`GET /api/agent/report/midday/2026-09-04`，DB `agent_analysis_reports`）核实：

```
opportunities = [ AI算力/半导体, 石油石化, 黄金/有色, 智能驾驶, 工业母机 ]
risks         = [ 缩量反弹乏力, 冲高回落风险, 高位题材补跌, 两融持续流出, 油价地缘扰动 ]
```

字段映射无误（后端 `sections[].opportunities` + `display_report.risks`；前端把 opportunities→机会提示、risks→风险提示，[index.vue](../../aistock-app-frontend/src/pages-sub-app/briefing/index.vue#L60-L85)）。**问题不在前后端契约错位，而在生成层无真实盘面数据锚点。**

### 1.1 根因

午间报生成「机会提示」时，**手里没有当日 A 股板块/行业真实涨跌数据**，只能由 LLM 依据「晨报结论 + 外盘 + 新闻 + 搜索」外推，因此可能把实际上在跌的板块列为「机会」。

- 午间报工具集 = `get_tools("morning")`，仅 `get_cls_news` + `get_global_markets` + `tavily_finance_search`，**无任何 A 股板块/行业盘中表现**：[midday.py#L127-L129](../src/aistock_agent/agents/workers/midday.py#L127-L129)。

- 提示词让 LLM「从上午盘面与晨报结论**归纳**午后机会关键词」（≤4-5 个、每个 ≤8 字），但注入的只有晨报文本 + 工具结果：[prompts/midday.py#L19-L22](../src/aistock_agent/prompts/workers/midday.py#L19-L22)。

- 报告自身「资金与情绪」已写「两融连续收缩、资金仍偏谨慎」——机会栏与真实盘面脱节。

### 1.2 为什么不是「方案B（仅改 prompt）」

因为数据源缺失：强市拿不到「真实走强板块」、弱市区分不了「真无机会 vs 只是没查到」，属治标不治本。

***

## 2. 目标

让「午后前瞻 · 机会提示」**锚定真实盘面**：

1. 机会候选来自「当日真实走强的板块/主线」（数据源），而非 LLM 自由外推。
2. 弱市/无明确机会时输出空数组 `[]`（前端已支持「键存在可空→隐藏机会栏」，零改动）。
3. 机会与风险从**不相交**的候选集生成，语义对位互补。
4. 机会项**强约束 ∈ 候选集**（代码侧全权持有），LLM 不再拥有机会生成权。
5. 全部改动**不破坏前端契约**：`sections[].opportunities` 仍 `string[]`、`schema_version` 保持 `2.1`。

***

## 3. 方案 A' v2（经 design-debate 两轮对抗后收敛）

> 正方（Proponent）主张；反方（Opponent）攻击 G1-G6 并追打压出 N1-N9；裁决收敛后形成 A' v2。

### 3.1 设计决策表

| 方案                   | 裁决                       | 理由                                                   |
| -------------------- | ------------------------ | ---------------------------------------------------- |
| A：接入真实板块行情 + 数据驱动筛选  | **采用（按本 A' v2，需先解绑 H6）** | 唯一从根上补数据源；复用腾讯源合规；前端「键存在可空」已就绪零改动                    |
| B：仅改 prompt 约束、不接数据源 | **不采用**                  | 无真相可校验；强市给不出真机会、弱市区分不了无信号                            |
| C：弱市整体降级无提示          | **不采用**                  | 把「无机会」当「降级」会污染 `_is_degraded_report`，平盘日不落库；强市也丢核心价值 |

### 3.2 六步落地

1. **解绑 H6（前置门槛，硬约束）**：`midday.py` 与提示词明文 `H6：不新增 A 股大盘结构化数据源`，而本方案恰是新增此类数据源（原「独立专项 #3」延期项）。需**先获批并把该决策回写 project\_memory**，否则方案撞墙。
2. **app-api 新增盘内薄端点**：`GET /internal/market/sectors`，复用腾讯源 `fetchTencentBoardRank('gn', ...)`（`rank/pt/getRank`，实时、无 15:30 门禁），**不触碰东财**；返回：top 领涨板块、top 领跌板块、主力净流入/流出、市场宽度（`advance_ratio`/`avg_change_pct`）、核心指数 `pct_chg`。

   - 绕开 15:30 门禁（现唯一板块快照 `/internal/market/quick-snapshot` 在 12:05 必 409：[TencentSnapshotService.ts#L145-L149](../../aistock-app-api/src/modules/quote/TencentSnapshotService.ts#L145-L149)）。
3. **agent-py 新增工具 + 候选集 ≥5**：走 `node_api.get_intraday_sectors()`，`register("morning")`；候选池需 **>5（如 20）** 再做置信度排序（腾讯 `TOP_SECTOR_COUNT=5` 太薄，叠加过滤后难凑 4-5 个：[TencentSnapshotService.ts#L34](../../aistock-app-api/src/modules/quote/TencentSnapshotService.ts#L34)）。
4. **周期 + 个体强度双重门槛**：

   - Regime gate（市场周期）：仅当「强势」才允许出机会；「弱势/修复性」→ `opportunities=[]`。

   - 个体强度：候选板块需达绝对/相对阈值方进入机会。

   - **阈值初值必须回测冻结，禁止静默上线**（见 §5 分歧记录）。
5. **机会/风险代码侧全权持有 + 覆写**：`_build_midday_report` 输出前，用候选集覆写 `sections[午后前瞻].opportunities` 与 `risks`；风险取自与机会**不相交**的危险候选集（领跌/净流出/系统性补跌）；数据源失败 → `opportunities=[]` 且 `risks=[]` 一并为空。
6. **契约不变 + 冒烟验证**：`string[]`/`schema_version=2.1` 不变、前端零改动；跨仓联调锁定方向语义。

### 3.3 数据契约示例（新端点）

```
GET /internal/market/sectors
=> 200 { code:200, data: {
  captured_at,
  breadth: { advance_ratio, avg_change_pct },
  indexes: [{ code, name, pct_chg }],
  gainers:  [ { name, pct_change, net_amount, lead_stock } ],   // 领涨（方向契约待测）
  losers:   [ { name, pct_change, net_amount, lead_stock } ],   // 领跌
  inflows:  [ { name, net_amount } ],
  outflows: [ { name, net_amount } ],
  availability: { state: 'available'|'partial'|'unavailable', reason? }
}}
```

> 字段键名以 Python 侧 `data_client`/工具最终契约为准；`direct` 排序方向（`'down'→gainers` / `'up'→losers`）**必须加方向契约测试 + 与 quick-snapshot 现值交叉校验**，否则会反向推荐领跌板块（[TencentSnapshotService.ts#L511-L512](../../aistock-app-api/src/modules/quote/TencentSnapshotService.ts#L511-L512)）。

***

## 4. 硬约束清单（后续实现必须遵守）

1. **先解绑 H6（前置）**，并把决策回写 project\_memory；否则方案 A 判为被阻断。
2. **跨仓**：app-api 新端点 + agent-py 新工具，须走跨端同步检查（Phase 4）。
3. **机会项 ⊆ 候选集**：`_build_midday_report` 输出前用候选集覆写；目前它是原样透传 `sections`（[midday.py#L30-L59](../src/aistock_agent/agents/workers/midday.py#L30-L59)）。
4. **风险与机会不相交 + 对称降级**：数据源失败时两者一并为空。
5. **方向映射契约测试**：机会只取领涨，方向反了会推荐领跌。
6. **候选池 >5**：避免过滤后凑不满 4-5 个。
7. **契约不变**：`string[]`、`schema_version=2.1`、前端零改动（前提：机会恒产自候选集）。
8. 数据源约束：行情走**腾讯**，龙头走同花顺，**东方财富禁止**（新端点不得走 `EmSnapshotService.getConceptFlow`）。

***

## 5. 分歧记录（design-debate 留待重估，不假装已收敛）

| 分歧          | 双方立场                                                                                  | 留待                                               |
| ----------- | ------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 「机会」如何定义    | 反方：弱市/修复普涨日「当日 top 领涨」恰是「冲高回落/高位补跌」对象，是**逆指标**；正方：认领，引入周期+强度门槛                        | 阈值需**回测校准**（以 09-04 修复日为负样本、放量强势日为正样本），参数冻结后才可上线 |
| Regime 阈值方向 | 反方：`advance_ratio>=0.60 且 avg>=0` 在修复普涨日**仍放行**，与 G2 逆指标同源（N4）；且会误杀弱市**逆势绝对强势**板块（N5） | 需实证数据标定，明确「什么算真正强势、什么算修复性伪机会」                    |
| risks 语义    | 反方：纯代码从「领跌/净流出」生成偏**回顾性描述**，失去 LLM「警惕/前瞻」语义（N9）；「系统性补跌」如何确定性判定                        | 决定是否保留 LLM 润色 + 定义「系统性」规则                        |
| details 一致性 | 反方：LLM 依据新闻/晨报写的「午后前瞻详述」与代码生成机会可能**前后矛盾**（N7）                                         | 约定：机会以代码为准，details 叙述需与之对齐或降权                    |

***

## 6. 实施步骤（跨仓）

1. app-api：`TencentSnapshotService` 暴露盘内板块数据（不破 15:30 门禁语义）→ `internal.ts` 注册 `/internal/market/sectors`（含宽度/指数/板块，3 源复合，如 `fetchIndexes`/`fetchMarketBreadth`/`fetchTencentBoardRank`）+ 方向契约测试。
2. agent-py：`data_client.get_intraday_sectors()` → 新工具（`tools/`，`register("morning")`）→ 机会/风险候选集提取器（纯函数）。
3. agent-py：`midday.py` 组装时注入候选集、跑周期+强度门槛、`_build_midday_report` 输出前覆写 `opportunities`/`risks`；提示词改「机会仅从候选集命名」「弱市输出 `[]`」。
4. 提示词与 Pydantic/产出契约对齐（机会恒 string\[]、schema\_version 2.1）。
5. 前端：**零改动**（仅验证「键存在可空 → 隐藏机会栏」冒烟）。

***

## 7. 验收标准

1. 强市（放量普涨、真实主线走强）：机会 = 当日真实领涨主线（数据源），无相悖板块。
2. 弱市/修复日（如 09-04）：机会非「当日全跌板块」；若确无走强主线，`opportunities=[]`，前端隐藏机会栏、风险栏全宽。
3. 机会项恒 ⊆ 当日候选集；无「同一板块既在机会又在风险」。
4. 数据源失败：`opportunities=[]` 且 `risks=[]`（对位区整块隐藏或仅提示数据不可用）。
5. 前端契约回归：`sections[].opportunities` 仍 `string[]`、`schema_version=2.1`、双端联调无错位。

***

## 8. 待用户决策（前置阻塞项）

- **是否解绑 H6**（接入 A 股板块结构化数据源），并把该决策回写 project\_memory。

- **是否接受跨仓改动**（app-api + agent-py），并同意阈值**先回测冻结、不静默上线**。

- 若两者任一否决，则本方案降级为「方案 B（仅 prompt 收紧）+ 数据一致性声明」，并明确接受其局限。

