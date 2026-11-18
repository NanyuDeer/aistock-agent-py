"""StateGraph 构建 + compile()

图拓扑层：只管骨架，不含节点实现逻辑。
"""

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from aistock_agent.agents.registry import GRAPH_AGENTS
from aistock_agent.agents.supervisor import node as supervisor
from aistock_agent.graph.retry import io_retry_policy
from aistock_agent.graph.routers.intent_router import route_by_intent
from aistock_agent.memory.checkpointer import get_checkpointer
from aistock_agent.observability.callback import get_default_callbacks
from aistock_agent.state.schema import AgentState


class _Default:
    """哨兵类型：标记 compile_graph 未显式传 checkpointer。

    用于区分「未传参」（挂载默认 get_checkpointer()）与「显式传 None」
    （跳过 checkpointer，无多轮恢复）——单纯的 None 默认值无法区分两者。
    """


_DEFAULT = _Default()


def build_graph() -> StateGraph:
    """构建 LangGraph 状态图

    拓扑：
        START → supervisor → [条件路由] → 各 Agent 节点 → END
    """
    graph = StateGraph(AgentState)

    # 节点由 registry 生成（agents/registry.py 单一真源）：节点名 = spec.node_name，
    # 运行体 = spec.run，重试 = spec.retryable。新增 agent 只需改 registry 一处。
    graph.add_node("supervisor", supervisor.run)
    for spec in GRAPH_AGENTS:
        # retry_policy 只在 spec.retryable 为真时挂（理由见 graph/retry.py）
        kwargs = {"retry_policy": io_retry_policy()} if spec.retryable else {}
        graph.add_node(spec.node_name, spec.run, **kwargs)  # type: ignore[arg-type]

    # 边：START → supervisor
    graph.add_edge(START, "supervisor")

    # 条件边：supervisor → 各 Agent
    graph.add_conditional_edges("supervisor", route_by_intent)

    # 各 Agent → END
    for spec in GRAPH_AGENTS:
        graph.add_edge(spec.node_name, END)

    return graph


def compile_graph(
    checkpointer: BaseCheckpointSaver[str] | None | _Default = _DEFAULT,
    callbacks: list[BaseCallbackHandler] | None = None,
) -> CompiledStateGraph:
    """编译图（可直接调用 invoke / stream）。

    Args:
        checkpointer: LangGraph checkpointer。
            - 不传（默认）：挂载 ``get_checkpointer()``，启用多轮对话恢复。
            - 传 ``None``：显式跳过 checkpointer（无多轮恢复）。
            - 传 saver 实例：使用该 saver。
        callbacks: LangChain 回调 handler 列表，绑定到编译后的图，用于图级
            可观测性（on_chain_start/end 等）。不传（默认）时自动挂载
            ``get_default_callbacks()``，使图级事件默认可追踪。LLM/工具级回调
            已在 ``services/llm.py`` 挂载到 ChatOpenAI 实例；图级回调追踪链路
            事件（on_chain_*），与 LLM 级回调（on_llm_*）事件类型不同，不会
            重复计数 token 用量（on_llm_end 仅由 LLM 自身回调链触发）。
            传 ``None`` 等价于默认——也会挂载默认回调；如需禁用图级回调，
            传入空列表 ``[]``。

    build_graph() 保持纯拓扑、不挂载 checkpointer；只有 compile_graph() 挂载。
    """
    if isinstance(checkpointer, _Default):
        checkpointer = get_checkpointer()
    if callbacks is None:
        # 未显式传入回调时，自动挂载默认可观测性回调，追踪图级 on_chain_* 事件。
        callbacks = get_default_callbacks()
    compiled = build_graph().compile(checkpointer=checkpointer)
    # LangGraph compile() 不支持 callbacks 参数；通过 with_config 绑定图级回调。
    # 返回 RunnableBinding，运行时兼容 ainvoke/astream/astream_events。
    return compiled.with_config(callbacks=callbacks)
