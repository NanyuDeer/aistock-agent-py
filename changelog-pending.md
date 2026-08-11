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
