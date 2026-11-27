"""有步数上限的受限分析师内核。

约束（计划 Task 6 Step 3）：
- 只使用 `langchain.agents.create_agent`，禁止旧版 ReAct 预构建 API；
- 按 `RunProfile` 注入工具、结构化输出 schema 与精确步数上限；
- 模型与工具全部由调用方注入，本模块不读 API key、不发起网络请求；
- 分析师只能产出 `AnalystResult`，绝不写 `V4Judgment` 或规范图；
- 超步数返回结构化 `stalled` 结果，错误分类写入结果而不是抛裸异常。

步数口径：一次 model call 或一次 tool call 各计一步；任一时刻已用步数
达到 `analyst_max_steps` 即停止。`as_of` / `data_cutoff` 等时间边界不由
内核产生，也不读取系统当前时间。
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, cast

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    InputAgentState,
    ModelCallLimitMiddleware,
    OutputAgentState,
    ToolCallLimitMiddleware,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from pydantic import Field

from ..contracts import V4Contract
from ..graph.profiles import RunProfile

AnalystStatus = Literal["ok", "stalled", "error"]

AnalystTool = BaseTool | Callable[..., Any] | dict[str, Any]

AnalystAgentGraph = CompiledStateGraph[
    AgentState[Any],
    None,
    InputAgentState,
    OutputAgentState[Any],
]

_SYSTEM_PROMPT = (
    "你是 AI Stock V4 的受限分析师席位 {role}。"
    "只能阅读给定案卷与证据，返回结构化 AnalystResult；"
    "不得撰写最终研判，不得修改假设图或因果图，不得编造证据 ID。"
)


class StepLimitExceededError(RuntimeError):
    """分析师达到步数上限时由中间件抛出，内核转换为结构化 stalled 结果。"""


class TokenBudgetExceededError(RuntimeError):
    """分析师累计 token 超出 profile 预算时抛出，内核转换为结构化降级。

    为什么不直接截断输出：截断会让模型误以为拿到了完整案卷；把整次运行标记为
    `budget_exhausted`，上层据此记 `partial` / `insufficient`，语义更诚实。
    """


class AnalystResult(V4Contract):
    """分析师的唯一结构化产物。

    `status` / `error_kind` / `steps_used` 由内核写回，模型输出的同名字段
    会被覆盖，避免模型自评绕过步数上限或错误分类。
    """

    result_id: str = ""
    role: str = ""
    research_round: int = Field(default=0, ge=0)
    claim: str
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    uncertainties: list[str] = Field(default_factory=list)
    requested_gaps: list[str] = Field(default_factory=list)
    status: AnalystStatus = "ok"
    error_kind: str | None = None
    steps_used: int = Field(default=0, ge=0)
    max_steps: int = Field(default=0, ge=0)
    # token 用量只由内核写回，模型自报值会被覆盖，防止模型自评绕过预算。
    tokens_used: int = Field(default=0, ge=0)
    token_budget: int = Field(default=0, ge=0)


class _StepBudgetMiddleware(AgentMiddleware[Any, Any]):
    """同时拦截 model call 与 tool call 的精确步数/ token 预算。"""

    def __init__(self, max_steps: int, token_budget: int | None = None) -> None:
        super().__init__()
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.model_calls = 0
        self.tool_calls = 0
        self.tokens_used = 0

    @property
    def steps_used(self) -> int:
        return self.model_calls + self.tool_calls

    def _before_step(self) -> None:
        if self.steps_used >= self.max_steps:
            raise StepLimitExceededError(
                f"analyst exceeded max_steps={self.max_steps}"
            )

    def _record_tokens(self, response: Any) -> None:
        """累计模型回报的 usage；只看 `total_tokens`，缺失时按 0 计。

        为什么在模型调用后检查：只有拿到本次响应才知道真实用量；超预算立即
        终止，避免继续调用工具/模型把成本放大。
        """
        for message in getattr(response, "result", ()):
            usage = getattr(message, "usage_metadata", None)
            if not isinstance(usage, Mapping):
                continue
            total = usage.get("total_tokens")
            if isinstance(total, int) and not isinstance(total, bool):
                self.tokens_used += total
        if self.token_budget is not None and self.tokens_used > self.token_budget:
            raise TokenBudgetExceededError(
                f"analyst exceeded token_budget={self.token_budget}"
            )

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        self._before_step()
        self.model_calls += 1
        response = handler(request)
        self._record_tokens(response)
        return response

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        self._before_step()
        self.model_calls += 1
        response = await handler(request)
        self._record_tokens(response)
        return response

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        self._before_step()
        self.tool_calls += 1
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        self._before_step()
        self.tool_calls += 1
        return await handler(request)


def _build_agent(
    *,
    model: BaseChatModel,
    tools: Sequence[AnalystTool],
    system_prompt: str,
    budget: _StepBudgetMiddleware,
) -> AnalystAgentGraph:
    agent = create_agent(
        model,
        tools=list(tools),
        system_prompt=system_prompt,
        response_format=ToolStrategy(AnalystResult),
        middleware=[
            cast(
                AgentMiddleware[Any, Any, Any],
                ModelCallLimitMiddleware(
                    run_limit=budget.max_steps, exit_behavior="end"
                ),
            ),
            cast(
                AgentMiddleware[Any, Any, Any],
                ToolCallLimitMiddleware(
                    run_limit=budget.max_steps, exit_behavior="end"
                ),
            ),
            cast(AgentMiddleware[Any, Any, Any], budget),
        ],
    )
    return cast(AnalystAgentGraph, agent)


def _result(
    *,
    role: str,
    research_round: int,
    max_steps: int,
    budget: _StepBudgetMiddleware,
    claim: str = "",
    evidence_refs: Sequence[str] = (),
    confidence: float = 0.0,
    uncertainties: Sequence[str] = (),
    requested_gaps: Sequence[str] = (),
    status: AnalystStatus = "ok",
    error_kind: str | None = None,
) -> AnalystResult:
    return AnalystResult(
        result_id=f"analyst_r{research_round:02d}_{role}",
        role=role,
        research_round=research_round,
        claim=claim,
        evidence_refs=list(evidence_refs),
        confidence=confidence,
        uncertainties=list(uncertainties),
        requested_gaps=list(requested_gaps),
        status=status,
        error_kind=error_kind,
        steps_used=budget.steps_used,
        max_steps=max_steps,
        tokens_used=budget.tokens_used,
        token_budget=budget.token_budget or 0,
    )


def _prepare_run(
    *,
    role: str,
    profile: RunProfile,
    model: BaseChatModel,
    tools: Sequence[AnalystTool] | None,
    system_prompt: str | None,
    max_steps: int | None,
    token_budget: int | None,
) -> tuple[int, _StepBudgetMiddleware, AnalystAgentGraph]:
    """构造一次运行所需的预算与 agent。

    `max_steps` 缺省取 `profile.analyst_max_steps`；token 预算缺省取
    `profile.token_budget`，调用方可按剩余额度覆盖。二者都不得回退到模块级默认。
    """
    ceiling = profile.analyst_max_steps if max_steps is None else max_steps
    if ceiling < 1:
        raise ValueError("analyst max_steps must be >= 1")
    token_ceiling = profile.token_budget if token_budget is None else token_budget

    budget = _StepBudgetMiddleware(ceiling, token_ceiling)
    agent = _build_agent(
        model=model,
        tools=tools or (),
        system_prompt=system_prompt or _SYSTEM_PROMPT.format(role=role),
        budget=budget,
    )
    return ceiling, budget, agent


def _finalize(
    output: Mapping[str, Any],
    *,
    role: str,
    research_round: int,
    ceiling: int,
    budget: _StepBudgetMiddleware,
) -> AnalystResult:
    structured = output.get("structured_response")
    if not isinstance(structured, AnalystResult):
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="stalled",
            error_kind="no_structured_response",
        )
    return structured.model_copy(
        update={
            "result_id": f"analyst_r{research_round:02d}_{role}",
            "role": role,
            "research_round": research_round,
            "status": "ok",
            "error_kind": None,
            "steps_used": budget.steps_used,
            "max_steps": ceiling,
            "tokens_used": budget.tokens_used,
            "token_budget": budget.token_budget or 0,
        }
    )


def run_analyst(
    *,
    role: str,
    profile: RunProfile,
    model: BaseChatModel,
    task: str,
    tools: Sequence[AnalystTool] | None = None,
    research_round: int = 0,
    system_prompt: str | None = None,
    max_steps: int | None = None,
    token_budget: int | None = None,
) -> AnalystResult:
    """运行一个受限分析师实例并返回结构化结果。

    `model` 必须是调用方注入的聊天模型；测试可传入确定性假模型，
    因此本函数不会读取 API key 或触网。`max_steps` 默认取
    `profile.analyst_max_steps`，不得退回到模块级默认值。
    """
    ceiling, budget, agent = _prepare_run(
        role=role,
        profile=profile,
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        max_steps=max_steps,
        token_budget=token_budget,
    )
    if budget.token_budget is not None and budget.token_budget <= 0:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="stalled",
            error_kind="budget_exhausted",
        )

    try:
        output = cast(
            Mapping[str, Any],
            agent.invoke(
                {"messages": [HumanMessage(content=task)]},
                # 递归上限必须高于步数预算，否则 LangGraph 会先抛
                # GraphRecursionError，掩盖我们自己的精确步数分类。
                config={"recursion_limit": ceiling * 4 + 4},
            ),
        )
    except StepLimitExceededError:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="stalled",
            error_kind="step_limit",
        )
    except TokenBudgetExceededError:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="stalled",
            error_kind="budget_exhausted",
        )
    except Exception as exc:  # noqa: BLE001 - 统一分类为结构化错误结果
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="error",
            error_kind=type(exc).__name__,
            uncertainties=[str(exc)],
        )

    return _finalize(
        output,
        role=role,
        research_round=research_round,
        ceiling=ceiling,
        budget=budget,
    )


async def run_analyst_async(
    *,
    role: str,
    profile: RunProfile,
    model: BaseChatModel,
    task: str,
    tools: Sequence[AnalystTool] | None = None,
    research_round: int = 0,
    system_prompt: str | None = None,
    max_steps: int | None = None,
    token_budget: int | None = None,
    timeout_s: float | None = None,
) -> AnalystResult:
    """异步运行分析师，支持真实异步工具与外层超时。

    为什么需要异步版本：P2 之后工具是 `StructuredTool` 的异步执行体，直接调用
    `ToolGateway.call`；同步 `agent.invoke` 无法执行 coroutine。超时在 agent
    外层用 `asyncio.wait_for` 控制，超时被转换为结构化 `stalled/timeout`，
    不会把异常抛给主图。
    """
    ceiling, budget, agent = _prepare_run(
        role=role,
        profile=profile,
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        max_steps=max_steps,
        token_budget=token_budget,
    )
    if budget.token_budget is not None and budget.token_budget <= 0:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="stalled",
            error_kind="budget_exhausted",
        )

    try:
        if timeout_s is None:
            output = cast(
                Mapping[str, Any],
                await agent.ainvoke(
                    {"messages": [HumanMessage(content=task)]},
                    config={"recursion_limit": ceiling * 4 + 4},
                ),
            )
        else:
            output = cast(
                Mapping[str, Any],
                await asyncio.wait_for(
                    agent.ainvoke(
                        {"messages": [HumanMessage(content=task)]},
                        config={"recursion_limit": ceiling * 4 + 4},
                    ),
                    timeout=timeout_s,
                ),
            )
    except TimeoutError:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="stalled",
            error_kind="timeout",
        )
    except StepLimitExceededError:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="stalled",
            error_kind="step_limit",
        )
    except TokenBudgetExceededError:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="stalled",
            error_kind="budget_exhausted",
        )
    except Exception as exc:  # noqa: BLE001 - 统一分类为结构化错误结果
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            status="error",
            error_kind=type(exc).__name__,
            uncertainties=[str(exc)],
        )

    return _finalize(
        output,
        role=role,
        research_round=research_round,
        ceiling=ceiling,
        budget=budget,
    )
