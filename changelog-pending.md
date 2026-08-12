# 待提交修改记录（changelog-pending）

## 2026-08-12 Phase 4 验收修复（正反辩论裁决，changer 未 push）

> 两轮正反辩论（正方 A1-A10 / 反方 B1-B10 / 追打 C1-C4）后用户拍板"先修必修缺口再复审"。本批为验收修复 commits：agent-py d486dbe..637efb1（10 commits）、app-api 2ac18b9..86e2d13（2）、app-frontend 5f00eb4..50bce70（4）。

- **B2 resume×confirm 死端修复**：pending-confirm 独立缓存（ChatTaskManager `set/get/clear_pending_confirm`，TTL 600s，独立于 ChatRunState 防阶段 2 覆盖）；主循环消费 `confirm_response`（resume 补发后点选不再报"消息不能为空"）；`_normalize_confirm_choice` 单一事实源（同连接与 resume 两处共用，qa_router 只消费 dict 形状）；幂等（消费后 clear，重复→"确认已失效"）
- **B7 确认等待期消息不静默**：`_wait_confirm_response` 返回 `ConfirmWaitResult`（choice/displaced/stopped 三互斥结局）；普通新消息→displaced 放弃确认按下一轮处理；stop→cancelled 终态（含越权校验）；request_id 不匹配仍忽略（B-1）
- **B9 多连接竞态**：阶段 2 start None → ERROR 提示 + clear pending
- **B5 点位红线代码级收口**：`_sanitize_metric_projection` 剥离绝对点位（负向前瞻排除周/天/月/年/倍；不含 ％；冗余清理），只作用于 chat 路径 `_render_facts`，`render_prediction_markdown` 不触碰（B2 溯源验证消费方）；`PREDICTION_CHAT_PROMPT` 硬性禁止绝对点位
- **A3 死代码**：移除 chat 路径 `_compute_due_dates` 调用（v1 不落库无消费方），`run_predict` 不受影响
- **A1② 免责恰好一次**：`_build_predict_section` 去逐节 append，`_synth_multi_goal` 合并后按 `predict`（dimension 过滤）追加一次
- **A2 对比闸门**：注释 + 锁定用例（"茅台和五粮液哪个更好，会涨吗"→仅 compare_stocks 无 prediction；原句"哪个会涨"走 confirm 消费预测属既有路径）
- **B8**：app-api `DELETE /api/user/profile`（PIPL 删除权）+ `delAgentProfileCache`（db=1 专用连接 DEL `user_profile:{userId}`，PUT/DELETE 双失效，工厂可注入）；app-frontend `deleteUserProfile()` + profile.vue"AI 个性化服务"说明与删除入口
- **B3**：manifest.json mp-weixin 声明 WechatSI（0.3.5/wx069ba97219f66d99，发布前后台核验）
- **B10**：`.env.server` 入 .gitignore；ConfirmSheet.vue 注释订正
- 验证：agent-py 定向 153 passed + 全量 1796 passed/12 failed（integration 既有基线零交集）+ ruff 0 新增；app-api tsc 0 + profile 14/14；app-frontend type-check 0 + build:h5 ok + vitest 221 passed（2 suite 为 mp-html 既有编译基线）；逐任务审查全部 Approved + 最终整仓审查 **Ready with fixes → 已修**（E501 折行/注释行号/docblock/返回类型）
- **验收状态**：Phase 4 **代码验收通过（修复后复审）**，待 V1 部署窗口生产验证（组成条件：问题 18 回归 + 三子项生产 WS 冒烟 + confirm/resume 变体时序 + B8 缓存失效实证 + B5 无点位冒烟）

## 2026-08-12 Phase 4 验收辩论数字口径修订（B6，验收裁决）
- "test_prediction_service.py 复跑 76 passed" → 该文件实为 14 个 test 函数（gate 用例 parametrize 展开 17 cases）；
  Phase 4 定向复跑口径改为"10 个 Phase 4 测试文件合计 76 passed"
- "profile 定向 15/15" → profile.spec.ts 实为 11 个 it()（GET 3 + PUT 8）
- Phase 4-3 "HEAD 22 ⊆ BASE 30"：8 个失败差异为路径/内存地址文本差异归一化（BASE 30 中 8 个属
  环境相关基线），HEAD 新增失败为 0——后续 A/B 记录必须附 BASE/HEAD commit 号与失败集明细
- A/B 复现命令：git worktree add <wt> BASE_COMMIT；HEAD 与 BASE 分别
  `$env:PYTHONPATH="<repo>/src"; python -m pytest tests/unit tests/integration --ignore tests/unit/test_tenx_tools -q`
  后 diff 失败集（HEAD 失败集 ⊆ BASE 即新增清零）

## 2026-08-12 Phase 4-3 全局用户记忆（user profile，3 commits 2445417 + d9be256 + 4393ad9，changer 未 push）
- **Task 3 拉取+注入（2445417）**：`data_client.get_user_profile(user_id)`（Redis 缓存 `user_profile:{user_id}` TTL 300s → GET /internal/user-profile/{user_id}；失败 None 不阻断）；`QuestionState.user_profile` 可选字段；ws.py 阶段 1/2 + routes.py（/chat/message、/chat/stream/messages）入口注入
- **Task 4 消费（d9be256）**：qa_router `_build_user_profile_context`（profile 存在时 prompt 追加称呼/投资偏好/风险偏好，None 字节不变）；synth_answer 风险段三档（`RISK_DISCLAIMER_CONSERVATIVE` conservative 强化，优先于 strong，三档互斥去重）+ `_sort_goals_by_preferences` 多子目标按偏好重排（不改 evidence 关联）
- **fix 防跨轮污染（4393ad9）**：注入改为无条件显式赋值——匿名写 None 覆盖 checkpointer 旧值（条件注入会在同 thread 多轮间残留上一轮登录态画像，集成冒烟实证：匿名轮误出 conservative 风险段）
- 测试：`test_data_client_user_profile.py`（4）、`test_ws_user_profile.py`（3）、`test_qa_router_user_profile.py`（7）、`test_synth_answer_user_profile.py`（11）全绿；相关回归 275 passed
- 验证：全量 A/B HEAD 22 failed ⊆ BASE 30（路径/内存地址文本差异归一化后新增清零）；ruff 改动文件 0 新增；**集成冒烟 SMOKE_AGENT_OK**（登录态 conservative 风险段生效 / 匿名常规档零行为变化）

## 2026-08-12 Phase 4-2 final review 修复（I-1，commit c19b9b9，changer 未 push）
- **I-1 HTTP/SSE 降级路径 confirm 回归**：qa_router 触发 confirm 是传输无关的，但 confirm 两阶段协议是 WS 专属——`routes.py` `/chat/message` 与 `/chat/stream/messages` 的 DONE 原先不认识 `confirm` 终态 → 同消息在 HTTP 路径从"有用澄清"退化为"抱歉，我暂时无法处理您的请求。"（严格劣化回归）
- 修复：两处处理器检测 `final_response` 为空且 `result/values.get("confirm")` 非空 → `final_response = _STOCK_SYMBOL_CLARIFICATION`（与 WS confirm_timeout 回退同文本）；import 自 qa_router
- 测试：`tests/unit/test_routes_sse_done_token_usage.py` +2（SSE confirm 降级 / 无 confirm 原样透出）、`tests/e2e/test_chat_message.py` +1（HTTP confirm 降级）；TDD RED→GREEN
- 验证：定向 51 passed；全量 A/B HEAD 失败集 = BASE（30=30）新增清零（1829→1832 passed）；ruff 改动文件 All checks passed；confirm WS 冒烟复测 5/5（case2「都不是」澄清回退即本路径）

## 2026-08-12 Phase 4-2 交互式确认（改进 13，3 commits c742a93..232e361，changer 未 push）
- 两阶段运行设计（spec §4.2 已按 Phase 2 实际协议修订）：阶段 1 图终态负载 `confirm_request`（替代 DONE）→ ws.py `_wait_confirm_response`（60s 单调时钟 deadline + FIRST_COMPLETED + ValueError 捕获 + `_owns_run` 归属）→ 阶段 2 携带 `confirm_choice` 重跑同 session → DONE；超时/「都不是」→ `confirm_timeout` 重跑回退既有澄清
- `qa_router.py`：confirm 触发（闸门 2 resolve-miss + 多候选 ≥2 可 resolve → confirm，options=可解析名称 + 「都不是」；<2 维持澄清）+ 消费（confirm_choice 直接构造 SkillCall；confirm_timeout 经 `_resolve_miss_clarification` 无条件回退澄清，不依赖 `len(messages)` 守卫）+ transient 三字段归零
- `synth_answer.py`：confirm 短路（在 goal is None 检查之前返回 confirm 不渲染）
- `ws.py`：`_run_chat_graph_to_events` 加 run_id 参数 + confirm_request 终态早返回（跳过落库）；阶段 2 重跑 `initial_state2["messages"] = []`（空列表对 add_messages 是 no-op，防消息重复污染 checkpoint 历史）+ reset_transient_state + reset_token_usage + 新 run_id 后缀 `_confirm`
- `deps.py`：`_TRANSIENT_KEYS` 补 confirm/confirm_choice/confirm_timeout（防 SSE 残留短路）
- `chat_schema.py`：QuestionState 加 confirm 字段（dict | None）
- 验证：定向 4 新测试文件全绿（test_chat_transient_reset/test_qa_router_confirm/test_synth_answer_confirm/test_ws_chat_confirm）；全量 A/B HEAD 失败集 = BASE（30=30，含 env 相关基线失败）**新增失败 0**（1808→1829 passed）；ruff 改动文件 All checks passed；WS confirm 冒烟 5/5（case1 点选续跑真实行情 / case2「都不是」澄清回退 / case3 非触发回归，每用例独立 session）

## 2026-08-12 Phase 4-1 对话内预测打通（8 commits c4b1030..d29597d，changer 未 push）
- 产品边界（2026-08-11 用户拍板）：影响持续性推演非点位预测 / 固定免责声明+低置信提示 / v1 不落库
- 契约：`chat_contract.py` 三处 Literal 追加 "prediction"；`PredictionResult` 复用（extra="forbid"）
- `prediction_service.py` 新增无溯源入口 `run_chat_prediction(snapshot, news, context)`：门禁 quote 必填/flow 可选（指数无资金流属"不适用"）；后处理强制 hypothesis + evidence_ids 过滤；到期日 best-effort（2027 超日历范围不阻断，v1 不落库无消费方）；**quick_think 单次调用（spec §3.4 P10 计费口径，deep_think 26-47s UX 不可接受）+ `with_chat_structured_output` json_mode（DeepSeek thinking 兼容）**
- `skills/prediction.py`：并发 ainvoke get_quote/get_capital_flow 组快照（指数走 `/internal/index/quotes`，仅由显式 index_name 触发防 000001 平安银行误判）；三段式 facts + 免责声明 + low 置信提示；降级复用 PREDICT_DEGRADED_HINT；registry 末尾注册
- `qa_router.py`：intent_map 加 prediction 键；`_build_default_skill_call` prediction 分支（无标的不硬塞）；**C2 闸门 1/2 短路主入口追加 prediction SkillCall（goal_id="g2"）**（"茅台会涨吗/上证后市如何"三段式可达）；**E1 `_build_gate4_context` 去掉"不指定预测 skill"压制文案**
- `synth_answer.py`：`_build_predict_section` 重写——prediction Evidence（skill_name 定位，fallback goal_id="g2"）非 degraded → 三段式渲染（现状趋势[validate] + 影响持续性[三档/置信/风险] + 免责声明恰好一次）；degraded/缺失 → D35 降级字节不变；多 predict 子目标 hint 只一次
- `prompts/workers/prediction.py`：新增 PREDICTION_CHAT_PROMPT（现状快照驱动 5 段思维链；required 字段含 schema_version:"1.0"——冒烟实测缺该字段恒降级）
- 验证：全量 A/B HEAD 28 failed ⊆ BASE 28 failed（新增清零，1776→1807 passed）；ruff 改动文件 0 新增；WS 冒烟 4/4（个股/指数三段式 + 科普防误伤 + 非预测不变）；spec 验收 1-5 全满足
- 辩论裁决回写：C2（闸门 1/2 主入口 prediction SkillCall）/ E1（gate4 去压制）均已落地并有 WS 实证

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

## 2026-08-12 问题 18 WS recv 竞态修复（Phase 2 回归补丁）
- 根因：_forward_until_done_or_cmd（ws.py#L291-292）recv_task.cancel() 后未 await 收尾即 return，主循环随即 receive_json() → uvicorn RuntimeError "cannot call recv while another coroutine is already waiting" → 每轮 done 后 WS 连接崩溃（closeCode=1005，Phase 3 生产冒烟 9 轮全部实证）
- 修复：cancel 后 await asyncio.gather(recv_task, return_exceptions=True) 再 return（ws.py#L293-296）；不改 resume/stop/归属校验协议与事件协议，前端零改动
- 测试：test_ws_chat_replacement.py 新增 _RecvTrackingWebSocket（模拟 uvicorn 并发 recv 防护）+ test_forward_until_done_or_cmd_clears_pending_recv_on_done（返回时无挂起 recv / 主循环可安全发起下次 receive / 不抛 RuntimeError）
- 改动文件：src/aistock_agent/api/ws.py + tests/unit/test_ws_chat_replacement.py
