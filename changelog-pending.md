# 待提交修改记录（changelog-pending）

## 2026-08-11 迭代 Agent 架构实现
- 新增 `src/aistock_agent/iterate/`：adapters/case_builder/ground_truth/replay_layer/replay_runner/evaluator/variant_engine/run_case/reporter/scheduler
- `config.py` 新增 iterate_* 配置（默认关闭）；`.gitignore` 追加 data/ 数据目录
- `services/scheduler.py` 按 `ITERATE_ENABLED` 条件注册 iterate_daily job
- 部署：scripts/setup_iterate_sandbox.sh（服务器 worktree 沙盒）

## 2026-08-11 迭代闭环修复（final review findings）
- C1 回放隔离：event_analyst 的 search_cls_news（event_news）与 tavily_finance_search（search）接入回放层，读切片 cls_telegraph 语料、不发网络请求
- I1 集成测试传 `repo_root=tmp_path`，restore_baseline 不再触碰真实仓库
- I2 `apply_variant` 路径穿越防护（resolve 包含性校验）；`restore_baseline(extra_files=())` 恢复变体实际写过的未声明文件；实验记录 `git_commit` 改为真实 `variant_hash`（new_content sha256）
- I3 `build_case` 对 market_snapshot 做 `MarketTraceSnapshot` 契约校验（生成期快速失败）；fixture 改为 schema-valid 快照
- I4 每日任务去重：只消费无实验记录（文件名前缀 `{case_id}_r`）的案例；空案例库报告注明"无待迭代案例"
- I5 基线轮（round 1）也落盘 `{case_id}_r1_baseline.json`；实验记录新增 `created_at`（ISO 日期），报告按日过滤（无 created_at 旧记录向后兼容包含）

## 2026-08-11 QQ 邮箱 SMTP 复用（同事交接）
- 新增 `services/mail_sender.py`：通用 SMTP 发送（复用已验证模式 smtp.qq.com:465 SSL + 授权码、HTML 正文、附件 MIME 映射、RFC 2231 中文文件名）；配置解析：显式参数 → `ITERATE_SMTP_*` → 环境变量 `QQ_SMTP_USER/AUTH/TO`
- `iterate/reporter.py` 改用 mail_sender 发送（报告以 HTML `<pre>` 正文），移除本地 smtplib 逻辑
- 测试：新增 tests/unit/test_mail_sender.py（配置解析/发送/重试/附件 RFC2231）；test_iterate_reporter.py 适配（fallback 写盘断言补齐）

## 2026-08-11 迭代闭环线上事故修复（服务器验证阶段）
- 变体生成丢失 run 入口（7e02449）：`generate_variant` 的 prompt 改为喂被改文件当前内容（`_files_with_content`，8000 字符截断）+ 禁止删除/重命名已有函数/常量/入口；回归测试 `test_generate_variant_feeds_current_file_content`
- 变体生成 JSON 截断（e00335c）：`get_deep_think` 支持按调用覆盖 `max_tokens`；`generate_variant` 用 12000 token + 关闭思考（`thinking.disabled` + `reasoning_effort=none`）；顺带修 llm.py 既有 `Runnable` 泛型缺参 mypy 错误
- 回放超时崩溃（2413066）：`_run_replay_subprocess` 捕获 `TimeoutExpired` 返回 `timed_out` 标记，调用侧记为超时失败轮（评分 0 + gap 注明），不崩整个闭环；服务器可调 `ITERATE_ROUND_TIMEOUT_SECONDS`（event_analyst 建议 1800）；回归测试 `test_run_experiment_round_timed_out_is_failed_round`

## 2026-08-11 迭代闭环验证结论（event_analyst，沙盒）
- `run_case event_analyst case_20260731_us_market_surge --max-rounds 2`：基线 0.7（方向/驱动命中，板块不足），变体轮 0.7；闭环全链路验证通过
- 0.7 天花板：切片仅 1 条电报，标准答案板块（半导体/算力/新能源）为后验知识，T 窗口下不可推导 → 二期真实切片需标准答案板块取自切片内实际轮动数据 + 加数据一致性校验

## 2026-08-11 迭代二期：真实切片生成 + 数据约束标准答案
- 新增 `iterate/case_scanner.py`：find_recent_trading_day（Node close-snapshot/last-close 降级）、scan_major_events（电报关键词 + 30 分钟窗口聚类）
- 新增 `iterate/gt_validator.py`：validate_gt_against_case 三条规则（方向/板块/驱动可推导）
- 改造 `iterate/ground_truth.py`：generate_data_constrained_gt（方向/板块确定性 + 驱动 LLM 受切片语料约束）；load_ground_truth 支持 data_dir
- `case_builder.py`：case_path/load_case/build_case 支持 data_dir 覆盖（落盘/查找目录隔离）
- 新增 `scripts/build_iterate_cases.py`：review（最近交易日）/ event_analyst（N 天电报事件）切片生成 CLI，--force 跳过校验

## 2026-08-12 事件聚类窗口有界性修复（final review 复审 round 3）
- `iterate/case_scanner.py::_cluster_events`：窗口判断从相对簇末条（`clusters[-1][-1]`，链式吸收导致 T 无界漂移、事件桥接合并）改为相对锚点/簇首条（`clusters[-1][0]`），簇有界（锚点起 30 分钟内）；docstring 同步「同 30 分钟窗口（相对锚点）合并，T = 窗口末条电报时间」
- 测试：test_iterate_case_scanner.py 新增 test_event_window_bounded_to_anchor（35min 外记录不吸收、T 不延伸）；test_gt_validator.py 新增 test_driver_traceable_via_global_markets（外盘语料驱动可溯源）+ test_no_index_skips_direction_check（无指数快照跳过方向强校验）

## 2026-08-12 迭代二期 final review 修复（round 1/2）
- I2 事件 T 窗口：`case_scanner._cluster_events` 的 `event_time` 改为簇末条时间（T=窗口末条），`telegraph_records` 改为 [锚点,T] 窗口内所有电报（含未命中关键词的后续报道，`_in_event_window` 辅助）——修复落盘 case 因 `build_case` 的 time<=T 过滤只剩 1 条电报的问题
- I3 规则统一：`ground_truth._direction_from_snapshot` 改为取首个含 change_pct 的指数分档（与 gt_validator 一致，修复原「首指数 |pct|<0.5 时错误跳过」）；`gt_validator._corpus_text` 外盘格式改为 `- 外盘 {ticker} {pct}%` 与生成器一致（避免「外盘传导」驱动被误拒）
- I4 neutral 严格语义：`gt_validator` 规则 1 改 `expected is not None and direction != expected`（GT 必须等于快照推导方向，含 neutral）；新增 `test_bullish_gt_rejected_when_snapshot_neutral`
- M5：`case_builder.build_case` 新增 `meta` 参数（并入 case 顶层随落盘写入）；CLI 两处调用传 meta（review: snapshot_kind full + t_window close；event: t_window event）
- M6：`_cluster_events` 簇 T 为空时跳过（避免 fromisoformat("") 崩溃）
- 测试：case_scanner 4、gt_validator 7、ground_truth 4、集成 2、全量 iterate 20 passed 全绿
- I1 已知限制：`scripts/build_iterate_cases.py` 按 `--agent` if/else 硬编码 review/event_analyst 流程，未消费 `adapter.data_deps` 声明（final review 裁决本期不做通用化）；接入第三个 agent 时需抽「按 data_deps 采集 → build_case → 生成 GT → 校验」通用流水线

## 2026-08-12 CHAT QA Phase 3 快赢补丁（T1 859b91c + T2 1d31a47 + docs 验证记录）
- 用例 7（T1 859b91c）：`qa_router._STOCK_NAME_STOPWORDS` 补「深度」——"深度分析贵州茅台" 候选名从"深度贵州茅台"（resolve 404 → 误澄清）修正为"贵州茅台"（resolve 命中）
- 问题 17（T2 1d31a47）：`get_quick_think(*, observe=True)` 新增 observe 参数；`_reasoning.py::stream_reasoning` 以 `observe=False` 调用（不挂计费 callbacks）→ reasoning 旁路 token 不进用户账单；主链路默认 True 零破坏
- 验证记录（T3 docs commit）：全量 A/B 回归（BASE 28 failed/1772 passed vs HEAD 28 failed/1775 passed，失败集逐项一致，**新增失败清零**）+ ruff 6 改动文件 0 新增 + 定向 135 passed

## 2026-08-12 CHAT QA Phase 3 生产部署验证（PR #65 → main 2e2bd34，git pull + pm2 restart）
- 用例 7「深度分析贵州茅台」→ resolve 命中（贵州茅台/600519）→ 真实行情回答 + stock_snapshot card，**不再澄清**；确定性预期实证：走闸门 2 短路固定 light（last_deep_report=null），force_deep 对短路路径无效 = 预期结果（移交 roadmap force_deep ⏸️ 待评估）
- 问题 17 计费实证：短路轮「你好」（137 reasoning 事件）/「我能买茅台吗」（153 reasoning 事件）reasoning 旁路实际运行但 `DONE.token_usage=null`（修复前必非零）→ reasoning 不计费；内容轮 token_usage 与主链路一致（9 行 chat_token_usage 落库 = usage summary API total 41076/turn_count 9 = DONE 之和）
- 护栏 4 问句回归 + P11 5 类 cards（stock_snapshot/market_snapshot/capital_flow/comparison/deep，deep report_id=282 落库）+ P9 多轮上下文隔离（"它"→600519 指代命中）全通过
- **新发现缺陷（待立项，非 Phase 3 回归）**：问题 18——Phase 2（PR #64）`_forward_until_done_or_cmd` recv_task.cancel() 未 await → done 后 WS 连接 1005 崩溃（9 轮全部实证）
