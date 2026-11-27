"""把 `RunProfile.tool_groups` 暴露为结构化 LangChain 工具。

为什么必须经过 `ToolGateway`：分析师由模型驱动，工具名与参数都不可信。
网关统一负责 allowlist、`as_of` 时间边界、future_data 拒绝、超时与 provenance
信封；本层只做“工具名 + Pydantic 参数 schema + JSON 结果”的薄适配。

边界约定：
- 只为 profile 显式声明、且登记在 `TOOL_MAP` 的工具组建工具；未登记工具组
  即使进入网关 allowlist 也无法真实取数，生成工具只会制造假故障，故跳过；
- 工具失败不抛裸异常，而是把 `ToolEnvelope.error` 作为结构化 JSON 返回给模型，
  让模型按错误分类降级而不是中断整个研究回合；
- 本模块不读取系统时间：`as_of` 只来自注入的网关。
"""

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import cast

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo

from ..graph.profiles import RunProfile
from ..tools.gateway import ToolGateway
from ..tools.tool_map import TOOL_MAP, ParamKind, ToolSpec

#: 工具参数到 Python 类型。日期类参数交给网关/客户端收敛，工具层按字符串接收。
_PARAM_PYTHON_TYPES: dict[ParamKind, type] = {
    "str": str,
    "int": int,
    "bool": bool,
    "date": str,
    "date_iso": str,
}


def _iso(value: datetime | None) -> str | None:
    """只在有值时输出 ISO 字符串，避免把 None 伪造成时间。"""
    return value.isoformat() if value is not None else None


def _args_schema(tool: str, spec: ToolSpec) -> type[BaseModel]:
    """按工具白名单生成 Pydantic 参数模型。

    必填参数无默认值；可选参数默认 `None`，工具执行前统一剔除 None，避免把
    “未提供”当成显式空值传给客户端。
    """
    fields: dict[str, tuple[type, FieldInfo]] = {}
    for name in sorted(spec.allowed_params):
        py_type = _PARAM_PYTHON_TYPES[spec.param_kinds[name]]
        if name in spec.required_params:
            fields[name] = (py_type, Field(description=f"{tool}.{name}（必填）"))
        else:
            fields[name] = (
                py_type,
                cast(FieldInfo, Field(default=None, description=f"{tool}.{name}（可选）")),
            )
    return cast(
        type[BaseModel],
        create_model(f"{tool}_args", **fields),  # type: ignore[call-overload]
    )


def _tool_description(tool: str, spec: ToolSpec) -> str:
    """向模型说明工具用途、允许参数与返回结构。"""
    allowed = ", ".join(sorted(spec.allowed_params)) or "无"
    return (
        f"V4 工具 {tool}：经 ToolGateway 读取 {spec.source} 证据。"
        f"允许参数：{allowed}。"
        "返回 JSON：tool / evidence_id / as_of / quality / error / error_detail / data；"
        "出错时 error 为错误分类而不是抛异常。"
    )


def _error_payload(tool: str, error: str, detail: str) -> str:
    """工具边界的结构化错误：模型可读、调用方不需要捕获异常。"""
    return json.dumps(
        {
            "tool": tool,
            "evidence_id": None,
            "as_of": None,
            "quality": "low",
            "error": error,
            "error_detail": detail,
            "data": None,
        },
        ensure_ascii=False,
    )


def _make_coroutine(
    tool: str, gateway: ToolGateway
) -> Callable[..., Awaitable[str]]:
    """生成异步工具执行体；语言模型用 `ainvoke` 调用时走这里。"""

    async def _invoke(**kwargs: object) -> str:
        params = {key: value for key, value in kwargs.items() if value is not None}
        try:
            envelope = await gateway.call(tool, params)
        except Exception as exc:  # noqa: BLE001 - 工具边界必须降级为结构化错误
            return _error_payload(tool, type(exc).__name__, str(exc))
        return json.dumps(
            {
                "tool": envelope.tool,
                "evidence_id": envelope.evidence_id,
                "as_of": envelope.as_of.isoformat(),
                "event_time": _iso(envelope.event_time),
                "published_at": _iso(envelope.published_at),
                "fetched_at": envelope.fetched_at.isoformat(),
                "quality": envelope.quality,
                "error": envelope.error,
                "error_detail": envelope.error_detail,
                "data": envelope.data,
            },
            ensure_ascii=False,
            default=str,
        )

    return _invoke


def build_analyst_tools(
    profile: RunProfile, gateway: ToolGateway
) -> list[BaseTool]:
    """为 profile 的每个已登记工具组生成一个结构化工具。

    返回顺序与 `profile.tool_groups` 一致，便于 trace 对齐；未登记工具组被
    跳过而不是报错，保证生产 profile 可以只用到当前已实现的取数映射。
    """
    tools: list[BaseTool] = []
    for group in profile.tool_groups:
        spec = TOOL_MAP.get(group.strip().lower())
        if spec is None:
            continue
        tools.append(
            StructuredTool.from_function(
                coroutine=_make_coroutine(group, gateway),
                name=group,
                description=_tool_description(group, spec),
                args_schema=_args_schema(group, spec),
            )
        )
    return tools
