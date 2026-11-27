"""V4 实时取数工具白名单：工具名 → NodeApiClient 方法 / 参数白名单。

为什么用静态表而不是动态反射：工具名与参数来自 LLM 或上层编排，不可信；
只有显式登记的组合才允许触达 Node 内部 API。未知工具、未知参数、缺失必填
参数都在发请求之前被拒绝，避免越权读取或把脏参数拼进 URL。
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

logger = logging.getLogger(__name__)

#: 参数取值类型。`date` 转 `datetime.date`（legacy 复盘接口只收 date）；
#: `date_iso` 归一为 `YYYY-MM-DD` 字符串（Node 端点按该格式解析）。
ParamKind = Literal["str", "int", "bool", "date", "date_iso"]


class ToolMappingError(ValueError):
    """工具名或参数不符合白名单（调用方参数错误，非上游故障）。"""


class UnknownToolError(ToolMappingError):
    """工具名不在 V4 白名单内。"""


class InvalidToolParamsError(ToolMappingError):
    """参数名非法、缺失必填参数或取值类型不符合契约。"""


def _empty_arg_map() -> Mapping[str, str]:
    return MappingProxyType({})


def _empty_mirror_args() -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType({})


def _empty_param_kinds() -> Mapping[str, ParamKind]:
    return MappingProxyType({})


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的白名单契约与调用映射。"""

    method: str
    allowed_params: frozenset[str]
    required_params: frozenset[str]
    #: provenance 来源标识，进入信封后供上层区分数据出处。
    source: str
    source_kind: str = "node_api"
    #: 工具参数名 → NodeApiClient 方法形参名（如 symbol → code）。
    arg_map: Mapping[str, str] = field(default_factory=_empty_arg_map)
    #: 同一参数值同时写入多个方法形参（calendar 的 date_from/date_to）。
    mirror_args: Mapping[str, tuple[str, ...]] = field(default_factory=_empty_mirror_args)
    #: 每个白名单参数的值类型；未登记类型的参数视为配置错误。
    param_kinds: Mapping[str, ParamKind] = field(default_factory=_empty_param_kinds)
    #: 进入 URL 路径的参数，必须再做一次字符集校验（防路径注入）。
    path_params: frozenset[str] = frozenset()
    #: 可选参数缺省时改走的无参方法（market_snapshot → last-close-snapshot）。
    fallback_method: str | None = None


#: V4 允许访问 Node 内部接口的全部工具。新增工具必须同时登记参数白名单。
TOOL_MAP: Mapping[str, ToolSpec] = MappingProxyType(
    {
        "quote": ToolSpec(
            method="get_quote",
            allowed_params=frozenset({"symbol"}),
            required_params=frozenset({"symbol"}),
            source="node_api:/internal/quote",
            arg_map=MappingProxyType({"symbol": "symbol"}),
            param_kinds=MappingProxyType({"symbol": "str"}),
            path_params=frozenset({"symbol"}),
        ),
        "stock_kline": ToolSpec(
            method="get_stock_kline",
            allowed_params=frozenset({"symbol", "limit"}),
            required_params=frozenset({"symbol"}),
            source="node_api:/internal/quote/kline",
            arg_map=MappingProxyType({"symbol": "code", "limit": "days"}),
            param_kinds=MappingProxyType({"symbol": "str", "limit": "int"}),
            path_params=frozenset({"symbol"}),
        ),
        "index_kline": ToolSpec(
            method="get_index_kline",
            allowed_params=frozenset({"ts_code", "limit"}),
            required_params=frozenset({"ts_code"}),
            source="node_api:/internal/index/kline",
            arg_map=MappingProxyType({"ts_code": "code", "limit": "days"}),
            param_kinds=MappingProxyType({"ts_code": "str", "limit": "int"}),
            path_params=frozenset({"ts_code"}),
        ),
        "market_snapshot": ToolSpec(
            method="get_close_snapshot",
            allowed_params=frozenset({"date", "slim"}),
            required_params=frozenset(),
            source="node_api:/internal/market/close-snapshot",
            arg_map=MappingProxyType({"date": "snapshot_date"}),
            param_kinds=MappingProxyType({"date": "date_iso", "slim": "bool"}),
            fallback_method="get_last_close_snapshot",
        ),
        "intraday_sectors": ToolSpec(
            method="get_intraday_sectors",
            allowed_params=frozenset(),
            required_params=frozenset(),
            source="node_api:/internal/market/sectors",
        ),
        "calendar_events": ToolSpec(
            method="get_calendar_events",
            allowed_params=frozenset({"date"}),
            required_params=frozenset({"date"}),
            source="node_api:/internal/calendar/events",
            arg_map=MappingProxyType({"date": "date_from"}),
            mirror_args=MappingProxyType({"date": ("date_to",)}),
            param_kinds=MappingProxyType({"date": "date_iso"}),
        ),
        "ths_index_map": ToolSpec(
            method="get_ths_index_map",
            allowed_params=frozenset(),
            required_params=frozenset(),
            source="node_api:/internal/ths/index-map",
        ),
        # 旧链复盘输出只允许进入 legacy_output，不得直接当 V4 evidence。
        "legacy_review": ToolSpec(
            method="get_review_analysis_report",
            allowed_params=frozenset({"date"}),
            required_params=frozenset({"date"}),
            source="node_api:/internal/analysis-reports/review",
            source_kind="legacy_output",
            arg_map=MappingProxyType({"date": "report_date"}),
            param_kinds=MappingProxyType({"date": "date"}),
        ),
    }
)


#: profile 声明的工具组 → 允许触达的原子工具。
#:
#: 为什么不直接让模型调用工具组名：组名只是编排层概念，Node 端点按原子工具
#: 一一对应；只有原子工具才登记了方法/参数白名单。`fundamental` 当前没有对应
#: Node 端点，显式留空而不是伪造一个取不到数的工具，避免分析师“有工具但必失败”。
TOOL_GROUPS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "realtime_quote": ("quote",),
        "news": ("calendar_events",),
        "market_snapshot": ("market_snapshot", "intraday_sectors"),
        "event_evidence": ("calendar_events",),
        "trend": ("ths_index_map", "index_kline"),
        # 基本面端点尚未接入 Node 内部 API：留空并在这里说明原因。
        "fundamental": (),
        "verification": ("legacy_review",),
    }
)


def expand_groups(groups: Iterable[str]) -> list[str]:
    """把 profile 工具组展开为原子工具，保持声明顺序并按首次出现去重。

    未知工具组跳过并记录日志：新建 profile 时多写一个组名不应让整个 run
    因 KeyError 中断，也不应悄悄生成一个永远失败的工具。返回空列表代表该
    profile 当前没有任何真实可用的原子工具，调用方据此决定降级策略。
    """
    expanded: list[str] = []
    for group in groups:
        key = group.strip().lower()
        atoms = TOOL_GROUPS.get(key)
        if atoms is None:
            # 兼容直接写原子工具名的 profile：已是白名单工具则原样保留。
            if key in TOOL_MAP:
                atoms = (key,)
            else:
                logger.warning(
                    "brain_v4_unknown_tool_group", extra={"tool_group": group}
                )
                continue
        for atom in atoms:
            if atom not in expanded:
                expanded.append(atom)
    return expanded


def resolve_tool(tool: str) -> ToolSpec:
    """按归一化工具名取白名单；未知工具立即抛错，不触达客户端。"""
    spec = TOOL_MAP.get(tool.strip().lower())
    if spec is None:
        raise UnknownToolError(f"tool {tool!r} is not allowed by the V4 tool map")
    return spec


def validate_params(tool: str, params: Mapping[str, object]) -> ToolSpec:
    """校验工具名与参数名集合；取值类型/内容由 node_client 统一收口。"""
    spec = resolve_tool(tool)
    unknown = sorted(set(params) - spec.allowed_params)
    if unknown:
        raise InvalidToolParamsError(
            f"tool {tool!r} got unexpected params: {', '.join(unknown)}"
        )
    missing = sorted(spec.required_params - set(params))
    if missing:
        raise InvalidToolParamsError(
            f"tool {tool!r} is missing required params: {', '.join(missing)}"
        )
    return spec
