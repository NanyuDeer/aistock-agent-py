# 待提交修改记录（changelog-pending）

## 2026-08-12 — 统一事件抓取中台 Task 6：全量验证与文档维护

- 全量验证：pytest tests/unit/ 1482 passed / 8 失败均环境性既有基线（test_iterate_variant 2 个为 PATH 无 git 导致，设置 PATH 后重跑通过；test_industry_vector_search 6 个为本地无 EMBEDDING/OPENAI 凭据触发 `semantic_match_industries_no_credentials` 早退，文件未被 Task 1-5 commit 触碰）；mypy 全量 140 存量错误（32 文件均非 Task 1-5 文件，event_scoring/event_store/event_scrape_sources 零错误）；Node tsc 0 错误；Node 新测试（queryJudgementsDateFrom + eventScrapeTrigger）4 passed
- 文档：README.md 中台章节 pipeline 补"规则评分"环节（架构分层表新增规则评分层，强词 5 分/弱词 3 分/语境降权）；AGENTS.md 中台行 pipeline 补规则评分 + Node.js 配合接口表新增 monitor/alerts 行（注明支持 `dateFrom=YYYY-MM-DDT00:00:00+08:00`，`days` 已弃用）

## 2026-08-12 — 迭代闭环设计辩论 R3（正方终轮回应书）

- `docs/superpowers/debates/2026-08-12-iterate-closed-loop-debate-proponent-r3.md`（新增）：正方第 3 轮终轮回应书。对反方追打书 R2 的 5 条未闭合主线（D4/D13/G4/G5/D14/D16/D2/G2）与 N1-N8 全部认领（D14 辩护正式撤除），给出可执行规格：D4 校准前禁用 no_improvement + δ=2σ 噪声度量 + 6 态 stopped_reason 枚举与归并优先级；D13/G4/G5 单一权威 `{case_id}.iterated.json` + 单向一次性迁移（升级当日零重跑）+ 事件指纹 hash 输入域 + 64 位 hash 碰撞处理 + 事件级去重；D14 保留 r1 及时落盘 + 记录 status/round_type + 与已迭代标记彻底解耦 + failed 退避重试封顶（N5）；D16 产片/迭代拆双 job + 非 complete 降级 last-close 扩展 + 15+120+10 分钟总时长预算 + 任务截断语义 + 交叉校验路线 (b)+周抽样（N6c）+ 节假日三层防御（N8）；D2/G2 终止证据并入 iterated.json（cases 目录，与可清日志 experiments 分离）。附 4 条分歧记录。文档型修改，无代码变更。

## 2026-08-12 — P0-2 东财日期窗口 dateFrom（Task 4）

- `src/aistock_agent/services/event_scrape_sources.py`：`collect_eastmoney_judgements` 请求 URL 由 `/internal/monitor/alerts?days=1` 改为 `?dateFrom={score_date}T00:00:00%2B08:00`（Node 端原忽略 days 只取最新 20 行导致当日事件可能 0 条；Node 新增 published_at >= dateFrom 过滤，Python 侧仍保留按行日期过滤兜底）
- `tests/unit/test_event_scrape_sources.py`：新增 `test_collect_eastmoney_judgements_passes_date_from`（URL 含 dateFrom 且不含 days=1）；既有 `test_collect_eastmoney_judgements_reads_existing_table` 的硬编码 `days=1` 断言同步更新为 dateFrom URL
- 验证：pytest 22 passed（既有 21 + 新增 1）

## 2026-08-12 — 归因相似度评分体系设计辩论 R3（正方终轮回应书）

- `docs/superpowers/debates/2026-08-12-iterate-evaluator-debate-proponent-r3.md`（新增）：正方第 3 轮（收敛轮）回应书，回应反方 R2 追打的 4 条未闭合项（A5/A9/A16/G9）与 N1-N12。三个结构性修正：撤销互相矛盾的修订（T=0 与多采样并列、逐字重叠衰减、transmission_path 第四维、整体门控 ×0.5）、声明修订族依赖图（A3 是 A12 前置）、全部主观判定补机械核验层。认领 N1/N2 并给出"评估端如何拿到结构化结果"的四段接线设计（agent structured 键 → replay_runner 回传 → 两处评分调用透传 → evaluator 签名扩展）；N5 给出引用机械核验设计（冻结语料入 GT、judge 报引用、机器做字符串包含二次核验）。文档型修改，无代码变更。

## 2026-08-12 — P0-4 save_event_scrape 并发丢批修复（Task 3）

- `src/aistock_agent/services/event_store.py`：新增模块级 `_save_lock = asyncio.Lock()`，`save_event_scrape` 的"读当日已有 → 按 content_hash 合并 → 整行覆盖落库"临界区整体包裹 `async with _save_lock`（load 与 save 之间跨 await，手动 trigger 与调度并发时后写覆盖先写导致丢批；`if not events` 早退保持在锁外）。多 worker 并发属记录不裁决项（辩论裁决 D2），上多 worker 前需 DB 级并发控制
- `tests/unit/test_event_store.py`：新增 `test_save_event_scrape_concurrent_batches_no_loss`（并发两批不同事件不丢批，无锁时确定性 RED 丢批、有锁时合并为 2 条）。mock 修正：fake_load 签名对齐 `node_api.get_analysis_report_quiet(report_type, score_date)` 且返回报告 dict 契约（简报原样签名 1 参数导致 TypeError、load 恒降级空库，GREEN 无法成立）；fake_save 加写窗口 sleep 保证无锁下两协程 load 都先于任一次 save（确定性 RED）
- 验证：pytest 19 passed（既有 18 + 新增 1），mypy event_store.py 无错误，ruff 通过

## 2026-08-12 — P0-1 三源采集接入规则评分（Task 2）

- `src/aistock_agent/services/event_scrape_sources.py`：`collect_cls_telegraph` / `collect_ths_original` / `collect_tavily` 三个采集函数在 normalize_event 前调用 `apply_rule_score(raw, source=...)`，三源事件携带真实 impact_score（不再恒 0），强词 5 分过阈入库、无词 1 分维持过滤面
- `tests/unit/test_event_scrape_sources.py`：新增 3 用例（cls 强正 5 分 / ths 强负 5 分 / tavily 无词 1 分）；tavily 用例断言按双 query 实际行为修正为 len==2 且全部 impact_score==1（简报原文 len==1 与 collect_tavily 遍历 2 个 query 的行为矛盾，RED 实证）
- 验证：pytest 27 passed（既有 18 + 新增 3 + scoring 6），mypy 2 文件无错误

## 2026-08-12 — 数据回放层设计辩论 R2（正方回应书）

- `docs/superpowers/debates/2026-08-12-iterate-replay-debate-affirmative-r2.md`（新增）：正方第二轮回应书，逐条回应反方 B1-B24 与 G1-G16（辩护/认领 + 修订思路，无完整代码）；认领核心缺陷 12 项（get_industry_chain 隔离、persist_event_report 双 patch 语义冲突、node_read 子串判定、run_review 函数体内 import 绕过等），辩护成立项 10 项（env 门控、state 锚定、run() 路径审计、模型侧先验边界等）

## 2026-08-12 — P0-1 事件规则评分模块（Task 1）

- `src/aistock_agent/services/event_scoring.py`（新增）：`apply_rule_score(raw, *, source)` 确定性规则评分——强词 5 分过阈（MAJOR_IMPACT_THRESHOLD=4）、弱词 3 分不过阈、NEUTRAL_CONTEXT 语境词降权 1 分防误判；已有有效 impact_score 不覆盖（eastmoney ai_impact 优先级更高）
- `tests/unit/test_event_scoring.py`（新增）：6 用例（强正/强负 5 分、弱正 3 分、语境降权 1 分、无词 1 分、已有评分不覆盖），pytest 6 passed + mypy 无错误

## 2026-08-12 — 统一事件抓取中台遗留 Minor 修复

- `src/aistock_agent/api/routes.py`：`event_scrape_list` / `event_scrape_by_symbol` 改用 `date = _validate_scrape_date(date)` 接收返回值（消除丢弃返回值歧义）
- `src/aistock_agent/services/event_scrape_sources.py`：`_event_shanghai_date` 非 Z 分支先判断 `dt.tzinfo is not None`，带显式偏移（如 +00:00）时 `astimezone(Asia/Shanghai)` 换算墙钟，否则 `replace(tzinfo=Asia/Shanghai)`（Final review Minor-1）
- `src/aistock_agent/agents/workers/morning.py`：`_event_records_to_major_events` docstring 去除行号引用 ":690-728"，改为注入路径过滤语义描述（Final review Minor-2）
- `tests/unit/test_scheduler_event_scrape.py`：交易日/盘中/异常用例补 `scheduler.logger` patch，断言 `event_scrape_job_done` 成功日志恰好一次、`event_scrape_job_failed` 失败日志不出现（Task 3 I1 回归保护）
- `tests/unit/test_event_scrape_query.py`：补 `test_scrape_list_degrades_on_node_error`（node 异常 → 路由 200 空列表，Task 6）
- `tests/unit/test_event_scrape_sources.py`：补 `test_collect_eastmoney_judgements_explicit_utc_offset_converted`（带 +00:00 偏移用例，Final review Minor-1）
- `tests/unit/test_event_scraper_conduction.py`：docstring 去除行号引用 "event_scraper.py:86-87"（Final review Minor-2）

## 2026-08-12 — 归因相似度评分体系设计辩论 R2（正方回应书）

- `docs/superpowers/debates/2026-08-12-iterate-evaluator-debate-proponent-r2.md`（新增）：正方第 2 轮回应书，逐条回应反方 A1-A17 攻击与 G1-G10 缺口。接受主持人核验的五项事实（Tavily 死代码、空 GT 满分路径、驱动兜底"指数neutral"、方向首键依赖+双端语料不一致、judge 自报且无温度/seed 控制）。修订方向：空维度豁免+重归一化、驱动 total 固定 len(truth)、direction_present 显式化+方向门控、共享语料/方向推导函数、跨模型 judge+temperature=0、confidence 进达标判定+gt_version、删除 generate_ground_truth 死代码。文档型修改，无代码变更。
- `docs/superpowers/debates/2026-08-12-iterate-closed-loop-debate-proponent-r2.md`（新增）：迭代闭环设计辩论正方第 2 轮回应书，逐条回应反方 D1-D23 攻击与 G1-G14 缺口（共 37 条）。接受主持人核验五项事实：F1 15:40 旧 iterate 为死代码（scheduler_iterate_cron 从未 add_job）、F2 iterate_daily 与 prediction_validate 同刻 16:00、F3 list_pending_cases 以 experiments 目录为单点事实源、F4 stalled 严格大于在噪声下永不触发、F5 16:00 任务不产切片。修订主线：评分可信度（空 GT 不给分+独立 GT 交叉校验+评估确定化）、终止证据落盘（summary 记录+agent_output+仅优雅终止写标记）、调度错峰与回放异步化+LLM 超时+每日预算、case 内嵌去重标记+case_id 加 hash、代码级沙盒守卫+finally 恢复基线+preserve-human-changes+文件锁、产片接线进每日链路。文档型修改，无代码变更。
