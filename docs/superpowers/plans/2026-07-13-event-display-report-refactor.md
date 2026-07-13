# 事件传导 Agent 后端对齐前端 types.ts 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use Skill(name="subagent-driven-development") (recommended) or Skill(name="executing-plans") to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 去掉 event agent 的 `display_report` 包装层，LLM 按 4 个模块独立输出，与前端 `types.ts` 1:1 对齐，新增 `transform_to_frontend()` 做字段映射。

**Architecture:** `event.py run()` 拆为 4 次串行 LLM 调用（3 flash + 1 deep_think），每次输出一个模块的结构化 JSON；`transform_to_frontend()` 做方向映射/类型转换/默认值填充；analysis_reports 按 `event_understanding`/`event_transmission`/`event_history`/`event_investment` 分 key 下发。

**Tech Stack:** LangChain ChatOpenAI, langgraph prebuilt create_react_agent, structlog, asyncio

## Global Constraints

- 前端 `types.ts` 零改动
- `aiSummary` ≤ 40 字，从 `investmentSummary.conclusion` 截取
- Call 2（TransmissionAnalysis）使用 `deep_think` 模型，其余 3 调用使用 `flash`（`quick_think`）
- 方向映射支持中英文双向：`"利好"/"利空"/"中性"` ↔ `"bullish"/"bearish"/"neutral"`
- JSON 解析复用现有二级回退策略（整段解析 → 正则匹配 JSON 块）
- 单模块失败独立降级，Call 1 失败为唯一阻断点
- 5 个 prompt 常量集中在 `prompts/workers/event.py` 单文件

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `src/aistock_agent/prompts/workers/event.py` | 重写 | 5 个 prompt 常量 |
| `src/aistock_agent/utils/output_parser.py` | 新增 `transform_to_frontend()` + `_parse_json()` | 字段映射 + JSON 解析 |
| `src/aistock_agent/agents/workers/event.py` | 重写 | `run()` + 4 helper + podcast 生成 |
| `tests/unit/test_output_parser.py` | 追加 | `transform_to_frontend` 单元测试 |

---

### Task 1: 重写 Prompt 文件 — 5 个 prompt 常量

**Files:**
- Modify: `src/aistock_agent/prompts/workers/event.py` — 全量替换

**Interfaces:**
- Produces: `EVENT_UNDERSTANDING_PROMPT`, `EVENT_TRANSMISSION_PROMPT`, `EVENT_HISTORY_PROMPT`, `EVENT_INVESTMENT_PROMPT`, `EVENT_PODCAST_PROMPT` (all `str`)

- [ ] **Step 1: 替换 prompts/workers/event.py 全量内容**

```python
"""事件传导链分析师提示词 — 4 模块拆分 + 播报

全部 prompt 常量，供 agents/workers/event.py 按调用顺序引用。
"""

from aistock_agent.prompts.general.system import SYSTEM_PROMPT

# ── Call 1: 事件理解（flash，无工具） ──

EVENT_UNDERSTANDING_PROMPT = SYSTEM_PROMPT + """

你是事件识别分析师。给定一起重大新闻事件，只做"事件本身"的分析，不涉及行业传导。

## 输出格式

严格输出 JSON，不要其他文字：
{
  "summary": "100字以内概括事件本质，聚焦'这个事件改变了什么'",
  "coreChanges": [
    { "variable": "被改变的变量名", "before": "变化前状态", "after": "变化后状态" }
  ]
}

## 约束
- summary 聚焦"这个事件改变了什么"，不写行业影响
- coreChanges 2-4 条，每条 before/after 各 ≤20 字
- 只输出 JSON 对象，不要 markdown 代码块包裹，不要多余文字
"""

# ── Call 2: 传导分析（deep_think，ReAct + 工具） ──

EVENT_TRANSMISSION_PROMPT = SYSTEM_PROMPT + """

你是事件传导链分析师。基于事件理解结果，推演事件沿产业链的传导路径。

## 分析步骤

**Step 1 — 影响变量提取**：
- 识别事件改变了哪些产业变量：需求、供给、成本、价格、库存、订单、技术、资金
- 判断每个变量的变化方向（bullish 利好 / bearish 利空 / neutral 中性）

**Step 2 — 首层行业定位**：
- 使用 match_industry_by_keywords 工具匹配受影响行业
- 从匹配结果中确定首层（直接影响）行业
- 确保行业名称来自数据库（不允许凭空编造）

**Step 3 — 产业链扩散**：
- 对首层行业，查询其上下游关系（Industry Relation）
- 逐层遍历上下游，最多 3 层
- 每一层说明传导原因

**Step 4 — 影响强度计算**：
- 综合评估每个行业的受影响程度（结合产业链距离、关联紧密程度）
- 方向由传导关系决定：需求拉动为 bullish，成本传导为 bearish
- impactStrength 取值 0-1

## 输出格式

严格输出 JSON，不要其他文字：
{
  "mechanism": "200字以内经济逻辑解释",
  "variables": [
    {
      "name": "变量名（如 '补贴金额'）",
      "direction": "bullish（利好）/ bearish（利空）/ neutral（中性）",
      "strength": 0.85,
      "explanation": "≤40字解释变量如何被事件改变"
    }
  ],
  "coreIndustry": {
    "name": "直接受益/承压的核心行业名",
    "impact": "≤30字影响总结",
    "reason": "≤80字原因说明"
  },
  "chain": [
    {
      "industry": "行业名",
      "relation": "核心行业 / 上游传导 / 下游传导",
      "level": 1,
      "direction": "bullish / bearish / neutral",
      "impactStrength": 0.72,
      "reason": "≤40字传导原因"
    }
  ]
}

## 约束
- mechanism ≤200 字
- variables 2-5 条
- chain 至少包含核心行业自身（level=1, relation="核心行业"），逐步扩散最多 3 层
- 方向值必须用英文：bullish / bearish / neutral
- 只输出 JSON 对象，不要 markdown 代码块包裹，不要多余文字
"""

# ── Call 3: 历史事件（flash，ReAct + 工具） ──

EVENT_HISTORY_PROMPT = SYSTEM_PROMPT + """

你是历史事件检索分析师。给定事件理解结果，根据事件本质检索相似历史事件。

使用 tavily_finance_search 搜索历史相似事件的行业影响数据。

## 输出格式

严格输出 JSON 数组，不要其他文字：
[
  {
    "historyId": "hist_2023_gx",
    "year": "2023",
    "title": "历史事件标题",
    "eventType": "产业政策",
    "sentiment": "bullish",
    "industryChange": "影响行业变化描述",
    "changePercentage": 15.0
  }
]

## 约束
- 返回 2-3 个最相似案例
- eventType 取值：产业政策 / 地缘政治 / 技术突破 / 市场动态 / 监管变化 / 公司公告
- sentiment 取值：bullish / bearish / neutral
- changePercentage 为数字类型（如 15.0、-8.3）
- 只输出 JSON 数组，不要 markdown 代码块包裹，不要多余文字
"""

# ── Call 4: 投资总结（flash，无工具） ──

EVENT_INVESTMENT_PROMPT = SYSTEM_PROMPT + """

你是投资研判分析师。基于前面三步的分析结果，生成最终投资观点。

## 输入

- 事件理解：{understanding}
- 传导分析：{transmission}
- 历史验证：{history}

## 输出格式

严格输出 JSON，不要其他文字：
{
  "conclusion": "XX行业受益/承压，短期/中期/长期景气改善/承压",
  "keyPoints": ["支撑该判断的核心逻辑要点"],
  "focusIndustries": [
    {
      "name": "行业名",
      "direction": "positive（利好）/ negative（利空）",
      "reason": "≤80字理由"
    }
  ],
  "opportunities": ["投资机会描述"],
  "risks": ["风险提示"],
  "rating": "positive（看好）/ neutral（中性）/ negative（看空）"
}

## 约束
- conclusion ≤40 字，模板："XX行业受益/承压，X期景气改善/承压"
- keyPoints 2-4 条，每条 15-30 字
- focusIndustries 1-5 条
- opportunities 1-3 条
- risks 1-3 条
- rating 必填：positive / neutral / negative
- direction 必填：positive / negative
- 只输出 JSON 对象，不要 markdown 代码块包裹，不要多余文字
"""

# ── 播报文本（flash，无工具） ──

EVENT_PODCAST_PROMPT = SYSTEM_PROMPT + """

你是财经播报员。基于事件分析结果，生成 150-200 字播报摘要。

## 输入

- 事件理解摘要：{understanding_summary}
- 投资观点结论：{conclusion}

## 约束
- 150-200 字
- 只含主题、事实、判断、风险
- 只输出纯文本，不要 JSON，不要 markdown
"""
```

- [ ] **Step 2: 验证 — 确认文件语法正确**

```powershell
python -c "import ast; ast.parse(open(r'src\aistock_agent\prompts\workers\event.py', encoding='utf-8').read()); print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```powershell
git add src/aistock_agent/prompts/workers/event.py ; git commit -m "refactor(event): 拆分 event prompt 为 5 个模块化常量"
```

---

### Task 2: 新增 `transform_to_frontend()` + `_parse_json()`

**Files:**
- Modify: `src/aistock_agent/utils/output_parser.py` — 追加 2 个函数
- Modify: `tests/unit/test_output_parser.py` — 追加测试

**Interfaces:**
- Consumes: spec 中的字段映射表，`EventDetailResponse` 类型结构
- Produces:
  - `_parse_json(text: str) -> dict | list | None` — LLM 输出 JSON 解析（复用现有二级回退策略）
  - `transform_to_frontend(understanding: dict | None, transmission: dict | None, history: list | None, investment: dict | None, event_meta: dict) -> dict[str, object]` — 组装 analysis_reports

- [ ] **Step 1: 在 output_parser.py 末尾追加两个函数**

在文件末尾（第 103 行 `return []` 之后）追加：

```python
# ── 通用 JSON 解析（供 event.py 各 helper 复用） ──


def _parse_json(text: str) -> dict | list | None:
    """从 LLM 输出文本中提取 JSON 对象或数组。

    解析策略（与 parse_event_output 一致）：
    1. 去掉 markdown 代码块（```json ... ``` 或 ``` ... ```）
    2. 整段 JSON 解析
    3. 正则匹配 JSON 块（花括号/方括号平衡）
    4. 都失败返回 None
    """
    if not text:
        return None

    # 去掉 markdown 代码块
    cleaned = re.sub(r'```(?:json)?\s*\n?', '', text)
    cleaned = re.sub(r'\n?\s*```', '', cleaned)
    cleaned = cleaned.strip()

    # 策略 1: 整段解析
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, (dict, list)):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # 策略 2: 正则匹配 JSON 块
    for pattern in [r'\{.*\}', r'\[.*\]']:
        match = re.search(pattern, cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, (dict, list)):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass

    logger.warning("json_parse_failed", text_preview=text[:200])
    return None


# ── 方向映射 ──

_DIRECTION_MAP: dict[str, str] = {
    "bullish": "bullish",
    "bearish": "bearish",
    "neutral": "neutral",
    "利好": "bullish",
    "利空": "bearish",
    "中性": "neutral",
    "positive": "positive",
    "negative": "negative",
}


def _normalize_direction(value: str, field: str) -> str:
    """方向值标准化，未知值打 log 后降级为 neutral"""
    normalized = _DIRECTION_MAP.get(value)
    if normalized is None:
        logger.warning("direction_normalize_fallback", field=field, raw=value)
        return "neutral"
    return normalized


# ── 字段映射 ──


def transform_to_frontend(
    understanding: dict | None,
    transmission: dict | None,
    history: list | None,
    investment: dict | None,
    event_meta: dict,
) -> dict[str, object]:
    """将 4 个 LLM 模块输出 + 事件元信息映射为 analysis_reports。

    Args:
        understanding: Call 1 输出（EventUnderstanding JSON dict）
        transmission: Call 2 输出（TransmissionAnalysis JSON dict）
        history: Call 3 输出（HistoryEvent[] list）
        investment: Call 4 输出（InvestmentSummary JSON dict）
        event_meta: {"eventId": str, "title": str, "source": str}

    Returns:
        analysis_reports dict，结构：
        {
            "event_understanding": {...},
            "event_transmission": {...},
            "event_history": [...],
            "event_investment": {...},
        }
    """
    reports: dict[str, object] = {}

    # ── event_understanding ──
    if understanding and isinstance(understanding, dict):
        reports["event_understanding"] = {
            "summary": str(understanding.get("summary", "")),
            "coreChanges": [
                {
                    "variable": str(c.get("variable", "")),
                    "before": str(c.get("before", "")),
                    "after": str(c.get("after", "")),
                }
                for c in understanding.get("coreChanges", [])
                if isinstance(c, dict)
            ],
        }
    else:
        reports["event_understanding"] = None

    # ── event_transmission ──
    if transmission and isinstance(transmission, dict):
        variables = transmission.get("variables", [])
        chain = transmission.get("chain", [])
        core_industry = transmission.get("coreIndustry", {})

        reports["event_transmission"] = {
            "eventId": event_meta.get("eventId", ""),
            "mechanism": str(transmission.get("mechanism", "")),
            "variables": [
                {
                    "name": str(v.get("name", "")),
                    "direction": _normalize_direction(str(v.get("direction", "")), "variables.direction"),
                    "strength": float(v.get("strength", 0)),
                    "explanation": str(v.get("explanation", "")),
                }
                for v in variables
                if isinstance(v, dict)
            ],
            "coreIndustry": {
                "name": str(core_industry.get("name", "")),
                "impact": str(core_industry.get("impact", "")),
                "reason": str(core_industry.get("reason", "")),
            } if isinstance(core_industry, dict) else {"name": "", "impact": "", "reason": ""},
            "chain": [
                {
                    "industry": str(c.get("industry", "")),
                    "relation": str(c.get("relation", "核心行业")),
                    "level": int(c.get("level", 1)),
                    "direction": _normalize_direction(str(c.get("direction", "")), "chain.direction"),
                    "impactStrength": float(c.get("impactStrength", 0)),
                    "reason": str(c.get("reason", "")),
                }
                for c in chain
                if isinstance(c, dict)
            ],
        }
    else:
        reports["event_transmission"] = None

    # ── event_history ──
    if history and isinstance(history, list):
        reports["event_history"] = [
            {
                "historyId": str(h.get("historyId", "")),
                "year": str(h.get("year", "")),
                "title": str(h.get("title", "")),
                "eventType": str(h.get("eventType", "")),
                "sentiment": _normalize_direction(str(h.get("sentiment", "")), "history.sentiment"),
                "industryChange": str(h.get("industryChange", "")),
                "changePercentage": float(h.get("changePercentage", 0)),
            }
            for h in history
            if isinstance(h, dict)
        ]
    else:
        reports["event_history"] = []

    # ── event_investment ──
    if investment and isinstance(investment, dict):
        focus_industries = investment.get("focusIndustries", [])
        reports["event_investment"] = {
            "id": event_meta.get("eventId", ""),
            "conclusion": str(investment.get("conclusion", "")),
            "keyPoints": [
                str(kp) for kp in investment.get("keyPoints", [])
            ],
            "focusIndustries": [
                {
                    "name": str(fi.get("name", "")),
                    "direction": _normalize_direction(str(fi.get("direction", "")), "focusIndustries.direction"),
                    "reason": str(fi.get("reason", "")),
                }
                for fi in focus_industries
                if isinstance(fi, dict)
            ],
            "opportunities": [
                str(o) for o in investment.get("opportunities", [])
            ],
            "risks": [
                str(r) for r in investment.get("risks", [])
            ],
            "rating": _normalize_direction(str(investment.get("rating", "neutral")), "rating"),
        }
    else:
        reports["event_investment"] = None

    return reports
```

- [ ] **Step 2: 验证 — 确认函数可导入**

```powershell
python -c "from src.aistock_agent.utils.output_parser import transform_to_frontend, _parse_json; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: 追加测试用例到 tests/unit/test_output_parser.py**

在文件末尾追加：

```python
# ── _parse_json 测试 ──


def test_parse_json_simple_dict():
    """纯 JSON 对象 → 正确解析"""
    result = _parse_json('{"key": "value"}')
    assert isinstance(result, dict)
    assert result == {"key": "value"}


def test_parse_json_simple_list():
    """纯 JSON 数组 → 正确解析"""
    result = _parse_json('[{"a": 1}, {"b": 2}]')
    assert isinstance(result, list)
    assert len(result) == 2


def test_parse_json_markdown_code_block():
    """```json ... ``` 包裹 → 正确解析"""
    result = _parse_json('```json\n{"key": "value"}\n```')
    assert isinstance(result, dict)
    assert result == {"key": "value"}


def test_parse_json_bare_code_block():
    """``` ... ``` 包裹 → 正确解析"""
    result = _parse_json('```\n{"key": "value"}\n```')
    assert isinstance(result, dict)
    assert result == {"key": "value"}


def test_parse_json_nested_in_text():
    """JSON 嵌在文本中 → 正则匹配提取"""
    result = _parse_json('前面有文字\n{"key": "value"}\n后面也有文字')
    assert isinstance(result, dict)
    assert result == {"key": "value"}


def test_parse_json_invalid():
    """无效文本 → 返回 None"""
    result = _parse_json('这不是 JSON')
    assert result is None


def test_parse_json_empty():
    """空字符串 → 返回 None"""
    result = _parse_json('')
    assert result is None


# ── transform_to_frontend 测试 ──


def test_transform_to_frontend_full():
    """4 模块全有 → 完整映射"""
    understanding = {
        "summary": "政策延续至2027年",
        "coreChanges": [
            {"variable": "补贴预期", "before": "不确定", "after": "明确延续"}
        ]
    }
    transmission = {
        "mechanism": "补贴延续降低购车门槛",
        "variables": [
            {"name": "补贴金额", "direction": "bullish", "strength": 0.85, "explanation": "单辆最高1.5万"}
        ],
        "coreIndustry": {"name": "新能源汽车", "impact": "直接利好", "reason": "终端销量预期上调"},
        "chain": [
            {"industry": "动力电池", "relation": "上游传导", "level": 1, "direction": "bullish", "impactStrength": 0.72, "reason": "销量拉动电池需求"}
        ]
    }
    history = [
        {"historyId": "hist_001", "year": "2023", "title": "类似政策", "eventType": "产业政策", "sentiment": "bullish", "industryChange": "普涨15%", "changePercentage": 15.0}
    ]
    investment = {
        "conclusion": "新能源汽车产业链受益，中期景气改善",
        "keyPoints": ["补贴延续刺激终端需求"],
        "focusIndustries": [{"name": "新能源汽车", "direction": "positive", "reason": "直接受益"}],
        "opportunities": ["终端销量增长"],
        "risks": ["补贴依赖风险"],
        "rating": "positive"
    }
    meta = {"eventId": "evt_001", "title": "补贴延续", "source": "新华社"}

    result = transform_to_frontend(understanding, transmission, history, investment, meta)

    assert result["event_understanding"]["summary"] == "政策延续至2027年"
    assert len(result["event_understanding"]["coreChanges"]) == 1
    assert result["event_transmission"]["mechanism"] == "补贴延续降低购车门槛"
    assert result["event_transmission"]["variables"][0]["direction"] == "bullish"
    assert result["event_transmission"]["variables"][0]["strength"] == 0.85
    assert result["event_transmission"]["coreIndustry"]["name"] == "新能源汽车"
    assert len(result["event_transmission"]["chain"]) == 1
    assert result["event_transmission"]["chain"][0]["level"] == 1
    assert len(result["event_history"]) == 1
    assert result["event_history"][0]["changePercentage"] == 15.0
    assert result["event_investment"]["conclusion"] == "新能源汽车产业链受益，中期景气改善"
    assert result["event_investment"]["rating"] == "positive"


def test_transform_to_frontend_null_modules():
    """部分模块为 None → 对应位置为 None 或空数组"""
    meta = {"eventId": "evt_002", "title": "测试", "source": ""}

    result = transform_to_frontend(None, None, None, None, meta)

    assert result["event_understanding"] is None
    assert result["event_transmission"] is None
    assert result["event_history"] == []
    assert result["event_investment"] is None


def test_transform_to_frontend_chinese_direction():
    """LLM 输出中文方向值 → 正确映射为英文"""
    transmission = {
        "mechanism": "测试",
        "variables": [{"name": "x", "direction": "利好", "strength": 0.5, "explanation": ""}],
        "coreIndustry": {"name": "x", "impact": "", "reason": ""},
        "chain": [{"industry": "x", "relation": "核心行业", "level": 1, "direction": "利空", "impactStrength": 0.3, "reason": ""}]
    }
    meta = {"eventId": "evt_003", "title": "", "source": ""}

    result = transform_to_frontend({"summary": "", "coreChanges": []}, transmission, [], None, meta)

    assert result["event_transmission"]["variables"][0]["direction"] == "bullish"
    assert result["event_transmission"]["chain"][0]["direction"] == "bearish"
```

- [ ] **Step 4: 运行测试**

```powershell
python -m pytest tests/unit/test_output_parser.py -v
```
Expected: ALL PASS

- [ ] **Step 5: Commit**

```powershell
git add src/aistock_agent/utils/output_parser.py tests/unit/test_output_parser.py ; git commit -m "feat(event): 新增 transform_to_frontend() 字段映射 + _parse_json() 通用解析"
```

---

### Task 3: 重写 `agents/workers/event.py`

**Files:**
- Modify: `src/aistock_agent/agents/workers/event.py` — 全量替换

**Interfaces:**
- Consumes: `EVENT_UNDERSTANDING_PROMPT`, `EVENT_TRANSMISSION_PROMPT`, `EVENT_HISTORY_PROMPT`, `EVENT_INVESTMENT_PROMPT`, `EVENT_PODCAST_PROMPT` (from Task 1)
- Consumes: `_parse_json()`, `transform_to_frontend()` (from Task 2)
- Consumes: `get_quick_think()`, `get_deep_think()` (from `services/llm`)
- Consumes: `get_tools("event")`, `get_cached_event`, `set_cached_event`, `persist_event_report` (现有)
- Produces: `run(state: AgentState) -> dict[str, object]` — 返回值不变（`final_response` + `analysis_reports`），但 `analysis_reports` 结构变化

- [ ] **Step 1: 替换 agents/workers/event.py 全量内容**

```python
"""Event Analyst Agent — 事件传导链分析（v3 升级）

工具集：search_cls_news, get_news_fulltext, get_quote,
        tavily_finance_search, match_industry_by_keywords

v3 升级内容：
- 去掉 display_report 包装层，4 模块独立 LLM 调用
- Call 1 (flash) → EventUnderstanding
- Call 2 (deep_think) → TransmissionAnalysis
- Call 3 (flash) → HistoryEvents
- Call 4 (flash) → InvestmentSummary
- Podcast (flash) → 播报文本（final_response）
- 新增 transform_to_frontend() 字段映射

缓存/持久化逻辑保持不变。
"""

import json
from typing import cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.event import (
    EVENT_HISTORY_PROMPT,
    EVENT_INVESTMENT_PROMPT,
    EVENT_PODCAST_PROMPT,
    EVENT_TRANSMISSION_PROMPT,
    EVENT_UNDERSTANDING_PROMPT,
)
from aistock_agent.services.cache import get_cached_event, set_cached_event
from aistock_agent.services.event_persister import persist_event_report
from aistock_agent.services.llm import get_deep_think, get_quick_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_last_human_message
from aistock_agent.utils.output_parser import _parse_json, transform_to_frontend

logger = structlog.get_logger()


# ── 内部 helper：LLM 调用模式 ──


async def _call_llm_no_tools(
    system_prompt: str,
    user_msg: str,
    model: str = "flash",
) -> dict | None:
    """调用 LLM（无工具），返回解析后的 JSON dict 或 None。

    Args:
        system_prompt: 系统提示词
        user_msg: 用户消息
        model: "flash" → get_quick_think() / "deep" → get_deep_think()

    Returns:
        解析后的 dict，失败返回 None。
    """
    try:
        llm = get_deep_think() if model == "deep" else get_quick_think()
        result = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_msg),
        ])
        text = cast(str, result.content) if hasattr(result, 'content') else str(result)
        parsed = _parse_json(text)
        if isinstance(parsed, dict):
            return parsed
        return None
    except Exception:
        logger.exception("llm_call_no_tools_failed", model=model)
        return None


async def _call_llm_with_tools(
    system_prompt: str,
    user_msg: str,
    model: str = "flash",
) -> dict | list | None:
    """调用 LLM（ReAct + 工具），返回解析后的 JSON dict/list 或 None。

    Args:
        system_prompt: 系统提示词
        user_msg: 用户消息
        model: "flash" → get_quick_think() / "deep" → get_deep_think()

    Returns:
        解析后的 dict 或 list，失败返回 None。
    """
    try:
        llm = get_deep_think() if model == "deep" else get_quick_think()
        tools = get_tools("event")
        agent = create_react_agent(llm, tools)
        result = await agent.ainvoke({
            "messages": [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_msg),
            ]
        })
        # 提取最后一条 AI 消息
        messages = result.get("messages", [])
        text = ""
        for msg in reversed(messages):
            if hasattr(msg, 'content') and isinstance(msg.content, str):
                text = msg.content
                break
        if not text:
            logger.warning("llm_with_tools_no_text")
            return None
        return _parse_json(text)
    except Exception:
        logger.exception("llm_call_with_tools_failed", model=model)
        return None


# ── 4 模块分析 helper ──


async def _analyze_understanding(user_msg: str) -> dict | None:
    """Call 1: 事件理解（flash，无工具）"""
    return await _call_llm_no_tools(EVENT_UNDERSTANDING_PROMPT, user_msg, model="flash")


async def _analyze_transmission(user_msg: str, understanding: dict) -> dict | None:
    """Call 2: 传导分析（deep_think，ReAct + 工具）

    Args:
        user_msg: 原始事件文本
        understanding: Call 1 输出 dict，含 summary + coreChanges
    """
    # 上下文拼接：事件理解结果注入用户消息
    context = json.dumps(understanding, ensure_ascii=False)
    prompt = f"事件理解结果：\n{context}\n\n原始事件描述：\n{user_msg}"
    return await _call_llm_with_tools(EVENT_TRANSMISSION_PROMPT, prompt, model="deep")


async def _analyze_history(user_msg: str, understanding: dict) -> list | None:
    """Call 3: 历史事件（flash，ReAct + 工具）

    Args:
        user_msg: 原始事件文本
        understanding: Call 1 输出 dict，含 summary + coreChanges
    """
    context = json.dumps(understanding, ensure_ascii=False)
    prompt = f"事件理解结果：\n{context}\n\n原始事件描述：\n{user_msg}"
    result = await _call_llm_with_tools(EVENT_HISTORY_PROMPT, prompt, model="flash")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        # 兼容 LLM 输出单对象而非数组
        return [result]
    return None


async def _analyze_investment(
    understanding: dict | None,
    transmission: dict | None,
    history: list | None,
) -> dict | None:
    """Call 4: 投资总结（flash，无工具）

    将前三步结果注入 prompt 的 {understanding} / {transmission} / {history} 占位符。
    """
    ud = json.dumps(understanding, ensure_ascii=False) if understanding else "无"
    td = json.dumps(transmission, ensure_ascii=False) if transmission else "无"
    hd = json.dumps(history, ensure_ascii=False) if history else "无"

    prompt = EVENT_INVESTMENT_PROMPT.format(
        understanding=ud,
        transmission=td,
        history=hd,
    )
    return await _call_llm_no_tools(prompt, "综合上述分析生成投资观点", model="flash")


async def _generate_podcast(understanding: dict | None, conclusion: str) -> str:
    """生成播报文本（flash，无工具）

    Args:
        understanding: Call 1 输出 dict
        conclusion: Call 4 investmentSummary.conclusion

    Returns:
        150-200 字播报摘要，失败返回错误提示。
    """
    summary = (understanding.get("summary", "") if understanding else "")
    prompt = EVENT_PODCAST_PROMPT.format(
        understanding_summary=summary,
        conclusion=conclusion,
    )
    try:
        llm = get_quick_think()
        result = await llm.ainvoke([
            SystemMessage(content=prompt),
            HumanMessage(content="生成播报摘要"),
        ])
        text = cast(str, result.content) if hasattr(result, 'content') else str(result)
        return text.strip()
    except Exception:
        logger.exception("podcast_generation_failed")
        return "事件播报生成失败，请稍后重试"


# ── 主函数 ──


async def run(state: AgentState) -> dict[str, object]:
    """事件传导链分析 v3：4 模块独立 LLM 调用 → transform_to_frontend → 缓存/持久化"""
    try:
        # 提取用户输入
        user_msg = extract_last_human_message(state.get("messages", []))
        if not user_msg:
            return {"final_response": "请提供需要分析的事件描述。", "analysis_reports": {}}

        # Step 1: Redis 缓存检查（不变）
        cached = await get_cached_event(user_msg)
        if cached:
            logger.info("event_analysis_cache_hit", event_preview=user_msg[:50])
            return {
                "final_response": cached["podcast_brief"],
                "analysis_reports": {
                    **state.get("analysis_reports", {}),
                    "event_display_report": cached["display_report"],
                    "event_podcast_brief": cached["podcast_brief"],
                },
            }

        # Step 2: Call 1 — 事件理解（flash，阻断点）
        understanding = await _analyze_understanding(user_msg)
        if not understanding:
            logger.warning("event_understanding_failed", event_preview=user_msg[:50])
            return {"final_response": "事件分析暂时不可用，请稍后重试", "analysis_reports": {}}

        # Step 3: Call 2 — 传导分析（deep_think）
        transmission = await _analyze_transmission(user_msg, understanding)

        # Step 4: Call 3 — 历史事件（flash）
        history = await _analyze_history(user_msg, understanding)

        # Step 5: Call 4 — 投资总结（flash）
        investment = await _analyze_investment(understanding, transmission, history)

        # Step 6: 播报文本生成
        conclusion = ""
        if investment and isinstance(investment, dict):
            conclusion = str(investment.get("conclusion", ""))
        podcast_brief = await _generate_podcast(understanding, conclusion)

        # Step 7: 字段映射 → analysis_reports
        # 从用户消息中提取事件元信息（title/source 由上游传入，这里用占位）
        event_meta = {
            "eventId": f"evt_{hash(user_msg) & 0xFFFFFFFF:08x}",
            "title": user_msg[:50],
            "source": "",
        }
        analysis_reports = transform_to_frontend(
            understanding, transmission, history, investment, event_meta,
        )
        # 播报文本单独存入
        analysis_reports["event_podcast_brief"] = podcast_brief

        # Step 8: Redis 缓存 + 持久化（保持旧版兼容 — display_report 字段暂留，30 分钟后自然过期）
        display_report = investment if investment else {}
        await set_cached_event(user_msg, display_report, podcast_brief)
        await persist_event_report(user_msg, display_report, podcast_brief)

        return {
            "final_response": podcast_brief,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                **analysis_reports,
            },
        }

    except Exception:
        logger.exception("agent_run_failed", agent="event_analyst_v3")
        return {"final_response": "事件分析暂时不可用，请稍后重试", "analysis_reports": {}}
```

- [ ] **Step 2: 验证 — 确认语法正确 + 可导入**

```powershell
python -c "import ast; ast.parse(open(r'src\aistock_agent\agents\workers\event.py', encoding='utf-8').read()); print('Syntax OK')"
python -c "from src.aistock_agent.agents.workers.event import run; print('Import OK')"
```
Expected: `Syntax OK` + `Import OK`

- [ ] **Step 3: 运行现有测试确保无回归**

```powershell
python -m pytest tests/unit/test_output_parser.py -v
```
Expected: ALL PASS（包括新增的 transform_to_frontend 测试）

- [ ] **Step 4: Commit**

```powershell
git add src/aistock_agent/agents/workers/event.py ; git commit -m "refactor(event): v3 4模块独立 LLM 调用，去掉 display_report 包装层"
```

