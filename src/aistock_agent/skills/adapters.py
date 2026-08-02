"""Tool → Skill 自动适配器（D5 能力层 C 分级）。

简单工具（quote/flow/news/leader/global/tavily 六类）经本模块以最小包装
暴露为 Skill：``build_skill_adapter(tool)`` 产出 ``(args, goal) -> Evidence``
的调用对象（facts/sources/degraded/raw），复合能力仍保持手写 skill。

适配语义：
- ``facts = [await tool.ainvoke(args) 返回文本]``。
- ``sources``：按工具名生成 ``ChatSource``（kind 复用既有 Literal，最小改动）。
- ``degraded``：返回文本 == ``DEGRADED_MESSAGE`` → True。
- ``raw``：``{"result": 文本}``（D5：供 P3 前端卡片的结构化数据）。
- ``expose=False`` 的内部工具不生成 skill（沿用 tools/registry expose 语义）。
"""
from __future__ import annotations

import importlib
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from langchain_core.tools import BaseTool

from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.registry import SkillCallable, register_skill
from aistock_agent.tools.base import DEGRADED_MESSAGE

logger = structlog.get_logger()

#: 首批适配的简单工具（D5 六类）——适配 skill 名 = tool.name，与手写名不冲突
ADAPTER_TOOL_NAMES: tuple[str, ...] = (
    "get_quote",
    "get_capital_flow",
    "search_cls_news",
    "get_leader_stocks",
    "get_global_markets",
    "tavily_finance_search",
)

#: 工具名 → ChatSource.kind（复用既有 Literal；未覆盖工具用默认值）
_TOOL_SOURCE_KIND: dict[str, str] = {
    "get_quote": "realtime_quote",
    "get_capital_flow": "capital_flow",
    "search_cls_news": "news",
    "get_leader_stocks": "industry",
    "get_global_markets": "realtime_quote",
    "tavily_finance_search": "news",
}
_DEFAULT_SOURCE_KIND = "realtime_quote"

#: 工具名 → 所在模块（延迟导入触发自注册，避免顶层引入整棵 tools 依赖树）
_TOOL_MODULE_BY_NAME: dict[str, str] = {
    "get_quote": "aistock_agent.tools.stock_tools",
    "get_capital_flow": "aistock_agent.tools.stock_tools",
    "search_cls_news": "aistock_agent.tools.news_tools",
    "get_leader_stocks": "aistock_agent.tools.sector_tools",
    "get_global_markets": "aistock_agent.tools.market_tools",
    "tavily_finance_search": "aistock_agent.tools.search_tools",
}


def _tool_prompt_description(tool: BaseTool) -> str:
    """适配 skill 的提示词描述：取 tool docstring 首行（简短，供 LLM 路由）。"""
    doc = getattr(tool, "description", None) or getattr(tool, "__doc__", None)
    text = str(doc).strip() if doc is not None else ""
    return text.splitlines()[0] if text else tool.name


def build_skill_adapter(tool: BaseTool) -> SkillCallable:
    """把 ``@tool`` 包装为 Skill 调用（args, goal）→ Evidence。

    适配器自包含异常处理（tool 内部异常 → degraded Evidence），
    与手写 skill 经 ``@skill`` 装饰后的 degraded 语义对齐。
    """
    tool_name = tool.name
    kind = _TOOL_SOURCE_KIND.get(tool_name, _DEFAULT_SOURCE_KIND)

    async def adapter(args: dict[str, Any], goal: InsightGoal | None) -> Evidence:
        start = time.monotonic()
        metrics = get_metrics_collector()
        now = datetime.now(UTC)
        try:
            text = await tool.ainvoke(args)
        except Exception as exc:
            metrics.record_skill_latency(tool_name, int((time.monotonic() - start) * 1000))
            metrics.record_skill_degraded(tool_name)
            logger.warning(
                "skill_adapter.tool_failed", tool=tool_name, err=str(exc), exc_info=True
            )
            return Evidence(
                facts=[],
                sources=[],
                as_of=now,
                degraded=True,
                degraded_reason=f"{tool_name}: {exc}",
                skill_name=tool_name,
                raw={},
            )
        ms = int((time.monotonic() - start) * 1000)
        metrics.record_skill_latency(tool_name, ms)
        if text == DEGRADED_MESSAGE:
            metrics.record_skill_degraded(tool_name)
        source_id = f"tool:{tool_name}:{int(now.timestamp())}"
        source = ChatSource(
            source_id=source_id,
            kind=kind,
            title=tool_name,
            snippet=str(text)[:200],
            captured_at=now,
        )
        return Evidence(
            facts=[text],
            sources=[source],
            as_of=now,
            degraded=text == DEGRADED_MESSAGE,
            skill_name=tool_name,
            raw={"result": text},
        )

    # 与 @skill 装饰函数保持一致（日志/指标按工具名记录）
    adapter.__name__ = tool_name
    return adapter


def register_tool_skills(*tool_names: str) -> None:
    """为指定工具名注册适配 skill（skill 名 = tool.name，prompt_exposed=True）。

    - 未知工具名 → 跳过并告警（不中断）。
    - 同名冲突（手写 skill）→ 由 ``register_skill`` 拒绝（手写优先）。
    - 延迟导入 tool 模块触发自注册，保证 ``tools.registry.get_all_tools()`` 可查。
    """
    from aistock_agent.tools.registry import get_all_tools

    for name in tool_names:
        module = _TOOL_MODULE_BY_NAME.get(name)
        if module is not None:
            importlib.import_module(module)

    by_name = {t.name: t for t in get_all_tools()}
    for name in tool_names:
        tool = by_name.get(name)
        if tool is None:
            logger.warning("skill_adapter.tool_not_found", name=name)
            continue
        register_skill(
            name,
            build_skill_adapter(tool),
            description=_tool_prompt_description(tool),
        )
