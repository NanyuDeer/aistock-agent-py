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

import structlog
from langchain_core.language_models.chat_models import BaseChatModel

from ...services.llm import get_deep_think, get_quick_think
from ..graph.profiles import RunProfile

logger = structlog.get_logger()

#: 温度固定 0：分析师在相同案卷下必须给出一致结论，便于回放与审计。
_ANALYST_TEMPERATURE = 0

#: DeepSeek thinking 关闭参数：thinking 模型默认开启 thinking，会拒绝
#: `tool_choice`（`Thinking mode does not support this tool_choice`）。
_DISABLED_THINKING_EXTRA_BODY: dict[str, object] = {"thinking": {"type": "disabled"}}


def _disable_thinking_for_deepseek(model: BaseChatModel) -> BaseChatModel:
    """DeepSeek base_url 上关闭 thinking，使结构化输出可用的 `tool_choice` 生效。

    为什么在构造后处理而不是给 `get_deep_think` 传参：模型工厂是共享入口，
    改调用签名会波及其他路径；这里只对分析师拿到的那一个实例做补丁，且用
    `model_copy` 返回新实例，避免污染调用方缓存。

    settings 派生出的 base_url 落在 ``openai_api_base``；非 deepseek（或替身
    对象没有该字段）原样返回。模型不支持 `extra_body` 时跳过并记录，绝不抛异常。
    """
    base_url = getattr(model, "openai_api_base", None)
    if not isinstance(base_url, str) or "deepseek" not in base_url.lower():
        return model

    if "extra_body" not in getattr(type(model), "model_fields", {}):
        logger.warning(
            "brain_v4_thinking_disable_skipped",
            reason="extra_body unsupported",
            model=type(model).__name__,
        )
        return model

    existing = getattr(model, "extra_body", None)
    merged: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    merged.update(_DISABLED_THINKING_EXTRA_BODY)
    return model.model_copy(update={"extra_body": merged})


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
        model = get_quick_think(temperature=_ANALYST_TEMPERATURE)
    else:
        model = get_deep_think(temperature=_ANALYST_TEMPERATURE)
    return _disable_thinking_for_deepseek(model)


def build_thinking_model() -> BaseChatModel:
    """构造启用 thinking 的终局推理模型。

    与 `build_analyst_model` 分离：分析师主循环需要 `tool_choice` 做结构化
    输出，只能关闭 thinking；终局推理不带工具、不强制 `tool_choice`，因此
    必须保留 thinking 以恢复推理深度。这里刻意不调用
    `_disable_thinking_for_deepseek`，即不注入 `extra_body.thinking=disabled`。
    """
    return get_deep_think(temperature=_ANALYST_TEMPERATURE)
