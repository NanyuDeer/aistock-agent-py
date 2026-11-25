"""主图装配器 —— 组员只在这里挂子图，不改主图骨架。

插槽规则（C 段分发给组员时以此为准）：
1. 子图必须用 BrainState 作为 state schema；
2. 子图只允许写 evidence / claims / conflicts / method_prior / trace / cost / render_targets；
3. 子图不得写 judgment（定稿权只在 judgment_writer）；
4. 通过 register_subgraph("名字", 工厂函数) 注册，主图按注册顺序串行执行。
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from aistock_agent.graph.brain.nodes.debate import bear_researcher, bull_researcher
from aistock_agent.graph.brain.nodes.perception import market_perception
from aistock_agent.graph.brain.nodes.render import briefing_writer
from aistock_agent.graph.brain.nodes.research import insight_research
from aistock_agent.graph.brain.nodes.resolve import conflict_resolver, judgment_writer
from aistock_agent.graph.brain.state import BrainState

SubgraphFactory = Callable[[], CompiledStateGraph[BrainState, None, BrainState, BrainState]]

_SUBGRAPHS: dict[str, SubgraphFactory] = {}

# 子图输入/输出白名单。直接把编译好的子图 add_node 进去，子图会把"进入时的
# 完整父状态 + 自己新增的通道"一起回写，父图的 append reducer 再追加一次，
# trace/cost/conflicts 会随子图数量成倍膨胀（实测 market_perception ×4）。
# 因此只喂子图真正需要的输入键，只回传子图自己产出的通道。
_SUBGRAPH_INPUT_KEYS = ("run_id", "subject_code", "subject", "as_of", "mode", "evidence", "claims")
_SUBGRAPH_OUTPUT_KEYS = (
    "evidence",
    "claims",
    "conflicts",
    "method_prior",
    "render_targets",
    "trace",
    "cost",
)


def register_subgraph(name: str, factory: SubgraphFactory) -> None:
    """注册研究子图；同 name 覆盖，便于本地迭代。"""
    _SUBGRAPHS[name] = factory


def registered_subgraphs() -> dict[str, SubgraphFactory]:
    return dict(_SUBGRAPHS)


def _mount_subgraph(
    name: str, factory: SubgraphFactory
) -> Callable[[BrainState], Awaitable[dict[str, object]]]:
    """把子图包成主图里的单节点：输入/输出都走白名单，避免父状态被整包回写。"""
    compiled = factory()

    async def _run(state: BrainState) -> dict[str, object]:
        state_view: Mapping[str, object] = state
        payload = {key: state_view[key] for key in _SUBGRAPH_INPUT_KEYS if key in state_view}
        result = await compiled.ainvoke(cast(BrainState, payload))
        result_view: Mapping[str, object] = result
        return {key: result_view[key] for key in _SUBGRAPH_OUTPUT_KEYS if key in result_view}

    return _run


def build_insight_research_subgraph() -> CompiledStateGraph[BrainState, None, BrainState, BrainState]:
    """市场研判子图（组长负责的样板，可独立编译与单测）。"""
    graph: StateGraph[BrainState, None, BrainState, BrainState] = StateGraph(BrainState)
    graph.add_node("insight_research", insight_research)
    graph.add_node("bull_researcher", bull_researcher)
    graph.add_node("bear_researcher", bear_researcher)
    graph.add_node("conflict_resolver", conflict_resolver)
    graph.add_edge(START, "insight_research")
    graph.add_edge("insight_research", "bull_researcher")
    graph.add_edge("bull_researcher", "bear_researcher")
    graph.add_edge("bear_researcher", "conflict_resolver")
    graph.add_edge("conflict_resolver", END)
    return graph.compile()


register_subgraph("insight_research", build_insight_research_subgraph)


def build_brain_graph() -> CompiledStateGraph[BrainState, None, BrainState, BrainState]:
    graph: StateGraph[BrainState, None, BrainState, BrainState] = StateGraph(BrainState)
    graph.add_node("market_perception", market_perception)

    previous = "market_perception"
    for name, factory in registered_subgraphs().items():
        # mypy 对「协议联合类型 + Callable 变量」的匹配会退化成 _Node[Never]，
        # 这里用 Any 仅让其接受一个类型明确的 async 节点函数，运行期行为不变。
        graph.add_node(name, cast(Any, _mount_subgraph(name, factory)))
        graph.add_edge(previous, name)
        previous = name

    graph.add_node("judgment_writer", judgment_writer)
    graph.add_node("briefing_writer", briefing_writer)
    graph.add_edge(previous, "judgment_writer")
    graph.add_edge("judgment_writer", "briefing_writer")
    graph.add_edge(START, "market_perception")
    graph.add_edge("briefing_writer", END)
    return graph.compile()
