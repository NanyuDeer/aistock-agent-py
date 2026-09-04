# CHANGELOG.md — aistock-agent-py 变更记录

> 所有修改记录按时间倒序排列。每条记录标注分支、时间、开发者。

## [changer] 2026-09-04 — 节奏大师「大师级判断」重建

**开发者**: 37588

### 新增
- `schemas/rhythm_master.py`：Pydantic 契约（Stage/Certainty/Direction/PositionAction Literal + PositionBand/EventAnchor/RhythmEvidence/MainlineRef/LaunchOutlook/RhythmSynthesis/MasterRhythmCard）
- `services/rhythm_rebuilt_evidence.py`：确定性证据层纯函数——`detect_stage`/`detect_certainty`/`compute_position`/`build_event_anchors`（阶段/确定性/仓位/事件锚点不依赖 LLM）
- `services/rhythm_rebuilt_validate.py`：校验层纯函数——`validate_synthesis`/`_contains_price_point`（G19 禁点位拦截 + confidence/grounding 校验）
- `services/rhythm_rebuilt_synthesis.py`：研研判层——`run_synthesis` 结构化输出（`with_chat_structured_output` json_mode，DeepSeek thinking 兼容，失败返回 None 不 500）
- 测试：6 个单元测试 + `tests/integration/test_rhythm_master_rebuilt.py`

### 改进
- `agents/workers/rhythm_master.py`：重构接入「确定性证据层 → LLM 研研判层 → 校验层」三层流水线，产出 `MasterRhythmCard`（四段 + 仓位）；三时点统一走 `_compose_card`，保留 `_load_sentiment_series`，顶层 try-catch 永不 500（合成失败降级 `DEGRADED_TEXT`）
- `prompts/workers/rhythm_master.py`：追加 `build_synthesis_prompt`（主线段只引用 P0 + 标注来源时效；启动节点概率式展望 + confidence + 假设推演；禁点位；narrative ≤60 字 + 不构成投资建议）；保留旧叙事导出

### 文档
- 本次改动记录写入本 CHANGELOG

---

## [changer] 2026-09-04 — 午间报「午后前瞻机会提示」接入真实盘面数据

**开发者**: 37588

### 改进
- 午间报「午后前瞻 · 机会提示」改为基于当日真实板块行情筛选：仅列当日真实走强的方向；弱市或无明确机会时输出空提示，避免把实际下跌的板块列为「机会」。
- 机会与风险提示由不相交的数据来源生成，并在数据不可用时对称降级；前端展示契约保持兼容（机会提示仍为关键词数组、schema 2.1），无需前端改动。

---

## [changer] 2026-09-04 — 节奏大师预判语义重构（密集触碰带 + 结构化仓位动作 + 可验证锚点）

**开发者**: 37588

### 新增
- `services/rhythm_dense_band.py`：历史密集触碰带确定性纯函数 `dense_band`（touch_gap 容差带聚类 + 量能加权选带 + 近端 clamp），替代"max/min(20日极值)×MA20 系数"的机械支撑/压力计算——符合"压力位应来自前期反复触碰不破/跌不破密集区"语义
- `services/rhythm_engine.py`：`build_technical_branches`/`build_event_branch` 分支新增 `position_action`（direction=add/reduce/hold + change 成数 + band）、`anchor`（metric/threshold/direction 可验证锚点）、`touch_strength`（历史触碰强度，非命中概率）；`conclusion.range` 降级为辅助参考；新增 `position_band_to_action` 确定性映射（bullish→add/+2成、bearish→reduce/-1成、neutral→hold/持仓不变）

### 改进
- `agents/workers/rhythm_master.py`：`get_index_kline` 传 `start_date` 解锁长历史取数（命中 internal.ts startDate 5000 行分支）；amount 量能分档对齐近 20 交易日（修复 closes/highs/lows/amounts 各自独立过滤的取样错位既有 bug）；`dense_band` 的 `dense_support/dense_pressure` 接入 `build_technical_branches`（支撑/压力改为密集触碰带）；每分支回填真实 `touch_strength`
- `services/rhythm_verification.py`：`evaluate_branch` 改用 `anchor.direction`/`anchor.threshold` 机械判 hit/miss（仅对"上证指数点位"触发做点位机械判定，成交额/enum 分支回退到 `conclusion.range`，避免"指数点位 vs 亿元"单位错配）；保留 `_triggered` 前置判断（条件未发生 → insufficient）；事件分支落档后按 range 判 hit/miss（D11 保持）

---

## [changer] 2026-09-03 — 盘中报「午后前瞻」schema 2.1（机会/风险短词契约）

**开发者**: 37588

### 改进
- `prompts/workers/midday.py`：「午后前瞻」分段输出改为结构化 `opportunities` 关键词（4-5 个、每个 ≤8 字纯词、无 conclusion 段落，完整叙述保留于 details 第2部分）；`risks` 输出收紧为 4-5 条 ≤8 字短词；schema_version 示例升 `"2.1"`
- `agents/workers/midday.py`：`_build_midday_report` 成功分支 schema_version `"2.0"`→`"2.1"`（降级分支 `"1.0"` 不动；sections/risks 原样透传，12:15 广播链路不受影响）

### 测试
- 新增 `tests/unit/test_midday_prompt_contract.py`：锁定 prompt 契约（opportunities ≤8 字 4-5 个、risks 短词、schema 2.1、午后前瞻 JSON 示例无 conclusion）+ `_build_midday_report` 版本与透传断言（断言贴 prompt 原文并负向锁定）

### 文档
- 根 `AGENTS.md`：12:05 盘中报定时链路行补充 schema 2.1 契约说明（12:15 广播行无需改）

## [main] 2026-09-02 — Spec D 收口：板块迭代回放接线 + 板块预判生产触发 + 个股验证/迭代环同构

**开发者**: Aria

### 新增
- `services/prediction_service.py`：统一**个股预判入口 `predict_stock`**（keyword-only `(*, report_date, stock_code, stock_snapshot)`，stock_code 收 6 位裸码/带后缀 ts_code）+ `_stock_prediction_core`（LLM 结构化链，生产/回放共用）+ `_replay_predict_stock_from_case`（REPLAY 转调，case.meta 重建）；落库 `source_type="stock_prediction"`（source_id=`stock:{code}:{date}`）默认 pending
- `services/prediction_targets.py`：`resolve_index_or_stock_code`——指数/个股 target code 归一（6 位裸码/带后缀 ts_code/指数后缀消歧防 `000001.SH` vs `000001.SZ`），验证器与预判入口共用
- `iterate/adapters.py`：注册 `stock_prediction` 迭代 adapter（verification 验证驱动 + `prediction_verified_scan` 产片源，`data_deps={}`——回放输入全来自 case.meta）
- `tools/sector_tools.py`：新增 `predict_sector_trend` 对话工具（板块预判对话补充入口）+ `prompts/workers/sector.py` 预判意图说明
- `agents/workers/sector_trace.py`：`SectorTraceRunResult.snapshot`——溯源快照随结果返回（级联预判的 `sector_snapshot` 输入）

### 修复/改进
- **板块迭代回放接线（收口已知缺口 #1）**：`replay_runner._build_state` 建 sector_trace/sector_prediction 分支 + `_report_date_from_case`（meta.trade_date 优先，回退切片快照）；`sector_trace.run()` 返回 final_response（trace JSON）+ 顶层 `sectors`（对齐 review.run 契约，run_once 转 structured 供 `evaluate_attribution`）；`run_once` 验证分支适配 predict_sector keyword-only 签名；`predict_sector` 顶部 REPLAY_CASE_ID 转调 `_replay_predict_sector_from_case`；LLM 链抽为 `_sector_prediction_core`（生产/回放共用，后处理语义一致）；adapters 移除两 adapter「回放未接线」缺口注记
- **板块预判生产触发接线（收口已知缺口 #2）**：`SectorTraceConsumer` 溯源成功后串行级联 `predict_sector`（`_cascade_sector_prediction`，失败仅日志不阻断，不把溯源事件拖进 retry/DLQ）；Node `(source_type, source_id)` UNIQUE 幂等防 quick/full 重复触发堆积
- **个股验证环就绪**：`prediction_validator` horizon/condition 两解析入口改用共享 `resolve_index_or_stock_code`（支持 6 位裸码/带后缀 ts_code，stock 不再 no_source）；无法解析 reason 文案更正
- **个股迭代支撑**：`_persist_chat_prediction` 移除 stock→skipped 过时分流（验证器已支持个股，对话预判即时进 16:00 验证队列）；`replay_runner` 补 stock_prediction `_build_state` 分支 + run_once keyword-only 调用

### 测试
- 新增 `tests/unit/test_prediction_stock_service.py`（predict_stock 落 pending/后缀归一/非 stock 拒绝/REPLAY 重建/日期错位）；新增/更新约 30 例（sector/stock 回放状态、级联触发传快照、对话工具、验证 code 归一、个股产片走 stock profile 判定、chat stock pending）；回归：prediction/validator/targets/stats/iterate/replay/skills/consumers/sector/case_sourcers 282 例全绿 + src mypy/ruff 0 告警

### 文档
- changelog-pending.md 收口已知缺口 #1/#2；总架构 `2026-08-31-四环三粒度复用架构-design.md` §5.4.1/§5.4.2 与自选股洞察升级 spec §10 登记下游契约（含对同事 light_predict 的落库约定）

---

## [changer] 2026-09-02 — 节奏大师「下一重大事件锚点」（design-debate P1）

**开发者**: changer-collab

### 新增
- 节奏大师下一重大事件锚点：`rhythm_engine.build_next_event_anchor` 纯函数（取事件窗口内首条 high 事件，N=事件日与 basis_date 自然日差，note 三态 今日/明日/N 天后；无 high 返回 None，日期异常跳过不抛异常 G6）（`services/rhythm_engine.py`）
- rhythm_master 三时点接线：after_close 全量卡与 morning/midday 事件驱动增量统一以 basis_date 为锚写入 `rhythm_card.next_event_anchor`（`agents/workers/rhythm_master.py`）

### 测试
- `tests/unit/test_rhythm_engine.py`：锚点无 high/取首条 high/今日明日/坏日期跳过 4 用例（26 passed）
- `tests/integration/test_rhythm_master_worker.py`：after_close 全量 + morning 增量锚点 2 集成用例

---

## [main] 2026-09-01 — Spec B 预判验证闭环（验证 skill + 画像 + 个股数据源 + 三处反哺）

**开发者**: Aria

### 新增
- `skills/prediction_validation.py`：验证 skill——`read_validation_profile`（缓存优先，miss 拉 verified 重算，key 用 `internal_id`）+ `explain_verification`（LLM 解释层，失败降级规则兜底）+ `enrich_prediction_input`（纯函数并入 `validation_profile` 块，红线：不改判定/不产指令/不覆盖 A3）
- `prompts/workers/prediction_validation.py`：解释层 prompt
- `services/cache.py`：`get/set_cached_validation_profile`（key `prediction:profile:{internal_id}`，TTL 86400）
- `services/prediction_stats.py`：`build_validation_profile` 纯函数（condition 级命中率/miss_patterns/condition_met 分布/失效模式/degradation）
- `services/data_client.py`：`get_stock_kline(code, days, start_date, end_date)`（复用 `get` 解包 `data.rows`）

### 改进
- `services/prediction_validator.py`：`_fetch_kline_window` 补 stock 分支（个股日 K 走 `get_stock_kline`，带区间参数 [due-20, due+10]）+ `_write_validation_profiles` 到期验证落画像（接管）
- `services/prediction_service.py`：`run_chat_prediction` 绑定 `_enrich_chat_input_with_profile`；`run_predict` 绑定 `_enrich_market_predict_input`（大盘溯源代表 target=上证指数）
- `services/morning_forecast_extractor.py`：新增 `_enrich_morning_summary_with_profile` 展示侧反哺（sufficient_sample 时 summary 追加历史命中率；缓存存原始 LLM 结果防陈旧；异常降级保持原文）

### 测试
- 新增 `tests/unit/test_prediction_validation.py`；扩充 prediction_stats/prediction_validator/prediction_service/morning_forecast_extractor 测试。晨报 9 例、预测相关 113 例全绿；mypy 通过

---

## [main] 2026-09-01 — 四环三粒度 Target 维度地基（TargetProfile 引擎独立提交）

**开发者**: Aria

### 新增
- `services/target_profile.py`：`TARGET_PROFILES` 注册表（index/sector/stock 三粒度，每项含溯源 prompt/证据源/快照构造/默认周期/K线拉取/迭代阈值）+ `get_profile` 一次查表 + `get_iterate_threshold`（horizon×场景分层阈值，`resolve_raw_threshold` fail-closed）+ `make_target`（LLM 自由文本 → 首类 `Target`）+ `canonical_ts_code`（裸 6 位码带交易所后缀，防指数/个股空间冲突）
- `tests/unit/test_target_profile.py`：20 例（模型约束 `extra=forbid`/注册表三粒度覆盖/阈值分层命中与 fallback/首类构造/ts_code 数据卫生）

### 说明
- 仅依赖已提交的 `schemas/target.py` 与 `prediction_targets.py`；`prediction_targets.classify_target`（legacy 字符串四分类）保持不动。本模块是后续 Spec B/C/D 落地的统一入口，`get_iterate_threshold` 消费方待阶段 5 / Spec1 接入。

### 文档
- 同步 CHANGELOG.md；changelog-pending.md 清空 TargetProfile 待办

---

## [main] 2026-09-01 — 条件化预判改造（Spec A，三端全量收尾）

**开发者**: Aria

### 新增
- 预判 schema 升 3.0（`schemas/prediction.py`）：新增 `PredictionDirection`/`PredictionMetric`/`PredictionAnchor`（horizon+threshold+metric+direction 自带方向）/`PredictionCondition`（condition/scenario/anchor 三段式）；`PredictionResult` 增加可选 `conditions` 与 `target: Target | None`，并新增 `schemas/target.py`（`Target`/`TargetProfile` 纯数据模型，关联统一 Target 维度，兼容 `classify_target` 归类）
- `PredictionAnchor.direction` 缺省 neutral，归一化层从文本兜底（regex），确保验证不因缺失方向失败
- `services/prediction_service.py`：`_coerce_prediction_payload` 兜底 schema_version=3.0；`run_chat_prediction` 恢复到期日计算并落库 chat 预判（`_persist_chat_prediction`），按 `classify_target` 分流——index/sector→pending 入 16:00 验证，stock→skipped 防验证队列堆积
- `services/prediction_validator.py`：新增 `_verify_conditions`，对每条 condition 产出 `c{i}` 验证 entry（hit/miss），`run_once` 双验证调度（horizon 与 condition 并行互不干扰，窗口未满 wait 不回写）

### 改进
- `prompts/workers/prediction.py`：PREDICTION_PROMPT / PREDICTION_CHAT_PROMPT 强制 `conditions[]`（2-3 条，至少 1 条含成交量维度），禁止"无条件短中长期"式空洞预判

### 文档
- 同步 CHANGELOG.md；changelog-pending.md 重置

---
## [changer] 2026-08-31 — 预判/节奏/迭代增强（TradingVane 研报借鉴 v2）

**开发者**: changer-collab

### 新增
- 预判 A3 确定性置信钳制：`clamp_confidence_by_bucket`（Wilson 95%CI 上界 vs baseline，n<30 不动作）+ run_predict/run_chat_prediction 接线（short 恒启用/mid 配置开关/long 不启用）+ `PredictionHorizon.confidence_source` 标记（`prediction_stats.py`/`prediction_service.py`/`config.py`）
- 预判 A1 失效复核触发器：`prediction_invalidation.py` 三态迟滞状态机 `update_trigger_state` + `scan_active_pending` 早退扫描（MA20 读数触发，early_exit 标记与 result 分离存储，验证器 skip 改按 `"result" in entry` 判定）
- 预判 A2 独立源冲突检测：`corroborate_evidence`（quote/flow/news 三通道，claim 不计入）+ `PredictionResult.evidence_corroboration`（run_chat_prediction 接线，不覆盖 confidence）
- 节奏大师 C1 指数技术位佐证：`ma_breadth` + `detect_phase(tech=)`（kline 扩至 120 日，佐证只进 phase_evidence，不进背离判定）
- 节奏大师 C2 背离传导：`conflict_kind` 顶/底区分 + `conflict_penalty` 进 `compose_score`（顶背离 -8.0 降档/底背离禁止，背离用 tech=None 原始相位防双降）
- 迭代 B1 维度证据标注：`build_scorecard` 每维度加 `evidence_kind`（deterministic/llm_derived）

### 文档
- AGENTS.md：PUT verification 早退契约行 + A1/A2/A3 增强小节 + services 目录登记 prediction_invalidation.py；README 环境变量表补充 PREDICTION_CONF_CAP_SHORT/MID

## [junliang] 2026-08-27 — 个股异动溯源读层 skill（阶段 2.2）

**开发者**: Aria

### 新增
- `src/aistock_agent/skills/stock_trace_lookup.py`：对话内查登录用户个股异动溯源（价格异动/涨停雷达归因结果，只读列表）；入参 `{symbol?}`（可空——无代码返回该用户全部异动溯源），user_id 由 qa_router postprocess 登录态注入（未登录移除 call），无数据/异常 → degraded；`ChatSource.kind="stock_trace"`，source_id=`stock_trace:{event_id}`
- `src/aistock_agent/services/data_client.py`：`list_stock_traces(openid, symbol?, limit?)` 走 `/internal/stock-trace/events` 只读端点

### 改进
- `src/aistock_agent/schemas/chat_contract.py`：3 处 Literal（InsightGoal.intent / SubGoal.intent / SkillCall.skill_name）加 `stock_trace_lookup` + `ChatSource.kind` 加 `stock_trace`
- `src/aistock_agent/graph/nodes/qa_router.py`：KEYWORD_FALLBACK 词条优先级——`异动/异动归因/异动原因` → `stock_trace_lookup`（置于 `涨停雷达/自选股/洞察/归因` → `insight_lookup` 之前）；`_STOCK_SKILLS` 扩为 5 项；`_infer_stock_skill`/`_build_default_skill_call` 增加分支（symbol 可空）；postprocess 5.6 合并 `insight_lookup`/`stock_trace_lookup` 注入 user_id
- `src/aistock_agent/skills/registry.py`：注册 `stock_trace_lookup`

### 测试
- `tests/unit/test_skills.py`：stock_trace_lookup 正常/未登录/无数据/异常 + **只读断言**（仅触发 list_stock_traces）
- `tests/unit/test_qa_router.py`：异动词条路由优先于洞察、_infer_stock_skill 分支、postprocess 注入/移除

## [junliang] 2026-08-27 — 自选股洞察读层 skill（阶段 2.1）

**开发者**: Aria

### 新增
- `src/aistock_agent/skills/insight_lookup.py`：对话内查登录用户自选股洞察（涨停雷达/价格异动归因结果，只读）；入参 `{symbol?}`，user_id 由 qa_router postprocess 登录态注入（未登录移除 call），无数据/异常 → degraded
- `src/aistock_agent/services/data_client.py`：`list_insights(openid, symbol?, limit?)` / `get_insight(openid, event_id)` 走 `/internal/insight/events` 只读端点

### 改进
- `src/aistock_agent/schemas/chat_contract.py`：3 处 Literal（InsightGoal.intent / SubGoal.intent / SkillCall.skill_name）加 `insight_lookup` + `ChatSource.kind` 加 `insight`
- `src/aistock_agent/graph/nodes/qa_router.py`：KEYWORD_FALLBACK 加词条（异动/归因/涨停雷达/自选股/洞察）、`_STOCK_SKILLS` 扩展、`_infer_stock_skill` 分支、`_build_default_skill_call` 分支、postprocess 注入 user_id；**footer 白名单动态化**——`_build_system_prompt` 从 registry 实时渲染 `goal.intent` 枚举（占位符 `__INTENT_ENUM__` 替换，新增 skill 无需改硬编码）
- `src/aistock_agent/skills/registry.py`：注册 `insight_lookup`

### 测试
- `tests/unit/test_skills.py`：insight_lookup 正常/未登录/无数据/异常 + **只读断言**（仅触发 list_insights）
- `tests/unit/test_qa_router.py`：关键词路由、_infer_stock_skill、postprocess user_id 注入/未登录移除、footer 动态化断言

## [junliang] 2026-08-27 — 预测验证口径升级 3.0（阶段 0）

**开发者**: Aria

### 改进
- `src/aistock_agent/services/prediction_validator.py`：`_METHODOLOGY_VERSION` 2.0→3.0，`_judge_window` 主判改**窗口累计口径**（bullish sum>0 / bearish sum<0 / neutral mean(|p_i|)<thr；v2"任一日符号命中"保留给存量回补）；`_verify_horizon` 增 `methodology_version` 参数，`baseline_neutral` 随版本（v2: any(|p|)<thr / v3: mean(|p_i|)<thr）
- `src/aistock_agent/services/prediction_stats.py`：`_filter_v2` 参数化（`_CURRENT_METHODOLOGY_VERSION` 默认 2.0 防跳变/混桶），`hit_rate_summary`/`bucket_summary`/`baseline_neutral_summary` 支持传版本观测 3.0 分桶
- `backfill_no_data`：版本判断引用 `_BACKFILL_METHODOLOGY_VERSION`（2.0），存量 2.0/no_data 用 2.0 口径重验、写 2.0 不混版本

### 测试
- `tests/unit/test_prediction_validator.py`：3.0 窗口累计主判用例（bullish/bearish/neutral 反例）+ baseline_neutral 双版本差异 + backfill 版本隔离
- `tests/unit/test_prediction_stats.py`：版本过滤参数化用例（默认 2.0 / 显式 3.0）

## [changer] 2026-08-30 — 节奏大师语义修正 + 调度修复（design-debate）

**开发者**: changer-collab

### 修复
- 分支目标区间（range）锚定突破后空间：bullish `[P, P+Δ]` / bearish `[S-Δ, S]` / neutral `[S, P]`（Δ=半通道宽），触发值=区间边界，替换 `±0.5%` 对称带（消除"站上 P 却给 P±0.5% 带"语义错位）；成交额分支同步（`rhythm_engine.py`）

### 改进
- after_close 调度 cron 由 `5 16 * * 0-4` 改为 `5 16 * * 1-5`：周一至周五 16:05 收盘基准，周五收盘生成下周一预告（修复"周一 after_close 永久缺失"）（`config.py`）

### 测试
- `test_rhythm_engine.py` 新增锚定语义用例（触发值=区间边界、三档互斥）

### 文档
- `README.md` 环境变量表、`AGENTS.md` 定时链路 cron 描述同步

---

## [changer] 2026-08-26 — AI 投顾追问面板：questions 结构化下发（后端链路）

**开发者**: changer-collab

### 新增
- 回答后建议追问（追问面板数据源）：`SynthInsightOutput.questions: list[str] = []`（LLM 结构化输出，prompt 2b 指令生成 2-4 条同主题问句）+ `QuestionState.questions: list[str] | None`（LangGraph 通道声明）+ `ChatResponse.questions`；WS DONE / HTTP 非流式 / SSE DONE（`round_questions`，G1 事件流采集模式）三通道透出
- deep 分支 `_build_deep_questions` 零 LLM 模板化（worker 名骨架；多子目标每节前 2 + 全局截 4）；澄清/闸门/无 goal/degraded 出口恒 `[]`

### 改进
- synth_answer 7 个带 cards 返回点统一写 `questions`；移除结论结尾固定引导句要求与降级引导句文本（G21——问答不重复引导，追问统一走 questions 字段）

### 验证
- `tests/e2e/test_ws_chat.py` DONE 负载断言已同步 `questions` 键（85370a2），原 2 例新增失败清零
- 全量回归 2592 passed / 20 failed：20 例均为合并前基线既有环境类失败（event/hot_burst/sector/iterate/lifespan、full_flow 实栈、AsyncMock 交互等，baseline c1098ec 交叉验证）——新增清零

---

## [changer] 2026-08-25 — LLM 前缀缓存命中观测（可观测先行）

**开发者**: changer-collab

### 新增
- LLM 前缀缓存命中观测（design-debate 裁决「可观测先行」，不做 prompt 重排/前缀冻结）：callback 层归一化提取 OpenAI `prompt_tokens_details.cached_tokens` / astream 路径 `input_token_details.cached_tokens` 与 DeepSeek `prompt_cache_hit_tokens`，按 provider 分桶进 `metrics["llm_cache"]`（`{prompt_tokens, cached_tokens, hit_rate}`）；只做观测不落库、不进计费链（`token_usage.py` / `ws.py` / `node_api.save_token_usage` 字节零改动）

### 改进
- `observability/callback.py`：抽取 `_get_raw_token_usage`（llm_output.token_usage → usage_metadata fallback）供计费与缓存观测复用；新增 `_extract_cache_usage` 字段归一化，无缓存字段不记录不抛异常
- `observability/metrics.py`：新增 `record_llm_cache_hit`（max(x,0) 防负）、快照 `hit_rate`（prompt=0 取 0.0）、reset 同步清零

### 验证
- 定向 38 passed（含 7 个新增缓存命中用例）；全量 unit 2100 passed / 3 failed（checkpointer 基线失败，A/B 验证新增清零）；ruff 0；mypy 0

---

## [changer] 2026-08-25 — 短线情绪温度 + 冰点次日晨报引用预判

**开发者**: Aria

### 新增
- 短线情绪温度（每日收盘 15:45 计算并落盘 `docs/agent-outputs/sentiment/`）：涨停 / 跌停 / 炸板率 / 连板高度 / 涨跌家数比 / 主力净额六指标加权，0-100 分档（冰点 ≤20 / 低迷 / 常温 / 活跃 / 亢奋）
- 冰点判定与连冰升级：温度 ≤20 判冰点，连续 2 日升级连冰标记；冰点触发快速模型生成「修复概率 + 关注方向 + 风险」预判，模型不可用时自动降级为模板话术（不阻断链路）
- 次日晨报联动：冰点 → 注入预判全文与指标概览供晨报结合外盘/消息演绎引用；非冰点 → 注入一行温度概览；无数据 → 不注入（晨报行为与现状一致）

### 改进
- 收盘快照日期与报告日不一致时跳过当日温度落盘，防止旧交易日数据污染连冰计数与次日晨报引用

### 验证
- 全量自动化测试无新增失败（与基线 A/B 对比零回归，含 22 个新增用例）；代码规范检查改动文件 0 新增错误

---

## [junliang] 2026-08-24 — stock_trace 提示词补板块证据要求

**开发者**: Aria

### 改进
- `src/aistock_agent/prompts/workers/stock_trace.py`：sector 候选证据要求——只要上下文中存在板块/行业联动相关 source，sector 候选必须引用至少一条并置 supported 或 weak，不得置 insufficient 且留空支撑证据；仅当上下文完全不存在板块相关 source 时才允许 insufficient

## [changer] 2026-08-24 — 搜索链路顺序可配 + 搜索观测 + 午报语音播报 + 盘中报

**开发者**: Aria

### 新增
- 工作日盘中报（12:05 生成「上午盘面回顾 + 午后前瞻」，仅大盘，存档可查不推送；复用晨报结论 + 新闻 + 外盘 + 搜索组装式，快速模型生成控制盘中算力占用）
- 午报双人语音播报（12:15 错峰于盘中报落库后：生成分析师 + 主持人双人对话并合成 MP3 音频，回填到当日午报，不产生独立广播报告、不混入晨间/晚间播报）
- 搜索链路观测计数：按供应商统计搜索尝试 / 失败 / 预算耗尽 / 空结果，通过 /metrics 端点暴露，便于定位搜索链路异常（如某供应商配额耗尽）

### 改进
- 搜索链路顺序可配置：供应商优先级由配置决定（可配为 AnySearch 优先），不再固定 Tavily 优先；空配置保持默认顺序，重复配置自动去重
- 交易时段行情回答降级文案诚实化：去掉反问句与误导性措辞，改为自洽陈述并显式标注「非今日实时」（非实时数据时）

### 验证
- 全量自动化测试无新增失败；代码规范与类型检查通过

---

## [junliang] 2026-08-20 — stock_trace 归因新增 primary_phrase 短语输出

**开发者**: Aria

### 改进
- `src/aistock_agent/schemas/stock_trace.py`：`StockTraceResultPayload` 新增必填 `primary_phrase`（≤20 字归因短语，供列表/卡片展示；insufficient 时给简短结论如"证据不足"）
- `src/aistock_agent/prompts/workers/stock_trace.py`：提示词新增 primary_phrase 输出要求（关键词概括主因短语）

### 测试
- `tests/test_stock_trace_validator.py`：用例补 `primary_phrase` 参数

### 文档
- `AGENTS.md`：更新归因输出字段说明

### 验证
- `pytest tests/test_stock_trace_validator.py` 通过

---

## [junliang] 2026-08-15 — 自选股价格异动归因：stock_trace_consumer 默认启用 + 五层候选归因

**开发者**: Aria

### 改进
- `config.py`：`stock_trace_consumer_enabled` 默认值改为 `True`（此前默认 False，需显式启用）

### 新增
- 五层候选归因：`schemas/stock_trace.py` 新增 `capital`（资金流向）与 `technical`（技术指标）两层候选 schema，`_validate_selected_chain_shape` 要求候选覆盖五层；`prompts/workers/stock_trace.py` 提示词扩展为五层（company/sector/market/capital/technical）；`services/stock_trace_validator.py` 的 confirmed 门槛保持 company 主候选

### 验证
- `pytest tests/unit -q`：回归通过；ruff 0 errors

---

## [changer] 2026-08-19 — 搜索引擎多供应商故障转移链（Tavily + Doubao + AnySearch）

**开发者**: 37588

### 新增
- 全网搜索升级为多供应商故障转移链：Tavily 主源 + Doubao（火山每月 500 次）+ AnySearch（每日 1000 次 finance 域）按 tavily→doubao→anysearch 顺序失败切换，整链预算 fail-fast；Tavily 多 key 健康感知池（熔断冷却 + 限流固定窗口 + 全冷却 fail-open），Doubao/AnySearch 惰性注册（未配 key 不占位）
- 搜索溯源透传：`TavilyService.search` 返回加性 `provider`（真实命中源）/`outcome`（ok/degraded/empty/error）键，快照与事件采集的 `source` 从硬编码 tavily 改为读真实 provider，`SourceCollectionStatus.state` 新增 `degraded`（低质兜底可观测）
- 配置：`DOUBAO_API_KEYS` / `ANYSEARCH_API_KEYS` / `SEARCH_ENABLED_PROVIDERS`（空=默认全部）/ `SEARCH_BUDGET_SECONDS`（默认 10s）

### 改进
- async 内同步阻塞的搜索调用全部下沉 `asyncio.to_thread`（快照 4 处 + `tavily_finance_search` 工具），并新增 AST 契约测试防止回归裸同步调用
- 工具输出契约回归锁定（`tavily_finance_search` 输出格式逐字节稳定）

### 修复
- KeyPool 健康状态跨请求持久化（模块级缓存，冷却/熔断不随请求重置）；fail-open 选「距上次失败最久」的 key
- 命中结果缺 url 时输出空串（避免「来源: None」）；Doubao `Result` 字段非 dict 解析守卫

### 验证
- 定向 87 passed + 全量 2454 passed（24 个基线环境失败，与本次零交集）；ruff 0；mypy strict 0
- 待生产验证：Doubao/AnySearch 真实 key 联调已由人工完成

---

## [changer] 2026-08-18 — 复盘报告生成偶发失败加固（市场洞见数据恢复）

**开发者**: 37588

### 修复
- 复盘溯源报告生成时，AI 输出的部分校验字段取值偶发越界（如把"部分命中"填进不支持该取值的字段）会导致整份报告生成失败——现对越界取值做归一化兜底，不再拖垮整份报告
- 复盘溯源生成对 AI 单次输出增加一次重试兜底：AI 偶发返回空内容或非法格式时自动重试一次，避免整份报告不可用

---

## [changer] 2026-08-17 — 深度分析结果跨轮陈旧值修复 + 对话结果一致性（批次 5）

**开发者**: 37588

### 修复
- HTTP 非流式对话（WS 降级路径）改为从本轮末节点输出取结果，消除普通问题轮泄漏上一轮深度分析的陈旧引用；SSE 流式完成事件同步补齐深度分析引用与结构化卡片字段（与 WS 主路径对齐）
- 对话澄清分支不再携带结构化卡片与深度分析引用（避免图文信息打架）

### 新增
- 三层测试：SSE 流式完成事件字段契约（键存在且为空）、回复生成节点返回契约、真实图 + 内存检查器两轮集成验证（深度轮→普通轮后深度引用仍保留，多轮追问不退化）

### 验证
- 相关测试 14/14 通过

---

## [changer] 2026-08-17 — LLM 请求级耗时埋点（诊断「等很久」根因）

**开发者**: 37588

### 背景
「等很久没回答」根因未坐实。design-debate 两轮裁决：连接池修复有效但只解决泄漏，不解决推理慢/
争用；真正的 149s「静默黑洞」在队列/上游/双 graph 三者间未定，缺 LLM 请求级耗时证据。

### 修复
- `src/aistock_agent/observability/callback.py`：新增 `LatencyCallback`——挂载到所有 ChatOpenAI
  工厂（get_quick_think/get_deep_think）默认回调，记录请求级指标：
  - `llm.call.duration`：每次 LLM 调用总耗时（覆盖非流式 ainvoke）
  - `llm.call.first_token`：首 token 延迟（流式链路，可区分「上游慢」vs「池排队」）
  - `llm.call.error`：失败时总耗时 + 异常类型（ReadTimeout/ConnectError/RemoteProtocolError，区分超时 vs 连接异常）
- `get_default_callbacks()` 现返回 3 个 handler（TokenUsage + AgentTrace + Latency），全局生效，
  不侵入业务代码（回调链注入）

### 验证
- 新增 6 个单测（`test_observability_callback`：duration/first_token/error/缺失 start 不崩/注入）
- 改动相关单测 53 passed；ruff 0；mypy 0 新增
- 注入链路实测：get_quick_think() 生成的 model 挂 3 回调 + http_async_client

### 待生产验证
- 部署后重复对话，观察日志 `llm.call.*`，据 total_ms/first_token_ms 定位 149s 黑洞：排队（start→首
  token 大）vs 上游慢（first_token 大）vs 双 graph 叠加（同消息多条 duration）

---

## [changer] 2026-08-17 — LLM 连接池泄漏修复 + WS 悬挂止血（问题 20 延续）

**开发者**: 37588

### 背景
线上偶现聊天 WS"一直转圈"（前端收不到 done）+ 偶发 500。现场 `ss` 观测：agent 进程向
DeepSeek（api.deepseek.com / 43.242.198.77:443）累积 **50+ 条 CLOSE-WAIT**、fd 增至 85
（正常 <20），每次对话只增不减 → 连接池泄漏 → 偶发阻塞 LLM 调用 → `synth_answer.ok` 后
producer 悬挂 → `_runner` finally 不执行 → `state.done` 永 False → `_forward` 无限 await →
前端永久转圈。

### 修复
- `src/aistock_agent/services/http_client.py`：新增 `LlmHttpClient`——LLM（DeepSeek/ChatOpenAI）
  专用 httpx.AsyncClient 单例，带 `httpx.Limits(max_connections=20, max_keepalive_connections=10)`
  （显式限定连接/keep-alive 上限，杜绝 CLOSE-WAIT 无限堆积）；init/client/close 幂等
- `src/aistock_agent/services/llm.py`：`get_quick_think()`/`get_deep_think()` 注入
  `http_async_client=LlmHttpClient.client()`（原实现每实例新建 httpx client 且无回收）
- `src/aistock_agent/main.py`：lifespan 启动 `LlmHttpClient.init(timeout=600)`、关闭 `close()`
- `src/aistock_agent/api/ws.py`：`_forward_until_done_or_cmd` 新增**静默段看门狗**
  （`_FORWARD_STALL_TIMEOUT_SEC=240`）——events 长度无新增且 recv 无新消息持续超阈值 →
  主动 `chat_task_manager.cancel(session_id)` + 补发 error「生成超时，请重试」，再由 `_runner`
  终态 notify 补发 cancelled，保证前端绝不无限转圈；finally 补 `await asyncio.gather` 收尾
  （对齐"问题18"规范）
- 测试：
  - `tests/unit/test_llm.py`：新增连接池共享/受限断言（quick/deep 共用同一单例 + Limits 生效）
  - `tests/unit/test_ws_chat_replacement.py`：新增看门狗测试（悬挂 → cancel + error 终态）

### 验证
- 全量 unit 1987 passed；ws/chat 集成 17 passed
- ruff 改动文件 0；mypy 与 baseline 一致（20 pre-existing，无新增）
- app import + `http_async_client` 共享单例确认

### 待生产验证
- 部署后重复对话，`ss -tnp | grep CLOSE-WAIT | grep 43.242.198.77` 计数应稳定不再增长
- 聊天转圈 / 500 应消除（若另一用户 500 为同源连接池问题则一并解决；否则独立排查）

---

## [changer] 2026-08-16 — 对话卡死恢复止血（问题 20）

**开发者**: 37588

### 修复
- `src/aistock_agent/api/ws.py`：主循环 `except WebSocketDisconnect` → `except (WebSocketDisconnect, RuntimeError)`（disconnect 被 recv_task 消费后再 receive 抛 starlette RuntimeError → 不再崩溃刷 error log）；非 "receive" 的 RuntimeError 打 `chat.ws_main_loop_runtime_error` warning 保留可观测性
- `src/aistock_agent/services/chat_task_manager.py`：`ChatRunState.finalizing` 护栏（cancel 在 finalizing/done 时返回 False，防前端超时 stop 误杀将成之轮）+ `_RUN_TOTAL_TIMEOUT_SEC=660` 总时长兜底（`asyncio.timeout` 内联执行 producer，超时 → ERROR 终态「生成超时，请稍后重试」）
- 测试：`tests/unit/test_ws_chat_replacement.py`（RuntimeError 捕获回归）+ `tests/unit/test_chat_task_manager.py`（finalizing 护栏 2 例 + 总时长兜底 2 例）

### 验证
- 全量 A/B HEAD 27 failed = BASE 27（新增清零）+ ruff 0

### 配套（前端 aistock-app-frontend，同批）
- useChatStream idle 超时兜底（见 frontend changelog）

---

## [changer] 2026-08-15 — 预测验证口径升级 v2（B2.2 P0）

**开发者**: changelog

### 新增
- 指数日 K 客户端 `get_index_kline`（GET /internal/index/:code/kline，Tushare index_daily 历史窗口，P0 验证 v2 数据源）
- 预测验证统计模块 `prediction_stats.py`：Wilson 95% CI + 命中率汇总（methodology_version=2.0 分桶、insufficient/approximate 剔除）+ baseline 同口径对比；独立 scheduler 任务输出结构化日志（D3 真实消费方）
- target 枚举外置 `prediction_targets.py`：`classify_target` 四类分类（index/sector/stock/unknown），sector/stock 归 insufficient 且 reason 区分，unknown 保留抽象词漂移信号（P0-2 target 分布监控）

### 改进
- 验证器重写 v2 窗口判定（[due, due+3 交易日] 符号命中主判，无累计净值兜底 G13）：grade 幅度分级（仅 bullish/bearish，G14）、baseline_neutral 同窗口标记（H6）、approximate 结构化标记（H2）；窗口未满返回 wait 不回写（D1）、数据源故障落 insufficient（D7）
- chat 预测后处理红线硬校验（P0-3）：`_contains_absolute_point` 覆盖 metric_projection/evolution_narrative/attribution_summary 全文本字段（含 D5 裸数字点位正则），命中剥离 + 独立日志事件 `hard_validation_failed`，不静默
- schema_version 升 2.0 四向同步（D6）：schemas/prompts/prediction_service 兜底/测试构造

### 修复
- 全量回归修复计划引入的 4 处失败：replay 隔离名单补 `get_index_kline`/`list_verified_predictions` 登记；test_prediction_prompt/test_review_prediction/test_evening_chain_event_driven 的 schema_version 断言与 fixture 同步 2.0

---

## [changer] 2026-08-14 — 预测到期日越年逐档容错

**开发者**: changelog

### 修复
- 大盘溯源影响持续性预判：到期日不再因 chinese_calendar 覆盖（2004-2026）越年而整条落 skipped（due_dates_failed）——改为逐档容错，越年档按「周末+已发布节假日(HOLIDAYS_EXTRA)」近似计算并显式标记 `due_dates_approximate`（wire 键，Node 合并进 prediction jsonb），其余档精确
- 删除 `DueDatesComputationError` / `due_dates_failed` 状态（`PredictionRunResult.status` Literal 移除），`event_consumers` 同步
- 到期验证器：近似档 reason 加 `(approximate_due_date)` 前缀，供统计分桶归因

### 说明
- 理由（P2 辩论裁决）：验证器对照扫描日单日涨跌幅符号（低信噪比），精确日历无统计增益；显式标注优于预测停产；2027 官方节假日 2026-11 发布后经 HOLIDAYS_EXTRA 注入或 chinese_calendar 升级自动恢复精确

---

## [changer] 2026-08-14 — SPEC 设计文档忽略规则

**开发者**: changelog

### 改进
- `.gitignore` 新增 `docs/*-SPEC.md` 忽略规则：SPEC 设计文档不提交不推送，仅本地维护

---

## [changer-prediction-split] 2026-08-14 — 大盘溯源影响持续性预判独立成模块 + 跨年日期可靠性

**开发者**: changelog

### 新增
- 影响持续性预判独立成模块：复盘完成后自动触发生成，独立状态追踪（进行中/已跳过/已完成），不再依赖复盘流程内联执行
- 按需补偿接口：支持手动触发当日预判生成（防重复覆盖保护 + 频率限制 + 仅限当日）
- 补充节假日数据源配置：跨年到期日计算精度提升，2027 数据发布或日历库升级后自动恢复精确

### 修复
- 大盘溯源页预判卡片空态（统一读取预判记录数据）
- 跨年日期计算失败由静默降级改为显式失败状态与告警，不再静默产出近似日期

### 改进
- 预判生成结果状态化（门禁跳过/生成失败/解析失败/日期计算失败/成功），瞬时失败自动重试一次
- 无效预判记录落"已跳过"状态，不计入进行中统计；大盘溯源页展示空态占位文案

---

## [junliang] 2026-08-06 — 自选股洞察：LLM 归因 category 字段兼容 + 归因链路联调修复

**开发者**: Aria

### 修复
- `src/aistock_agent/schemas/insight.py`：`DriverOutput` 增加可选 `category` 字段（LLM 常回传候选分类，此前 `extra=forbid` 直接拒绝导致归因全部回退规则兜底、LLM 路径失效）
- `src/aistock_agent/services/insight_validator.py`：校验 LLM 回传的 `category` 与所选候选分类一致，不一致拒绝（分类权威在候选，防 LLM 注入分类）
- `src/aistock_agent/prompts/workers/insight.py`：提示词新增第 6 条输出字段白名单（每个 driver 只允许 candidate_id/label/confidence/category）

### 文档
- `docs/自选股洞察-PRD.md`：更新事件归属规则——事件股票必须与标题主体股票一致，详情页推荐股票不建事件（修复事件挂错标的，如国投中鲁文章被挂到中芯国际事件）

---

## [main] 2026-08-06 — 修复存量 unit 测试失败 + 清理遗留测试文件

**开发者**: Aria

### 修复
- `services/scheduler.py`：`_publish_review_quick_event` / `_publish_review_full_event` 的 trace_id 由 `asyncio.get_event_loop().time()` 改为 `time.monotonic()`，消除对"当前事件循环"的隐式依赖（同步/多线程场景无 loop 会抛 RuntimeError）
- `graph/nodes/qa_router.py`：`_resolve_multi_symbols` 在 `_extract_multi_symbols` 返回空（候选 <2 约定返回 []）时按正则补全消息中显式给出的 6 位代码，修复 "600519 和五粮液哪个更好" 对比闸门无法短路的问题
- `tests/unit/test_skills.py`：3 个 normal + 3 个 exception 测试改用 `mock.ainvoke.return_value / side_effect` 配置（原 `AsyncMock(return_value=...)` 只作用于 mock 自身，`.ainvoke` 子 mock 拿不到导致 degraded/coroutine 报错与假通过）；stock_snapshot normal 测试补充 node_api.get 与交易时段 mock，消除真实时间依赖
- `tests/unit/test_qa_router.py`：`test_qa_router_llm_single_validate_collapses` 补充 `get_quick_think` mock（缺 mock 时真实调用 ChatOpenAI 抛 OpenAIError 走兜底，断言 KeyError）
- `tests/unit/test_industry_vector_search.py`：2 处降级断言由旧文本"数据暂不可用"更新为当前 `DEGRADED_MESSAGE`（safe_tool_call 稳定契约）
- `tests/unit/test_qa_briefing.py`：morning 前置报告补 `trend_score` mock（`_REQUIRED_TYPES["morning"]` 已含 trend_score）
- `tests/unit/test_scheduler.py`：`test_start_scheduler_explicitly_passes_configured_timezone_to_cron` 显式创建/清理事件循环，消除全套运行时的 loop 顺序污染

### 清理
- 删除 `tests/unit/test_tenx_tools.py`（tenx_tools.py 已被 trend_tools.py 替代移除，遗留测试文件）
- 清理 5 个测试文件的 ruff 存量警告（E402/E501/F401/I001 等，21 处）

### 验证
- `pytest tests/unit -q`：1171 passed（修复前 1162 passed + 9 failed + 1 collection error）
- `pytest tests/e2e/test_chat_message.py -q`：4 passed
- ruff：全部改动文件 All checks passed

---

## [changer] 2026-08-06 — WS 路径 token_usage 时序修复 + HTTP 降级补返回 + 服务端口 8000→8080 对齐

**开发者**: Aria

### 修复
- `api/ws.py`：astream_events config 传入 `get_default_callbacks()`（astream_events 不触发 LLM 构造函数 callbacks=）；循环结束后 `await asyncio.sleep(0)` yield 事件循环让延迟 on_llm_end 回调执行，再从 contextvar 刷新 token_usage 覆盖 stale None——根因：LangGraph v2 异步回调延迟，synth_answer 节点执行期间 contextvar 尚未写入，DONE 事件恒 None
- `observability/callback.py`：清理 DEBUG print；`_extract_token_usage` fallback 失败日志改为 `logger.debug`
- `services/token_usage.py`：清理 DEBUG print
- `api/routes.py` + `schemas/chat.py`：HTTP 非流式 `chat_message` 补返回 `token_usage`（降级路径用量缺口，P10 线 2；c926e9d + 8944635）；e2e 新增 `test_chat_message_returns_token_usage_when_graph_provides`

### 改进
- 服务端口 `8000`→`8080` 对齐 app-api `AGENT_PY_URL` 默认值（app-api 已在 2026-08-05 改 8080，agent-py config 落后导致反代错端口）
- `config.py`：`port: int = 8080` + 注释
- `Dockerfile`：`EXPOSE 8080` + `CMD --port 8080`
- `README.md` / `AGENT_STANDARDS.md`：启动命令与 docker run 端口同步更新

### 验证
- WS 直连冒烟 `token_usage={'prompt_tokens': 455, 'completion_tokens': 353, 'total_tokens': 808}`（3 次 LLM 调用之和，非翻倍）
- 43 单测全绿；HTTP 路径 e2e 回归通过

---

## [main] 2026-08-06 — 晚报结论重构：归因结论放头条，三条均为 30-40 字一句话（去冒号）

**开发者**: Aria

### 改进
- `services/briefing.py`：晚报三条顺序调整为 归因结论（主因链）→ 市场快照 → 收盘复盘；归因结论去掉"触发：/传导：/结果："阶段标签与冒号，confirmed 句式"今日市场主因是{…}"、hypothesis 句式"今日市场可能受{…}等因素影响"；市场快照改为"今日X涨4.21%、…，Y跌2.13%、…"一句话（含"无显著领跌/领涨板块"降级分支）；删除不再使用的 `_STAGE_LABELS`
- `services/phenomenon_discovery.py`：`_SUMMARIES` 五个现象文案扩写为 30-40 字一句话（收盘复盘条目摘要来源，无冒号）

### 修复
- `agents/workers/review.py`：`_extract_trace_summary` 优先提取"确认的市场现象"段的 `- 摘要：xxx` 行（易读中文现象描述），不再取到"类型：broad_rally"内部字段行；保留旧格式回退

### 测试
- `tests/unit/test_briefing.py`：更新 sectors/attribution 契约断言为无冒号一句话、`missing_sources` 顺序随新 variant 顺序调整、新增晚报头条顺序断言；`tests/unit/test_review_report.py` 新增 `_extract_trace_summary` 3 个测试；相关测试 60 passed

---

## [master] 2026-08-05 — cls_news/main_force 缺失诊断日志（agent-py）

**开发者**: NanyuDeer

### 新增
- `services/market_trace_snapshot.py` `_normalize_news_facts`：新增三种缺失场景结构化日志 `cls_news_missing_fetch_error` / `cls_news_missing_empty` / `cls_news_missing_invalid_for_causality`（含 raw_item_count、kept_count、skipped_future、skipped_no_time），成功时输出 `cls_news_available`
- `services/market_trace_snapshot.py` `_normalize_aggregate_facts`：新增 `main_force_invalid` 日志（is_quick、availability_state、availability_reason、value），区分 quick 快照预期缺失（Tushare 未就绪）与异常缺失
- `services/market_trace_snapshot.py` 新增 `_log_telegraph_response` 辅助函数：记录 telegraph 接口返回 item_count、total、degraded（兼容 `{date,items,total}` 与 `{code,data}` 两种结构），full/quick 两条路径均接入

### 说明
- 目的：解决 grep cls_news|main_force 无输出问题，后续运行可在 pm2 日志中定位确切根因

---

## [master] 2026-08-05 — 测试同步：event_conduction 返回结构变更（PR #52 回归修复）

**开发者**: NanyuDeer

### 修复
- `tests/unit/test_event_conduction_service.py`：断言适配 `EventConductionOutput.status` 新结构（`result.success` → `result.status.success` 等），import 改为 `EventConductionOutput`
- `tests/test_routes_briefing.py`：event conduction mock 返回改为 `EventConductionOutput(status=EventConductionResult(...))`（3 处），修复 PR #52（EventConductionOutput 包装类）引入的 8 个测试回归

### 测试
- `test_event_conduction_service.py` + `test_routes_briefing.py`：43 passed

---

## [master] 2026-08-05 — market_snapshot 板块命中率失真修复（粗/细粒度名称对齐）

**开发者**: Aria

### 修复
- `services/snapshot_builder.py`：`match_sectors_code_level` 在别名字典精确匹配之外新增**双向包含匹配**（`_norm_sector_name` 去"概念/板块/行业/指数"后缀 + `_has_contains_match` 双向子串判断），解决 morning 粗粒度预测板块（AI/CPO/半导体）与 review 行情细粒度概念（存储芯片/光刻机）字面完全无交集导致的 hit_rate=0、new_coverage=1 失真
- `data/sector_aliases.json`：补齐高频缺口——"AI/CPO/半导体"（映射到半导体/AI光模块）、"白酒概念"→白酒、"CRO/医药"、"医药电商"→医药、"MLCC概念"→电子元器件

### 测试
- `tests/unit/test_sector_matching.py`：新增 2026-08-05 实况回归（morning 5 板块 vs review 10 概念，命中率 0.00→0.40）+ 包含匹配兜底 + 不相关板块不误判；清理顶部未使用 import

---

## [master] 2026-08-05 — 新增"一键补跑完整晚间链路"端点

**开发者**: Aria

### 新增
- `api/routes.py`：`POST /api/agent/admin/trigger/evening_chain`（内网 token 鉴权）——一键补跑完整晚间链路（review → market_snapshot → iterate → evening Brief → broadcast），供错过 15:30 调度或灰度验证时使用；显式传 `report_date` 时跳过交易日检查；返回各阶段状态 `stages` 供诊断

### 改进
- `services/scheduler.py`：`_run_evening_chain_task` 增加可选 `report_date` 参数（缺省走原交易日检查逻辑，向后兼容）并返回各阶段状态 dict（review/market_snapshot/iterate/brief/broadcast 与失败 stage/error 信息）

### 测试
- `tests/test_admin_trigger.py`：新增 `test_trigger_evening_chain_returns_200`
- `tests/unit/test_scheduler.py`：新增 3 个测试（显式日期跳过交易日检查 / 缺省日期非交易日返回 skipped / review 失败返回 failed+stage）

---

## [changer] 2026-08-05 — ChatAgent P10 线 2 用户计费（token_usage）+ P11 线 3 后端卡片（cards）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\chat-agent-roadmap.md` §1 P10/P11 行

### P10 线 2（用户维度计费，billing）
- `services/token_usage.py`（新增）：contextvar 累加器 `TokenUsageAccumulator`/`TokenUsageContext` + 模块级 `reset_token_usage`/`get_token_usage`/`record_token_usage`；contextvar 随 `asyncio.create_task` 继承（`TokenUsageCallback` 挂在 ChatOpenAI callbacks= 无法访问节点 state，ws 后台图任务与节点内 LLM 调用同 context 副本）；`observability/callback.py` `on_llm_end` 在 record_llm_tokens 后追加 record_token_usage（非 chat 场景零副作用）
- `graph/nodes/synth_answer.py`：原节点改名 `_synth_answer_node_core`，新增包装 `synth_answer_node` 统一附加 `result["token_usage"] = get_token_usage()`（与 cards 汇总块分居两层，合并友好）
- `api/ws.py`：入口 `reset_token_usage()` 按轮重置；on_chain_end 一次性捕获 token_usage + cards；`_drain_reasoning_tasks` 后、DONE 前**落库（选项 A）**——user_id 非空且 token_usage 非空 → `node_api.save_token_usage`（try/except + warning 不阻断 DONE）；DONE 负载新增 `token_usage` + `cards`（None 默认）
- `api/routes.py`：`chat_message` / `chat_stream_messages` 入口 `reset_token_usage()`；SSE DONE（`_stream_messages`）从 final_state.values 附带 token_usage + cards（仅展示不落库）；HTTP 非流式路径只重置不消费
- `services/data_client.py`：`save_token_usage(*, user_id, session_id, prompt_tokens, completion_tokens, total_tokens, question=None)` → `POST /internal/usage/records`（app-api）

### P11 线 3（后端卡片结构化，cards）
- skills raw 结构化字段：stock_snapshot `raw["quote"]`（`_QUOTE_FIELD_MAP`）、capital_flow `raw["flow"]`（`_FLOW_FIELD_MAP`，flow_5d 恒 []）、market_snapshot `raw["a_share_card"]`（`_build_a_share_card`，仅 scope 含 a_share）、compare_stocks `raw["parsed"]`（available True/False 条目）；get_quote/get_capital_flow TEXT 输出冻结不变
- `graph/nodes/synth_answer.py` `_synth_answer_node_core` 每个 return 带 cards：no_goal/澄清/闸门/异常 → None；deep → `_build_deep_card`；LLM 成功与 `_synth_multi_goal` → `_build_cards`（`_CARD_HANDLERS` 按 skill_name 分派 + 逐卡片 try-except 跳过）；包装 `synth_answer_node` 不动
- 契约：`schemas/chat_contract.py` `ChatCard`（card_type Literal 5 值 + title + data，extra="forbid"）+ `QuestionState.cards`/`token_usage`（B-T1 定义，P11/P10 共享）

### 测试
- 新增：`test_token_usage` / `test_data_client_save_token_usage` / `test_synth_answer_token_usage` / `test_ws_token_usage_record` / `test_routes_sse_done_token_usage`（P10 线 2）；`test_chat_card_contract` / `test_stock_snapshot_raw` / `test_capital_flow_raw` / `test_market_snapshot_card` / `test_compare_stocks_parsed` / `test_synth_answer_cards`（P11 线 3）
- 适配：`test_ws_chat_replacement` / `test_ws_chat`

### 文档
- AGENTS.md 补 CHAT QA P10+P11 段；CHANGELOG.md 本条目；project_memory.md 经验教训 #30

### 验证
- Commits：P10 线 2 `d3d772c`/`114ee07`/`da26a81`/`0e3b5b4`/`42a6524`/`d98692a`/`9142de3`；P11 线 3 `fcfcf5a`/`5f9d6ab`/`4c82bc8`/`fe56222`/`2b1ec00`/`83fea5d`；线间 merge `4d42fe7`

---

## [changer] 2026-08-05 — ChatAgent P5-fix 验收补丁（对比问句短路 / 名称候选净化 / 多轮指代兜底）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\chat-agent-roadmap.md` §1 P5-fix 行 / §4 问题 8/11/14

### 修复
- 问题 8（对比问句被闸门 2 澄清拦截）：`_STOCK_NAME_STOPWORDS` 补对比口语词（哪个/更好/更强/比较/对比）；新增 `_COMPARE_KEYWORDS` 增强对比词表 + `_extract_multi_name_candidates`（按"和/与/还是/vs"分隔符切分逐段提取名称）+ async `_resolve_multi_symbols`（过滤非 6 位代码候选）；**对比闸门 2.5 独立于闸门 2 且在其之前**（含代码对比句"600519 和五粮液哪个更好"短路 compare_stocks，避免落 LLM flaky）；`route_by_keyword_fallback` 对比分支仅接受纯代码
- 问题 11（候选名被口语词污染 resolve 404）：停用词补意图词/连接词（新闻/资讯/消息/公告/有/是/说/它/这/那，与 `_infer_stock_skill` 对齐）
- 问题 14（多轮指代失效）：qa_router LLM 失败路径新增多轮指代兜底——`len(messages)>=3`（有上一轮）+ 当前消息含指代词（它/这/那/该/其/刚才/上次/这只/那只）时从上一轮 resolve symbol 复用（`_infer_stock_skill` 推断意图，`multiturn_ref` 约束标记），不落澄清；守卫防"帮我推荐股票"等误指代

### 测试
- 新增 `tests/unit/test_qa_router_fix.py` 12 项（对比闸门短路/名称净化/多轮兜底 3 守卫：复用/无标的守卫/首轮守卫）；qa_router 全量 137 passed；ruff 0 errors
- WS 冒烟（真实后端）：宁德时代新闻 / 茅台五粮液对比 / 3 组多轮指代全部 clarified=false

### 文档
- AGENTS.md 补充 CHAT QA P5-fix 段（含"qa_router 单测必须 mock LLM 防 flaky"测试注意）

---

## [changer] 2026-08-04 — ChatAgent P6 退役清理（ai_advisor / market-trace-qa / advisor_trace）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-04-chat-agent-p6-retirement.md`

### 重构
- 退役 market-trace-qa 端点：`services/market_trace_qa.py`（400 行）原位瘦身改名 `services/trace_loader.py`（仅保留 `load_validated_trace`，供 trace_lookup skill 使用）；`schemas/market_trace_qa.py` / `prompts/workers/market_trace_qa.py` / `test_market_trace_qa.py`（unit+e2e）删除
- 退役 ai_advisor worker / prompt / 路由 / constants（`agents/workers/ai_advisor.py` 477 行 + prompt + `test_ai_advisor.py` 924 行删除；`intent_router.py` VALID_INTENTS 收敛，未知意图 → general 兜底；旧图 `compile_graph()` 保留供 `/briefing/morning`）
- 移除 `advisor_trace` 协议字段（**消失语义**，`"advisor_trace" not in payload`，非 null）+ 孤儿 `AdvisorTrace`/`AdvisorSubquestionTrace` TypedDict（T3 review 补删）

### 测试
- 全量 A/B 回归（worktree 6f51d89 + PYTHONPATH 覆盖 editable install，--ignore test_tenx_tools）：HEAD 27 failed ⊆ BASE 34 failed，**新增失败清零**；ruff P6 改动文件 0 errors（--no-cache）

### 文档
- AGENTS.md / README.md 零 ai_advisor 可达引用（图拓扑 / 产品映射表 / 目录结构 / 降级文本表 / 双层输出消费方）

---

## [changer] 2026-08-04 — ChatAgent P5 能力补齐（D40-D42 三 skill + index_snapshot + P4 遗留优化）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-04-chat-agent-p5-capability.md`

### 新增
- `src/aistock_agent/schemas/chat_contract.py`：`InsightGoal.intent` / `SubGoal.intent` / `SkillCall.skill_name` 3 Literal 各追加 `compare_stocks` / `stock_history` / `trend_ranking` / `index_snapshot`（extra="forbid" 不变）
- `src/aistock_agent/skills/compare_stocks.py`：D40 多标的并发对比（`asyncio.gather` 并发 `get_quote.ainvoke`，2~5 标的，部分失败不整条丢弃，仅个股语义）
- `src/aistock_agent/skills/stock_history.py`：D41 个股日 K 区间（`/internal/quote/{symbol}/kline`，`近N天` 确定性短路）
- `src/aistock_agent/skills/trend_ranking.py`：D42 趋势股 Top 榜（`/internal/trend/top`，空榜 degraded）
- `src/aistock_agent/skills/index_snapshot.py`：对话快速指数快照（`/internal/index/quotes`，绕开 quick 全市场 33s 慢路径，部分 null 不整体 degraded）
- `src/aistock_agent/graph/nodes/qa_router.py`：KEYWORD_FALLBACK 对比/历史/排行词条 + `_extract_multi_symbols` + `_DAYS_RE 近N天` 短路（`_match_other_skill_intent` 排除其他意图词）+ 闸门 1 A 股指数名路由（`_INDEX_SNAPSHOT_CODES` → index_snapshot，恒生/大盘维持 market_snapshot）+ D27 白名单（compare/stock_history/trend_ranking 参数归一）
- `src/aistock_agent/skills/registry.py`：注册 4 新 skill

### 修复
- P4 遗留 ①：闸门 1/2 单意图预测附加收紧为 `_STRONG_PREDICT_KEYWORDS`（弱词仅闸门 4 候选注入，消除"茅台明天的新闻"误附加）
- P4 遗留 ②：兜底 `_build_fallback_goals` 同标的 validate+predict 只发一条取数 call（`seen_calls` 去重）
- P4 遗留 ③：兜底 trace 子目标改走 `trace_lookup`（溯源数据而非 validate 快照）

### 测试
- 新建 test_skills_compare_stocks / test_skills_stock_history / test_skills_trend_ranking / test_skills_index_snapshot + test_qa_router 触发/迁移用例（§2.6 消歧五行全覆盖）
- 目标单测 122 passed；全量 pytest A/B（worktree 24830c5 + PYTHONPATH）：HEAD 28 failed ⊆ BASE 28 failed（新增失败清零），passed 1440→1496；ruff 改动文件 0 errors

### 文档
- `AGENTS.md`：Node 配合接口表补 `/internal/quote/:symbol/kline` + `/internal/index/quotes`；CHAT QA P5 小节（4 skill + §2.6 消歧硬边界）
- roadmap §1 P5 行（✅ 11/11）、§2 P5 小节、§5.5 验证记录、§4 两项调研遗留已完成

---

## [changer] 2026-08-04 — ChatAgent P4 多意图（D34 goal→goals）+ 维度预筛（D30 闸门 4）+ 预测维降级（D35）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-03-chat-agent-p4-multi-intent-dimension.md`

### 新增
- `src/aistock_agent/schemas/chat_contract.py`：`SubGoal`（id/question/intent/dimension/symbols/tag_codes/time_range，extra=forbid）；`SkillCall.goal_id` / `Evidence.goal_id` / `AnswerTrace.goals` / `QARouterOutput.goals`（默认 None）
- `src/aistock_agent/graph/nodes/qa_router.py`：D30 闸门 4 维度预筛（`_DIMENSION_KEYWORDS` predict/trace/validate + 候选集提取 + prompt 注入 + LLM 失败兜底增强）；D27 goals 后处理（id 重编号 g1..gN、goal_id 归一、单非预测坍缩回单意图、goal 投影第一个子目标）；D35 单意图预测附加（闸门 1/2 短路命中 predict 词时附加 predict 子目标）
- `src/aistock_agent/graph/nodes/skill_executor.py`：`SkillCall.goal_id` 透传到 `Evidence.goal_id`（None 不覆盖）
- `src/aistock_agent/graph/nodes/synth_answer.py`：`state.goals` 非空时按子目标分节回答（先 validate/trace 现状数据后 predict 提示）；`_synth_multi_goal` / `_synth_section` / `_build_predict_section`；D35 预测降级提示（`PREDICT_DEGRADED_HINT` 代码生成、多个 predict 只输出一次、不编造预测）
- `src/aistock_agent/prompts/general/system.py`：`PREDICT_DEGRADED_HINT = "预测功能开发中，可先查看当前趋势分析。"`
- `src/aistock_agent/state/chat_schema.py`：`QuestionState.goals`；`api/ws.py` / `api/routes.py` 入口 goals 每轮归零（单轮 transient）

### 修复
- `qa_router.py` 兜底链整体 try-except（最终审查 M-1：异常回落关键词兜底，防二次抛异常中断图）

### 测试
- 契约/候选集/后处理/坍缩/兜底/透传/分节/D35 用例新增（test_qa_router +21、test_chat_contract +12、test_skill_executor +2、test_synth_answer +6）
- 目标单测 6 文件 185 passed；chat 集成回归失败集与 TRUE BASE 逐断言一致；全量 pytest A/B：HEAD 28 failed ⊆ BASE 33 failed（新增失败清零）；ruff P4 改动文件 0 errors

### 文档
- `AGENTS.md`：CHAT QA P4 小节（多子目标/维度预筛/预测降级/兼容性）
- roadmap §1 P4 行（✅ 5/5）、§2 P4 小节、§5.5 验证记录；P4 遗留优化挂入 §1 P5 行

### 验证
- SDD 逐 Task review Approved（5/5）+ 最终整分支审查 READY TO MERGE（0 Critical / 0 Important；M-1 已修）
- Commits：00f879d / 997a858 / 44c354e / a6194f6 / 09f4fd8 / 250c00b

## [feat/market-trace-improvement] 2026-08-03 — 板块别名补充：新增 AI/光模块/半导体

**开发者**: Aria

### 新增
- `src/aistock_agent/data/sector_aliases.json`：新增 "AI/光模块/半导体" 板块（光刻机 / 共封装光学(CPO) / 存储芯片 / 芯片概念 / 中芯国际概念 / F5G概念），补齐生产环境此前手动热修中有效的 AI 光模块/CPO 热点映射；误删的中医药/科创芯片ETF 等映射已随 main 合并恢复

---

## [feat/market-trace-improvement] 2026-08-03 — 生产故障修复：手动触发链路 trigger_source 改 scheduler 使报告落库

**开发者**: Aria

### 修复
- `src/aistock_agent/api/routes.py`：4 个手动触发端点（morning/review/broadcast/trend-score）state 的 `trigger_source` 由 `"manual"` 改为 `"scheduler"`（与 09:00 调度任务语义一致），修复手动触发链路跑成功但 wind_leader/hot_burst/broadcast/review 因持久化门控 `== "scheduler"` 不落库的问题；stock_trace 端点保持 `"stock_trace"`、chat 端点保持 `"user"` 不动

### 文档
- 新增 `docs/superpowers/plans/2026-08-02-market-trace-review-improvement.md`、`docs/superpowers/specs/2026-08-02-market-trace-review-improvement-design.md`（大盘溯源改进方案/设计文档入库）

---

## [changer] 2026-08-03 — P3-fix-3 大盘数据正确性最小补丁 + P2 落库/D27 归一化遗留

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-03-p3-fix-3-market-data-correctness.md`

### P3-fix-3 大盘数据正确性最小补丁
- `src/aistock_agent/skills/market_snapshot.py`：facts 始终带交易日 — 新增 `_date_label()`（YYYYMMDD→MM-DD，异常 None）；`_build_a_share_facts(normalized, trade_date="")` 首位锚点 `数据日期：MM-DD` + 指数行 `名称(MM-DD): ...`；`_fetch_a_share` 传 `trade_date`（消除 LLM 把最近交易日误标"今日"）
- `src/aistock_agent/graph/nodes/synth_answer.py`：新增 `_quote_data_not_today(ev)`（market_snapshot 按 `raw.scope/used_last_close/a_share_success` 判定，其他行情 skill 按 degraded；防误伤：A 股今日数据 + global 失败不触发）；`_append_non_trading_time_hint` 触发条件放宽为"时段非 trading + 行情证据 + 数据非今日"，四状态引导确认文案（含"你说的是否是这个交易日…"）

### P2 落库 / D27 归一化（遗留一并提交）
- `src/aistock_agent/memory/checkpointer.py`：chat 会话持久化（+147 行，落库）
- `src/aistock_agent/api/routes.py`、`state/chat_schema.py`、`schemas/chat.py`：chat 落库接口与状态字段
- `src/aistock_agent/graph/nodes/qa_router.py`（+56）、`utils/date.py`、`services/data_client.py`、`skills/capital_flow.py`、`skills/stock_snapshot.py`、`skills/report_lookup.py`（+100 新增 report_lookup）：D27 参数归一化
- `pyproject.toml`、`.env.example`、`.gitignore`、`README.md`：配置与文档

### 测试
- P3-fix-3：`test_market_snapshot.py` +3、`test_synth_answer.py` +6 且迁移 2 个节点级测试（补 patch trading_session_status）、`test_synth_answer_non_trading_hint.py` 断言更新
- P2 遗留：`test_chat_persist_followup.py`、`test_chat_state.py`、`test_report_lookup.py` 新增；`test_qa_router.py` +155、`test_chat_multiturn.py`、`test_chat_legacy_replacement.py`、`test_memory.py`、`test_ws_chat.py` 适配

### 文档
- `AGENTS.md`：Node.js 侧配合接口表 +3 行 market 端点；"market_snapshot Skill 降级语义"段 +2 条 P3-fix-3 bullet

### 验证
- 目标测试合计 86 passed；全量回归与基线一致（worktree A/B 对比失败集完全一致，新增失败清零）
- ruff：改动文件 0 errors；SDD 审查：逐 Task Approved + 最终整分支审查 Ready to merge Yes

---

## [feat/market-trace-improvement] 2026-08-02 — 大盘溯源 Agent 改进

**开发者**: Aria

### 新增
- schema：MorningForecast / PredictionValidation / SectorHit / EventHit 模型
- service：morning_forecast_extractor 晨报结构化提取服务 + Redis 缓存（TTL=2h）
- snapshot：build_market_trace_snapshot 接入 morning_forecast 注入；财联社数据源切换为 /internal/news/telegraph 当日全量电报
- market_tools：GLOBAL_MARKET_TICKERS 新增欧洲股市 ticker（^GDAXI / ^FTSE / ^FCHI）
- review：validate_trace_against_snapshot 预判对照校验 + render_market_trace_markdown 预判对照章节

### 兼容性
- MarketTraceResult.prediction_validation / MarketTraceSnapshot.morning_forecast 均 Optional 默认 None，兼容旧缓存

---

## [main] 2026-08-02 — ChatAgent 最小落地 M1-M5 完成 + 非交易日统一提示

---

## [changer] 2026-08-14 — 大盘溯源影响持续性预判可靠性修复
**开发者**: 37588

### 修复
- 影响持续性预判（B2 预测）可靠性修复：大盘溯源报告「影响持续性预判」区块此前持续为空（服务器实测 `prediction=null`）——根因①预测到期日跨年（long 档 +120 交易日进入 2027）触发 `chinese_calendar` 越界异常导致预测整体丢弃；②LLM 输出零容错（缺 schema_version / 多余字段 / 围栏文本即整体失败）；③证据 ID 一票否决（任一幻觉即整体抛错）
- 交易日判断越年 fallback：`chinese_calendar` 仅覆盖 2004-2026，2027 年起 `is_workday` 抛异常；现捕获越界并按可交易日处理（只跳周末），库更新后自动恢复精确判断——同步修复 2027 年定时调度（晨报/晚报/复盘/预测验证）与预测到期日计算
- 预测输出三层容错：到期日计算 best-effort（失败降级空字典不阻断预测）；证据 ID 过滤而非一票否决（对齐对话内预测路径）；LLM 输出解析容错（围栏/前缀剥离、JSON 提取、剔除 thinking 等多余键、缺 schema_version 自动注入 1.0）

### 测试
- 新增 9 个回归用例（2027 越年交易日、跨年 +120 交易日、到期日失败降级、证据 ID 过滤、缺 schema_version 注入、多余键剔除、围栏/前缀提取、纯文本降级 None）；全量单测 1553 passed，ruff 0，mypy 无新增错误

> 代码验收通过（待生产验证：服务器 08-14 20:30 review_full 实测 prediction 非 null）。

---


## [changer] 2026-08-13 — 对话体验优化：回答内容流式显示
**开发者**: 37588

### 新增
- 回答内容流式显示：AI 回答生成完成后按内容分节渐进呈现（配合打字机动画），替代此前的整段一次性弹出
- 内容流式事件通道：回答文本增量与异常整段替换两类事件，经统一通道下发，支持断线续传回放

### 改进
- 生成中断时保留已生成内容并追加「已停止生成」提示，不再清空半截内容
- 流式展示与既有「思考过程」「工具进度」展示协同，回答完成时按前缀校验只补尾部，避免内容跳变

> 代码验收通过（待生产验证）。

---


## [changer] 2026-08-13 — 对话体验优化：深度分析触发修复
**开发者**: 37588

### 修复
- 对话「深度分析」触发修复：此前使用股票中文名称提问（如"贵州茅台今天怎么样"）会被固定为轻量回答，「深度分析」入口无法生效；现支持在明确表达深度分析意图（如"深度分析贵州茅台"）或点击「深度分析」按钮时正确进入深度分析流程

> 代码验收通过（待生产验证）。

---



---

## [feat/event-scrape-schedule-adjust] 2026-08-13 — 事件抓取中台调度调整（盘前 07:30→08:45 + 盘中恢复 12:00）
**开发者**: Aria

### 改进
- `config.py`: `scheduler_event_scrape_cron` 由 `30 7 * * 1-5` 改为 `45 8 * * 1-5`（盘前全量档 07:30→08:45）
  - 原因：07:30 时点早间公告（08:00-09:00 发布）尚未出，全量价值低；08:45 紧邻晨报 08:50，事件更全
  - `scheduler_event_scrape_early_cron` 保留字段（兼容已部署配置），不再单独注册 job
- `config.py`: `scheduler_event_scrape_intraday_cron` 由 `0 10-11,13-14 * * 1-5` 改回 `0 10-14 * * 1-5`（恢复 12:00 午间档，用户裁决：午休期间仍有午间公告/新闻发布，M8 移除属误删）
- `scheduler.py`: 删除 `event_scrape_early` job（原 08:45 intraday 增量档），盘前档 `event_scrape_daily` 以 `full_daily` 在 08:45 运行，与早间刷新合并

### 测试
- `test_scheduler_event_scrape.py`: `event_scrape_early` 断言改为 `event_scrape_daily`（08:45）+ 确认 early 已删除
- `test_scheduler.py`: `from_crontab.call_count` 9→8（删 1 档）；两个注册断言 `event_scrape_early`→`event_scrape_daily`；intraday cron mock 值同步为 `0 10-14 * * 1-5`
- 验证：55 passed（scheduler 相关）；ruff All checks passed；mypy 3 个既有错误（_get_event_bus 无类型标注，与本次改动无关）

---

## [fix/iterate-replay-user-profile] 2026-08-13 — 回放隔离清单补登记：get_user_profile（PR #71 缺口）
**开发者**: Aria

### 修复
- `iterate/replay_layer.py`: `NodeApiClient.get_user_profile` 加入 `_ISOLATION_EXEMPT_METHODS`（经 `get` 间接隔离分组）
  - 背景：PR #71 新增 `get_user_profile`（用户画像，内部 `await self.get("/internal/user-profile/{user_id}")`），未登记回放隔离清单，I-3 清单封闭测试 `test_service_isolation_covers_all_public_network_methods` 失败（服务器沙盒全量测试暴露）
  - 依据：`get_user_profile` 无独立网络入口，经 `get → node_read` 返回 None 后 `not isinstance(data, dict)` 走失败降级，符合豁免条件；回放模式下不触达真实 Node 后端

### 测试
- `tests/unit/test_iterate_replay.py`: 17 passed（含清单封闭测试 RED→GREEN）；ruff All checks passed

## [fix/iterate-case-sufficiency] 2026-08-13 — 产片链路数据完整性防御（case_20260731 全 0 分事故）
**开发者**: Aria

### 修复
- `scripts/build_iterate_cases.py`: 新增 `_snapshot_data_sufficient(snapshot_dict)` 产片数据完整性检查
  - 背景：服务器沙盒 `case_20260731_us_market_surge` 跑 run_case 全 0 分，根因是该 case 为测试 fixture 样例（`a_share={}`、missing_fields 3 项），且真实产片链路 `build_market_trace_snapshot` 的 `normalize_a_share` 只做字段复制不校验完整性——Node 返回 status=complete + coverage.complete=true 但 indexes 等字段缺失时，空壳 case 照样产片进闭环，跑满 max_rounds 全部 0 分浪费 LLM 预算
  - 修复：`build_review_case` 在 build_case 之前检查快照 A 股数据完整性（`a_share.indexes` 非空），数据不足且非 `force` 时抛 `RuntimeError` 拒绝产片（省一次 case/GT 落盘与 LLM 调用）；`force=True` 跳过
- `scripts/build_iterate_cases.py`: `snapshot.model_dump` 改用 `cast("Any", ...)`（跨 SimpleNamespace/MarketTraceSnapshot 类型边界，消除 mypy attr-defined/union-attr 错误码不一致）

### 测试
- 新增 2 条：空壳快照拒绝产片（+ 不残留文件）、force 跳过检查
- 验证：产片链路 + case/GT/校验/评估/调度 59 passed；ruff All checks passed；mypy iterate clean

---


## [feat/event-scrape-hub] 2026-08-13 — 迭代辩论裁决修复第二轮收尾（T9 M3/T10 Q1/T11 + 基线清理）
**开发者**: Aria

### 修复
- `variant_engine.py`: `_content_hash` 参数类型从 `dict[str, str]` 放宽为 `dict[str, object]`，兼容 `_compute_variant_hash` 传入的嵌套 dict 补丁规格（T9 M3 补充修复）
- `test_iterate_variant.py`: `test_experiment_record_has_real_variant_hash` 更新为用 `_compute_variant_hash` 计算预期值（适配 T9 M3）；移除未使用的 `hashlib` 导入
- `test_iterate_loop.py`: `test_stale_experiment_records_cleaned_before_run` 添加 `result` 断言消除 ruff F841

### 改进
- `config.py`: 修复 2 个 E501 行过长（event_scrape 调度 cron 表达式换行）
- `AGENTS.md`: iterate 模块描述补充 T9 M3/T10 Q1/T11 M1-M4 修复要点

### 验证
- pytest: 73 passed, 3 deselected（2 个依赖 git 可执行文件、1 个预存不相关失败）
- ruff: All checks passed（iterate 模块 + 测试文件 + config.py）
- mypy: 无错误（iterate 模块）

---


## [changer] 2026-08-12 — Phase 5 长会话上下文管理
**开发者**: 37588

### 新增
- `src/aistock_agent/utils/context_window.py`：`trim_messages(messages, *, max_turns=6, summary_chars=200)` 纯函数——≤12 条消息原样透出（summary=None，短会话 prompt 字节不变硬约束）；超窗 → LLM prompt 只喂最近 12 条，超窗部分收敛为零 LLM 确定性摘要（≤200 字，逐轮"用户：问句｜AI：回复片段"，幂等无累积）；`build_summary_context` 生成"此前对话摘要"注入段
- `QuestionState.messages_summary` 可选字段（qa_router 超窗时写入随 checkpointer 持久化，write-only；synth_answer 消费侧从当前 messages 确定性重算，防跨轮陈旧残留）
- `DELETE /api/agent/internal/chat/threads/:session_id`（内部访问令牌 403 / 非法 400 / 幂等 200 / 异常 500）+ `checkpointer.delete_thread()`（AsyncSqliteSaver.adelete_thread，sqlite/memory 幂等、redis best-effort）
- `config.py sqlite_busy_timeout=30.0` → `_build_async_sqlite_saver` 的 `aiosqlite.connect(timeout=...)`（多 worker 争用缓解）

### 改进
- qa_router/synth_answer：窗口+摘要注入（SYSTEM_PROMPT 常量字节不变，节点内拼接），LLM 输入用 12 条窗口；多子目标 `_synth_multi_goal`/`_synth_section` 路径同步注入

### 测试
- `tests/unit/test_context_window.py`、`tests/unit/test_qa_router_summary.py`、`tests/unit/test_synth_answer_summary.py`、`tests/unit/test_checkpointer_busy_timeout.py`、`tests/e2e/test_chat_threads.py`、`tests/unit/test_checkpointer_delete_thread.py`、`tests/integration/test_phase5_long_session_smoke.py`

> 验证：全量测试回归新增失败清零；ruff 改动文件 0 新增；集成冒烟 2/2（7 轮 13 条 → 12 条窗口 + 摘要注入 + messages_summary 持久化 + 删会话 thread 消失；短会话字节不变）。代码验收通过（待生产验证），待组长 merge 后部署验证。

---


## [changer] 2026-08-12 — 问题 18 WS recv 竞态修复（Phase 2 回归补丁）
**开发者**: 37588

### 修复
- `src/aistock_agent/api/ws.py`：`_forward_until_done_or_cmd` 的 send 完成分支在 `recv_task.cancel()` 后新增 `await asyncio.gather(recv_task, return_exceptions=True)` 收尾再 return——`task.cancel()` 仅请求取消，不同步 await 则底层 uvicorn/websockets 同连接 recv 并发防护未释放，主循环随即 `receive_json()` 触发 `RuntimeError: cannot call recv while another coroutine is already waiting` → 每轮 done 后 WS 连接 1005 崩溃（Phase 3 生产冒烟 9 轮实证，Phase 2 PR #64 引入）
- 回归测试：`tests/unit/test_ws_chat_replacement.py` 新增 `_RecvTrackingWebSocket`（复刻 uvicorn 并发 recv 抛 RuntimeError 防护语义）+ `test_forward_until_done_or_cmd_clears_pending_recv_on_done`（断言返回时无挂起 recv、主循环可安全发起下次 receive、不抛 RuntimeError）

> 验证：TDD RED→GREEN；单元 test_ws_chat_replacement.py 15/15 + 定向契约回归 22/22（chat_task_manager / ws 集成 / ws_resume / token_usage）；全量测试回归新增失败清零（+1 新增回归测试）；ruff 改动文件 0；真实 WS 冒烟同一连接连续 3 轮 done 全部送达、连接保持、主动关闭 code=1000（非 1005 崩溃）。不改 resume/stop/归属校验协议与事件协议，前端零改动。生产部署验证待 V1 部署窗口。

---


## [feat/event-scrape-hub] 2026-08-12 — 统一事件抓取中台 final review 复审修复（Round 2）
**开发者**: 37588

### 修复
- `services/event_scrape_sources.py` I1 过滤由 `published.startswith(score_date)`（北京日期前缀）改为 `_event_shanghai_date(published) == score_date`（上海时区日期归属）——Node `published_at TIMESTAMPTZ` 经 `toISOString()` 输出 UTC ISO（如 `2026-08-12T02:00:00.000Z`），北京 00:00-07:59 当日事件 UTC 日期落前一日（`2026-08-11T22:00:00.000Z` = 北京 8-12 06:00）被旧逻辑误过滤；新增 `_event_shanghai_date`（UTC 带 Z → 转上海；本地无时区 → 显式 `replace(tzinfo=Asia/Shanghai)` 保证确定性；解析失败宽容回退 `raw[:10]`）；保留"无时间字段保守保留"守卫
- `agents/workers/morning.py::_event_records_to_major_events` 加 `impact_score >= MAJOR_IMPACT_THRESHOLD`（=4）过滤（对齐注入路径过滤语义，docstring 注明），缓存命中时 `analysis_reports["major_events"]` 不再混入 impact=1 普通证据（手动晨报端点 major_event_count 诊断计数失真）；函数为模块私有、仅缓存路径一处调用，无复用歧义

### 测试
- 新增 4 条：UTC 上海日期归属（当日保留/北京凌晨保留/真陈旧过滤，`shanghai_today()` 相对日期无炸弹）、非法时间回退、缓存命中 major/minor 过滤、全普通证据降级回 details 提取

### 说明
- 偏差：任务单测描述"`2026-08-11T22:00:00.000Z` 应过滤"与其 Issue-1 正文"北京凌晨当日事件应保留"矛盾（该时间戳上海日期=当日），按 Issue-1 正确语义实现并补充真陈旧行用例锁定
- 验证：定向 pytest 28 passed；全量 1471 passed / 6 既有失败（test_industry_vector_search API 依赖，基线一致零回归）；mypy 2 文件 0 错误；ruff 4 文件 All checks passed

---


## [feat/event-scrape-hub] 2026-08-12 — 统一事件抓取中台 final whole-branch review 修复（C1 + I1-I4 + Minor）
**开发者**: 37588

### 修复
- **C1（Critical）**：`event_scrape_sources.py::collect_eastmoney_judgements` 响应键名 `"items"` → `"events"`（Node `StockMonitorService.getEvents` 返回 `{total, events}`）——修复东财三模式（full_daily/intraday/event_triggered）生产恒空
- **I1**：东财行按 `published_at`/`event_time` 日期前缀过滤（alerts 接口不支持日期窗口），防昨日/前日陈旧行以当日 score_date 反复入库
- **I2**：`raw.setdefault("url", raw.get("detail_url") or "")`——Node 输出 `detail_url` 非 `url`，修复东财事件 url 恒空
- **I3**：`save_event_scrape` 返回值增加 `added`/`added_events`（本批真正新增数），传导守卫 `persisted>0` → `added>0` 且只传新增子集——全去重批次不再重复触发整批传导（LLM 成本）
- **I4**：`scheduler._run_morning_task` 降级分支——当日事件库为空且 morning 产出 major_events 时兜底触发传导（恢复 `_pending_event_tasks` 强引用）；M1 随之解决
- **M2**：`data_client.py` 新增 `get_analysis_report_quiet`（404 静默）；`load_event_scrape` 改走该方法并对空库降级 warning（不再刷 error 级 404）
- **M4**：晨报注入前按 `impact_score >= MAJOR_IMPACT_THRESHOLD` 过滤；全普通时降级自主检索文案
- **M5**：`scrape_event_triggered` 加 symbol 空守卫（不采集不落库）
- **M8**：盘中 cron `0 10-14 * * 1-5` → `0 10-11,13-14 * * 1-5`（避开 A 股午休）

### 验证
- 全量 pytest 1467 passed / 6 既有失败（基线一致零回归）；mypy 0 新增；ruff All checks passed

---


## [changer] 2026-08-11 — Phase 2 断点续传 + 打断/停止/重试（问题 15）
**开发者**: 37588

### 新增
- `src/aistock_agent/services/chat_task_manager.py`：ChatTaskManager 单例——`start(session_id, run_id, producer, user_id=None)`（同 session 活跃任务拒绝）/ `get`（done 且超 TTL 600s 惰性清理）/ `has_active` / `cancel(session_id)->bool`；`ChatRunState`（events 回放 / waiters.notify / done / cancelled / result 缓存 / user_id 归属）；`_runner` 显式 `except asyncio.CancelledError` → `{"type":"cancelled","content":"已停止生成"}`
- 测试：`tests/unit/test_chat_task_manager.py`（7）、`tests/integration/test_ws_chat_resume.py`（7）

### 改进
- `src/aistock_agent/api/ws.py`：生成任务与 WS 连接解耦——生产者 `_run_chat_graph_to_events`（事件 sink 进 state.events + notify；`reset_token_usage` 在 create_task 前同 context；token 计费落库仅 warning 不阻断）+ 转发器 `_forward(state, send, replay)`（断连仅终止转发不取消任务）+ `_forward_until_done_or_cmd`（转发/接收并行，生成中可即时 stop）
- 协议增量（向后兼容字节不变）：普通消息可选 `run_id`；控制消息 `{type:"resume",session_id}` → `resume_status`（none/running+run_id）/ done 直接补发终态 payload；`{type:"stop",session_id}` → `stop_status`（cancelled/not_found）；`cancelled` 终态经既有 `_forward` 终态路径下发
- 归属校验 `_owns_run`（resume/stop 共用）：state None → True；双方 user_id 非空必须相等；任一 None → True；越权 → error "无权访问该会话" + WARN（不静默）
- `src/aistock_agent/graph/nodes/_reasoning.py`：`stream_reasoning(websocket,...)` → `stream_reasoning(sink, node, message)`（sink 化解耦连接）
- DONE 负载字段（content/last_deep_report/token_usage/cards）字节不变；闸门短路语义不受影响

> 验证：定向 40/40 + ruff 改动文件 0；全量测试回归新增失败清零（并修复 8 个基线失败）；三仓库整分支 review Ready to merge。

---


## [changer] 2026-08-11 — P0 端口层封堵（uvicorn 改绑）+ 文档
**开发者**: 37588

### 改进
- `deploy/ecosystem.config.json`：uvicorn `--host 0.0.0.0` → `--host 127.0.0.1`（8080 只监听本机，公网不可直连 agent-py；app-api 本机回环仍可达）——P0 身份鉴权端口层封堵第二步（Caddy 域名层已由管理员完成）

### 文档
- AGENTS.md：user_id 信任边界由 P0 解决（app-api 验签注入，客户端自报失效）

> 部署注意：勿用 `pm2 restart`（不重读配置），须先 `pm2 delete` 再 `pm2 start deploy/ecosystem.config.json`，并验证端口仅监听本机回环地址。
