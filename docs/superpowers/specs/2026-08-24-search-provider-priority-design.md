# 设计文档：搜索 Provider 优先级可配 + 搜索观测计数

> 日期：2026-08-24
> 范围：aistock-agent-py
> 目标问题：#3「国内政策面证据不足，tavily_domestic_policy 为空」的**本质**是搜索额度与调用链路问题，非取数规则问题。

## 1. 背景与问题本质

复盘/大盘溯源（`review`）链路的 `tavily_domestic_policy` 为空，直接现象是国内政策/全球风险证据缺失。排查后发现根因不在 `market_trace_snapshot` 的取数规则，而在**搜索供应商额度与调用顺序**：

- 全系统有大量调用点会打 `TavilyService.search` / `tavily_finance_search`：
  - `market_trace_snapshot`（复盘 full/quick 各 2 次：国内政策 + 全球风险）
  - 统一事件抓取中台 `event_scrape_*`（盘前 + 每小时 + 收盘）
  - `morning` / `event` / `alert_news` / `advisor` / `broadcast` 的 ReAct 循环按需
  - 对话 `general` 缺口模式（用户触发）
- `_build_providers`（`services/tavily.py`）**硬编码链路顺序 `tavily→doubao→anysearch`**，tavily 恒排首位。
- tavily **月度额度已耗尽** → 每个调用点先打 tavily → 429 → `RateLimited` → key 冷却 5s；`KeyPool.select_key` 全冷却时有 fail-open 仍会再打一次最旧 key。
- `search_query` 有 `budget_seconds=10.0` 总预算，tavily 反复 429 消耗预算，可能未走到 doubao/anysearch 就 `budget_exhausted`；即便走到，三个源同时 429 的瞬间也会整链失败 → 上层降级 → `tavily_domestic_policy` 进 missing。

**已确认事实**：`tavily / doubao / anysearch` 三个 key 均已配置，failover 链路本应完整，只是顺序把额度紧张的 tavily 放在首位。

**业务决策（用户拍板）**：`anysearch` 每日 1000 次额度充足，应作为首选；tavily 月度已透支，退为兜底；doubao 紧随其后。tavily 不再承担日常高频调用，避免月度额度继续被消耗。

## 2. 决策（方案 A：按配置顺序建链）

实现方式选定 **选项 A**：

- 让 `SEARCH_ENABLED_PROVIDERS` 从「仅启停」升级为「启停 + 顺序」。
- `_build_providers` 遍历该配置按出现顺序追加 provider。
- 生产配置 `SEARCH_ENABLED_PROVIDERS=anysearch,tavily,doubao` → 每次调用先打 anysearch，tavily/doubao 兜底。
- 保留 `if not chain: chain.append(TavilyClientProvider())`（配置全空时保底 tavily）不变。

**为什么不选 B（独立 `SEARCH_PROVIDER_ORDER`）**：多一个配置字段增加接口面，且当下无启停与顺序分离的诉求；选项 A 一处开关即可切换，语义直观。

**为什么不选 C（仅 anysearch）**：anysearch 万一 429/接口变更时整链立即无兜底，风险偏高；保留 tavily/doubao 作兜底更稳。

## 3. 改动点

### 3.1 `services/tavily.py::_build_providers` — 按配置顺序建链

- 现状：`if "tavily" in enabled: ... if "doubao" in enabled: ... if "anysearch" in enabled: ...`（固定顺序）。
- 改为：解析 `settings.search_enabled_providers` 的逗号分隔顺序，按出现先后 append provider；集合成员判断改为顺序遍历。
- 空值默认仍回落到 `{"tavily", "doubao", "anysearch"}`（保底顺序 tavily→doubao→anysearch，与现状一致）。

### 3.2 `services/search_service.py::search_query` — 埋观测计数

在 `search_query`（当前为纯函数）内新增计量调用，通过 `get_metrics_collector()`（线程安全单例）写入：

| 事件 | 计数 |
|------|------|
| 每尝试一个 provider（进入 for 循环体） | `record_search_attempt(provider.name)` |
| `budget.expired()` 命中 | `record_search_budget_exhausted()` |
| `result.outcome == "empty"`（有 hits 为空） | `record_search_empty()` |
| provider.search 抛异常（含 429/网络/解析） | `record_search_failed(provider.name)` |

注意：`search_service.py` 当前不 import metrics 模块（保持纯函数）。需引入 `get_metrics_collector()`；若引入形成循环依赖，则改为在 `TavilyService.search`（`services/tavily.py`）做计量，或通过惰性 import。**实现时以不破坏模块依赖为准**，计量点可选其一。

### 3.3 `observability/metrics.py` — 新增搜索指标

- 新增计数器：
  - `_search_attempts: dict[str, int]`（按 provider 分桶）
  - `_search_failed: dict[str, int]`（按 provider 分桶）
  - `_search_budget_exhausted: int`
  - `_search_empty: int`
- 新增方法：`record_search_attempt(provider)` / `record_search_failed(provider)` / `record_search_budget_exhausted()` / `record_search_empty()`。
- `get_metrics()` 返回新增 `"search": {...}` 块（attempts by provider / failed by provider / budget_exhausted / empty）。
- `reset()` 同步清空上述计数。

### 3.4 文档同步

- `config.py` 第 117-120 行注释：`SEARCH_ENABLED_PROVIDERS` 语义从「只控制启停、不控制顺序」更新为「控制启停 + 顺序」。
- `aistock-agent-py/AGENTS.md`「搜索多供应商 failover 配置」段同步：链路顺序由该配置决定，不再硬编码。

## 4. 不改动范围

- **不触碰** `market_trace_snapshot.py` 取数规则、`phenomenon_discovery.py` 阈值、`prompts/workers/review.py`。
- 不新增第三个搜索源，不改变及侧 `tavily_finance_search` 工具输出契约（逐字节稳定）。
- 本次不解决 #1（外盘时间顺序）、#2（主力资金细分）、#4（涨跌停情绪）。

> #1/#2/#4 后续单独评估，用户约定使用 design-debate 流程。

## 5. 测试

- `tests/unit/test_search_contract.py`：补**顺序敏感**用例——`SEARCH_ENABLED_PROVIDERS=anysearch,tavily` 时，`search_query` 先调用 anysearch，其次 tavily；断言 provider/failover 顺序正确。
- 新增 metrics 计数单测：`record_search_attempt/failed/budget_exhausted/empty` 累加正确，`get_metrics()["search"]` 与 `reset()` 一致。
- `ruff check src/` 通过。
- 全量 `pytest tests/ -v` 无新增失败（A/B：HEAD 失败集 ⊆ BASE）。

## 6. 验收标准

1. 生产切 `SEARCH_ENABLED_PROVIDERS=anysearch,tavily,doubao` 后，每次搜索调用先打 anysearch，tavily 不再承担日常高频调用。
2. 复盘 `tavily_domestic_policy` 不再因 tavily 额度空而恒空：anysearch 作为首选源正常返回政策/风险搜索结果。
3. `/metrics` 暴露 `search.attempts/failed/budget_exhausted/empty`，可量化每日搜索调用量与各源失败次数。
4. 现有 `tavily_finance_search` 输出契约与既有测试（`test_search_contract.py`）零破坏。