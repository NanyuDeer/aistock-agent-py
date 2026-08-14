"""DeepSeek json_mode astream 一致性 spike（改进 17「回答内容流式显示」立项门禁，Task 0）。

只在确认流式路径五个断言全部成立后，才允许后续 Task 1-3 构建流式架构；
任一断言失败 → 流式不可行，改为节级伪流式（D9 变体）或维持现状（见 roadmap）。

断言矩阵（真实 API，生产同路径 get_deep_think + with_chat_structured_output）：
| # | 断言 | 失败含义 |
|---|------|---------|
| 1 | 增量性：partial conclusion 逐段差分拼接 == 该次 astream 最终 conclusion | 流式不可用 |
| 2 | 前缀性：任意 partial conclusion 是最终 conclusion 的前缀 | 逐字前缀方案不可行 |
| 3 | 端到端一致：节点内 dispatch → 顶层 on_custom_event，累积 == 最终文本 | 字节全等契约不成立 |
| 4 | 计费一致：astream/ainvoke 记录机制一致（prompt 严格相等） | 计费口径零变化假设不成立 |
| 5 | 空 chunk：content 空窗/缺键/None 显式跳过，不产生脏增量 | 增量解析受污染 |

关键事实（G4/I4 修订的实测落点，2026-08-13 本地 probe + 真实 API 验证）：
- **G4 假设不成立**：``with_structured_output(schema, method="json_mode")``
  对 Pydantic schema 使用 **PydanticOutputParser**（非 JsonOutputParser）。其
  ``parse_result(partial=True)`` 对每个 partial 做**全量 pydantic 校验**——缺必填字段即返回
  None（不产出 partial dict）；仅当整段 JSON 完整（含全部必填字段）时才产出，且产出物是
  **完整的 SynthOutput pydantic 实例**（首个实例出现时 conclusion 已经完整，无增量可 diff）。
- 因此增量 diff 方案（G4 原文"逐段差分 insight.conclusion"）在当前生产链路上**无源可用**；
  若 Task 1-3 要逐字流式，必须把流式腿换成 ``bind(response_format) | JsonOutputParser()``
  （dict schema 的 json_mode 即走该解析器，probe 验证产出 partial dict），最终校验仍走
  PydanticOutputParser——这是需要 roadmap 裁决的生产路径变更（本 spike 只负责把事实摆出来）。
- ``adispatch_custom_event(name, data)``（langchain-core 0.3.58 **不接受** ``version`` kwarg）
  在节点 run 上下文内调用，可传播到顶层 ``astream_events(version="v2")`` 流与 config 挂载的
  ``on_custom_event`` handler（本地 probe + 真实 LLM 运行下均验证通过）。
- env 门禁以**生产路径实际生效的 key**（settings.deep_think_api_key or settings.openai_api_key，
  与 get_deep_think fallback 同逻辑）判定；本地 .env.development 配置了 OPENAI_API_KEY，
  本 spike 对真实 API 跑 1 次 astream + 1 次 ainvoke（低成本，门禁必需）。

断言 4 说明：不做两次调用 completion_tokens 的**字面相等**——两次独立 LLM 调用输出长度
非确定（temperature=0.3），completion 必然不同；断言的是计费机制一致（两路径都通过
TokenUsageCallback 记录、三字段齐备、同 prompt 的 prompt_tokens 严格相等）。

## 实测记录（2026-08-13，本地真实 key，deepseek-v4-pro @ api.deepseek.com）
（Step 2 要求：跑通后回填各断言结果与用量数据）
实测产出：astream 仅 1 个 chunk（type=SynthOutput，pydantic 完整实例），dispatched=1
（整段文本一次性到达，55/59 字符）；两轮实测 prompt_tokens 均为 239（astream 与 ainvoke
严格相等），completion_tokens 随输出长度波动（run2: stream 465/invoke 805；run3: stream
698/invoke 463）——断言 4 按计费机制断言而非 completion 字面相等（两次独立调用输出
非确定，字面相等必然失败）。
- [x] 断言 1（增量性）：**FAIL**——dispatched=1 < 2（文本非增量到达；根因：json_mode +
      Pydantic schema → PydanticOutputParser 全量校验，整段 JSON 完整才产出唯一实例，
      首个实例出现时 conclusion 已完整，无增量可 diff，见"关键事实"）
- [x] 断言 2（前缀性）：**FAIL（与断言 1 同源）**——non_empty=1 < 2，无多个 partial
      conclusion 可验证前缀
- [x] 断言 3（端到端）：**FAIL（增量侧）**——delta_received=1 < 2，无增量事件流；
      但传播机制本身经补充 E2E（节点内 dispatch 完整文本 → 顶层 handler 逐字收到，
      e2e_ok=True）与本地 probe 双重验证通过
- [x] 断言 4（计费一致）：**PASS**——astream/ainvoke 均记录三字段；prompt_tokens 239==239
      严格相等；completion 均 > 0（465/805）
- [x] 断言 5（空 chunk）：**PASS**——单元级空窗用例通过；真实流无脏增量（join==final 反证）
- 结论：**门禁未通过（断言 1/2/3 增量侧失败）**——逐字流式需先改生产链路（流式腿换
  ``bind(response_format) | JsonOutputParser()``，probe 验证可产出 partial dict）或改走
  节级伪流式（D9 变体），提交 roadmap 裁决后再启动 Task 1-3
"""
from __future__ import annotations

import os
from typing import Any, TypedDict

import pytest
from langchain_core.callbacks import BaseCallbackHandler, adispatch_custom_event
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph

from aistock_agent.config import settings
from aistock_agent.graph.nodes.synth_answer import SynthOutput
from aistock_agent.services.llm import get_deep_think, with_chat_structured_output
from aistock_agent.services.token_usage import get_token_usage, reset_token_usage

# ── 环境门禁 ──────────────────────────────────────────────────────

# 用生产路径实际生效的 key 判定（get_deep_think fallback 同逻辑：deep → openai），
# 而非裸 os.getenv：本地 key 配置在 .env.development（pydantic-settings 加载）。
_HAS_LLM_KEY = bool(settings.deep_think_api_key or settings.openai_api_key)

# 端到端核验为"显式触发模式"（证据卫生，非造假）：Task 0 门禁已裁决
# json_mode + Pydantic schema 无增量可 diff（D9 节级伪流式），真实 API 五断言
# 按裁决结果固定失败——不允许常驻失败测试污染套件，默认 SKIP；仅当显式设置
# STREAM_SPIKE_RUN=1（且配置了 LLM key）时才运行，用于复跑/复判门禁证据。
_STREAM_SPIKE_RUN = os.getenv("STREAM_SPIKE_RUN") == "1"

_STREAM_SPIKE_RUN_GATE = not (_STREAM_SPIKE_RUN and _HAS_LLM_KEY)

_STREAM_SPIKE_SKIP_REASON = (
    "流式一致性核验为显式触发模式：需设置 STREAM_SPIKE_RUN=1 且配置 "
    "DEEP_THINK_API_KEY 或 OPENAI_API_KEY 后运行（门禁已裁决 D9 节级伪流式）"
)

# ── 常量 ──────────────────────────────────────────────────────────

# 最小化 schema 约束 prompt（镜像 _build_prompt 的 JSON 输出契约骨架，
# 保证 LLM 稳定产出 insight.conclusion 结构；文本尽量短以控制门禁成本）
_SPIKE_PROMPT = """用 2-3 句中文生成一段市场观点（直接回答问题本身，无需分析过程）。

严格按下方 JSON 输出契约返回，唯一顶层包装：
{
  "insight": {
    "conclusion": "一段 2-3 句的中文市场观点（直接回答）",
    "basis_indices": [],
    "confidence": "low",
    "uncertainty": [],
    "answer_mode": "validate"
  }
}

字段约束：
- 顶层只能有 insight 一个字段
- conclusion 必须是一段完整的中文回答
只返回合法 JSON 对象，不使用 Markdown 或 schema 外字段
"""

# 自定义事件名（与 Task 1 ws.py 分支约定一致）
_CUSTOM_EVENT_NAME = "chat_content_delta"
# 补充 E2E 事件名（验证传播机制本身，非五断言组成部分）
_E2E_FULL_EVENT_NAME = "chat_content_e2e_full"


class _StreamState(TypedDict):
    """最小 StateGraph 状态（仅占位）。"""

    done: bool


# ── 辅助：增量提取（断言 1/2/5 的公共逻辑）─────────────────────────


def _extract_conclusion(chunk: object) -> str:
    """从 astream 产出物提取 conclusion 文本。

    兼容两种形态：partial dict（chunk["insight"]["conclusion"]）与
    SynthOutput pydantic 实例（chunk.insight.conclusion）；非上述形态 → ""。
    （实测生产路径产出 pydantic 实例——见模块 docstring"关键事实"。）
    """
    if isinstance(chunk, dict):
        try:
            value = chunk["insight"]["conclusion"]
        except (KeyError, TypeError):
            return ""
        return value if isinstance(value, str) else ""
    insight = getattr(chunk, "insight", None)
    if insight is None:
        return ""
    value = getattr(insight, "conclusion", None)
    return value if isinstance(value, str) else ""


def _text_delta(prev: str, curr: str) -> str:
    """文本增量：curr 相对 prev 新增的尾部；空/None → 空增量（断言 5）。

    非前缀防御：流式解析异常导致 curr 非 prev 前缀时返回 ""（丢弃该 chunk 的
    内容，不产生脏增量污染逐字拼接；真实 DeepSeek 流未观测到该情况）。
    """
    if not curr:
        return ""
    if not prev:
        return curr
    if curr.startswith(prev):
        return curr[len(prev) :]
    return ""


class _CustomEventRecorder(BaseCallbackHandler):
    """记录顶层 on_custom_event 收到的合成事件（模拟 Task 1 ws.py 的 on_custom_event 分支）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def on_custom_event(
        self,
        event_name: str,
        data: Any,
        *,
        run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        del run_id, kwargs
        self.events.append((event_name, data))


# ── 断言 5 单元级检查（构造空 chunk 场景，不调 API）─────────────────


def test_empty_chunks_produce_no_delta() -> None:
    """空窗 chunk（缺键 / None / 空串 / 非 dict）显式跳过，不产生脏增量。"""
    # 缺 insight / 缺 conclusion / None / 空串 / 非 dict
    assert _extract_conclusion({}) == ""
    assert _extract_conclusion({"insight": {}}) == ""
    assert _extract_conclusion({"insight": {"conclusion": None}}) == ""
    assert _extract_conclusion({"insight": {"conclusion": ""}}) == ""
    assert _extract_conclusion(["not", "a", "dict"]) == ""
    # 空窗 chunk 不产生增量
    assert _text_delta("已有文本", "") == ""
    assert _text_delta("已有文本", _extract_conclusion({"insight": {}})) == ""
    # 非前缀异常（防御）不产生脏增量
    assert _text_delta("abc", "xyz") == ""
    # 正常增量不受影响
    assert _text_delta("你好", "你好世界") == "世界"


# ── 五个断言（真实 API，生产同路径）────────────────────────────────


@pytest.mark.skipif(
    _STREAM_SPIKE_RUN_GATE,
    reason=_STREAM_SPIKE_SKIP_REASON,
)
@pytest.mark.asyncio
async def test_json_mode_astream_end_to_end_consistency() -> None:
    """生产同路径 astream 五个断言：增量/前缀/端到端/计费/空 chunk。

    1 次 astream（含节点内 adispatch_custom_event）+ 1 次 ainvoke（计费对照）。
    五断言独立求值、汇总裁决，避免首个失败短路导致其余断言无证据。
    """
    structured_llm = with_chat_structured_output(get_deep_think(), SynthOutput)
    messages = [HumanMessage(content=_SPIKE_PROMPT)]

    recorder = _CustomEventRecorder()
    partials: list[Any] = []
    dispatched: list[str] = []
    full_dispatched: list[str] = []

    async def stream_node(state: _StreamState) -> dict[str, Any]:
        """图节点：跑嵌套 LLM astream；逐段差分后 dispatch 增量事件。

        增量事件（chat_content_delta）按 G4 设计 dispatch；补发一次完整文本事件
        （chat_content_e2e_full）用于单独验证"节点内 dispatch → 顶层 handler"
        传播机制本身（与增量是否存在解耦）。
        """
        del state
        prev = ""
        async for chunk in structured_llm.astream(messages):
            partials.append(chunk)
            curr = _extract_conclusion(chunk)
            delta = _text_delta(prev, curr)
            if delta:
                dispatched.append(delta)
                await adispatch_custom_event(_CUSTOM_EVENT_NAME, {"content": delta})
            prev = curr
        if prev:
            full_dispatched.append(prev)
            await adispatch_custom_event(_E2E_FULL_EVENT_NAME, {"content": prev})
        return {"done": True}

    builder = StateGraph(_StreamState)
    builder.add_node("synth_stream", stream_node)
    builder.set_entry_point("synth_stream")
    builder.set_finish_point("synth_stream")
    graph = builder.compile()

    # ---- astream 端（生产计费路径：TokenUsageCallback → contextvar 累加）----
    reset_token_usage()
    stream_custom_events: list[Any] = []
    async for event in graph.astream_events(
        {"done": False},
        version="v2",
        config={"callbacks": [recorder]},
    ):
        if event["event"] == "on_custom_event":
            stream_custom_events.append(event)
    stream_usage = get_token_usage()

    # 产出物诊断：partials 到底是什么（供门禁报告/根因定位）
    partial_types = sorted({type(c).__name__ for c in partials})
    non_empty = [c for c in (_extract_conclusion(c) for c in partials) if c]
    final_conclusion = non_empty[-1] if non_empty else ""
    joined = "".join(dispatched)

    # 断言 1：增量性——文本须分多次增量到达（≥2 段）且逐段差分拼接 == 最终 conclusion。
    # 关键：单次全量产出（dispatched==1，如 PydanticOutputParser 在整段 JSON 完整后才
    # 产出唯一实例）使 join==final 恒等式平凡成立，但**不等于**流式可用——门禁必须
    # 显式要求增量段数 ≥ 2，否则"整段生成完一次性到达"会假通过。
    verdicts: dict[str, bool] = {}
    verdicts["A1_增量性"] = (
        bool(joined)
        and len(dispatched) >= 2
        and joined == final_conclusion
    )
    # 断言 2：前缀性——存在 ≥2 个 partial conclusion 且都是最终 conclusion 的前缀
    verdicts["A2_前缀性"] = (
        len(non_empty) >= 2
        and all(final_conclusion.startswith(conc) for conc in non_empty)
    )

    # 断言 3：端到端一致——增量事件累积 == 最终文本（逐字前缀/字节全等）
    delta_received = [
        data.get("content", "")
        for name, data in recorder.events
        if name == _CUSTOM_EVENT_NAME
        and isinstance(data, dict)
        and isinstance(data.get("content"), str)
    ]
    stream_delta_events = [
        ev
        for ev in stream_custom_events
        if ev.get("name") == _CUSTOM_EVENT_NAME
    ]
    verdicts["A3_端到端增量"] = (
        len(delta_received) >= 2
        and len(delta_received) == len(dispatched) == len(stream_delta_events)
        and "".join(delta_received) == final_conclusion
    )

    # 断言 5（真实流内证据）：空窗 chunk 存在且未产生脏增量（join==final 反证）
    empty_window = len(partials) - len(non_empty)
    verdicts["A5_空chunk"] = len(dispatched) <= len(non_empty)

    # 补充 E2E：传播机制本身（与增量无关，单独验证）
    e2e_received = [
        data.get("content", "")
        for name, data in recorder.events
        if name == _E2E_FULL_EVENT_NAME
        and isinstance(data, dict)
        and isinstance(data.get("content"), str)
    ]
    e2e_ok = bool(e2e_received) and "".join(e2e_received) == final_conclusion

    # ---- ainvoke 端（计费对照）----
    reset_token_usage()
    output = await structured_llm.ainvoke(messages)
    assert isinstance(output, SynthOutput), f"ainvoke 未返回 SynthOutput：{type(output)}"
    invoke_usage = get_token_usage()

    # 断言 4：计费一致——两路径都记录、三字段齐备、prompt 严格相等
    usage_ok = (
        stream_usage is not None
        and invoke_usage is not None
        and set(stream_usage) == {"prompt_tokens", "completion_tokens", "total_tokens"}
        and set(invoke_usage) == {"prompt_tokens", "completion_tokens", "total_tokens"}
        and stream_usage["prompt_tokens"] == invoke_usage["prompt_tokens"]
        and stream_usage["completion_tokens"] > 0
        and invoke_usage["completion_tokens"] > 0
    )
    verdicts["A4_计费一致"] = usage_ok

    # 证据输出（供门禁报告回填 docstring）
    print(
        f"\n[spike] partials={len(partials)} types={partial_types} "
        f"non_empty={len(non_empty)} empty_window={empty_window} "
        f"dispatched={len(dispatched)} final_len={len(final_conclusion)} "
        f"delta_received={len(delta_received)} e2e_ok={e2e_ok}"
    )
    print(f"[spike] stream_usage={stream_usage}")
    print(f"[spike] invoke_usage={invoke_usage}")
    print(f"[spike] final_conclusion[:160]={final_conclusion[:160]!r}")
    print(f"[spike] verdicts={verdicts}")

    assert all(verdicts.values()), (
        "门禁断言未全通过，五断言裁决见 [spike] verdicts；"
        "根因提示：json_mode + Pydantic schema 使用 PydanticOutputParser，"
        "首产出即完整 pydantic 实例（conclusion 无增量可 diff）"
        f"（partials 类型：{partial_types}）"
    )
