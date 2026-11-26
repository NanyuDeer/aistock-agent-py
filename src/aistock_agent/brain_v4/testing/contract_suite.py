"""可复用的 V4 功能契约套件。

任何 feature 的 adapter 只要实现 `build` / `project`，即可在**不修改主图**的前提下
运行本套件，覆盖团队接入规范要求的六类硬约束：

1. `as_of_explicit`：运行必须使用请求传入的 `as_of`，不得回落到系统时间；
2. `snapshot_consistent`：运行回显的 snapshot id/sha 必须与请求一致；
3. `idempotent`：同一请求两次运行结果逐字节一致；
4. `degradation_states`：partial / insufficient 降级状态必须显式可达；
5. `shadow_isolation`：运行必须标记为影子（`is_shadow=True`）；
6. `renderer_preserves_claims`：出口投影只能复制已有 claim，不得新增结论。

本模块只使用固定时间常量，不读取系统时间，也不发起网络调用。
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

SuiteStatus = Literal["ok", "partial", "insufficient"]

_FIXED_AS_OF = datetime(2024, 1, 2, 9, 30, tzinfo=UTC)
_FIXED_CUTOFF = datetime(2024, 1, 2, 9, 0, tzinfo=UTC)
_ALTERNATE_AS_OF = datetime(2021, 6, 1, 10, 0, tzinfo=UTC)
_ALL_STATUSES: tuple[SuiteStatus, ...] = ("ok", "partial", "insufficient")


class SuiteRequest(BaseModel):
    """套件发给 adapter 的显式请求边界。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    as_of: datetime
    data_cutoff: datetime
    snapshot_id: str
    snapshot_sha: str
    status_hint: SuiteStatus = "ok"


class SuiteRun(BaseModel):
    """adapter 运行结果的最小可校验投影。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    as_of: datetime
    data_cutoff: datetime
    snapshot_id: str
    snapshot_sha: str
    status: SuiteStatus
    is_shadow: bool
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)


class SuiteProjection(BaseModel):
    """renderer 投影结果：只允许引用运行中已有的 claim。"""

    model_config = ConfigDict(extra="forbid")

    claim_ids: list[str] = Field(default_factory=list)
    body: dict[str, Any] = Field(default_factory=dict)


class FeatureAdapter(Protocol):
    """feature 需要实现的最小接口。"""

    def build(self, request: SuiteRequest) -> SuiteRun: ...

    def project(self, run: SuiteRun) -> SuiteProjection: ...


class ContractCheck(BaseModel):
    """单项检查结果；`ok=False` 时 `message` 给出可定位原因。"""

    name: str
    ok: bool
    message: str = ""


class ContractSuiteResult(BaseModel):
    """一次套件运行的全部检查结果。"""

    checks: list[ContractCheck] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> list[ContractCheck]:
        return [check for check in self.checks if not check.ok]


def _request(*, as_of: datetime = _FIXED_AS_OF, status_hint: SuiteStatus = "ok") -> SuiteRequest:
    return SuiteRequest(
        run_id="suite-run-1",
        as_of=as_of,
        data_cutoff=_FIXED_CUTOFF,
        snapshot_id="snap_suite",
        snapshot_sha="0" * 64,
        status_hint=status_hint,
    )


def _canonical(run: SuiteRun) -> str:
    return json.dumps(
        run.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def run_contract_suite(feature_adapter: FeatureAdapter) -> ContractSuiteResult:
    """运行六项契约检查；返回逐项结果，不因单点违规中断其余检查。"""
    checks: list[ContractCheck] = []

    def record(name: str, check: Callable[[], None]) -> None:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - 汇总所有违规，交由调用方统一报告
            checks.append(ContractCheck(name=name, ok=False, message=str(exc)))
        else:
            checks.append(ContractCheck(name=name, ok=True, message=""))

    def check_as_of() -> None:
        request = _request(as_of=_FIXED_AS_OF)
        first = feature_adapter.build(request)
        assert first.as_of == _FIXED_AS_OF, "build 未使用请求 as_of，疑似回落到系统时间"
        second = feature_adapter.build(_request(as_of=_ALTERNATE_AS_OF))
        assert second.as_of == _ALTERNATE_AS_OF, "as_of 随系统时间漂移，未显式传递"

    def check_snapshot() -> None:
        request = _request()
        run = feature_adapter.build(request)
        assert run.snapshot_id == request.snapshot_id, "snapshot_id 与请求不一致"
        assert run.snapshot_sha == request.snapshot_sha, "snapshot_sha 与请求不一致"

    def check_idempotent() -> None:
        request = _request()
        first = feature_adapter.build(request)
        second = feature_adapter.build(request)
        assert _canonical(first) == _canonical(second), "同一请求两次运行结果不一致"

    def check_degradation() -> None:
        for hint in ("partial", "insufficient"):
            run = feature_adapter.build(_request(status_hint=hint))
            assert run.status == hint, f"请求降级状态 {hint} 时运行返回 {run.status}"

    def check_shadow() -> None:
        for hint in _ALL_STATUSES:
            run = feature_adapter.build(_request(status_hint=hint))
            assert run.is_shadow is True, f"状态 {hint} 的运行未标记为影子"

    def check_renderer() -> None:
        run = feature_adapter.build(_request())
        projection = feature_adapter.project(run)
        extra = sorted(set(projection.claim_ids) - set(run.claim_ids))
        assert not extra, f"renderer 新增了运行中不存在的 claim: {extra}"

    record("as_of_explicit", check_as_of)
    record("snapshot_consistent", check_snapshot)
    record("idempotent", check_idempotent)
    record("degradation_states", check_degradation)
    record("shadow_isolation", check_shadow)
    record("renderer_preserves_claims", check_renderer)
    return ContractSuiteResult(checks=checks)


__all__ = [
    "ContractCheck",
    "ContractSuiteResult",
    "FeatureAdapter",
    "SuiteProjection",
    "SuiteRequest",
    "SuiteRun",
    "run_contract_suite",
]
