"""BrainState —— 智慧大脑主图状态，与旧 AgentState 完全隔离。

为什么必须独立：LangGraph 只保留 state schema 声明过的键，未声明键会被静默丢弃
（services/task_runner.py 已有实测记录：stream_mode="updates" 与终态都没有该键、无告警）。
所以 9 条通道（8 个多值 + 单值 judgment）必须显式声明在这里，禁止复用 AgentState。
"""

from collections.abc import Callable
from typing import Annotated, TypedDict, TypeVar

from aistock_agent.schemas.judgment import Judgment, JudgmentClaim, JudgmentEvidence

# 去重 reducer 对元素类型透明：证据是 pydantic 模型、claims 亦然，未来也可能混入 dict。
_T = TypeVar("_T")


_MISSING = object()


def _ident(item: object, key: str) -> object:
    """取业务 id；缺键/属性或值为 None 即报错，绝不让缺 id 元素被静默折叠。"""
    if isinstance(item, dict):
        value = item.get(key, _MISSING)
    else:
        value = getattr(item, key, _MISSING)
    if value is _MISSING or value is None:
        raise ValueError(f"append_dedupe: missing key {key!r}")
    return value


def append(
    left: list[dict[str, object]],
    right: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    """纯追加：conflicts / verification / trace / cost 允许同 id 多条（审计留痕不合并）。"""
    return [*left, *(right or [])]


def append_dedupe(key: str) -> Callable[[list[_T], list[_T] | None], list[_T]]:
    """按业务 id 去重追加，保留先到的一条。

    同 id 不同内容的分歧不在这里处理（reducer 无法跨通道写 conflicts），
    由 conflict_resolver 节点比对后显式写入 conflicts 通道。
    """

    def reducer(left: list[_T], right: list[_T] | None) -> list[_T]:
        merged = list(left)
        seen = {_ident(item, key) for item in merged}
        for item in right or []:
            ident = _ident(item, key)
            if ident in seen:
                continue
            seen.add(ident)
            merged.append(item)
        return merged

    return reducer


def latest_committed(left: Judgment | None, right: Judgment | None) -> Judgment | None:
    """只接受最后一次 committed 研判；None 不覆盖已有值（部分节点失败时保住结论）。"""
    return left if right is None else right


def latest_by_version(
    left: dict[str, dict[str, object]],
    right: dict[str, dict[str, object]] | None,
) -> dict[str, dict[str, object]]:
    """method_prior 按 key 合并，同 key 后写覆盖（方法先验允许迭代修正）。"""
    merged = dict(left)
    merged.update(right or {})
    return merged


def set_union(left: list[str], right: list[str] | None) -> list[str]:
    """render_targets 取并集并排序，保证快照稳定、便于断言。"""
    return sorted({*left, *(right or [])})


class BrainState(TypedDict, total=False):
    run_id: str
    subject_code: str
    subject: str
    as_of: str
    mode: str
    evidence: Annotated[list[JudgmentEvidence], append_dedupe("evidence_id")]
    claims: Annotated[list[JudgmentClaim], append_dedupe("claim_id")]
    conflicts: Annotated[list[dict[str, object]], append]
    judgment: Annotated[Judgment | None, latest_committed]
    verification: Annotated[list[dict[str, object]], append]
    method_prior: Annotated[dict[str, dict[str, object]], latest_by_version]
    render_targets: Annotated[list[str], set_union]
    trace: Annotated[list[dict[str, object]], append]
    cost: Annotated[list[dict[str, object]], append]
