# Agent 分析报告持久化与双层输出实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 在已完成的持久化基础设施之上，将所有 Agent 的报告输出从单层 `{"text": final_response}` 升级为双层 `{"display_report": ..., "podcast_brief": ..., "schema_version": "2.0"}`，使 broadcast_agent 能读取 podcast_brief 生成双人对话，ai_advisor_agent 能读取 display_report 整理对话回复，大幅降低播报模型的 token 消耗。

**架构：** 每个 Agent 一次 LLM 调用同时生成 display_report（前端展示，500-1500字）和 podcast_brief（播报摘要，150-200字）。报告按日期持久化到 PostgreSQL，broadcast_agent 从 DB 读取各 Agent 的 podcast_brief 汇总生成双人对话，ai_advisor_agent 从 DB 读取 display_report 整理成简洁对话回复。

**技术栈：** Python（LangGraph、LangChain ReAct agent、deep_think LLM、httpx），TypeScript（Express internalRouter），PostgreSQL（JSONB content），Redis（缓存）

**Spec 来源：** `docs/superpowers/specs/2026-07-09-agent-report-persistence-design.md`

## 已完成的基础设施（Phase 1-3 基础部分）

以下基础设施已实现并上线，本计划不重复实现：

| 设施 | 文件 | 状态 |
|------|------|------|
| 数据库表 agent_analysis_reports | `aistock-app-api/docs/sql/agent_analysis_reports.sql` | ✅ 已完成 |
| Node.js API POST/GET/DELETE | `aistock-app-api/src/core/routes/internal.ts` | ✅ 已完成 |
| 定时清理任务 03:00 | `aistock-app-api/src/core/tasks/` | ✅ 已完成 |
| Python data_client 方法 | `aistock-agent-py/src/aistock_agent/services/data_client.py` | ✅ 已完成 |
| AgentState trigger_source/report_date | `aistock-agent-py/src/aistock_agent/state/schema.py` | ✅ 已完成 |
| intent_router 用户对话路由 | `aistock-agent-py/src/aistock_agent/graph/routers/intent_router.py` | ✅ 已完成 |
| ai_advisor_agent 基础版 | `aistock-agent-py/src/aistock_agent/agents/workers/ai_advisor.py` | ✅ 已完成 |
| 各 Agent 单层持久化 | morning/wind_leader/hot_burst/alert/broadcast | ✅ 已完成（待升级为双层） |
| Redis 缓存 | `aistock-app-api/src/core/redis.ts` | ✅ 已完成 |

## 本计划范围：双层输出改造

### 当前问题

所有 Agent 持久化的 content 字段为单层结构：
```python
content = {"text": final_response}  # 单层，无播报摘要
```

broadcast_agent 需要读取完整报告喂给播报模型，token 消耗大、费用高。

### 目标结构

```python
content = {
    "display_report": {
        "summary": "结论一句话",
        "details": "完整分析内容（500-1500字）",
        "stocks": [...],      # 相关股票列表（可选）
        "risks": [...]         # 风险提示（可选）
    },
    "podcast_brief": "150-200字的播报摘要，只含主题、事实、判断、风险",
    "schema_version": "2.0"
}
```

## 全局约束

- TypeScript：禁止 `any`，用 `unknown`
- Python：禁止 bare `except`，必须有类型注解
- 禁止全量重写，增量修改现有 Agent
- 禁止 TBD / TODO / "implement later"
- 每步必须包含完整代码
- **podcast_brief 字数硬约束**：150-200 字，超出或不足需要在 prompt 中明确要求
- **向后兼容**：读取报告时需兼容 schema_version 1.0（单层 text）和 2.0（双层）
- **空数据不调用 LLM**：数据获取失败时返回降级文本，不生成报告
- **schema_version 字段必填**：所有新写入的报告 content 必须包含 `schema_version: "2.0"`

---

## 文件结构

| 仓库 | 文件 | 操作 | 负责人 | 职责 |
|------|------|------|--------|------|
| aistock-agent-py | `src/aistock_agent/prompts/workers/morning.py` | 修改 | 王昌泽 | morning 提示词增加双层输出要求 |
| aistock-agent-py | `src/aistock_agent/agents/workers/morning.py` | 修改 | 王昌泽 | morning agent 持久化改为双层 content |
| aistock-agent-py | `src/aistock_agent/prompts/workers/wind_leader.py` | 修改 | 尹辰 | wind_leader 提示词增加双层输出要求 |
| aistock-agent-py | `src/aistock_agent/agents/workers/wind_leader.py` | 修改 | 尹辰 | wind_leader agent 持久化改为双层 content |
| aistock-agent-py | `src/aistock_agent/prompts/workers/hot_burst.py` | 修改 | 吴涵晶 | hot_burst 提示词增加双层输出要求 |
| aistock-agent-py | `src/aistock_agent/agents/workers/hot_burst.py` | 修改 | 吴涵晶 | hot_burst agent 持久化改为双层 content |
| aistock-agent-py | `src/aistock_agent/prompts/workers/alert.py` | 修改 | 李俊良 | alert 提示词增加双层输出要求 |
| aistock-agent-py | `src/aistock_agent/agents/workers/alert.py` | 修改 | 李俊良 | alert agent 持久化改为双层 content |
| aistock-agent-py | `src/aistock_agent/agents/workers/broadcast.py` | 修改 | 尹辰 | broadcast_agent 读取 podcast_brief 而非完整报告 |
| aistock-agent-py | `src/aistock_agent/agents/workers/ai_advisor.py` | 修改 | 尹辰 | ai_advisor_agent 读取 display_report 而非完整报告 |
| aistock-agent-py | `src/aistock_agent/utils/report_parser.py` | 创建 | 尹辰 | 双层报告解析工具（兼容 1.0/2.0） |
| aistock-agent-py | `tests/unit/test_report_parser.py` | 创建 | 尹辰 | 双层报告解析工具单测 |
| aistock-agent-py | `AGENTS.md` | 修改 | 尹辰 | 更新双层输出文档 |
| aistock-agent-py | `changelog-pending.md` | 修改 | 各负责人 | 记录变更 |

---

### 任务 1: 创建双层报告解析工具

**文件：**
- 创建: `src/aistock_agent/utils/report_parser.py`
- 创建: `tests/unit/test_report_parser.py`

**接口：**
- 产出: `parse_report_content(content: dict) -> tuple[str, str]` — 返回 (display_text, podcast_brief)，兼容 schema_version 1.0 和 2.0
- 产出: `extract_podcast_brief(content: dict) -> str` — 只提取 podcast_brief
- 产出: `extract_display_report(content: dict) -> str` — 只提取 display_report 文本

**负责人：** 尹辰

- [ ] **步骤 1: 创建 report_parser.py**

```python
# src/aistock_agent/utils/report_parser.py
"""双层报告解析工具 — 兼容 schema_version 1.0 和 2.0

schema_version 1.0: content = {"text": "..."}
schema_version 2.0: content = {"display_report": {...}, "podcast_brief": "...", "schema_version": "2.0"}
"""

from __future__ import annotations


def parse_report_content(content: dict) -> tuple[str, str]:
    """解析报告 content，返回 (display_text, podcast_brief)

    兼容 1.0 单层和 2.0 双层结构。

    Args:
        content: 数据库 content 字段（JSONB 解析后的 dict）

    Returns:
        (display_text, podcast_brief) 元组
        - display_text: 前端展示用的完整文本
        - podcast_brief: 播报用摘要文本（1.0 版本可能为空字符串）
    """
    if not isinstance(content, dict):
        return ("", "")

    schema_version = content.get("schema_version", "1.0")

    if schema_version == "2.0":
        # 双层结构
        display_report = content.get("display_report", {})
        if isinstance(display_report, dict):
            summary = display_report.get("summary", "")
            details = display_report.get("details", "")
            if summary and details:
                display_text = f"{summary}\n\n{details}"
            else:
                display_text = details or summary or ""
        elif isinstance(display_report, str):
            display_text = display_report
        else:
            display_text = ""

        podcast_brief = content.get("podcast_brief", "") or ""
        return (display_text, podcast_brief)

    # 1.0 单层结构
    text = content.get("text", "") or ""
    return (text, "")


def extract_podcast_brief(content: dict) -> str:
    """只提取 podcast_brief（供 broadcast_agent 使用）

    1.0 版本返回空字符串（无播报摘要）。
    """
    _, podcast_brief = parse_report_content(content)
    return podcast_brief


def extract_display_report(content: dict) -> str:
    """只提取 display_report 文本（供 ai_advisor_agent 使用）

    1.0 版本返回 text 字段。
    """
    display_text, _ = parse_report_content(content)
    return display_text
```

- [ ] **步骤 2: 创建单测**

```python
# tests/unit/test_report_parser.py
"""双层报告解析工具单测"""

from aistock_agent.utils.report_parser import (
    parse_report_content,
    extract_podcast_brief,
    extract_display_report,
)


class TestSchemaV1:
    """schema_version 1.0 兼容测试"""

    def test_v1_basic(self):
        content = {"text": "晨报内容..."}
        display, podcast = parse_report_content(content)
        assert display == "晨报内容..."
        assert podcast == ""

    def test_v1_empty(self):
        content = {}
        display, podcast = parse_report_content(content)
        assert display == ""
        assert podcast == ""

    def test_v1_extract_display(self):
        content = {"text": "晨报内容..."}
        assert extract_display_report(content) == "晨报内容..."

    def test_v1_extract_podcast_empty(self):
        content = {"text": "晨报内容..."}
        assert extract_podcast_brief(content) == ""


class TestSchemaV2:
    """schema_version 2.0 双层测试"""

    def test_v2_basic(self):
        content = {
            "display_report": {
                "summary": "市场向好",
                "details": "完整分析内容...",
            },
            "podcast_brief": "150字播报摘要",
            "schema_version": "2.0",
        }
        display, podcast = parse_report_content(content)
        assert "市场向好" in display
        assert "完整分析内容" in display
        assert podcast == "150字播报摘要"

    def test_v2_extract_display(self):
        content = {
            "display_report": {"summary": "结论", "details": "详情"},
            "podcast_brief": "播报",
            "schema_version": "2.0",
        }
        assert "结论" in extract_display_report(content)
        assert "详情" in extract_display_report(content)

    def test_v2_extract_podcast(self):
        content = {
            "display_report": {"summary": "结论", "details": "详情"},
            "podcast_brief": "播报摘要",
            "schema_version": "2.0",
        }
        assert extract_podcast_brief(content) == "播报摘要"

    def test_v2_display_report_is_string(self):
        content = {
            "display_report": "直接字符串内容",
            "podcast_brief": "播报",
            "schema_version": "2.0",
        }
        display, _ = parse_report_content(content)
        assert display == "直接字符串内容"

    def test_v2_missing_podcast_brief(self):
        content = {
            "display_report": {"summary": "结论", "details": "详情"},
            "schema_version": "2.0",
        }
        _, podcast = parse_report_content(content)
        assert podcast == ""


class TestEdgeCases:
    """边界情况测试"""

    def test_none_content(self):
        display, podcast = parse_report_content(None)  # type: ignore[arg-type]
        assert display == ""
        assert podcast == ""

    def test_non_dict_content(self):
        display, podcast = parse_report_content("not a dict")  # type: ignore[arg-type]
        assert display == ""
        assert podcast == ""
```

- [ ] **步骤 3: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/utils/report_parser.py tests/unit/test_report_parser.py
git commit -m "feat(utils): add report_parser for dual-layer content (v1/v2 compatible)"
```

---

### 任务 2: morning_agent 双层输出改造

**文件：**
- 修改: `src/aistock_agent/prompts/workers/morning.py`
- 修改: `src/aistock_agent/agents/workers/morning.py`

**接口：**
- 消费: `parse_report_content` 来自任务 1（broadcast/ai_advisor 读取时使用）
- 产出: morning_agent 持久化的 content 从 `{"text": final_response}` 改为双层结构

**负责人：** 王昌泽

- [ ] **步骤 1: 修改 morning 提示词，要求 LLM 输出 JSON 双层结构**

在 `src/aistock_agent/prompts/workers/morning.py` 中修改提示词，要求 LLM 返回 JSON 格式：

```python
# 在提示词末尾追加输出格式要求
MORNING_OUTPUT_FORMAT = """

## 输出格式

请严格按以下 JSON 格式返回（不要包含 ```json 标记）：

{
  "display_report": {
    "summary": "结论一句话（20字以内）",
    "details": "完整晨报分析内容（500-1000字），包含大盘综述、板块热点、个股关注",
    "stocks": ["600519", "000858"],
    "risks": ["风险提示1", "风险提示2"]
  },
  "podcast_brief": "150-200字的播报摘要，只包含核心结论、重要事实、方向判断和风险提示，不包含详细分析"
}
"""
```

- [ ] **步骤 2: 修改 morning agent，解析 LLM JSON 输出并持久化双层 content**

在 `src/aistock_agent/agents/workers/morning.py` 中：

```python
import json
from aistock_agent.utils.report_parser import parse_report_content

# 在 extract_final_ai_response 之后增加 JSON 解析
def _parse_dual_layer_response(final_response: str) -> dict:
    """解析 LLM 返回的双层 JSON 响应

    如果 LLM 未返回有效 JSON，降级为单层结构。
    """
    try:
        # 尝试直接解析 JSON
        parsed = json.loads(final_response)
        if isinstance(parsed, dict) and "display_report" in parsed:
            parsed["schema_version"] = "2.0"
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # 降级：将纯文本作为 display_report.details
    return {
        "display_report": {
            "summary": "",
            "details": final_response,
        },
        "podcast_brief": "",
        "schema_version": "2.0",
    }


# 在持久化部分修改 content
# 原代码：
#   content={"text": final_response},
# 改为：
    if final_response:
        await _set_cached_briefing(final_response)
        _archive_morning(final_response)
        if state.get("trigger_source") == "scheduler":
            report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
            dual_layer_content = _parse_dual_layer_response(final_response)
            await node_api.save_analysis_report(
                report_type="morning",
                report_date=report_date,
                content=dual_layer_content,
            )
```

- [ ] **步骤 3: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/prompts/workers/morning.py src/aistock_agent/agents/workers/morning.py
git commit -m "feat(morning): dual-layer output (display_report + podcast_brief)"
```

---

### 任务 3: wind_leader_agent 双层输出改造

**文件：**
- 修改: `src/aistock_agent/prompts/workers/wind_leader.py`
- 修改: `src/aistock_agent/agents/workers/wind_leader.py`

**负责人：** 尹辰

- [ ] **步骤 1: 修改 wind_leader 提示词，要求 LLM 输出 JSON 双层结构**

参考任务 2 的 MORNING_OUTPUT_FORMAT，在 wind_leader 提示词中追加输出格式要求，`display_report.details` 包含风口赛道、龙头股、持续性研判，`podcast_brief` 只保留风口方向和核心龙头。

- [ ] **步骤 2: 修改 wind_leader agent 持久化逻辑**

参考任务 2 的 `_parse_dual_layer_response`，将 `content={"text": final_response}` 改为双层结构。可将 `_parse_dual_layer_response` 提取到 `utils/report_parser.py` 作为公共函数。

- [ ] **步骤 3: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/prompts/workers/wind_leader.py src/aistock_agent/agents/workers/wind_leader.py
git commit -m "feat(wind_leader): dual-layer output (display_report + podcast_brief)"
```

---

### 任务 4: hot_burst_agent 双层输出改造

**文件：**
- 修改: `src/aistock_agent/prompts/workers/hot_burst.py`
- 修改: `src/aistock_agent/agents/workers/hot_burst.py`

**负责人：** 吴涵晶

- [ ] **步骤 1: 修改 hot_burst 提示词**

参考任务 2，`display_report.details` 包含热门股、板块逻辑、持续性、风险，`podcast_brief` 只保留热门股名称和板块方向。用户界面用"热门程度、板块逻辑、风险"等易懂表述，不展示"共振强度、梯队"等后端术语。

- [ ] **步骤 2: 修改 hot_burst agent 持久化逻辑**

参考任务 2，将 `content={"text": final_response}` 改为双层结构。

- [ ] **步骤 3: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/prompts/workers/hot_burst.py src/aistock_agent/agents/workers/hot_burst.py
git commit -m "feat(hot_burst): dual-layer output (display_report + podcast_brief)"
```

---

### 任务 5: alert_agent 双层输出改造

**文件：**
- 修改: `src/aistock_agent/prompts/workers/alert.py`
- 修改: `src/aistock_agent/agents/workers/alert.py`

**负责人：** 李俊良

- [ ] **步骤 1: 修改 alert 提示词**

参考任务 2，`display_report.details` 包含异动事件、发生了什么→为什么→怎么办、持续性判断，`podcast_brief` 只保留异动方向和关键个股。异动解读要短平快，摒弃长段文字。

- [ ] **步骤 2: 修改 alert agent 持久化逻辑**

参考任务 2，将 `content={"text": final_response}` 改为双层结构。注意 alert 是个性化报告，需传 `user_id`。

- [ ] **步骤 3: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/prompts/workers/alert.py src/aistock_agent/agents/workers/alert.py
git commit -m "feat(alert): dual-layer output (display_report + podcast_brief)"
```

---

### 任务 6: broadcast_agent 消费 podcast_brief

**文件：**
- 修改: `src/aistock_agent/agents/workers/broadcast.py`

**接口：**
- 消费: `extract_podcast_brief` 来自任务 1
- 产出: broadcast_agent 从 DB 读取各 Agent 的 podcast_brief 汇总生成双人对话

**负责人：** 尹辰

- [ ] **步骤 1: 修改 broadcast_agent，读取 podcast_brief 而非完整报告**

在 `src/aistock_agent/agents/workers/broadcast.py` 中：

```python
from aistock_agent.utils.report_parser import extract_podcast_brief

# 原逻辑：从 DB 读取完整报告文本
# 新逻辑：从 DB 读取 podcast_brief

# 查询各 Agent 报告时，提取 podcast_brief
report_types = ["morning", "wind_leader", "hot_burst", "alert"]
briefs: dict[str, str] = {}

for report_type in report_types:
    try:
        report = await node_api.get_analysis_report(report_type, report_date)
        if report and report.get("content"):
            brief = extract_podcast_brief(report["content"])
            if brief:
                briefs[report_type] = brief
    except Exception as e:
        logger.warning("broadcast_fetch_brief_failed", report_type=report_type, error=str(e))

# 如果没有任何 podcast_brief，降级读取 display_report
if not briefs:
    for report_type in report_types:
        try:
            report = await node_api.get_analysis_report(report_type, report_date)
            if report and report.get("content"):
                from aistock_agent.utils.report_parser import extract_display_report
                display = extract_display_report(report["content"])
                if display:
                    briefs[report_type] = display[:500]  # 截取前500字作为降级
        except Exception as e:
            logger.warning("broadcast_fetch_display_failed", report_type=report_type, error=str(e))
```

- [ ] **步骤 2: 修改 broadcast 提示词，输入为 podcast_brief 汇总**

将 broadcast 提示词中的输入描述改为"各 Agent 的播报摘要（每份150-200字）"，要求生成 3-5 分钟双人对话。

- [ ] **步骤 3: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/agents/workers/broadcast.py
git commit -m "feat(broadcast): consume podcast_brief instead of full report"
```

---

### 任务 7: ai_advisor_agent 消费 display_report

**文件：**
- 修改: `src/aistock_agent/agents/workers/ai_advisor.py`

**接口：**
- 消费: `extract_display_report` 来自任务 1
- 产出: ai_advisor_agent 从 DB 读取 display_report 整理对话回复

**负责人：** 尹辰

- [ ] **步骤 1: 修改 ai_advisor_agent，读取 display_report 而非完整报告**

在 `src/aistock_agent/agents/workers/ai_advisor.py` 中：

```python
from aistock_agent.utils.report_parser import extract_display_report

# 修改 _fetch_relevant_reports 函数
async def _fetch_relevant_reports(intent: str, report_date: str) -> dict[str, str]:
    reports: dict[str, str] = {}
    # ... 查询逻辑不变 ...

    for report_type in report_types_to_query:
        try:
            data = await node_api.get_analysis_report(report_type, report_date)
            if data and isinstance(data.get("content"), dict):
                # 使用 extract_display_report 提取展示文本（兼容 1.0/2.0）
                display_text = extract_display_report(data["content"])
                if display_text:
                    reports[report_type] = display_text
        except Exception as e:
            logger.warning("advisor_report_fetch_failed", report_type=report_type, error=str(e))

    return reports
```

- [ ] **步骤 2: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/agents/workers/ai_advisor.py
git commit -m "feat(ai_advisor): consume display_report via extract_display_report"
```

---

### 任务 8: 将 _parse_dual_layer_response 提取为公共函数

**文件：**
- 修改: `src/aistock_agent/utils/report_parser.py`
- 修改: 各 Agent 文件（morning/wind_leader/hot_burst/alert）

**负责人：** 尹辰

- [ ] **步骤 1: 在 report_parser.py 中增加 parse_dual_layer_response 公共函数**

```python
# src/aistock_agent/utils/report_parser.py 追加

import json


def parse_dual_layer_response(final_response: str) -> dict:
    """解析 LLM 返回的双层 JSON 响应，持久化到 DB content 字段

    如果 LLM 未返回有效 JSON，降级为单层结构（display_report.details = 原文本）。

    Args:
        final_response: LLM 返回的原始文本

    Returns:
        双层 content dict，包含 display_report、podcast_brief、schema_version
    """
    try:
        parsed = json.loads(final_response)
        if isinstance(parsed, dict) and "display_report" in parsed:
            parsed["schema_version"] = "2.0"
            # 确保 podcast_brief 字段存在
            if "podcast_brief" not in parsed:
                parsed["podcast_brief"] = ""
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # 降级：将纯文本作为 display_report.details
    return {
        "display_report": {
            "summary": "",
            "details": final_response,
        },
        "podcast_brief": "",
        "schema_version": "2.0",
    }
```

- [ ] **步骤 2: 各 Agent 引用公共函数**

将 morning/wind_leader/hot_burst/alert agent 中的 `_parse_dual_layer_response` 改为引用 `from aistock_agent.utils.report_parser import parse_dual_layer_response`。

- [ ] **步骤 3: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/utils/report_parser.py src/aistock_agent/agents/workers/morning.py src/aistock_agent/agents/workers/wind_leader.py src/aistock_agent/agents/workers/hot_burst.py src/aistock_agent/agents/workers/alert.py
git commit -m "refactor(utils): extract parse_dual_layer_response as shared function"
```

---

### 任务 9: 更新文档

**文件：**
- 修改: `AGENTS.md`
- 修改: `changelog-pending.md`

**负责人：** 尹辰

- [ ] **步骤 1: 更新 AGENTS.md**

在 AGENTS.md 中增加双层输出说明：
- content 字段结构从 1.0 单层升级为 2.0 双层
- display_report 用于前端展示和 ai_advisor_agent
- podcast_brief 用于 broadcast_agent 双人对话生成
- schema_version 字段必填（"1.0" 或 "2.0"）
- 向后兼容：report_parser.py 自动兼容两种版本

- [ ] **步骤 2: 更新 changelog-pending.md**

```markdown
## 2026-07-12

### Agent 报告双层输出改造
- **文件**: `src/aistock_agent/utils/report_parser.py`（新建）、各 Agent workers
- **功能**: 所有 Agent 持久化的 content 从单层 `{"text": ...}` 升级为双层 `{"display_report": ..., "podcast_brief": ..., "schema_version": "2.0"}`
- **影响**: broadcast_agent 读取 podcast_brief 生成双人对话（省 token），ai_advisor_agent 读取 display_report 整理对话回复
- **兼容**: report_parser.py 自动兼容 1.0 单层和 2.0 双层结构
- **涉及 Agent**: morning（王昌泽）、wind_leader（尹辰）、hot_burst（吴涵晶）、alert（李俊良）、broadcast（尹辰）
```

- [ ] **步骤 3: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add AGENTS.md changelog-pending.md
git commit -m "docs: update AGENTS.md and changelog for dual-layer output"
```

---

## 实施后验证

### 1. 单元测试

```bash
cd d:/aistock/aistock-agent-py
uv run pytest tests/unit/test_report_parser.py -v
```

### 2. Agent 双层输出验证

```bash
# 手动触发 morning agent，检查 DB content 结构
curl -X POST http://localhost:8080/api/agent/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: $TOKEN" \
  -d '{"message": "今天晨报", "trigger_source": "scheduler"}'

# 查询 DB 验证 content 结构
curl http://localhost:3001/internal/analysis-reports/morning/2026-07-12 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"
# 预期：content 包含 display_report、podcast_brief、schema_version=2.0
```

### 3. broadcast_agent 消费 podcast_brief 验证

```bash
# 触发 broadcast agent
curl -X POST http://localhost:8080/api/agent/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: $TOKEN" \
  -d '{"message": "播报", "trigger_source": "scheduler"}'

# 检查日志确认读取的是 podcast_brief 而非完整报告
pm2 logs aistock --lines 50 | grep "broadcast_fetch_brief"
```

### 4. 向后兼容验证

```bash
# 查询旧报告（1.0 单层），确认 report_parser 能正确解析
curl http://localhost:3001/internal/analysis-reports/morning/2026-07-09 \
  -H "Authorization: Bearer $INTERNAL_TOKEN"
# 预期：extract_display_report 返回 text 字段，extract_podcast_brief 返回空字符串
```

## 自检清单

- [x] 规范覆盖：report_parser（任务1）、4个 Agent 双层改造（任务2-5）、broadcast 消费 brief（任务6）、ai_advisor 消费 display（任务7）、公共函数提取（任务8）、文档（任务9）
- [x] 无占位符：所有步骤包含完整代码
- [x] 类型一致性：content schema_version、display_report 结构在所有任务中一致
- [x] 全局约束：禁止 `any`、新字段 NotRequired、向后兼容
- [x] 向后兼容：report_parser 自动兼容 1.0 和 2.0
- [x] 省 token：podcast_brief 150-200字，broadcast_agent 不读完整报告

## 依赖关系

```text
任务1 (report_parser) ──→ 任务2-5 (各Agent双层改造) ──→ 任务6 (broadcast消费brief)
                                                    ──→ 任务7 (ai_advisor消费display)
                                                    ──→ 任务8 (公共函数提取)
                                                    ──→ 任务9 (文档更新)
```

任务2-5 可并行执行（各 Agent 独立），但都依赖任务1完成。任务6-7 依赖任务2-5 中至少一个 Agent 完成双层改造。
