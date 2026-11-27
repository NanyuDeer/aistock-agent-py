"""按 `RunProfile` 选择分析师聊天模型。

为什么单独一层：主图只依赖 `BaseChatModel`，不读 API key、不关心厂商；
模型选择规则集中在 profile 上，测试可用替身验证选择而不真正建网络客户端。

选择规则（计划 P2）：
- `alert_hot` 这类单轮/零议事档位用 quick：低延迟、低成本；
- `market_trace` / `briefing_*` 等深度研究档位用 deep：推理质量优先；
- 分析师输出必须可复现，故温度固定为 0。

注意：本模块在导入阶段只做函数声明，不构造任何客户端；真正建模型发生在
`build_analyst_model` 被调用时。
"""

from langchain_core.language_models.chat_models import BaseChatModel

from ...services.llm import get_deep_think, get_quick_think
from ..graph.profiles import RunProfile

#: 温度固定 0：分析师在相同案卷下必须给出一致结论，便于回放与审计。
_ANALYST_TEMPERATURE = 0


def should_use_quick_model(profile: RunProfile) -> bool:
    """是否使用 quick 模型。

    `alert_hot` 明确走 quick；另外两个条件表达同一意图：研究轮次不超过 1
    或完全不进行议事，都不值得付出 deep 模型的时延与成本。
    """
    return (
        profile.name == "alert_hot"
        or profile.max_research_rounds <= 1
        or profile.max_deliberation_rounds == 0
    )


def build_analyst_model(profile: RunProfile) -> BaseChatModel:
    """按 profile 构造分析师模型；调用方负责在 run 结束后释放。

    这里显式传 `temperature=0`，覆盖 settings 默认温度，保证分析结果可复现。
    """
    if should_use_quick_model(profile):
        return get_quick_think(temperature=_ANALYST_TEMPERATURE)
    return get_deep_think(temperature=_ANALYST_TEMPERATURE)
