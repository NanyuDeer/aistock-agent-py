"""Node API 实时取数客户端：把 V4 工具名安全映射到 NodeApiClient 只读方法。

本层只做三件事：参数校验/归一、调用映射、把结果包装成带来源与时间置信度的
原始 payload。`as_of` 与超时由 `ToolGateway` 统一控制，本层既不读取系统时间，
也不缓存结果——并发去重交给网关的 memo。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast

from .gateway import ToolClientResult
from .provenance import QualityLevel
from .tool_map import InvalidToolParamsError, ParamKind, ToolSpec, validate_params

if TYPE_CHECKING:
    from aistock_agent.services.data_client import NodeApiClient

#: 旧链输出标记：上层必须把它隔离到 legacy_output，不得直接当 V4 evidence。
LEGACY_SOURCE_KIND = "legacy_output"

#: payload 顶层可用于推断发布时间的字段（不同 Node 端点命名不一致）。
_PUBLISHED_AT_KEYS: tuple[str, ...] = ("published_at", "publish_time", "created_at")

#: 路径参数只允许代码字符，防止把 `../` 等注入拼进 URL 路径。
_SAFE_PATH_PARAM = re.compile(r"^[A-Za-z0-9._-]+$")
_DATE_COMPACT = re.compile(r"^\d{8}$")
_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class NodeToolPayload:
    """Node 工具调用的可审计载荷：数据 + 来源 + 时间置信度。

    为什么不直接把原始 payload 交给网关：`ToolEnvelope` 是冻结契约，
    本层用独立包装承载来源与 `time_confidence`，避免改动既有信封语义。
    """

    data: object
    source: str
    source_kind: str
    time_confidence: QualityLevel
    published_at: datetime | None
    slim: bool | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": self.source,
            "source_kind": self.source_kind,
            "time_confidence": self.time_confidence,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "data": self.data,
        }
        if self.slim is not None:
            payload["slim"] = self.slim
        return payload


class NodeToolClient:
    """`ToolClient` 协议的 Node 实现，供 `ToolGateway` 注入。"""

    def __init__(self, client: NodeApiClient | None = None) -> None:
        # 不在构造阶段建默认客户端：模块导入/对象创建时都不读环境变量，
        # 只有真正取数时才懒加载，避免测试与启动阶段被 settings 影响。
        self._client = client

    @property
    def client(self) -> NodeApiClient:
        if self._client is None:
            from aistock_agent.services.data_client import (  # noqa: PLC0415
                NodeApiClient as _NodeApiClient,
            )

            self._client = _NodeApiClient()
        return self._client

    async def call(self, tool: str, params: dict[str, object]) -> object:
        """按白名单调用一次 Node 接口，返回带来源的审计载荷。"""
        payload = await self._resolve(tool, params)
        return payload.as_dict()

    async def fetch(self, tool: str, params: dict[str, object]) -> ToolClientResult:
        """`ToolGateway` 协议入口；把时间置信度同时映射到质量档位。"""
        payload = await self._resolve(tool, params)
        return ToolClientResult(
            data=payload.as_dict(),
            published_at=payload.published_at,
            quality=payload.time_confidence,
        )

    async def _resolve(self, tool: str, params: dict[str, object]) -> NodeToolPayload:
        normalized_tool = tool.strip().lower()
        spec = validate_params(normalized_tool, params)
        coerced = _coerce_params(spec, normalized_tool, params)
        method_name, kwargs = _build_call(spec, coerced)
        raw = await self._invoke(method_name, kwargs)
        slim = cast("bool | None", coerced.get("slim")) if "slim" in coerced else None

        if spec.source_kind == LEGACY_SOURCE_KIND:
            return _legacy_payload(spec, raw, slim)

        published_at = _extract_published_at(raw)
        return NodeToolPayload(
            data=raw,
            source=spec.source,
            source_kind=spec.source_kind,
            time_confidence=_time_confidence(published_at),
            published_at=published_at,
            slim=slim,
        )

    async def _invoke(self, method_name: str, kwargs: dict[str, object]) -> object:
        # 方法名来自静态白名单而非外部输入；这里只做类型收敛，不做动态兜底。
        method = cast(
            "Callable[..., Awaitable[object]]",
            getattr(self.client, method_name),
        )
        return await method(**kwargs)


def _legacy_payload(
    spec: ToolSpec,
    raw: object,
    slim: bool | None,
) -> NodeToolPayload:
    """把 `ReviewReportReadResult` 收敛为普通 dict，并保留 legacy 标记。"""
    status: object = getattr(raw, "status", None)
    report: object = getattr(raw, "report", None)
    published_at = _extract_published_at(report)
    return NodeToolPayload(
        data={"status": status, "report": report},
        source=spec.source,
        source_kind=spec.source_kind,
        time_confidence=_time_confidence(published_at),
        published_at=published_at,
        slim=slim,
    )


def _coerce_params(
    spec: ToolSpec,
    tool: str,
    params: Mapping[str, object],
) -> dict[str, object]:
    """按 `param_kinds` 收口参数取值；未声明类型的参数属于映射表配置错误。"""
    coerced: dict[str, object] = {}
    for name, value in params.items():
        kind = spec.param_kinds.get(name)
        if kind is None:
            raise InvalidToolParamsError(
                f"tool {tool!r} param {name!r} has no declared value kind"
            )
        result = _coerce_value(tool, name, value, kind)
        if name in spec.path_params:
            text = result
            if not isinstance(text, str) or not _SAFE_PATH_PARAM.fullmatch(text):
                raise InvalidToolParamsError(
                    f"tool {tool!r} param {name!r} contains unsafe path characters"
                )
        coerced[name] = result
    return coerced


def _coerce_value(
    tool: str,
    name: str,
    value: object,
    kind: ParamKind,
) -> object:
    if kind == "int":
        # bool 是 int 子类，必须先排除，否则 True 会被当成 1 静默放行。
        if isinstance(value, bool):
            raise InvalidToolParamsError(f"tool {tool!r} param {name!r} must be int")
        if isinstance(value, int):
            result = value
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            result = int(value.strip())
        else:
            raise InvalidToolParamsError(f"tool {tool!r} param {name!r} must be int")
        if result <= 0:
            raise InvalidToolParamsError(
                f"tool {tool!r} param {name!r} must be a positive int"
            )
        return result

    if kind == "bool":
        if not isinstance(value, bool):
            raise InvalidToolParamsError(f"tool {tool!r} param {name!r} must be bool")
        return value

    if kind == "str":
        if not isinstance(value, str) or not value.strip():
            raise InvalidToolParamsError(
                f"tool {tool!r} param {name!r} must be a non-empty string"
            )
        return value.strip()

    parsed = _parse_date(value)
    if parsed is None:
        raise InvalidToolParamsError(
            f"tool {tool!r} param {name!r} must be a valid date"
        )
    return parsed if kind == "date" else parsed.isoformat()


def _parse_date(value: object) -> date | None:
    """接受 `datetime.date`、ISO 字符串与紧凑 `YYYYMMDD`。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().replace("/", "-")
    if _DATE_COMPACT.fullmatch(text):
        text = f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    if not _DATE_ISO.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _build_call(
    spec: ToolSpec,
    coerced: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    """把工具参数翻译成方法形参；可选参数全缺省时走 fallback 方法。"""
    kwargs: dict[str, object] = {}
    has_mapped_param = False
    for source_name, target_name in spec.arg_map.items():
        if source_name not in coerced:
            continue
        has_mapped_param = True
        value = coerced[source_name]
        kwargs[target_name] = value
        for mirror_name in spec.mirror_args.get(source_name, ()):
            kwargs[mirror_name] = value

    if spec.fallback_method is not None and not has_mapped_param:
        return spec.fallback_method, {}
    return spec.method, kwargs


def _time_confidence(published_at: datetime | None) -> QualityLevel:
    """只表达时间可信度：发布时刻缺失时绝不能默认 high。"""
    return "low" if published_at is None else "high"


def _extract_published_at(payload: object) -> datetime | None:
    """从 payload 顶层字段提取发布时间；缺失或格式非法一律返回 None。"""
    if not isinstance(payload, dict):
        return None
    mapping = cast("dict[str, object]", payload)
    for key in _PUBLISHED_AT_KEYS:
        parsed = _parse_datetime_value(mapping.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        # Node 端 `created_at` 可能是秒或毫秒时间戳；按量级判断，不读系统时钟。
        seconds = value / 1000 if abs(value) >= 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
