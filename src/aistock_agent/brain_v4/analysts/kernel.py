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
import json
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
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
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


#: 真实 token 硬上限：允许在预算的 1.2 倍内浮动，超过即强制停止。
_HARD_LIMIT_RATIO = 1.2


def _extract_usage_total(response: Any) -> int | None:
    """从一次 `LLMResult` 提取真实 token 总量。

    优先 `llm_output.token_usage.total_tokens`；否则累计每条 generation 的
    `message.usage_metadata.total_tokens`。两者都没有时返回 None（不虚报）。
    """
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, Mapping):
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if isinstance(usage, Mapping):
            total = usage.get("total_tokens")
            if isinstance(total, int) and not isinstance(total, bool):
                return total

    generations = getattr(response, "generations", None)
    if not generations:
        return None
    total_sum = 0
    found = False
    for batch in generations:
        for generation in batch:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if not isinstance(usage, Mapping):
                continue
            total = usage.get("total_tokens")
            if isinstance(total, int) and not isinstance(total, bool):
                total_sum += total
                found = True
    return total_sum if found else None


class _UsageBudgetCallback(BaseCallbackHandler):
    """直接读真实 `LLMResult` 累计 token，并在超硬上限时中断本次运行。

    为什么不用中间件 `response.result`：真实 ChatOpenAI/DeepSeek 路径下该字段
    拿不到 `usage_metadata`，`tokens_used` 长期为 0，预算从未触发。回调挂在
    `config["callbacks"]` 上，随每次模型调用自然收到真实 usage。`raise_error`
    置 True 才能让 `TokenBudgetExceededError` 穿透回调管理器真正终止运行。
    """

    raise_error = True

    def __init__(self, hard_limit: int | None = None) -> None:
        super().__init__()
        self.hard_limit = hard_limit
        self.tokens_used = 0

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: ARG002
        total = _extract_usage_total(response)
        if total is None:
            return
        self.tokens_used += total
        if self.hard_limit is not None and self.tokens_used > self.hard_limit:
            raise TokenBudgetExceededError(
                f"analyst exceeded token hard_limit={self.hard_limit}"
            )


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


_SYNTHESIS_PROMPT = (
    "你是终局推理席位。基于下列证据与初步结论，给出更审慎的最终判断。"
    "只输出一个严格 JSON 对象，不得输出 JSON 之外任何文字。字段仅允许："
    "claim(string)、confidence(0~1 之间的 number)、"
    "uncertainties(string array)、requested_gaps(string array)。\n\n"
    "任务：{task}\n\n初步结论 JSON：{seed}\n"
)


def _message_text(message: Any) -> str:
    """把模型返回的 content 归一为纯文本，兼容字符串与内容块列表。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def _parse_synthesis(text: str) -> dict[str, Any] | None:
    """解析终局推理 JSON；任一字段不合法即整体视为失败（返回 None）。"""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    claim = data.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        return None
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        return None
    uncertainties = data.get("uncertainties")
    requested_gaps = data.get("requested_gaps")
    if not isinstance(uncertainties, list) or not all(
        isinstance(item, str) for item in uncertainties
    ):
        return None
    if not isinstance(requested_gaps, list) or not all(
        isinstance(item, str) for item in requested_gaps
    ):
        return None
    return {
        "claim": claim,
        "confidence": min(1.0, max(0.0, float(confidence))),
        "uncertainties": list(uncertainties),
        "requested_gaps": list(requested_gaps),
    }


def _synthesis_messages(
    task: str, output: Mapping[str, Any]
) -> list[HumanMessage] | None:
    """只有拿到结构化结果时才值得做终局推理，否则返回 None。"""
    structured = output.get("structured_response")
    if not isinstance(structured, AnalystResult):
        return None
    seed = {
        "claim": structured.claim,
        "evidence_refs": list(structured.evidence_refs),
        "confidence": structured.confidence,
        "uncertainties": list(structured.uncertainties),
        "requested_gaps": list(structured.requested_gaps),
    }
    prompt = _SYNTHESIS_PROMPT.format(
        task=task, seed=json.dumps(seed, ensure_ascii=False, default=str)
    )
    return [HumanMessage(content=prompt)]


def _synthesize_with_thinking(
    model: BaseChatModel,
    task: str,
    output: Mapping[str, Any],
    budget: _StepBudgetMiddleware,
    *,
    callback: _UsageBudgetCallback | None = None,
) -> dict[str, Any] | None:
    """追加一次启用 thinking 的终局推理；失败一律回退（返回 None）。

    不带工具、不强制 `tool_choice`，因此可与 thinking 共存；合成消耗的 token
    经同一 callback 计入累计与预算。超预算异常继续上抛，由内核转结构化降级。
    """
    messages = _synthesis_messages(task, output)
    if messages is None:
        return None
    try:
        bound = model.bind()
        response = bound.invoke(
            messages,
            config={"callbacks": [callback]} if callback is not None else None,
        )
        return _parse_synthesis(_message_text(response))
    except TokenBudgetExceededError:
        raise
    except Exception:  # noqa: BLE001 - 终局推理失败必须回退到原有结果
        return None


async def _asynthesize_with_thinking(
    model: BaseChatModel,
    task: str,
    output: Mapping[str, Any],
    budget: _StepBudgetMiddleware,
    *,
    callback: _UsageBudgetCallback | None = None,
) -> dict[str, Any] | None:
    """`_synthesize_with_thinking` 的异步版本，供 `run_analyst_async` 使用。"""
    messages = _synthesis_messages(task, output)
    if messages is None:
        return None
    try:
        bound = model.bind()
        response = await bound.ainvoke(
            messages,
            config={"callbacks": [callback]} if callback is not None else None,
        )
        return _parse_synthesis(_message_text(response))
    except TokenBudgetExceededError:
        raise
    except Exception:  # noqa: BLE001 - 终局推理失败必须回退到原有结果
        return None


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


def _effective_tokens(
    budget: _StepBudgetMiddleware, usage: _UsageBudgetCallback | None
) -> int:
    """真实用量取回调与中间件累计的较大者：两者都只增不减，避免虚报/漏报。"""
    callback_tokens = usage.tokens_used if usage is not None else 0
    return max(budget.tokens_used, callback_tokens)


def _result(
    *,
    role: str,
    research_round: int,
    max_steps: int,
    budget: _StepBudgetMiddleware,
    usage: _UsageBudgetCallback | None = None,
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
        tokens_used=_effective_tokens(budget, usage),
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
    usage: _UsageBudgetCallback | None = None,
    synthesis: Mapping[str, Any] | None = None,
) -> AnalystResult:
    structured = output.get("structured_response")
    if not isinstance(structured, AnalystResult):
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            usage=usage,
            status="stalled",
            error_kind="no_structured_response",
        )
    result = structured.model_copy(
        update={
            "result_id": f"analyst_r{research_round:02d}_{role}",
            "role": role,
            "research_round": research_round,
            "status": "ok",
            "error_kind": None,
            "steps_used": budget.steps_used,
            "max_steps": ceiling,
            "tokens_used": _effective_tokens(budget, usage),
            "token_budget": budget.token_budget or 0,
        }
    )
    if synthesis is not None:
        # 终局推理只覆盖结论字段，运行元数据（id/步数/token/status）仍由内核掌控。
        result = result.model_copy(update=dict(synthesis))
    return result


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
    thinking_model: BaseChatModel | None = None,
) -> AnalystResult:
    """运行一个受限分析师实例并返回结构化结果。

    `model` 必须是调用方注入的聊天模型；测试可传入确定性假模型，
    因此本函数不会读取 API key 或触网。`max_steps` 默认取
    `profile.analyst_max_steps`，不得退回到模块级默认值。`thinking_model`
    缺省为 None：不传时完全不改变既有行为。
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
    usage_callback = _UsageBudgetCallback(
        int(budget.token_budget * _HARD_LIMIT_RATIO)
        if budget.token_budget is not None
        else None
    )
    if budget.token_budget is not None and budget.token_budget <= 0:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            usage=usage_callback,
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
                # callbacks 传入真实 usage 计数器，预算由它而非模型自报驱动。
                config={
                    "recursion_limit": ceiling * 4 + 4,
                    "callbacks": [usage_callback],
                },
            ),
        )
    except StepLimitExceededError:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            usage=usage_callback,
            status="stalled",
            error_kind="step_limit",
        )
    except TokenBudgetExceededError:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            usage=usage_callback,
            status="stalled",
            error_kind="budget_exhausted",
        )
    except Exception as exc:  # noqa: BLE001 - 统一分类为结构化错误结果
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            usage=usage_callback,
            status="error",
            error_kind=type(exc).__name__,
            uncertainties=[str(exc)],
        )

    synthesis: Mapping[str, Any] | None = None
    if thinking_model is not None:
        try:
            synthesis = _synthesize_with_thinking(
                thinking_model, task, output, budget, callback=usage_callback
            )
        except TokenBudgetExceededError:
            return _result(
                role=role,
                research_round=research_round,
                max_steps=ceiling,
                budget=budget,
                usage=usage_callback,
                status="stalled",
                error_kind="budget_exhausted",
            )

    return _finalize(
        output,
        role=role,
        research_round=research_round,
        ceiling=ceiling,
        budget=budget,
        usage=usage_callback,
        synthesis=synthesis,
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
    thinking_model: BaseChatModel | None = None,
) -> AnalystResult:
    """异步运行分析师，支持真实异步工具与外层超时。

    为什么需要异步版本：P2 之后工具是 `StructuredTool` 的异步执行体，直接调用
    `ToolGateway.call`；同步 `agent.invoke` 无法执行 coroutine。超时在 agent
    外层用 `asyncio.wait_for` 控制，超时被转换为结构化 `stalled/timeout`，
    不会把异常抛给主图。`thinking_model` 缺省为 None：不传时零改动。
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
    usage_callback = _UsageBudgetCallback(
        int(budget.token_budget * _HARD_LIMIT_RATIO)
        if budget.token_budget is not None
        else None
    )
    if budget.token_budget is not None and budget.token_budget <= 0:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            usage=usage_callback,
            status="stalled",
            error_kind="budget_exhausted",
        )

    run_config: RunnableConfig = {
        "recursion_limit": ceiling * 4 + 4,
        "callbacks": [usage_callback],
    }
    try:
        if timeout_s is None:
            output = cast(
                Mapping[str, Any],
                await agent.ainvoke(
                    {"messages": [HumanMessage(content=task)]},
                    config=run_config,
                ),
            )
        else:
            output = cast(
                Mapping[str, Any],
                await asyncio.wait_for(
                    agent.ainvoke(
                        {"messages": [HumanMessage(content=task)]},
                        config=run_config,
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
            usage=usage_callback,
            status="stalled",
            error_kind="timeout",
        )
    except StepLimitExceededError:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            usage=usage_callback,
            status="stalled",
            error_kind="step_limit",
        )
    except TokenBudgetExceededError:
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            usage=usage_callback,
            status="stalled",
            error_kind="budget_exhausted",
        )
    except Exception as exc:  # noqa: BLE001 - 统一分类为结构化错误结果
        return _result(
            role=role,
            research_round=research_round,
            max_steps=ceiling,
            budget=budget,
            usage=usage_callback,
            status="error",
            error_kind=type(exc).__name__,
            uncertainties=[str(exc)],
        )

    synthesis: Mapping[str, Any] | None = None
    if thinking_model is not None:
        try:
            synthesis = await _asynthesize_with_thinking(
                thinking_model, task, output, budget, callback=usage_callback
            )
        except TokenBudgetExceededError:
            return _result(
                role=role,
                research_round=research_round,
                max_steps=ceiling,
                budget=budget,
                usage=usage_callback,
                status="stalled",
                error_kind="budget_exhausted",
            )

    return _finalize(
        output,
        role=role,
        research_round=research_round,
        ceiling=ceiling,
        budget=budget,
        usage=usage_callback,
        synthesis=synthesis,
    )
