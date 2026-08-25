# 搜索 Provider 优先级可配 + 搜索观测计数 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `SEARCH_ENABLED_PROVIDERS` 按配置顺序构建搜索 provider 链（配 `anysearch,tavily,doubao` 使 anysearch 优先），并新增搜索调用/失败观测计数，解决 #3「tavily_domestic_policy 为空」的额度-顺序根因。

**Architecture:** 改 `services/tavily.py::_build_providers` 从固定顺序改为按配置顺序 append provider；在 `search_query` 埋 per-provider 观测计数；`observability/metrics.py` 新增搜索指标并在 `/metrics` 暴露。纯服务层改动，不触碰取数规则/现象阈值/review prompt。

**Tech Stack:** Python 3.x, pydantic-settings, httpx, pytest, ruff, mypy。

## Global Constraints

- 库版本与现状一致，不新增依赖。
- `tavily_finance_search` 工具输出契约逐字节稳定（`tests/unit/test_search_contract.py` 回归锁定），不破坏 title/content/url 只读契约。
- `_build_providers` 保底逻辑 `if not chain: chain.append(TavilyClientProvider())` 保持不变。
- 计量点不得引入 metrics 循环依赖；若 `search_service.py` 直接 import `get_metrics_collector` 形成环，则改在 `TavilyService.search` 计量或惰性 import。
- 禁止 `any`，用 `unknown`（项目约束代理 .trae 规则）。
- 全量 `pytest tests/ -v` 无新增失败（A/B：HEAD 失败集 ⊆ BASE）。
- ruff 通过。

---

### Task 1: 搜索 provider 顺序可配 —— `_build_providers`

**Files:**
- Modify: `src/aistock_agent/services/tavily.py:129-142`
- Test: `tests/unit/test_search_contract.py`

**Interfaces:**
- Consumes: `settings.search_enabled_providers`, `settings.search_enabled_providers_default`（若新增）——不加，直接沿用现有字符串解析。
- Produces: `_build_providers()` 按 `search_enabled_providers` 顺序返回 `list[SearchProvider]`。

- [ ] **Step 1: 读取现状**

确认 `_build_providers` 现状（`services/tavily.py:129-142`）：

```python
def _build_providers() -> list[SearchProvider]:
    enabled = {
        p.strip() for p in (settings.search_enabled_providers or "").split(",") if p.strip()
    } or {"tavily", "doubao", "anysearch"}
    chain: list[SearchProvider] = []
    if "tavily" in enabled and (settings.tavily_api_key or settings.tavily_api_keys):
        chain.append(TavilyClientProvider())
    if "doubao" in enabled and (settings.doubao_api_key or settings.doubao_api_keys):
        chain.append(DoubaoProvider())
    if "anysearch" in enabled and (settings.anysearch_api_key or settings.anysearch_api_keys):
        chain.append(AnySearchProvider())
    if not chain:
        chain.append(TavilyClientProvider())  # 配置缺失时保底主源
    return chain
```

- [ ] **Step 2: 写失败测试 —— 顺序敏感**

在 `tests/unit/test_search_contract.py` 追加（沿用该文件既有 import）：

```python
def test_build_providers_respects_config_order(monkeypatch):
    """SEARCH_ENABLED_PROVIDERS 决定 provider 链顺序（anysearch 优先）。"""
    from aistock_agent.services.tavily import _build_providers
    from aistock_agent.config import settings

    orig_enabled = settings.search_enabled_providers
    # 各 provider 需 key 非空才入链；显式铺开避免依赖部署环境 key 配置
    orig_keys = (settings.tavily_api_key, settings.tavily_api_keys,
                 settings.doubao_api_key, settings.doubao_api_keys,
                 settings.anysearch_api_key, settings.anysearch_api_keys)
    try:
        settings.search_enabled_providers = "anysearch,tavily,doubao"
        settings.tavily_api_key = "k-t"
        settings.tavily_api_keys = ""
        settings.doubao_api_key = "k-d"
        settings.doubao_api_keys = ""
        settings.anysearch_api_key = "k-a"
        settings.anysearch_api_keys = ""
        providers = _build_providers()
        assert [p.name for p in providers] == ["anysearch", "tavily", "doubao"]
    finally:
        settings.search_enabled_providers = orig_enabled
        (settings.tavily_api_key, settings.tavily_api_keys,
         settings.doubao_api_key, settings.doubao_api_keys,
         settings.anysearch_api_key, settings.anysearch_api_keys) = orig_keys
```

> 依赖注记：`_build_providers` 用 `settings.tavily_api_key or settings.tavily_api_keys` 判断 key 存在，故测试必须显式设一个单 key 使 provider 入链；若不设 key，provider 会被跳过、`[p.name ...]` 空列表，断言错误。测试结束时还原 key 与 enabled 配置，避免污染其他用例。

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/unit/test_search_contract.py::test_build_providers_respects_config_order -v`
Expected: FAIL（当前固定顺序返回 `["tavily","doubao","anysearch"]`）

- [ ] **Step 4: 实现 —— 按顺序 append**

改 `_build_providers`：

```python
def _build_providers() -> list[SearchProvider]:
    # SEARCH_ENABLED_PROVIDERS 同时控制启停与顺序（2026-08-24）；
    # 空值默认按 tavily→doubao→anysearch 保底（与历史链序一致）。
    configured = [
        p.strip()
        for p in (settings.search_enabled_providers or "").split(",")
        if p.strip()
    ] or ["tavily", "doubao", "anysearch"]
    chain: list[SearchProvider] = []
    for name in configured:
        if name == "tavily" and (settings.tavily_api_key or settings.tavily_api_keys):
            chain.append(TavilyClientProvider())
        elif name == "doubao" and (settings.doubao_api_key or settings.doubao_api_keys):
            chain.append(DoubaoProvider())
        elif name == "anysearch" and (settings.anysearch_api_key or settings.anysearch_api_keys):
            chain.append(AnySearchProvider())
    if not chain:
        chain.append(TavilyClientProvider())  # 配置缺失时保底主源
    return chain
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/unit/test_search_contract.py::test_build_providers_respects_config_order -v`
Expected: PASS

- [ ] **Step 6: 回归既有 contract 测试，确认零破坏**

Run: `python -m pytest tests/unit/test_search_contract.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 同步 config.py 注释**

改 `src/aistock_agent/config.py:117-120` 注释：

```
# 启用的 provider 集合，逗号分隔；空=默认 "tavily,doubao,anysearch"。
# 顺序即链路调用顺序：TavilyService.search 按此顺序逐个 provider failover。
# 注意：链路顺序由此配置决定，不再由 _build_providers 硬编码（2026-08-24）。
search_enabled_providers: str = ""
```

- [ ] **Step 8: Commit**

```bash
git add src/aistock_agent/services/tavily.py src/aistock_agent/config.py tests/unit/test_search_contract.py
git commit -m "feat: SEARCH_ENABLED_PROVIDERS 控制 provider 链顺序（anysearch 可优先）"
```

---

### Task 2: 搜索观测计数 —— metrics.py

**Files:**
- Modify: `src/aistock_agent/observability/metrics.py`
- Test: `tests/unit/test_metrics.py`（若存在）或新建 `tests/unit/test_search_metrics.py`

**Interfaces:**
- Produces: `MetricsCollector.record_search_attempt(provider)` / `record_search_failed(provider)` / `record_search_budget_exhausted()` / `record_search_empty()`；`get_metrics()["search"]`。

- [ ] **Step 1: 读取 metrics.py 现状**

确认 `MetricsCollector` 的 `__init__` 计数器字段、`get_metrics()`、`reset()` 结构（`observability/metrics.py`）。

- [ ] **Step 2: 写失败测试 —— 计数累加与快照**

新建 `tests/unit/test_search_metrics.py`：

```python
def test_search_metrics_accumulate_and_snapshot():
    from aistock_agent.observability.metrics import MetricsCollector

    c = MetricsCollector()
    c.record_search_attempt("anysearch")
    c.record_search_attempt("tavily")
    c.record_search_attempt("anysearch")
    c.record_search_failed("tavily")
    c.record_search_budget_exhausted()
    c.record_search_empty()

    m = c.get_metrics()["search"]
    assert m["attempts"]["anysearch"] == 2
    assert m["attempts"]["tavily"] == 1
    assert m["failed"]["tavily"] == 1
    assert m["budget_exhausted"] == 1
    assert m["empty"] == 1


def test_search_metrics_reset():
    from aistock_agent.observability.metrics import MetricsCollector

    c = MetricsCollector()
    c.record_search_attempt("anysearch")
    c.reset()
    m = c.get_metrics()["search"]
    assert m["attempts"] == {}
    assert m["failed"] == {}
    assert m["budget_exhausted"] == 0
    assert m["empty"] == 0
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/unit/test_search_metrics.py -v`
Expected: FAIL（`KeyError: 'search'` —— get_metrics 尚无该字段）

- [ ] **Step 4: 实现 —— 新增计数器与方法**

在 `MetricsCollector.__init__` 追加字段：

```python
        # 搜索 provider 链路指标（2026-08-24）
        self._search_attempts: dict[str, int] = {}
        self._search_failed: dict[str, int] = {}
        self._search_budget_exhausted = 0
        self._search_empty = 0
```

新增方法（放在 Stock Trace 区块前或后均可）：

```python
    def record_search_attempt(self, provider: str) -> None:
        """记录一次对某 provider 的搜索尝试。"""
        with self._lock:
            self._search_attempts[provider] = self._search_attempts.get(provider, 0) + 1

    def record_search_failed(self, provider: str) -> None:
        """记录一次某 provider 搜索失败（含 429/网络/解析）。"""
        with self._lock:
            self._search_failed[provider] = self._search_failed.get(provider, 0) + 1

    def record_search_budget_exhausted(self) -> None:
        """记录一次搜索总预算耗尽（budget_expired）。"""
        with self._lock:
            self._search_budget_exhausted += 1

    def record_search_empty(self) -> None:
        """记录一次搜索返回空 results（outcome == empty）。"""
        with self._lock:
            self._search_empty += 1
```

在 `get_metrics()` 快照块读取并返回：

```python
            search_attempts = dict(self._search_attempts)
            search_failed = dict(self._search_failed)
            search_budget_exhausted = self._search_budget_exhausted
            search_empty = self._search_empty
```

返回字典追加：

```python
            "search": {
                "attempts": search_attempts,
                "failed": search_failed,
                "budget_exhausted": search_budget_exhausted,
                "empty": search_empty,
            },
```

在 `reset()` 同步清空：

```python
            self._search_attempts = {}
            self._search_failed = {}
            self._search_budget_exhausted = 0
            self._search_empty = 0
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/unit/test_search_metrics.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/aistock_agent/observability/metrics.py tests/unit/test_search_metrics.py
git commit -m "feat: 新增搜索 provider 观测计数（attempts/failed/budget_exhausted/empty）"
```

---

### Task 3: search_query 埋观测计数

**Files:**
- Modify: `src/aistock_agent/services/search_service.py:74-122`
- Test: `tests/unit/test_search_service.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `MetricsCollector.record_search_attempt/failed/budget_exhausted/empty`；Task 1 的按序 `_build_providers`。
- Produces: `search_query` 在运行时记录搜索计量（每 attempt/failed/empty/budget_exhausted）。

- [ ] **Step 1: 判断依赖，选计量挂载点**

`search_service.py` 顶部 import `from aistock_agent.observability.metrics import get_metrics_collector`：
- `observability/metrics.py` 不 import `search_service` → 单向无环，可直接在 search_service 顶部 import。
- 已有先例：`test_search_service.py` 通过 patch `search_service.get_metrics_collector` 注入隔离单例，无需改单例本身。

- [ ] **Step 2: 写失败测试 —— 计量随 search_query 触发**

在 `tests/unit/test_search_service.py` 追加（复用该文件既有的 `_FakeProvider`/`_Hit`/`KeyPool`）：

```python
from aistock_agent.services import search_service as _ss
from aistock_agent.services.search_service import _Hit, SearchResult, search_query
from aistock_agent.observability.metrics import MetricsCollector


def test_search_query_records_metrics(monkeypatch):
    collector = MetricsCollector()
    monkeypatch.setattr(_ss, "get_metrics_collector", lambda: collector)

    long_a = (
        "本日重要宏观政策正式落地开始实施，涉及产业格局与市场"
        "预期的显著调整，并对实体经济多个层面产生深远影响"
    )
    p1 = _FakeProvider("anysearch", failures=[RuntimeError("boom")])
    p2 = _FakeProvider("tavily", results=[
        SearchResult(provider="tavily", hits=[_Hit("政策", long_a, "http://a")],
                     outcome="ok", provider_errors=[]),
    ])
    keys = {"anysearch": KeyPool(["a"]), "tavily": KeyPool(["b"])}
    res = search_query("政策", providers=[p1, p2], keys=keys)

    m = collector.get_metrics()["search"]
    assert res.outcome == "ok"
    assert m["attempts"]["anysearch"] == 1          # anysearch 尝试一次
    assert m["attempts"]["tavily"] == 1             # 失败后落到 tavily 再试一次
    assert m["failed"]["anysearch"] == 1            # anysearch 抛 RuntimeError
    assert m["budget_exhausted"] == 0
    assert m["empty"] == 0


def test_search_query_records_budget_exhausted(monkeypatch):
    collector = MetricsCollector()
    monkeypatch.setattr(_ss, "get_metrics_collector", lambda: collector)
    p1 = _FakeProvider("tavily", results=[
        SearchResult(provider="tavily", hits=[], outcome="ok", provider_errors=[]),
    ])
    keys = {"tavily": KeyPool(["a"])}
    res = search_query("x", providers=[p1], keys=keys, budget_seconds=0.0)
    assert res.outcome == "error"
    m = collector.get_metrics()["search"]
    assert m["budget_exhausted"] == 1
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/unit/test_search_service.py::test_search_query_records_metrics tests/unit/test_search_service.py::test_search_query_records_budget_exhausted -v`
Expected: FAIL（attempts/failed/budget_exhausted 均为 0 —— 尚未埋点）

- [ ] **Step 4: 实现 —— search_query 埋点**

在 `search_service.py` 顶部 import：

```python
from aistock_agent.observability.metrics import get_metrics_collector
```

在 `search_query` 内（基于现状 `search_service.py:74-122`，改动 `search_query` 函数体）：

```python
def search_query(
    query: str,
    *,
    providers: Sequence[SearchProvider],
    keys: dict[str, KeyPool],
    budget_seconds: float = 10.0,
    topic: str = "news",
    max_results: int = 5,
) -> SearchResult:
    budget = Budget(time.monotonic() + budget_seconds)
    errors: list[tuple[str, str]] = []
    first_provider = providers[0].name if providers else "tavily"
    metrics = get_metrics_collector()
    for provider in providers:
        if budget.expired():
            errors.append((provider.name, "budget_exhausted"))
            metrics.record_search_budget_exhausted()
            break
        key_pool = keys.get(provider.name)
        if key_pool is None or not key_pool._keys:
            errors.append((provider.name, "no_keys_configured"))
            continue
        api_key = key_pool.select_key()
        try:
            metrics.record_search_attempt(provider.name)
            result = provider.search(
                query, topic=topic, max_results=max_results, api_key=api_key
            )
            if result.outcome == "error":
                errors.append((provider.name, "provider_error"))
                key_pool.report_error(api_key, is_circuit=True)
                continue
            key_pool.report_success(api_key)
            if result.outcome == "empty":
                metrics.record_search_empty()
            if provider.name != first_provider:
                degraded = is_low_quality(result)
                return SearchResult(
                    provider=result.provider,
                    hits=result.hits,
                    outcome="degraded" if degraded else result.outcome,
                    provider_errors=errors,
                )
            return result
        except Exception as exc:  # noqa: BLE001
            is_quota = isinstance(exc, RateLimited)
            errors.append((provider.name, type(exc).__name__))
            metrics.record_search_failed(provider.name)
            key_pool.report_error(api_key, is_circuit=not is_quota)
    return SearchResult(
        provider=first_provider,
        hits=[],
        outcome="error",
        provider_errors=errors,
    )
```

> 校准：`record_search_attempt` 在进入 try 后、实际发请求前；`record_search_failed` 仅在抛异常路径；`record_search_empty` 在 `outcome=="empty"`；`budget_exhausted` 在 `budget.expired()` 分支。既有 `test_failover_to_second_provider` / `test_all_fail_returns_error_result_no_raise` / `test_empty_fallback_is_degraded` / `test_budget_expired_halts_chain` 断言结果值不变，仅新增计费副作用。

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/unit/test_search_service.py -v`
Expected: 既有 4 用例 + 新增 2 用例全部 PASS

- [ ] **Step 6: 回归 contract + 全量搜索相关测试**

Run:
```bash
python -m pytest tests/unit/test_search_contract.py tests/unit/test_search_service.py tests/unit/test_search_metrics.py -v
```
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add src/aistock_agent/services/search_service.py tests/unit/test_search_service.py
git commit -m "feat: search_query 埋入 per-provider 搜索观测计数"
```

---

### Task 4: 文档同步 + 全量验证

**Files:**
- Modify: `AGENTS.md`（「搜索多供应商 failover 配置」段）
- Test: 全量

**Interfaces:**
- Consumes: Task 1/2/3 全部改动。

- [ ] **Step 1: 改 AGENTS.md failover 配置段**

在 `AGENTS.md`「搜索多供应商 failover 配置（2026-08-18）」段更新第一条：

现状：
```
- **链路顺序固定**：`tavily → doubao → anysearch`（`services/tavily.py::_build_providers` 硬编码），`SEARCH_ENABLED_PROVIDERS` 只控制启停、不控制顺序；空值=默认 `tavily,doubao,anysearch`
```
改为：
```
- **链路顺序由配置决定（2026-08-24）**：`SEARCH_ENABLED_PROVIDERS` 同时控制启停与顺序（`_build_providers` 按配置顺序建链）；空值=默认 `tavily,doubao,anysearch`。生产配 `anysearch,tavily,doubao` 使 anysearch 优先（日 1000 次额度充足），tavily/doubao 兜底
```

- [ ] **Step 2: ruff 检查改动文件**

Run: `ruff check src/aistock_agent/services/tavily.py src/aistock_agent/services/search_service.py src/aistock_agent/observability/metrics.py src/aistock_agent/config.py`
Expected: 0 错误

- [ ] **Step 3: mypy 检查改动文件**

Run: `python -m mypy src/aistock_agent/services/tavily.py src/aistock_agent/services/search_service.py src/aistock_agent/observability/metrics.py`
Expected: 通过（或与改动前基线一致，无新增错误）

- [ ] **Step 4: 全量测试**

Run: `python -m pytest tests/ -v`
Expected: 无新增失败（HEAD 失败集 ⊆ BASE）；新增用例全绿

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md
git commit -m "docs: 同步 SEARCH_ENABLED_PROVIDERS 启停+顺序语义到 AGENTS.md"
```

---

## Self-Review 记录

- **Spec 覆盖**：§3.1→Task1，§3.2→Task3，§3.3→Task2，§3.4→Task4，§5 测试→分布在各 Task，§6 验收→Task4 全量验证，覆盖完整。
- **占位符扫描**：无 TBD/TODO；测试代码已给出。
- **类型一致性**：`record_search_attempt/failed/empty/budget_exhausted` 各 Task 引用一致；`_build_providers` 返回 `list[SearchProvider]` 一致；`get_metrics()["search"]` 键（attempts/failed/budget_exhausted/empty）在 Task2 定义、Task3 消费一致。