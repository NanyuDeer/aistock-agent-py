# 双人播报来源完整性与晚报语境修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复双人播报来源字段和晚报语境，使晚报文字卡片、音频和详情页均通过既有公开契约读取。

**Architecture:** `brief_evening` 是早点听的代码聚合事实层；LLM 只消费其受控文本投影并生成对话数组。播报 Agent 根据 `brief_type` 选择盘前或盘后 Prompt，代码构造 `broadcast.v1` 并以 `data_source="broadcast_agent"` 保存；Node 校验和前端接口不变。

**Tech Stack:** Python、pytest、pytest-asyncio、LangChain、Node 内部 HTTP API。

## Global Constraints

- 不修改数据库 schema、Node.js 报告校验、公开接口，或 `brief.v1` / `broadcast.v1` 字段。
- scheduler 保存的 `broadcast_morning` 和 `broadcast_evening` 必须都传 `data_source="broadcast_agent"`。
- 晚报只使用 `brief_evening.items` 的标题、结论、置信度和不确定性；不得读取盘前报告或原始快照 JSON。
- LLM 只输出 `[{"role":"host|analyst","content":"..."}]`；追溯、降级和音频字段由代码写入。
- 不添加依赖，不改变早报数据输入或音频接口。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `src/aistock_agent/prompts/workers/broadcast.py` | 保留早间 Prompt；新增收盘播报 Prompt。 |
| `src/aistock_agent/agents/workers/broadcast.py` | 按类型选择输入/Prompt，格式化受控晚报 Brief，并写入播报来源。 |
| `tests/integration/test_broadcast_agent.py` | 覆盖晚报上下文、安全降级、来源字段和音频调用。 |
| `tests/unit/test_briefing.py` | 现有晚报 Brief 卡片契约；仅作回归验证。 |

## Task 1: 为晚报增加受控 Brief 输入与收盘 Prompt

**Files:**
- Modify: `src/aistock_agent/prompts/workers/broadcast.py`
- Modify: `src/aistock_agent/agents/workers/broadcast.py`
- Test: `tests/integration/test_broadcast_agent.py`

**Interfaces:**
- Consumes: `AgentState.brief_type`、`AgentState.report_date`、以及 Node 返回的 `brief.v1`。
- Produces: `broadcast.run()` 在 `brief_type="evening"` 时只读取 `brief_evening`，并把收盘 Prompt 传给 LLM。
- Adds: `_scheduled_brief_type(state) -> Literal["morning", "evening"]` 与 `_format_evening_brief_for_prompt(report) -> str`。

- [ ] **Step 1: 写入 RED 测试**

在 `tests/integration/test_broadcast_agent.py` 增加 `test_evening_broadcast_uses_controlled_evening_brief_in_prompt`。Mock 的 `get_analysis_report` 只允许读取 `brief_evening`，并返回包含三个条目的 `brief.v1`：

```python
{
    "id": 74,
    "content": {
        "schema_version": "brief.v1", "brief_type": "evening",
        "as_of": "2026-07-31T00:00:00+08:00",
        "degraded": False, "missing_sources": [],
        "items": [
            {"title": "收盘复盘", "conclusion": "市场震荡收盘", "confidence": "unknown", "uncertainty": "数据有限"},
            {"title": "市场快照", "conclusion": "板块命中率 0.72", "confidence": "unknown", "uncertainty": "数据有限"},
            {"title": "迭代分析", "conclusion": "检测到异常维度：dimension_2", "confidence": "unknown", "uncertainty": "数据有限"},
        ],
    },
}
```

用 `brief_type="evening"` 调用 `run()`，断言系统消息含“收盘播报”“收盘复盘：市场震荡收盘”“市场快照：板块命中率 0.72”“迭代分析：检测到异常维度：dimension_2”，且不含“晨报：”。

- [ ] **Step 2: 运行 RED 测试**

Run: `python -m pytest tests/integration/test_broadcast_agent.py::test_evening_broadcast_uses_controlled_evening_brief_in_prompt -v`

Expected: FAIL；当前实现会请求 `morning` 并使用盘前 Prompt。

- [ ] **Step 3: 实现最小分支**

在 `broadcast.py` 增加：

```python
def _scheduled_brief_type(state: AgentState) -> Literal["morning", "evening"]:
    return "evening" if state.get("brief_type") == "evening" else "morning"

def _format_evening_brief_for_prompt(report: dict[str, object] | None) -> str:
    content = report.get("content") if report else None
    if not isinstance(content, dict) or content.get("schema_version") != "brief.v1":
        return "晚报事实输入暂不可用；请明确说明当前数据不足以判断。"
    items = content.get("items")
    if not isinstance(items, list):
        return "晚报事实输入暂不可用；请明确说明当前数据不足以判断。"
    lines = [f"{item['title'].strip()}：{item['conclusion'].strip()}" for item in items if isinstance(item, dict) and isinstance(item.get("title"), str) and item["title"].strip() and isinstance(item.get("conclusion"), str) and item["conclusion"].strip()]
    return "\n".join(lines) or "晚报事实输入暂不可用；请明确说明当前数据不足以判断。"
```

新增 `EVENING_BROADCAST_ANALYST_PROMPT`，只接受 `{{EVENING_BRIEF}}`，要求“晚上好/收盘播报”、收盘复盘→市场快照→迭代分析→下一交易日观察的顺序、风险提示、禁止盘前措辞，并强制只输出对话 JSON 数组。

在 `run()` 读取早报上游数据前解析 `brief_type`：晚报读取 `brief_evening`、格式化后替换 `{{EVENING_BRIEF}}`，并传 `{"role": "user", "content": "生成今日收盘播报"}`；早报保留既有四份输入和“生成今日盘前播报”。持久化阶段复用该已解析值。

- [ ] **Step 4: 运行 GREEN 测试**

Run: `python -m pytest tests/integration/test_broadcast_agent.py::test_evening_broadcast_uses_controlled_evening_brief_in_prompt -v`

Expected: PASS，且 mock 未收到任何盘前报告类型请求。

- [ ] **Step 5: 提交 Task 1**

```bash
git add src/aistock_agent/prompts/workers/broadcast.py src/aistock_agent/agents/workers/broadcast.py tests/integration/test_broadcast_agent.py
git commit -m "fix: use evening brief for closing broadcast"
```

## Task 2: 保存播报来源并固化晚报缺失数据行为

**Files:**
- Modify: `src/aistock_agent/agents/workers/broadcast.py`
- Modify: `tests/integration/test_broadcast_agent.py`

**Interfaces:**
- Consumes: `node_api.save_analysis_report()` 与 Task 1 的晚报分支。
- Produces: 每个 scheduler 播报带 `data_source="broadcast_agent"`；晚报 Brief 缺失时仍保持收盘语境，不回退到盘前输入。

- [ ] **Step 1: 写入 RED 测试**

扩展现有 `test_broadcast_persists_text_then_triggers_audio`：

```python
save_call = mock_node_api.save_analysis_report.await_args
assert save_call.kwargs["data_source"] == "broadcast_agent"
```

新增 `test_evening_broadcast_keeps_closing_context_when_brief_is_missing`：让 `get_analysis_report` 返回 `None`，以 `brief_type="evening"` 调用 `run()`；断言 Prompt 含“收盘播报”和“晚报事实输入暂不可用”，并断言首次读取为 `("brief_evening", "2026-07-31")`。

- [ ] **Step 2: 运行 RED 测试**

Run: `python -m pytest tests/integration/test_broadcast_agent.py -k "persists_text_then_triggers_audio or keeps_closing_context" -v`

Expected: FAIL；当前保存调用不传 `data_source`，而且缺失晚报时会进入晨报读取路径。

- [ ] **Step 3: 实现最小来源与降级修复**

把唯一 scheduler 保存调用改为：

```python
saved = await node_api.save_analysis_report(
    report_type=report_type,
    report_date=report_date,
    content=content,
    data_source="broadcast_agent",
)
```

晚报路径在 `brief_evening` 缺失或无有效 `items` 时只使用 Task 1 的固定降级文本；不得读取 `morning`、`wind_leader`、`hot_burst` 或 `trend_score`。保留已有 `source_brief` 降级结构以及“保存成功后才请求音频”的顺序。

- [ ] **Step 4: 运行 GREEN 测试**

Run: `python -m pytest tests/integration/test_broadcast_agent.py -k "persists_text_then_triggers_audio or keeps_closing_context" -v`

Expected: PASS。

- [ ] **Step 5: 提交 Task 2**

```bash
git add src/aistock_agent/agents/workers/broadcast.py tests/integration/test_broadcast_agent.py
git commit -m "fix: persist broadcast agent source"
```

## Task 3: 回归验证与服务器验收

**Files:**
- Verify: `tests/integration/test_broadcast_agent.py`
- Verify: `tests/unit/test_briefing.py`

**Interfaces:**
- Consumes: Task 1 和 Task 2 的实现。
- Produces: 早点听 Brief 卡片、晚报播报接口和音频来源均可验证的证据。

- [ ] **Step 1: 运行 Python 回归套件**

Run: `python -m pytest tests/integration/test_broadcast_agent.py tests/unit/test_briefing.py -v`

Expected: PASS。现有 `test_evening_brief_items_never_carry_raw_json_conclusion` 证明早点听的三条晚报卡片继续由受控 `brief.v1` 生成，不依赖音频或 LLM 对话。

- [ ] **Step 2: 运行静态与差异检查**

Run: `python -m compileall -q src/aistock_agent/agents/workers/broadcast.py src/aistock_agent/prompts/workers/broadcast.py`

Run: `git diff --check HEAD~2..HEAD`

Run: `git status --short --branch`

Expected: 前两条命令退出 0；不要提交已有的无关未跟踪文件。

- [ ] **Step 3: 在服务器重跑晚报链路**

Run:

```bash
cd ~/aistock-agent-py
source .venv/bin/activate
export APP_ENV=production
export PYTHONPATH=src
python3 - <<'PY'
import asyncio
from aistock_agent.config import settings
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.services.scheduler import _run_evening_chain_task

async def main():
    await RedisPool.init(settings.redis_url, max_connections=settings.redis_max_connections)
    await HttpClientPool.init(timeout=settings.http_timeout_seconds)
    try:
        await _run_evening_chain_task()
    finally:
        await HttpClientPool.close()
        await RedisPool.close()

asyncio.run(main())
PY
```

Expected: 日志出现 `analysis_report_saved report_type=broadcast_evening`，不出现“播报报告不存在”。

- [ ] **Step 4: 验证数据来源和公开接口**

```bash
export REPORT_DATE=$(TZ=Asia/Shanghai date +%F)
export NODE_API_BASE_URL=http://127.0.0.1:56790
docker exec -it pg psql -U root -d aistock -P pager=off -c "SELECT report_type, data_source, content->>'audio_path' AS audio_path FROM agent_analysis_reports WHERE report_date = DATE '$REPORT_DATE' AND report_type IN ('brief_evening', 'broadcast_evening') AND user_id IS NULL ORDER BY report_type;"
curl -sS "$NODE_API_BASE_URL/api/agent/brief/evening/$REPORT_DATE" | python3 -m json.tool
curl -sS "$NODE_API_BASE_URL/api/agent/broadcast/evening/$REPORT_DATE" | python3 -m json.tool
```

Expected: `broadcast_evening.data_source` 为 `broadcast_agent`；两个接口均返回非空 `data`；音频路径为 `broadcast-evening-日期.mp3`。

## Plan Self-Review

- **规格覆盖：** Task 1 覆盖收盘 Prompt、受控 Brief 输入和早报隔离；Task 2 覆盖 `data_source` 与晚报降级；Task 3 覆盖现有 Brief 页面事实层、公开接口和服务器验收。
- **占位符：** 已列出精确文件、测试、函数、字段、命令和断言；不存在待办式占位说明。
- **类型一致性：** `brief_type` 仅为 `morning|evening`；晚报读取 `brief_evening`、保存 `broadcast_evening`，来源精确为 `broadcast_agent`。
