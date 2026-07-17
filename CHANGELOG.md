# Changelog — aistock-agent-py

> 所有修改记录按时间倒序排列。每条记录标注分支、时间区间、开发者。

## [main] 2026-07-17 — 修复 CHANGELOG.md 残留 git 冲突标记
**开发者**: Aria

### 修复
- `CHANGELOG.md`：移除第 158 行孤立的 `>>>>>>> origin/main` 冲突标记（合并时遗留）

---

## [changer] 2026-07-16 — 板块别名扩展
**开发者**: 37588

### 改进
- `src/aistock_agent/data/sector_aliases.json`：石油石化板块新增"煤炭/油气"别名映射；新增"AI手机/消费电子"板块类别，映射"传媒/端侧AI"别名

---

## [changer] 2026-07-15 — podcast_brief 确定性校验 + title 清洗 + 持久化门控
**开发者**: 37588

### 修复
- `src/aistock_agent/agents/workers/event.py`：新增 `_validate_podcast_brief()` 确定性校验（len() 150-200），超限智能截断/不足从事实补齐，不可修复时跳过持久化；新增 `_truncate_at_sentence_boundary()` 句尾截断；`_generate_podcast()` 失败回退为空字符串（非降级占位文本）
- `src/aistock_agent/agents/workers/event.py`：title 来源改为 `understanding.summary`（纯业务标题），缺失时降级为空并跳过持久化；新增 `can_persist` 门控（title 非空 + brief ∈ [150,200] 才缓存+持久化）
- `src/aistock_agent/agents/workers/morning.py`：`_validate_podcast_brief()` 增强为智能截断（在句号/分号处断句），超限优先找 150+ 字符的断句点
- `src/aistock_agent/agents/workers/morning.py`：agent.ainvoke 新增 `recursion_limit=50`（晨报需大量工具调用）
- `src/aistock_agent/prompts/workers/morning.py`：新增 podcast_brief 字数硬约束说明（150-200）+ 参考示例

### 改进
- `src/aistock_agent/data/sector_aliases.json`：新增"科技"板块别名映射（存储芯片/光刻机/先进封装/第三代半导体/光刻胶/汽车芯片/国家大基金持股）
- `scripts/run_morning_test.py`：手动初始化 RedisPool + HttpClientPool，finally 块释放连接

### 测试
- `tests/integration/test_event_agent.py`：重写测试，新增 P1 用例（brief 校验边界/句尾截断/从事实补齐/不可持久化/标题清洗/空标题门控）

---

## [changer] 2026-07-14 — Event Agent v3 持久化重构：event_id 隔离 + 完整 analysis_reports 写入
**开发者**: 37588

### 改进
- `src/aistock_agent/services/event_persister.py`：重构 `persist_event_report()`，改为以 event_id 作为隔离键（复用 Node.js user_id 列），同日不同事件分别保存、同一事件重跑 upsert；写入完整事件元数据（eventId/title/source/publishTime/event）和完整 analysis_reports（四模块），data_source 升级为 event_agent_v3
- `src/aistock_agent/agents/workers/event.py`：`run()` 中调用 `persist_event_report()` 改为传递 event_id、event_meta、analysis_reports，删除废弃的 display_report 变量

### 新增
- `tests/unit/test_event_persister.py`：event_persister 单元测试

### 文档
- `README.md`：目录结构注释 event.py v2→v3，新增 event_persister.py，data_client.py 标注 post 支持

---

## [changer] 2026-07-14 — 晨报双层输出与公共报告持久化
**开发者**: 37588

### 新增
- `src/aistock_agent/services/morning_persister.py`：`persist_morning_report()` 调用 Node.js `/internal/analysis-reports` 持久化晨报（report_type=morning, user_id=null），非关键路径失败静默跳过

### 改进
- `src/aistock_agent/prompts/workers/morning.py`：追加「最终输出格式」指令，要求 LLM 输出 JSON 双层结构（display_report + podcast_brief + schema_version），details 内保留 MAJOR_EVENTS/SECTOR_LIST 标记
- `src/aistock_agent/agents/workers/morning.py`：全量重写 `run()`，复用 `parse_event_output()` 解析双层 JSON；`_ensure_dual_layer()` 兼容缓存中旧纯文本；`_validate_podcast_brief()` 校验 150-200 字；新增 `persist_morning_report()` 调用；归档改为 details 文本
- `tests/integration/test_morning_agent.py`：全量重写，24 个测试覆盖双层生成、podcast_brief 字数、持久化参数、缓存命中、JSON 解析失败降级等
- `README.md`：更新晨报调度说明、目录结构、输出归档说明

### 验证
- `pytest tests/integration/test_morning_agent.py -v` → 24 passed
- `ruff check` 4 个变更文件 → All checks passed

---

## [changer] 2026-07-14 — 迭代 Agent 输出契约优化：确定性评分卡 + LLM 输出清洗
**开发者**: 37588

### 改进
- `src/aistock_agent/services/iterate_analyzer.py`：新增 `build_scorecard()` 构建四维确定性评分卡；新增 `_sanitize_llm_output()` 以 `check_thresholds()` 为唯一真相清洗 LLM 输出；`analyze()` normal/alert 路径均注入 scorecard
- `src/aistock_agent/prompts/workers/iterate.py`：重写 ITERATE_PROMPT，约束 LLM 只分析已触发维度；suggestions 要求标注 dimension 字段；新增 observations 字段
- `tests/unit/test_iterate_threshold.py`：新增 8 个测试覆盖 build_scorecard 和 _sanitize_llm_output
- `tests/integration/test_iterate_agent.py`：新增 3 个测试覆盖 normal/alert scorecard 和核心过滤降级场景
- `AGENT_STANDARDS.md`：更新迭代 agent 输出契约，新增确定性评分卡 + 输出清洗规则表

### 验证
- `pytest tests/unit/test_iterate_threshold.py tests/integration/test_iterate_agent.py -v` → 21 passed
- `ruff check` → All checks passed

---

## [main] 2026-07-12 — Agent 报告双层输出改造 + 文档同步 + 单测修复
**开发者**: 尹辰

### 新增
- `src/aistock_agent/utils/report_parser.py`：双层报告解析工具，兼容 schema_version 1.0（单层 text）和 2.0（双层 display_report + podcast_brief），提供 4 个函数（parse_report_content / extract_podcast_brief / extract_display_report / parse_dual_layer_response）
- `tests/unit/test_report_parser.py`：20 个单测全部通过
- `docs/superpowers/plans/2026-07-12-agent-report-persistence.md`：持久化实施计划

### 修改 — Agent 报告双层输出改造
- `src/aistock_agent/prompts/workers/wind_leader.py`：提示词增加双层 JSON 输出格式要求
- `src/aistock_agent/agents/workers/wind_leader.py`：持久化 content 从 `{"text": final_response}` 改为 `parse_dual_layer_response(final_response)` 双层结构
- `src/aistock_agent/agents/workers/broadcast.py`：`_fetch_report_from_db` 优先读取 podcast_brief，降级读取 display_report（兼容旧数据）
- `src/aistock_agent/agents/workers/ai_advisor.py`：`_fetch_relevant_reports` 使用 `extract_display_report` 读取展示文本（兼容旧数据）
- `src/aistock_agent/tools/news_tools.py`：补上 `search_cls_news` 的 advisor 分类注册

### 修复
- `src/aistock_agent/config.py`：`model_config` 添加 `"extra": "ignore"`，解决 git pull 删除 volc_tts_* 字段后环境变量中仍有旧变量导致 pydantic 验证错误的问题
- `tests/unit/test_ai_advisor.py`：`test_run_exception_returns_fallback` 改为 mock `get_deep_think` 抛出异常（因 `_fetch_relevant_reports` 内部 try-catch 会吞掉 node_api 异常，无法触发顶层 try-catch）

### 文档
- `README.md`：新增"智能投顾Agent（ai_advisor_agent）"章节、"报告双层输出（schema_version 2.0）"章节；更新播报Agent章节说明消费 podcast_brief；更新目录结构添加 ai_advisor.py、alert.py、report_parser.py
- `AGENTS.md`：降级文本表补充 alert；产品功能映射表更新 alert_agent 状态为"已实现"
- `AGENT_STANDARDS.md`：新增"补充规范 14：报告双层输出"章节（content 结构、字段用途、解析工具、LLM 输出要求、持久化/消费方改造模板、改造状态、禁止项）；目录添加规范14链接；附录A目录结构更新

### 验证
- 25 个单测全部通过（20个 report_parser + 5个 ai_advisor）

---

## [main] 2026-07-09 — Agent 报告持久化架构 + 机构调研/播报/风口 Agent + 空数据预检

### 新增
- `src/aistock_agent/services/data_guard.py`：通用空数据预检模块（DataCheck dataclass + ensure_data_available 函数，3 次重试 + 调刷新接口）
- `scripts/run_broadcast_test.py` / `run_broadcast_test.bat`：播报生成测试脚本（双人对话 + TTS 语音输出）

### 修改 — Agent 报告持久化（Phase 2）
- `src/aistock_agent/agents/workers/morning.py`：scheduler 触发时持久化晨报到 DB
- `src/aistock_agent/agents/workers/wind_leader.py`：scheduler 触发时持久化风口报告到 DB
- `src/aistock_agent/agents/workers/hot_burst.py`：scheduler 触发时持久化机构调研报告到 DB
- `src/aistock_agent/agents/workers/review.py`：scheduler 触发时持久化复盘报告到 DB

### 改进 — 播报链路改造（Phase 3）
- `src/aistock_agent/agents/workers/broadcast.py`：双链路读取报告（scheduler 从 DB 读，实时请求降级到 state.analysis_reports）+ Node.js 内部 TTS 调用
- `src/aistock_agent/prompts/workers/broadcast.py`：播报提示词更新（双人对话格式）
- `src/aistock_agent/services/scheduler.py`：新增 09:00 播报串行链路（morning→wind_leader→hot_burst→broadcast，trigger_source="scheduler"，异常独立捕获）
- `src/aistock_agent/config.py`：新增 scheduler_broadcast_cron 配置（"0 9 * * 1-5"，9:10 前端可见）
- `src/aistock_agent/constants.py`：INTENT_SET 新增 hot_burst + TOOL_LABELS 新增 get_hot_burst/get_hot_burst_history

### 文档
- `AGENT_STANDARDS.md`：新增规范 13 空数据预检（可选，hot_burst 和纯外部 API 的 agent 豁免）+ 目录结构添加 data_guard.py
- `README.md`：播报 Agent 文档（音频路径 + 测试命令）；定时调度表新增 09:00 播报链路；目录结构新增 data_guard.py
- `AGENTS.md`：broadcast 状态改为"已实现"；降级文本表新增 review 和 broadcast 行
- `scripts/run_morning_test.bat`：微调

## [junliang] 2026-07-09 — 新增 alert_agent（异动提醒 Agent）
**开发者**: yueqili778-arch

### 新增
- `agents/workers/alert.py`：alert_agent，三步异动分析框架（发生了什么→为什么→怎么办），按短/中/长线分类，deep_think + ReAct
- `prompts/workers/alert.py`：ALERT_ANALYST_PROMPT，定义三步框架 + 周期分类 + 输出要求
- `api/routes.py`：新增 `GET /briefing/alert?symbol=xxx&cycle=short` SSE 流式端点
- `tests/integration/test_alert_agent.py`：5 个集成测试（工具绑定/提示词注入/响应提取/入口校验/deep_think 验证）

### 修改
- `tools/monitor_tools.py`：追加 `register("alert", ...)` 注册
- `tools/stock_tools.py`：追加 `register("alert", get_quote)`、`register("alert", get_capital_flow)`
- `tools/news_tools.py`：追加 `register("alert", search_cls_news)`
- `graph/builder.py`：注册 `alert_agent` 节点并加入 END 链路
- `graph/routers/intent_router.py`：添加 `alert` 意图 + 路由映射
- `prompts/supervisor/routing.py`：添加 alert 意图描述
- `constants.py`：INTENT_SET 补 alert/hot_burst，TOOL_LABELS 补 alert 工具标签
- `tests/unit/test_constants.py`：同步 INTENT_SET 断言

### 验证
- `pytest tests/integration/test_alert_agent.py`：5/5 通过
- `ruff check src/aistock_agent/agents/workers/alert.py`：All checks passed
- `mypy src/aistock_agent/agents/workers/alert.py`：Success, no issues found

---

## [changer] 2026-07-09 — 复盘工具 + Registry 自注册（SDD Task 1）
**开发者**: 37588

### 新增
- `src/aistock_agent/tools/review_tools.py`：复盘专用工具模块
  - `get_market_summary`：yfinance 获取 A 股主要指数（上证指数/深证成指/创业板指/科创50）行情，用于收盘复盘
  - `get_sector_performance`：调用 Node.js `/internal/wind-leaders` 获取热门板块涨幅 + 龙头股，用于复盘板块归因
  - 底部 `register("review", ...)` 自注册：跨分类复用 `tavily_finance_search` / `get_global_markets` / `get_cls_news` + 两个新工具
- `tests/unit/test_review_tools.py`：4 个单元测试（mock yfinance / mock node_api），覆盖成功/部分失败/空数据场景

### 改进
- `src/aistock_agent/tools/__init__.py`：导入列表新增 `review_tools`（按字母序，位于 `news_tools` 与 `search_tools` 之间），触发 review category 自注册

### 验证
- ruff check：All checks passed
- mypy：Success，2 source files 无问题
- pytest：test_registry.py (11) + test_review_tools.py (4) = 15 passed；全量 306 passed（2 个预存失败与本次无关：test_constants / test_sector_agent 的 wind_leader/broadcast 意图）

---

## [changer] 2026-07-08 — SDD 基础设施：Tavily 拆分 + Tool Registry + APScheduler 定时调度
**开发者**: 37588

### 新增
- `src/aistock_agent/services/tavily.py`：Tavily 客户端封装层（TavilyService.search），从 market_tools 抽出，支持多 key 轮换
- `src/aistock_agent/tools/search_tools.py`：`tavily_finance_search` 从 market_tools 迁移，底层委托 TavilyService
- `src/aistock_agent/tools/registry.py`：Tool Registry 工具注册中心，按 category 分组（morning/stock/sector/event/iterate），支持 `get_tools("category")` / `get_tools()` 全量 / 直接 import 三种模式
- `src/aistock_agent/services/scheduler.py`：APScheduler AsyncIOScheduler 定时调度，4 个交易日任务（08:50 晨报 / 15:30 复盘 / 15:35 快照 / 15:40 迭代），非交易日自动跳过
- `tests/unit/test_tavily_service.py`：3 个 mock 测试
- `tests/unit/test_search_tools.py`：3 个测试（成功/空结果/异常）
- `tests/unit/test_registry.py`：9 个测试（category/去重/引用一致性/事件工具集）
- `tests/unit/test_scheduler.py`：4 个测试（单例/job 注册/非交易日跳过/交易日执行）
- `docs/superpowers/specs/2026-07-08-review-iterate-agent-design.md`：复盘/迭代 agent 设计规范
- `docs/superpowers/plans/2026-07-08-infra-tavily-registry-scheduler.md`：基础设施实现计划

### 重构
- `src/aistock_agent/tools/market_tools.py`：移除 `tavily_finance_search`，回归纯 yfinance 行情职责
- `src/aistock_agent/agents/workers/morning.py`：工具列表改为 `get_tools("morning")`
- `src/aistock_agent/agents/workers/stock.py`：工具列表改为 `get_tools("stock")`
- `src/aistock_agent/agents/workers/sector.py`：工具列表改为 `get_tools("sector")`
- `src/aistock_agent/agents/workers/event.py`：工具列表改为 `get_tools("event")`

### 改进
- `src/aistock_agent/main.py`：lifespan 集成 start_scheduler/shutdown_scheduler
- `src/aistock_agent/config.py`：新增 6 个调度配置项（scheduler_enabled + 4 cron + timezone）
- `src/aistock_agent/api/routes.py`：list_skills import 排序修正
- `pyproject.toml`：dependencies 新增 apscheduler==3.10.4
- `README.md`：Mermaid 拓扑图、工具注册中心、调度器章节、环境变量表更新
- `AGENT_STANDARDS.md`：Tavily 归属更新、Tool Registry 注册规范、mock 路径更新、目录结构更新、类型注解同步

### 修复
- `ruff I001`：routes.py list_skills 函数内 import 排序（monitor → news → search）
- `mypy type-arg`：registry.py 4 处 bare `list` → `list[BaseTool]`
- `mypy attr-defined`：search_tools.py `result["results"]` cast 为 `list[dict[str, str]]`
- `mypy import-untyped`：scheduler.py apscheduler 2 处 import 加 `# type: ignore`

### 验证
- `ruff check src/`：All checks passed
- `mypy src/`：Success, no issues in 74 source files
- `pytest tests/`：293 passed in 3.68s

---

## [changer] 2026-07-08 — Task 5 review fix: X-Request-ID on 500 responses + OPTIONS assertion
**开发者**: 37588

### 修复
- `src/aistock_agent/api/middleware.py`：`request_id_middleware` 新增 try/except 捕获未处理异常，返回 500 JSONResponse 并注入 X-Request-ID header（主修复）。根因：Starlette 的 ExceptionMiddleware 跳过 Exception 类型 handler（由 ServerErrorMiddleware 处理），而 ServerErrorMiddleware 位于用户中间件栈外，其 500 响应不流经 request_id_middleware
- `src/aistock_agent/api/middleware.py`：新增 `global_exception_handler` 防御性全局异常处理器（注册到 ServerErrorMiddleware），确保边缘场景返回 JSON 而非纯文本
- `tests/e2e/test_middleware.py`：`test_cors_preflight_options` 新增 X-Request-ID 断言（Finding 2）
- `tests/e2e/test_middleware.py`：新增 `test_request_id_present_on_500_response` 验证 500 响应携带 X-Request-ID
- `tests/e2e/test_middleware.py`：更新 `test_contextvar_cleanup_even_on_exception` 适配新的异常捕获行为

### 验证
- `pytest tests/ -v`：250/250 通过
- `ruff check src/`：All checks passed
- `mypy src/`：Success, no issues found in 66 source files

---

## [changer] 2026-07-06 — 清理晨报工具注释并将测试输出归档到 docs
**开发者**: changer-collab

### 改进
- `src/aistock_agent/tools/news_tools.py`：`get_cls_news` 移除"Node.js 接口未实现"的 NOTE 注释，空数据提示从"接口未实现"改为"暂无财联社快讯"
- 测试输出归档：新增 `docs/agent-outputs/morning/2026-07-06-briefing.md`，存放 `morning_agent` 生成的真实晨报样本，便于后续对比和审阅

### 验证
- `pytest tests/ -v`：23/23 通过
- 端到端 `GET /api/agent/briefing/morning`：成功生成晨报，调用 `get_global_markets`、`get_cls_news`、`tavily_finance_search` 等工具，输出 3176 字符完整报告

---

## [changer] 2026-07-06 — 修复工具字段映射 bug（stock_analyst LLM "数据不可用" 根因）
**开发者**: changer-collab

### Bug 修复
- **根因（双重 bug）**：
  1. `services/data_client.py` 的 `get()` 返回整个 `{code, data}` 响应，工具函数直接对整个响应取字段，永远拿不到业务数据
  2. `tools/stock_tools.py` 和 `tools/sector_tools.py` 的 `_format_*` 函数字段名与 Node.js `/internal/*` 实际返回完全不匹配（英文 key vs 中文 key）
- **影响**：所有 4 个工具文件（stock/news/sector）的格式化函数都返回默认值"-"或"未知"，LLM 看到后判断"数据暂不可用"
- **修复**：
  - `data_client.py`：`get()` 解包 `data` 字段，返回业务数据；增加 `code != 200` 业务错误日志
  - `stock_tools._format_quote`：用中文 key（`股票简称`/`最新价`/`涨跌幅`）
  - `stock_tools._format_capital_flow`：用新浪字段（`r0_in`/`r0_out`/`netamount`）
  - `stock_tools._format_forecast`：用同花顺字段（`摘要` + `业绩预测详表_详细指标预测`），输出完整预测表
  - `sector_tools._format_leaders`：兼容 `tag_code`（Node.js 实际返回）和 `tag_name`
  - `news_tools.get_cls_news`：加注释说明 `/internal/news/latest` 接口在 Node.js 未实现（404），待补充
- **测试**：`test_stock_tools.py` 3 个用例的 mock 数据同步更新为 Node.js 真实字段格式

### 验证
- `pytest tests/ -v`：23/23 通过
- 端到端 `/api/agent/chat/message`（"分析 600519 贵州茅台"）：LLM 正确解读真实数据，生成包含行情/资金流/机构预测/新闻的综合分析报告（主力净流出 7.07 亿、46 家机构预测 EPS 68.82 元、5 条真实新闻）

---

## [changer] 2026-07-06 — 清理 deprecation 警告（lifespan 迁移 + pytest 配置）
**开发者**: changer-collab

### 重构
- `src/aistock_agent/main.py`：`@app.on_event("startup")` → `lifespan` async context manager（FastAPI 已弃用 on_event，推荐 lifespan）
- `pyproject.toml`：新增 `[tool.pytest.ini_options]`，显式设置 `asyncio_mode = "strict"` 和 `asyncio_default_fixture_loop_scope = "function"`，消除 pytest-asyncio 0.25 的默认值警告

### 验证
- `pytest tests/ -v`：23/23 通过，0 警告（修复前有 2 个 on_event deprecation + 1 个 asyncio loop scope 警告）
- `curl /health` + `curl /api/agent/skills`：lifespan 启动钩子正常触发，9 个工具全部注册

---

## [changer] 2026-07-05 — 移除冗余 AGENTS.md，加入 .gitignore
**开发者**: changer-collab

### 文档
- 删除 repo 根级 AGENTS.md（与 README.md 内容重叠 80%+，维护两份易漂移）
- .gitignore 新增 AGENTS.md 忽略项
- 跨仓库约定（git 分支策略等）改由项目根 AGENTS.md 和 project_memory.md 承载（不在 git 仓库内）

---
