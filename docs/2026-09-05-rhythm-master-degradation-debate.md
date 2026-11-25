# 2026-09-05 rhythm\_master 降级链 — design-debate 裁决书

> 日期：2026-09-05
> 方式：design-debate（subagent 式，4 轮辩论 R1-R4，8 次 subagent 派发 + 主 Agent 裁决核验）
> 触发：用户点名使用 design-debate 复查手工重跑节奏大师暴露的降级 bug
> 状态：**已收敛，全部分歧已裁决**（F1 归属语义=方案丙、F3 score=level 同源派生、Stage↔level 映射定死，见 §4）

## 0. 背景

2026-09-05 在生产服务器（121.37.46.229，APP\_ENV=production，Node API localhost:56790）以**只读直连**方式手动重跑节奏大师 `after_close` 档，产出**降级卡**：

```json
{"schema_version":"1.0","target_date":"2026-09-07","basis_date":"2026-09-05","refresh_slot":"after_close",
 "evidence":{"stage":null,"stage_reason":"证据不足，无前阶段","certainty":"low","certainty_reason":"多空未共振，观望","position":null,"event_anchors":[],"data_missing":["研研判暂不可用"]},
 "synthesis":null,"synthesis_available":false}
```

运行日志两条 400：

1. `GET /internal/index/000001/kline?days=260&start_date=20260905` → status=400
2. LLM 调用 → `openai.BadRequestError: Prompt must contain the word 'json' in some form to use 'response_format' of type 'json_object'.`

## 1. 根因链（裁决者最终核验，全部读码实证）

降级原因不是单一 bug，而是**四层叠加 + 一层跨仓契约断裂**：

| # | 根因                                                | 证据                                                                                                                                                                                                                                                                                                                                               | 影响                                        |
| - | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------- |
| 1 | `days=260` 超 Node 端点上限（校验 1-200）                  | `rhythm_master.py` L98 vs `internal.ts` L367                                                                                                                                                                                                                                                                                                     | 400 → kline=\[]                           |
| 2 | **`start_date=今天`** **顺向过滤 → K 线窗口坍缩**（更深根因）      | `rhythm_master.py` L96-98 传 `start_date=basis`；`internal.ts` L392-398 过滤 `d>=startDate`；after\_close=凌晨已收盘日→仅 1 根，morning/midday=未收盘日→**0 根**                                                                                                                                                                                                    | 即使 400 修复，closes<20 → trend/vol 恒 0       |
| 3 | synthesis 提示词无字母序列 "json"                         | `build_synthesis_prompt`（prompts/workers/rhythm\_master.py L43-56）vs `llm.py` L156-166 固定 json\_mode                                                                                                                                                                                                                                             | DeepSeek 400 → synthesis=None → "研研判暂不可用" |
| 4 | validate 空壳认过                                     | `rhythm_rebuilt_validate.py` L20-35：空 mainline+空 narrative 返回 True                                                                                                                                                                                                                                                                               | 空壳卡被标记 synthesis\_available=True（假正常）     |
| 5 | **跨仓契约断裂：producer 永不写** **`content.rhythm_card`** | `rhythm_master.py` L151-159 的 content 只有 schema\_version/target\_date/basis\_date/refresh\_slot/evidence/synthesis/synthesis\_available；而 Node `publicRouter.ts` L94-97/L126-129 读 `content->'rhythm_card'->>'level'/'score'/'position_band'`、`run_once`（rhythm\_verification.py L186）读 `content["rhythm_card"]`、前端 `agent.ts` RhythmCard 接口读该对象 | 日历热力图恒灰格、run\_once 恒"基准报告缺失"、前端节奏区块静默消失   |

**事实修正（辩论中被证伪/修正）**：

- "days=260 是根因" → 修正：窗口坍缩更深，260 只是第一层直接触发。

- "1 根大概率 stage=null" → 证伪：n=1 时实为**锁定三态** {ice, ebb, None}，且 `detect_stage` L106 可由 senti+fg+breadth 凑出 "ice"（看似正常实则看空的卡），比 null 更糟。

- "改 run\_once 匹配键即可救" → 证伪：run\_once、Node 日历、前端三大消费端共用同一 rhythm\_card 契约，必须 producer 补发字段（单点修复）。

- "前端零改动" → 修正：producer 补字段后前端代码零改，但字段**必须按必补清单**补全（temperature\_series/conflict 为前端 TS 必填），否则区块静默消失。

- "仅改 days=200 即可恢复" → 证伪：需窗口 + prompt + rhythm\_card + 留痕四件一起修。

## 2. 方案决策表

| 决策项                 | 裁决                                                                                                   | 理由                                                                                                                                    |
| ------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| K 线窗口修复（G1）         | **采用 agent 侧**：`get_index_kline(INDEX_CODE, days=200, end_date=basis)`（去 start\_date）                | v3 已验证；同时救 after\_close(1根)/morning/midday(0根)；**不修改** internal.ts 区间语义（prediction\_validator L129、rhythm\_verification L172 合法依赖该语义） |
| days 常量（G9）         | **采用** `KLINE_LOOKBACK=200`，Node 上限不改                                                                | 全仓唯一超限点即 rhythm\_master；200 已够 closes≥65/amounts≥120；prediction\_invalidation L106 days=130、prediction\_validator L55 days=200 均合法    |
| prompt 补 json（G3）   | **采用**：build\_synthesis\_prompt 追加第 5 条要求含 "JSON" + 锚定 RhythmSynthesis 三字段                           | DeepSeek json\_object 硬性要求 + 契约字段对齐（memory #46/#8 印证）                                                                                 |
| validate 空壳门槛（G4）   | **采用**：mainline、launch\_outlook、narrative 三者皆空 → False；mainline=\[]+narrative 非空 → 放行                | 空 mainline 是合法态（当期无主线 P0）；narrative 非空即非空壳                                                                                            |
| 归属日语义（F1/G5）        | **✅ 已裁决（2026-09-05）：方案丙**（存 target\_date + run\_once/前端读最新卡）                                         | 见分歧记录 §4 F1                                                                                                                           |
| score/level 口径（F3）  | **✅ 已裁决（2026-09-05）**：score=level\_idx×20（level 同源派生）；Stage→前端五档映射按 §4 映射表定死（实现 `STAGE_TO_LEVEL` 常量） | 消灭"活跃档+40"双源错位；前端裸数字与色带同源；确定性映射不进 LLM/提示词                                                                                             |
| rhythm\_card 补发（G6） | **采用**：producer 新增 `_build_rhythm_card` 并入 content                                                   | 单点修复救活 run\_once+前端+日历三消费端（前端零改动）                                                                                                     |
| K 线留痕（G2）           | **采用**：行数<20 时 stage=None + data\_missing="指数K线不足"，严格以行数为门                                           | 阻止"空 closes 拼出假 ice"；留痕不污染中性语义                                                                                                        |
| 跨仓契约收敛（G9）          | **采用**：agent.ts RhythmCard ↔ python schema ↔ publicRouter SQL 三处 key 同源校验                            | 已证三处分裂是事故放大器                                                                                                                          |

## 3. 硬约束清单（后续实现必守）

1. **K 线行数 <20 时严禁由 detect\_stage 产出 stage/level**（否则空 closes 能凑出"看似正常实则看空"的 ice 卡）——必须走 `data_missing="指数K线不足"` 留痕，且留痕门禁严格以行数为准（不以 stage==None 为准，防"无信号"被伪造成"数据缺失"）。
2. **凡走** **`with_chat_structured_output`(json\_mode) 的 prompt 必须含字母序列 "json" + 与 Pydantic 契约逐字对齐的字段名**，缺任一即视为不合法调用。
3. **producer 与消费端契约键必须同源**：python content.rhythm\_card 字段 == agent.ts RhythmCard 接口 == publicRouter SQL 列；level/score/position\_band/branches/temperature\_series/conflict/data\_missing 必补齐全，next\_event\_anchor/event\_high\_hint/event\_window/phase 可容忍缺失（前端有 v-if 兜底）。
4. **异常不得静默整卡 DEGRADED 且无留痕**；kline 失败必须进 data\_missing 而非裸降级。
5. **下发的支撑/压力点位必须真实可算**：high/low 空值时宁可空 branches+留痕，不伪造点位；兜底层级=剔空行 > close 近似 > 不产出（build\_technical\_branches L304-307 现无条件 max/min，需加空值防护）。
6. **rhythm\_verification / 盘前读取 = 读最新 after\_close 卡 + basis\_date 校验**（若采方案丙），不再按 report\_date 精确当天匹配。

## 4. 分歧记录（不假装收敛）

- **✅ F1（G5）after\_close 归属日语义——已由产品负责人拍板（2026-09-05）**：**采用方案丙**。

  - 甲（status quo）：`report_date=target_date`（次日），run\_once 当天读取错位已知；D+1 盘前有"前晚预览"档。

  - 乙：存储特判 `report_date=basis_date`（D），run\_once 对齐，但 D+1 盘前热力格变灰、详情缺收盘基准槽。

  - **丙（已采纳）**：存储 `report_date=target_date` 不变，run\_once/前端盘前改读"最新 after\_close 卡"+`content.basis_date≈D` 校验，不改存储、不加 slot 特判分支。

  **方案丙的落地要求（并入实施指引）**：

  1. 存储：`rhythm_master.run()` 继续用 `report_date=card.target_date` + `user_id=slot`（**不改**）。
  2. `run_once`（rhythm\_verification.py）：不再按 `report_date=当天` 精确匹配 after\_close，改为读取**最新 after\_close 卡**并校验 `content.basis_date` 为最近交易日（morning/midday 仍按当天 target 读，slot 优先级 midday>morning>after\_close 不变）。
  3. 前端盘前块/日历：消费端按 target\_date 归格（D+1 格显示 D 收盘基准），沿用现有读取逻辑，仅确认"读最新卡"语义即可——**default 不动 app-api/frontend，除非联调发现必改再走 cross-repo 评审**。

- **✅ F3 score 语义——已裁决（2026-09-05）**：**score 由 level 派生同源**，`score = LEVEL_IDX × 20`（ice=0 / low=20 / normal=40 / active=60 / euphoria=80）；`level` 为唯一语义源，`score` 恒等于其档位刻度。否决"certainty 映射 40/60/80"（双源错位：会出现"active 档+40"）；否决"新造连续温度合成"（过度设计，与新版 stage 判据并存会打架）。前端 `score` 大数字与五档色带永远同源，温度柱按 0-100 取高度不受影响（5 个离散取值单调）。

- **✅ Stage ↔ 前端 level 五档映射——已定死（2026-09-05）**：

  | stage（新版证据）    | level（前端五档） | idx | score         |
  | -------------- | ----------- | --- | ------------- |
  | ice（冰点）        | `ice`       | 0   | 0             |
  | ebb（退潮）        | `low`       | 1   | 20            |
  | launch（启动）     | `normal`    | 2   | 40            |
  | rally（主升）      | `active`    | 3   | 60            |
  | overheat（过热）   | `euphoria`  | 4   | 80            |
  | None（证据不足/数据缺） | `null`      | —   | null（灰格，如实展示） |

  依据：前端 `LEVEL_META`（RhythmCard.vue L108-114 五档标签/idx）与 `_LEVELS`（rhythm\_engine.py L33-35 阈值档）同构；新版 `Stage`（schemas/rhythm\_master.py L8）为独立阶段语义，实现时在 `_build_rhythm_card` 内做 `STAGE_TO_LEVEL` 常量映射（禁止在提示词/LLM 层做，保持确定性）。`phase` 字段（前端 PHASE\_META 四态 ice/warm\_up/overheat/ebb）判定为**可容忍缺失**（前端 v-if 兜底"数据缺失（沿用前值）"），不强补。

## 5. 待生产验证项

- after\_close 16:05 后 Tushare `index_daily` 当日 bar 是否必然到位（加"末行==basis 探针"，缺失时 data\_missing 追加而非静默）。

- `build_technical_branches` 依赖 high/low 非缺失；high/low 缺失时"不产分支+留痕"路径生效。

- 修复落地后：落库 content.rhythm\_card 各字段实际可读、日历热力格不再灰、run\_once 当天对齐、temperature\_series 不足 7 日留痕路径生效。

## 6. 实施指引（最小可合并集 ↔ 完整集）

**最小可合并集（恢复降级卡 + 展示 + 验证的最小改动，全部 agent-py 侧）**：

1. 窗口修正：`rhythm_master.py` L96-102 → `days=KLINE_LOOKBACK(200), end_date=basis`（去 start\_date）+ 顶行常量。
2. prompt 补 json：`prompts/workers/rhythm_master.py` build\_synthesis\_prompt 追加含 "JSON" + 锚定 RhythmSynthesis 字段的第 5 条要求。
3. validate 门槛：`rhythm_rebuilt_validate.py` 三者皆空 → False。
4. producer 补 `rhythm_card`：`rhythm_master.py` run() 构造 content 时并入 `_build_rhythm_card`（score/level/position\_band/phase\_evidence/temperature\_series/event\_window/conflict/next\_event\_anchor/branches/data\_missing）——branches 由 `rhythm_engine.build_technical_branches` + `build_event_branch` 确定性生成。
5. K 线留痕：行数<20 → stage=None + data\_missing="指数K线不足"，加 `logger.warning`。

**完整集 = 最小集 +**：high/low 空值兜底（rhythm\_engine.py build\_technical\_branches 签名加 data\_missing、剔空行/close 近似/不产出三级兜底）+ **F3 已裁决落地**（`STAGE_TO_LEVEL` 常量映射 + score=level\_idx×20 同源派生，§4 映射表）+ days 契约注释 + 文档更新（本文件 + AGENTS.md 相关段）+ **方案丙落地**（run\_once 改读最新 after\_close 卡 + basis\_date 校验；存储 `report_date=target_date` **不改**——已由产品拍板，见 §4 F1）。

**验证方法**：

- V1：本地起 app-api，`GET /internal/index/000001/kline?days=200&end_date=20260905` → 期望 200 根、末行 trade\_date==basis（after\_close 当日 bar 存在时）；`?days=260...` → 期望 400（钉住旧 400，防回归）。

- V2：agent 单测（mock node\_api.get\_index\_kline 返回 200 根历史 close/amount）→ `_compose_card` 断言 stage 非 None、certainty 非默认低、synthesis\_available=True、data\_missing 不含 DEGRADED。

- V3：prompt 单测 `assert "json" in build_synthesis_prompt(ev)` 且含 mainline/launch\_outlook/narrative。

- V4：validate 单测 RED→GREEN（三者皆空 False，非空 True）。

- V5：三时点参数一致性单测（after\_close/morning/midday 均 `days=200,end_date=basis`）。

- V6：端到端本地连 Node 重跑 after\_close，断言无两处 400、evidence.stage 非 null、synthesis\_available=True。

## 7. 执行边界（评审复盘要求）

- 本裁决书来自 design-debate（**只验证方案，不写实现代码**），实现前须按 aistock-workflow 走 changer 分支 + test-driven-development。

- 归属日语义（F1）已由产品负责人拍板**方案丙**（2026-09-05）：存储 `report_date=target_date` 不变，run\_once/前端盘前改读最新 after\_close 卡 + basis\_date 校验（落地细节见 §4 F1）。

- 涉及 app-api（internal.ts 语义、publicRouter）与前端（agent.ts）的改动，按 G1/G7/G8 裁决**不应**发生（agent 侧修复即可，方案丙前端按"读最新卡"沿用现有逻辑）；若实施中发现必须动 Node/前端，需按 cross-repo 流程单独评审。

## 8. 实施状态回填（2026-09-05）

- [x] Task 1 prompt json + validate 空壳门槛
- [x] Task 2 build_technical_branches high/low 兜底
- [x] Task 3 K 线窗口 + 行数留痕
- [x] Task 4 producer 补发 rhythm_card
- [x] Task 5 STAGE_TO_LEVEL 常量
- [x] Task 6 _compose_card 三元组
- [x] Task 7 run_once 方案丙（min 边界）
- [x] Task 8 集成验证 + 文档
- 生产验证：待组长 merge 主分支 + 服务器 git pull + pm2 restart 后，手动重跑 after_close 断言无 400、evidence.stage 非 null、synthesis_available=true、日历热力格非灰。

