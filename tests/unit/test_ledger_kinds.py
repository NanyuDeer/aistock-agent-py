"""账本枚举白名单测试（契约 2026-11-24 · 组长 2026-11-25 要求「应用层加白名单 + CI 测试」）。

这三组常量必须与以下两处保持一致：
  1. 契约文档 docs/superpowers/specs/2026-11-24-evidence-claim-ledger-contract.md
  2. DDL 格式 CHECK  aistock-app-api/src/db/migrations/031_evidence_claim_ledger.sql

**改枚举时三处一起改**，否则本测试失败——这正是它存在的意义：
DDL 不锁白名单（避免加值改表），所以拼写错误只能在这里被拦住。
"""

import re

import pytest

from aistock_agent.constants import (
    CAPABILITIES,
    CLAIM_KINDS,
    EVIDENCE_KINDS,
    LEDGER_KIND_PATTERN,
    Capability,
    ClaimKind,
    EvidenceKind,
)

# 期望值硬编码：与契约文档一字不差地对应。
# 若这里报错，先确认是"文档改了"还是"代码写错了"，不要直接改测试。
EXPECTED_EVIDENCE_KINDS = {
    "market",
    "event",
    "entity",
    "fundamental",
    "causal",
    "flow",
    "user_context",
}

EXPECTED_CLAIM_KINDS = {
    "trend",
    "wind",
    "event",
    "fundamental",
    "causal",
    "risk",
}

EXPECTED_CAPABILITIES = {
    "insight",
    "rhythm",
    "trend",
    "wind",
    "event",
    "fundamental",
}


def test_evidence_kinds_match_contract() -> None:
    assert EVIDENCE_KINDS == EXPECTED_EVIDENCE_KINDS, (
        "evidence.kind 与契约 §2.3 不一致。改枚举请同步改契约文档。"
    )


def test_claim_kinds_match_contract() -> None:
    assert CLAIM_KINDS == EXPECTED_CLAIM_KINDS, (
        "claim.kind 与契约 §2.4 不一致；注意 kind 只放判断性质，能力线放 capability。"
    )


def test_capabilities_match_contract() -> None:
    assert CAPABILITIES == EXPECTED_CAPABILITIES, (
        "capability 与契约 §2.4 不一致。"
    )


def test_claim_kind_excludes_capability_names() -> None:
    """组长 2026-11-25 裁决：kind 不得混入能力线名。

    这条单独测，是因为它是**曾经真的写错过**的地方——
    初版把 insight / rhythm 当成 kind，语义上把"判断性质"和"谁产出的"混为一谈。
    """
    leaked = CLAIM_KINDS & CAPABILITIES
    # trend/wind/event/fundamental 在两边都合法（既是能力线也能是判断性质），
    # 真正不该出现的是纯能力线名。
    forbidden = {"insight", "rhythm"}
    assert not (CLAIM_KINDS & forbidden), (
        f"claim.kind 混入了能力线名: {CLAIM_KINDS & forbidden}。"
        "能力线应写 capability 字段。"
    )
    assert leaked <= {"trend", "wind", "event", "fundamental"}, (
        f"claim.kind 与 capability 出现了非预期重名: {leaked}"
    )


@pytest.mark.parametrize("value", sorted(EVIDENCE_KINDS | CLAIM_KINDS | CAPABILITIES))
def test_every_value_matches_ddl_pattern(value: str) -> None:
    """所有枚举值必须通过 DDL 的格式 CHECK，否则写库会被拒。"""
    assert re.match(LEDGER_KIND_PATTERN, value), (
        f"{value!r} 不符合 DDL 格式约束 {LEDGER_KIND_PATTERN}，写库会被 CHECK 拒绝。"
    )


@pytest.mark.parametrize(
    "bad",
    ["Causal", "", " causal", "causal ", "causal-kind", "因果", "a"],
)
def test_ddl_pattern_rejects_bad_values(bad: str) -> None:
    """反向验证：格式约束确实能挡住脏数据（与 DDL 行为一致）。"""
    assert not re.match(LEDGER_KIND_PATTERN, bad), (
        f"{bad!r} 应被格式约束拒绝，但通过了——DDL 的 CHECK 可能写错。"
    )


def test_kind_classes_are_plain_strings() -> None:
    """枚举类只作命名空间，不是 Enum——与项目既有 SSEEventType 风格一致。"""
    assert EvidenceKind.CAUSAL == "causal"
    assert EvidenceKind.FLOW == "flow"
    assert ClaimKind.CAUSAL == "causal"
    assert ClaimKind.RISK == "risk"
    assert Capability.INSIGHT == "insight"
