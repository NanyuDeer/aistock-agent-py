"""溯源弱反馈观测层单测（spec §13.3 / 计划 Phase 7 Task 7.1，P6'）。

覆盖：关联正确性（hit/miss 计数、hit_rate、样本不足→观望）、source_id 匹配失败跳过并计数、
`c{i}` 条件层不污染档位样本、dry-run 零写入、observe 模式零副作用（除审计表外无任何写入）、
上报失败只 warning、路径带 /api 前缀、unit 口径（relation / sector / driver_type）、
配置默认值锁定、CLI 装配默认 dry-run。

测试日期 2026-09-17（周四，已核实为交易日）。
"""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.config import settings
from aistock_agent.services import attribution_feedback as af
from scripts.attribution_feedback import _parse_args, render_report

D = "2026-09-17"
D2 = "2026-09-16"

# observe 模式绝不允许触达的既有写入端点/方法（零副作用断言用）
_FORBIDDEN_WRITES = (
    "save_prediction",
    "update_prediction_verification",
    "save_analysis_report",
    "put",
    "patch",
    "delete",
)


def _chain(children: list[dict[str, object]], *, date_str: str = D) -> dict[str, object]:
    return {
        "date": date_str,
        "root": {"type": "market", "date": date_str, "summary": "半导体领涨", "index_pct": 0.8},
        "children": children,
    }


def _child(
    sector: str,
    relation: str = "self_driven",
    *,
    pct: float | None = 3.2,
    events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {"sector": sector, "relation": relation, "pct": pct, "events": events or []}


def _record(
    sector: str,
    verification: dict[str, object],
    *,
    date_str: str = D,
    record_id: int = 1,
) -> dict[str, object]:
    """板块预判记录（source_id 口径见 §13.5：sector:{resolved名}:{YYYY-MM-DD}）。"""
    return {
        "id": record_id,
        "source_type": "sector_prediction",
        "source_id": f"sector:{sector}:{date_str}",
        "schema_version": "3.0",
        "prediction": {"horizons": [{"horizon": "short", "target": sector}]},
        "verification": verification,
    }


def _verification(
    *,
    short: str | None = None,
    mid: str | None = None,
    long: str | None = None,
    condition_hit: int = 0,
    condition_miss: int = 0,
    extra_met_true: int = 0,
    extra_met_false: int = 0,
) -> dict[str, object]:
    """验证 dict 形状（真实：档位键 short/mid/long + 条件键 c{i}）。

    - `condition_hit`/`condition_miss`：带 result 的条件 entry（同时带 condition_met 布尔）；
    - `extra_met_true`/`extra_met_false`：只有布尔 condition_met、无 result 的 entry
      （到期前第①段点亮 / 到期未成立态）。
    """
    ver: dict[str, object] = {}
    for key, result in (("short", short), ("mid", mid), ("long", long)):
        if result is not None:
            ver[key] = {"result": result}
    index = 0
    for _ in range(condition_hit):
        ver[f"c{index}"] = {"result": "hit", "condition_met": True}
        index += 1
    for _ in range(condition_miss):
        ver[f"c{index}"] = {"result": "miss", "condition_met": False}
        index += 1
    for _ in range(extra_met_true):
        ver[f"c{index}"] = {"condition_met": True}
        index += 1
    for _ in range(extra_met_false):
        ver[f"c{index}"] = {"condition_met": False}
        index += 1
    return ver


def _review_report(trace: dict[str, object]) -> dict[str, object]:
    return {"status": "completed", "content": {"market_trace": {"trace": trace}}}


def _candidate_trace(category: str) -> dict[str, object]:
    return {
        "candidates": [{"id": "c1", "category": category, "status": "supported"}],
        "primary_chain_id": "c1",
    }


class _Reads:
    """读链路假体 + 零副作用守卫（上下文管理器）。

    - `chain` / `records` / `reports` 为 AsyncMock（可断言调用次数）；
    - `post` 只接受审计表端点，其余端点 pytest.fail（观测层零副作用红线）；
    - `_FORBIDDEN_WRITES` 任一既有写入入口被调用即 pytest.fail。
    """

    def __init__(
        self,
        *,
        chains: dict[str, dict[str, object]] | None = None,
        records: list[dict[str, object]] | None = None,
        reports: list[dict[str, object]] | None = None,
        post_result: dict[str, object] | None = None,
        post_error: Exception | None = None,
    ) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.chain = AsyncMock(side_effect=lambda d, **_k: (chains or {}).get(d))
        self.predictions = AsyncMock(return_value=list(records or []))
        self.reports = AsyncMock(return_value=list(reports or []))
        result = {"ok": True} if post_result is None else post_result

        async def _post(path: str, body: dict[str, object], **_kwargs: object):
            if not path.startswith("/api/internal/attribution-feedback"):
                pytest.fail(f"observe 模式不得向其他端点写入：{path}")
            self.posts.append((path, body))
            if post_error is not None:
                raise post_error
            return result

        async def _forbidden(name: str, *args: object, **kwargs: object) -> None:
            pytest.fail(f"observe 模式不得调用既有写入入口：{name}")

        self.post = AsyncMock(side_effect=_post)
        self._patches = [
            patch.object(af.node_api, "get_attribution_chain", new=self.chain),
            patch.object(af.node_api, "list_all_predictions", new=self.predictions),
            patch.object(af.node_api, "list_analysis_reports", new=self.reports),
            patch.object(af.node_api, "post", new=self.post),
        ]
        for name in _FORBIDDEN_WRITES:
            self._patches.append(
                patch.object(af.node_api, name, new=AsyncMock(side_effect=_forbidden))
            )

    def __enter__(self) -> "_Reads":
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in self._patches:
            p.stop()


# ────────────────────────── 纯函数：建议规则 / 窗口 / unit key ──────────────────────────


def test_settings_defaults_lock_observe_layer() -> None:
    """配置默认值锁定（spec §13.3：本期默认 observe，零副作用）。"""
    assert settings.attribution_feedback_mode == "observe"
    assert settings.attribution_feedback_window == 60
    assert settings.attribution_feedback_min_samples == 10
    assert settings.attribution_feedback_low_threshold == 0.35
    assert settings.attribution_feedback_high_threshold == 0.65
    assert settings.attribution_feedback_unit == "relation"


@pytest.mark.parametrize(
    ("hit_rate", "sample_size", "expected"),
    [
        (0.0, 10, "downgrade"),          # 远低于 low_threshold
        (0.34, 10, "downgrade"),         # 略低于低阈
        (0.35, 10, "hold"),              # 等于低阈 → 观望（严格小于才降权）
        (0.50, 10, "hold"),              # 居中 → 观望
        (0.65, 10, "hold"),              # 等于高阈 → 观望（严格大于才提级）
        (0.66, 10, "upgrade"),           # 略高于高阈
        (1.0, 10, "upgrade"),
        (0.0, 9, "insufficient"),        # 样本不足优先于命中率（即使 0%）
        (1.0, 0, "insufficient"),        # 无样本
        (None, 0, "insufficient"),       # 无命中率且无样本
    ],
)
def test_suggest_rules(hit_rate: float | None, sample_size: int, expected: str) -> None:
    suggestion, _reason = af.suggest(
        hit_rate, sample_size, min_samples=10, low_threshold=0.35, high_threshold=0.65
    )
    assert suggestion == expected


def test_suggest_reason_codes() -> None:
    assert af.suggest(0.2, 10, min_samples=10, low_threshold=0.35, high_threshold=0.65)[1] == (
        "low_hit_rate"
    )
    assert af.suggest(0.8, 10, min_samples=10, low_threshold=0.35, high_threshold=0.65)[1] == (
        "high_hit_rate"
    )
    assert af.suggest(0.5, 10, min_samples=10, low_threshold=0.35, high_threshold=0.65)[1] == (
        "mid_hit_rate"
    )
    assert af.suggest(0.5, 1, min_samples=10, low_threshold=0.35, high_threshold=0.65)[1] == (
        "insufficient_sample"
    )


def test_window_dates_are_trading_days_descending() -> None:
    """窗口口径 = 过去 N 个交易日（含结束日，降序）；非交易日结束日回退到最近交易日。"""
    dates = af.window_dates(D, 5)
    assert len(dates) == 5
    assert dates[0] == D
    assert dates == sorted(dates, reverse=True)
    assert all(af.is_trading_day(date.fromisoformat(d)) for d in dates)

    # 结束日为周六（2026-09-19）→ 回退到 2026-09-18
    assert af.window_dates("2026-09-19", 1) == ["2026-09-18"]


def test_window_dates_zero_or_negative_window() -> None:
    assert af.window_dates(D, 0) == []


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("relation", "relation:self_driven"),
        ("relation_sector", "relation:self_driven:sector:半导体材料"),
        ("sector", "sector:半导体材料"),
        ("driver_type", "driver_type:policy_macro"),
        ("driver_type_sector", "driver_type:policy_macro:sector:半导体材料"),
    ],
)
def test_unit_key_variants(unit: str, expected: str) -> None:
    key = af.unit_key_for(
        unit, relation="self_driven", sector="半导体材料", driver_type="policy_macro"
    )
    assert key == expected


def test_unit_key_unknown_relation_and_sector_fallbacks() -> None:
    assert af.unit_key_for("relation", relation="", sector="", driver_type=None) == (
        "relation:unknown"
    )
    assert af.unit_key_for("sector", relation="", sector="", driver_type=None) == "sector:unknown"


def test_unit_key_driver_type_missing_raises() -> None:
    with pytest.raises(ValueError):
        af.unit_key_for("driver_type", relation="self_driven", sector="半导体", driver_type=None)


def test_unit_needs_driver_type() -> None:
    assert af.unit_needs_driver_type("driver_type") is True
    assert af.unit_needs_driver_type("driver_type_sector") is True
    assert af.unit_needs_driver_type("relation") is False
    assert af.unit_needs_driver_type("sector") is False


def test_unknown_unit_raises() -> None:
    with pytest.raises(ValueError):
        af.unit_key_for("driver", relation="self_driven", sector="半导体", driver_type=None)


# ────────────────────────── 聚合：关联正确性 ──────────────────────────


@pytest.mark.asyncio
async def test_aggregate_hit_miss_rate_and_detail() -> None:
    """档位级 hit/miss → sample_size/hit_rate；c{i} 条件层只进 detail，不污染档位样本。"""
    records = [
        _record(
            "半导体材料",
            _verification(
                short="hit", mid="miss", long="insufficient",
                condition_hit=2, condition_miss=1, extra_met_true=3, extra_met_false=1,
            ),
        )
    ]
    with _Reads(chains={D: _chain([_child("半导体材料")])}, records=records) as reads:
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=False)

    assert stats.matched == 1
    assert stats.unmatched == 0
    assert len(stats.signals) == 1
    signal = stats.signals[0]
    assert signal.unit_key == "relation:self_driven"
    assert signal.mode == "observe"
    assert (signal.sample_size, signal.hit_count, signal.miss_count) == (2, 1, 1)
    assert signal.hit_rate == 0.5
    assert signal.suggestion == "insufficient"  # 样本 2 < 10 → 样本不足-观望
    detail = signal.detail
    assert detail["insufficient_entries"] == 1  # long=insufficient 计入 detail 不计数为 miss
    assert detail["condition_result_hit"] == 2
    assert detail["condition_result_miss"] == 1
    # 布尔计数含带 result 的条件 entry（2 真 + 3 额外真 = 5；1 假 + 1 额外假 = 2）
    assert detail["condition_met_true"] == 5
    assert detail["condition_met_false"] == 2
    assert detail["sectors"] == ["半导体材料"]
    assert detail["source_ids_sample"] == ["sector:半导体材料:2026-09-17"]
    assert [path for path, _ in reads.posts] == ["/api/internal/attribution-feedback"]
    assert stats.written == 1
    assert stats.write_failed == 0


@pytest.mark.asyncio
async def test_aggregate_splits_by_relation_and_sums_multi_days() -> None:
    """同一 unit 跨日累加；不同 relation 分桶（多信号）。"""
    records = [
        _record("半导体材料", _verification(short="hit"), date_str=D),
        _record("半导体材料", _verification(short="miss"), date_str=D2, record_id=2),
        _record("券商", _verification(short="miss"), date_str=D, record_id=3),
    ]
    chains = {
        D: _chain([_child("半导体材料"), _child("券商", "market_follow")]),
        D2: _chain([_child("半导体材料")], date_str=D2),
    }
    with _Reads(chains=chains, records=records) as reads:
        stats = await af.run_attribution_feedback(date=D, window=2, unit="relation", dry_run=True)

    by_key = {s.unit_key: s for s in stats.signals}
    assert set(by_key) == {"relation:self_driven", "relation:market_follow"}
    self_driven = by_key["relation:self_driven"]
    market_follow = by_key["relation:market_follow"]
    assert (self_driven.sample_size, self_driven.hit_count) == (2, 1)
    assert self_driven.hit_rate == 0.5
    assert (market_follow.sample_size, market_follow.miss_count) == (1, 1)
    assert market_follow.hit_rate == 0.0
    assert reads.posts == []  # dry-run 零写入


@pytest.mark.asyncio
async def test_aggregate_recommends_downgrade_and_upgrade_over_thresholds() -> None:
    """样本≥最小样本时按阈值给建议（降权 / 提级）。"""
    records = [_record("半导体材料", _verification(short="miss"), record_id=i) for i in range(8)]
    records += [
        _record("半导体材料", _verification(short="hit"), record_id=i + 100) for i in range(2)
    ]
    with _Reads(chains={D: _chain([_child("半导体材料")])}, records=records):
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=True)
    signal = stats.signals[0]
    assert (signal.sample_size, signal.hit_count) == (10, 2)
    assert signal.hit_rate == 0.2
    assert signal.suggestion == "downgrade"

    high_hits = [_record("半导体材料", _verification(short="hit"), record_id=i) for i in range(10)]
    with _Reads(chains={D: _chain([_child("半导体材料")])}, records=high_hits):
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=True)
    assert stats.signals[0].suggestion == "upgrade"
    assert stats.signals[0].hit_rate == 1.0


@pytest.mark.asyncio
async def test_aggregate_skips_unmatched_source_id_and_counts() -> None:
    """链上板块无对应预判记录（source_id 匹配不上）→ 跳过并计数，不报错。"""
    chains = {D: _chain([_child("半导体材料"), _child("券商", "market_follow")])}
    records = [_record("半导体材料", _verification(short="hit"))]
    with _Reads(chains=chains, records=records):
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=True)
    assert stats.sectors_scanned == 2
    assert stats.matched == 1
    assert stats.unmatched == 1
    assert stats.signals[0].detail["unmatched_sectors"] == ["券商"]


@pytest.mark.asyncio
async def test_aggregate_skips_dates_without_chain() -> None:
    """无链日期跳过并计数（不报错、不产生信号）。"""
    records = [_record("半导体材料", _verification(short="hit"))]
    with _Reads(chains={}, records=records):
        stats = await af.run_attribution_feedback(date=D, window=3, unit="relation", dry_run=True)
    assert stats.dates_scanned == 3
    assert stats.no_chain == 3
    assert stats.signals == []


@pytest.mark.asyncio
async def test_aggregate_ignores_non_sector_predictions_and_blank_sector() -> None:
    """非板块来源记录不参与（source_id 前缀过滤）；空白板块名不计数、不报错。"""
    records = [
        {"source_id": "review:2026-09-17", "verification": {"short": {"result": "hit"}}},
        _record("半导体材料", _verification(short="hit")),
    ]
    chains = {D: _chain([_child("半导体材料"), {"sector": "  ", "relation": "unknown"}])}
    with _Reads(chains=chains, records=records):
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=True)
    assert stats.sectors_scanned == 1  # 空板块名不计数
    assert stats.matched == 1
    assert len(stats.signals) == 1


@pytest.mark.asyncio
async def test_aggregate_sector_unit() -> None:
    """unit=sector：仅按板块聚合（不区分 relation）。"""
    chains = {
        D: _chain([_child("半导体材料", "self_driven")]),
        D2: _chain([_child("半导体材料", "market_follow")], date_str=D2),
    }
    records = [
        _record("半导体材料", _verification(short="hit"), date_str=D),
        _record("半导体材料", _verification(short="miss"), date_str=D2, record_id=2),
    ]
    with _Reads(chains=chains, records=records):
        stats = await af.run_attribution_feedback(date=D, window=2, unit="sector", dry_run=True)
    assert [s.unit_key for s in stats.signals] == ["sector:半导体材料"]
    assert stats.signals[0].sample_size == 2


@pytest.mark.asyncio
async def test_relation_unit_does_not_read_review_reports() -> None:
    """relation/sector 口径无需 driver_type → 不读复盘报告（成本与依赖最小化）。"""
    records = [_record("半导体材料", _verification(short="hit"))]
    with _Reads(chains={D: _chain([_child("半导体材料")])}, records=records) as reads:
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=True)
        assert reads.reports.await_count == 0
    assert stats.driver_unavailable == 0


# ────────────────────────── driver_type 口径（可选 unit） ──────────────────────────


@pytest.mark.asyncio
async def test_driver_type_unit_from_review_report() -> None:
    """unit=driver_type：复用大盘溯源既有映射（_TRACE_CATEGORY_TO_DRIVER）→ policy_macro。"""
    records = [_record("半导体材料", _verification(short="hit"))]
    with _Reads(
        chains={D: _chain([_child("半导体材料")])},
        records=records,
        reports=[_review_report(_candidate_trace("domestic_macro_policy"))],
    ):
        stats = await af.run_attribution_feedback(
            date=D, window=1, unit="driver_type", dry_run=True
        )
    assert [s.unit_key for s in stats.signals] == ["driver_type:policy_macro"]


@pytest.mark.asyncio
async def test_driver_type_unit_sector_variant() -> None:
    records = [_record("半导体材料", _verification(short="hit"))]
    with _Reads(
        chains={D: _chain([_child("半导体材料")])},
        records=records,
        reports=[_review_report(_candidate_trace("industry_technology_supply"))],
    ):
        stats = await af.run_attribution_feedback(
            date=D, window=1, unit="driver_type_sector", dry_run=True
        )
    assert [s.unit_key for s in stats.signals] == [
        "driver_type:trend_fundamental:sector:半导体材料"
    ]


@pytest.mark.asyncio
async def test_driver_type_unit_skips_date_when_report_unavailable() -> None:
    """复盘报告不可读 → 整日样本跳过并计数（不猜 driver、不硬造标识）。"""
    records = [_record("半导体材料", _verification(short="hit"))]
    with _Reads(chains={D: _chain([_child("半导体材料")])}, records=records, reports=[]):
        stats = await af.run_attribution_feedback(
            date=D, window=1, unit="driver_type", dry_run=True
        )
    assert stats.driver_unavailable == 1
    assert stats.signals == []
    assert stats.matched == 0


# ────────────────────────── 零副作用 / 幂等 / 容错 ──────────────────────────


@pytest.mark.asyncio
async def test_dry_run_writes_nothing() -> None:
    """dry-run（CLI 默认）：只统计与渲染，不发起任何上报。"""
    records = [_record("半导体材料", _verification(short="hit"))]
    with _Reads(chains={D: _chain([_child("半导体材料")])}, records=records) as reads:
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=True)
    assert reads.posts == []
    assert stats.written == 0
    assert stats.pending_write == 1
    assert len(stats.signals) == 1


@pytest.mark.asyncio
async def test_observe_mode_zero_side_effects() -> None:
    """observe 模式：除审计表端点外无任何写入；读入的链/记录对象不被就地修改。"""
    chain = _chain([_child("半导体材料")])
    records = [_record("半导体材料", _verification(short="hit", condition_hit=1))]
    chain_snapshot = repr(chain)
    records_snapshot = repr(records)
    with _Reads(chains={D: chain}, records=records) as reads:
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=False)
    # 唯一写入 = 审计表端点；_FORBIDDEN_WRITES 任一被调用会 pytest.fail
    assert [path for path, _ in reads.posts] == ["/api/internal/attribution-feedback"]
    assert repr(chain) == chain_snapshot, "observe 模式不得就地修改链数据"
    assert repr(records) == records_snapshot, "observe 模式不得就地修改预判记录"
    assert stats.written == 1
    # 上报体携带 mode（配置值）与聚合结果，供审计
    body = reads.posts[0][1]
    assert body["mode"] == "observe"
    assert body["unit_key"] == "relation:self_driven"
    assert body["sample_size"] == 1
    assert body["hit_count"] == 1
    assert body["hit_rate"] == 1.0
    assert body["suggestion"] == "insufficient"
    assert body["date"] == D
    assert body["detail"]["unit"] == "relation"


@pytest.mark.asyncio
async def test_store_path_has_api_prefix() -> None:
    """路径必须带 /api 前缀（R13 教训：不带会命中错误 router 恒 404）。"""
    store = af.AttributionFeedbackStore()
    signal = af.build_signal(
        date=D,
        unit_key="relation:self_driven",
        mode="observe",
        sample_size=1,
        hit_count=1,
        miss_count=0,
        detail={"unit": "relation"},
    )
    with patch.object(af.node_api, "post", new=AsyncMock(return_value={"ok": True})) as post:
        assert await store.save(signal) is True
    path = post.await_args.args[0]
    assert path.startswith("/api/internal/attribution-feedback")
    assert not path.startswith("/internal/")


@pytest.mark.asyncio
async def test_store_failure_only_warns_without_raising() -> None:
    """上报失败（返回 None / 抛异常）只 warning，不抛出、不影响主链路。"""
    store = af.AttributionFeedbackStore()
    signal = af.build_signal(
        date=D,
        unit_key="relation:self_driven",
        mode="observe",
        sample_size=0,
        hit_count=0,
        miss_count=0,
        detail={"unit": "relation"},
    )
    with patch.object(af.node_api, "post", new=AsyncMock(return_value=None)):
        assert await store.save(signal) is False
    with patch.object(af.node_api, "post", new=AsyncMock(side_effect=RuntimeError("boom"))):
        assert await store.save(signal) is False


@pytest.mark.asyncio
async def test_write_failure_counted_not_raised() -> None:
    """上报失败 → write_failed 计数，run 不抛出（观测层不得影响既有链路）。"""
    records = [_record("半导体材料", _verification(short="hit"))]
    with _Reads(
        chains={D: _chain([_child("半导体材料")])},
        records=records,
        post_error=RuntimeError("app-api 未部署"),
    ):
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=False)
    assert stats.written == 0
    assert stats.write_failed == 1
    assert len(stats.signals) == 1


@pytest.mark.asyncio
async def test_mode_off_skips_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """mode=off（运维开关）：不读不写，直接返回。"""
    monkeypatch.setattr(settings, "attribution_feedback_mode", "off")
    with _Reads(chains={D: _chain([_child("半导体材料")])}) as reads:
        stats = await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=False)
        assert reads.chain.await_count == 0
    assert stats.mode == "off"
    assert stats.signals == []
    assert reads.posts == []


@pytest.mark.asyncio
async def test_run_uses_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """未显式传参时取配置默认（window/unit/mode），date 取上海今日。"""
    today = af.shanghai_today().isoformat()
    monkeypatch.setattr(settings, "attribution_feedback_window", 1)
    monkeypatch.setattr(settings, "attribution_feedback_unit", "relation")
    records = [_record("半导体材料", _verification(short="hit"), date_str=today)]
    with _Reads(chains={today: _chain([_child("半导体材料")], date_str=today)}, records=records):
        stats = await af.run_attribution_feedback(dry_run=False)
    assert stats.date == today
    assert stats.window == 1
    assert stats.unit == "relation"
    assert stats.mode == "observe"


@pytest.mark.asyncio
async def test_inconsistent_thresholds_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """阈值配置自洽性守卫：low >= high 属配置错误 → 显式报错（不静默产出错误建议）。"""
    monkeypatch.setattr(settings, "attribution_feedback_low_threshold", 0.7)
    monkeypatch.setattr(settings, "attribution_feedback_high_threshold", 0.6)
    with _Reads(chains={D: _chain([_child("半导体材料")])}):
        with pytest.raises(ValueError):
            await af.run_attribution_feedback(date=D, window=1, unit="relation", dry_run=True)


# ────────────────────────── 报告渲染 / CLI 装配 ──────────────────────────


def test_render_report_lists_signals_and_counts() -> None:
    signal = af.build_signal(
        date=D,
        unit_key="relation:self_driven",
        mode="observe",
        sample_size=12,
        hit_count=3,
        miss_count=9,
        detail={"unit": "relation", "reason": "low_hit_rate"},
    )
    stats = af.FeedbackRunStats(
        date=D,
        window=60,
        unit="relation",
        mode="observe",
        dry_run=True,
        dates_scanned=60,
        no_chain=2,
        matched=30,
        unmatched=4,
        signals=[signal],
    )
    text = render_report(stats)
    assert "dry-run" in text
    assert "relation:self_driven" in text
    assert "downgrade" in text
    assert "unmatched=4" in text


def test_cli_defaults_to_dry_run() -> None:
    args = _parse_args([])
    assert args.dry_run is True
    assert args.window is None
    assert args.unit is None
    assert args.date is None

    executed = _parse_args(["--execute", "--date", D, "--window", "5", "--unit", "sector"])
    assert executed.dry_run is False
    assert executed.date == D
    assert executed.window == 5
    assert executed.unit == "sector"
