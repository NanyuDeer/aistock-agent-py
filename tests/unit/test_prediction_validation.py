# tests/unit/test_prediction_validation.py
"""Spec B P2：read_validation_profile（缓存优先 + miss 重算降级）。

@contextmanager 不依赖 Redis 真实实例——patch skills.prediction_validation 的
get/set_cached_validation_profile 与 node_api.list_verified_predictions。
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.target import Target
from aistock_agent.skills import prediction_validation as pv

_STOCK = Target(kind="stock", internal_id="600519", code="600519.SH", name="贵州茅台")


def _profile(**over):
    p = {"target": "600519", "n": 0, "hits": 0, "hit_rate": 0.0, "ci": [0.0, 0.0],
         "sufficient_sample": False, "condition_met_rate": None,
         "condition_summary": {}, "miss_patterns": [],
         "horizon_breakdown": {}, "degradation_rate": 0.0}
    p.update(over)
    return p


def test_slice_horizon_picks_sub_bucket():
    profile = {
        "target": "600519", "n": 3, "hit_rate": 0.6, "ci": [0.0, 1.0],
        "sufficient_sample": False, "condition_met_rate": None,
        "horizon_breakdown": {"short": {"n": 2, "hits": 2, "hit_rate": 1.0,
                                        "ci": [0.0, 1.0], "sufficient_sample": False}},
    }
    out = pv._slice_horizon(profile, "short")
    assert out["horizon"] == "short"
    assert out["n"] == 2 and out["hit_rate"] == 1.0
    # 空档切片 → 零值
    missing = pv._slice_horizon(profile, "long")
    assert missing["n"] == 0 and missing["hit_rate"] == 0.0


@pytest.mark.asyncio
async def test_read_validation_profile_cache_hit():
    """Spec B §7 P2：缓存命中直接返回（不打 verified、不重算）；带 horizon 叠加单档切片。"""
    cached = {"target": "600519", "n": 2, "hits": 1, "hit_rate": 0.5, "ci": [0.0, 1.0],
              "sufficient_sample": False, "condition_met_rate": None,
              "horizon_breakdown": {"short": {"n": 1, "hits": 1, "hit_rate": 1.0,
                                              "ci": [0.0, 1.0], "sufficient_sample": False}}}
    with patch.object(pv, "get_cached_validation_profile",
                      new=AsyncMock(return_value=cached)) as getc, \
         patch.object(pv, "_collect_target_entries", new=AsyncMock(return_value=[])) as collect, \
         patch.object(pv, "set_cached_validation_profile",
                      new=AsyncMock(return_value=True)) as setc:
        out = await pv.read_validation_profile(_STOCK, horizon="short")
    assert out["source"] == "cache" and out["cached"] is True
    assert out["horizon"] == "short" and out["hit_rate"] == 1.0
    assert out["n"] == 1
    collect.assert_not_awaited()
    setc.assert_not_awaited()
    getc.assert_awaited_once_with("600519")


@pytest.mark.asyncio
async def test_read_validation_profile_cache_miss_rebuild():
    """Spec B §7 P2：缓存 miss → 拉 verified 计算 → 落缓存 → 返回 rebuilt。"""
    entries = [
        {"result": "hit", "methodology_version": "3.0", "horizon": "short",
         "target_type": "stock", "approximate": False},
        {"result": "miss", "methodology_version": "3.0", "horizon": "short",
         "target_type": "stock", "approximate": False},
    ]
    with patch.object(pv, "get_cached_validation_profile",
                      new=AsyncMock(return_value=None)), \
         patch.object(pv, "_collect_target_entries", new=AsyncMock(return_value=entries)), \
         patch.object(pv, "set_cached_validation_profile",
                      new=AsyncMock(return_value=True)) as setc:
        out = await pv.read_validation_profile(_STOCK)
    assert out["source"] == "rebuilt" and out["cached"] is False
    assert out["target"] == "600519"
    assert out["n"] == 2 and out["hit_rate"] == 0.5
    setc.assert_awaited_once()
    # 落缓存的 profile 与实际返回一致（不含 source/cached 元信息）
    written = setc.await_args.args[1]
    assert written["target"] == "600519" and written["n"] == 2


@pytest.mark.asyncio
async def test_read_validation_profile_source_failure_degrades():
    """Spec B §7 P2：数据源故障（verified 拉取抛异常）→ 降级为零画像，不 crash。"""
    with patch.object(pv, "get_cached_validation_profile",
                      new=AsyncMock(return_value=None)), \
         patch.object(pv.node_api, "list_verified_predictions",
                      side_effect=Exception("network down")), \
         patch.object(pv, "set_cached_validation_profile", new=AsyncMock(return_value=False)):
        out = await pv.read_validation_profile(_STOCK)
    assert out["source"] == "rebuilt"
    assert out["n"] == 0 and out["hit_rate"] == 0.0
    assert out["miss_patterns"] == []


@pytest.mark.asyncio
async def test_explain_verification_llm_success():
    """Spec B §7 P3：LLM 成功 → 结构化输出四键，判定字段不被改写（红线）。"""
    from aistock_agent.skills.prediction_validation import ValidationExplanation

    fake = ValidationExplanation(
        summary="命中率一般，样本充足",
        miss_reasons=["板块共振走弱"],
        condition_met_insights=["条件化判定样本尚不足"],
        prediction_implications=["short 档命中率低，建议降档"],
    )
    entries = [{"result": "hit"}, {"result": "miss"}]
    with patch.object(pv, "with_chat_structured_output") as wco, \
         patch.object(pv, "get_quick_think"):
        wco.return_value.ainvoke = AsyncMock(return_value=fake)
        out = await pv.explain_verification(_profile(n=10, hit_rate=0.5), entries)
    assert set(out) == {"summary", "miss_reasons", "condition_met_insights",
                        "prediction_implications"}
    assert out["summary"] == "命中率一般，样本充足"
    assert out["prediction_implications"] == ["short 档命中率低，建议降档"]
    # 解释层不得覆盖判定
    assert "hit" not in out and "miss" not in out


@pytest.mark.asyncio
async def test_explain_verification_llm_failure_fallback():
    """Spec B §7 P3：LLM 异常 → 降级为确定性规则兜底（仍含四键，不产交易指令、不改判定）。"""
    entries: list[dict[str, object]] = []
    with patch.object(pv, "with_chat_structured_output") as wco, \
         patch.object(pv, "get_quick_think"):
        wco.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("llm down"))
        out = await pv.explain_verification(
            _profile(n=0, hit_rate=0.0, sufficient_sample=False), entries)
    assert set(out) == {"summary", "miss_reasons", "condition_met_insights",
                        "prediction_implications"}
    assert "样本不足" in out["summary"] or "0" in out["summary"]


@pytest.mark.asyncio
async def test_explain_verification_low_rate_implies_downgrade():
    """Spec B §7 P3：规则兜底——样本充足且命中率低 → 预判含义为降档/补条件（不改判定）。"""
    with patch.object(pv, "with_chat_structured_output") as wco, \
         patch.object(pv, "get_quick_think"):
        wco.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("llm down"))
        out = await pv.explain_verification(
            _profile(n=40, hit_rate=0.25, sufficient_sample=True,
                     miss_patterns=[{"pattern": "plain_miss", "count": 30}]),
            [])
    assert any("降档" in i or "补充条件" in i for i in out["prediction_implications"])
    assert out["miss_reasons"] == ["plain_miss x30"]


def test_enrich_prediction_input_adds_block_preserves_base():
    """Spec B §7 P5：画像并入 LLM 输入——新增 validation_profile 块，不改原 dict、不改判定字段。"""
    base = {"input_mode": "snapshot_driven", "symbol": "上证指数"}
    out = pv.enrich_prediction_input(base, _profile(n=12, hit_rate=0.5))
    assert set(out) == {"input_mode", "symbol", "validation_profile"}
    vp = out["validation_profile"]
    assert vp["n"] == 12 and vp["hit_rate"] == 0.5
    assert vp["target"] == "600519" and vp["sufficient_sample"] is False
    assert "note" not in vp  # 样本不足 → 不提示
    # 原 dict 未被修改（红线：不污染输入源）
    assert "validation_profile" not in base


def test_enrich_prediction_input_low_rate_sufficient_notes():
    """Spec B §7 P5：样本充足且命中率低 → 附"降置信/补条件"提示；样本充足命中率正常 → 不提示。"""
    low = pv.enrich_prediction_input({}, _profile(n=40, hit_rate=0.25, sufficient_sample=True))
    note = low["validation_profile"].get("note")
    assert note and "命中率低" in note and "降低置信" in note
    # 命中率正常 → 不附 note
    ok = pv.enrich_prediction_input({}, _profile(n=40, hit_rate=0.6, sufficient_sample=True))
    assert "note" not in ok["validation_profile"]


def test_enrich_horizon_low_hit_attaches_warning():
    """Task5（B 期）：mid/long 档样本≥3 且命中率<0.4 → note 附抑制提示；样本不足档不提示。"""
    profile = {"target": "000001", "n": 10, "hit_rate": 0.5, "sufficient_sample": True,
               "horizon_breakdown": {
                   "short": {"n": 6, "hit_rate": 0.67},
                   "mid": {"n": 4, "hit_rate": 0.25},   # n>=3 且 <0.4 → 抑制提示
                   "long": {"n": 1, "hit_rate": 0.0},   # n<3 → 不提示
               }}
    out = pv.enrich_prediction_input({"trace": "x"}, profile)
    txt = str(out.get("validation_profile", {}).get("note", ""))
    assert "mid" in txt and "印证少" in txt
    assert "long" not in txt  # 无样本档不提示


def test_enrich_horizon_ok_no_warning():
    """Task5（B 期）：mid 档命中率正常（≥0.4）→ 不附任何 note。"""
    profile = {"target": "000001", "n": 10, "hit_rate": 0.5, "sufficient_sample": True,
               "horizon_breakdown": {"mid": {"n": 4, "hit_rate": 0.6}}}
    out = pv.enrich_prediction_input({"trace": "x"}, profile)
    assert "note" not in out.get("validation_profile", {})


def test_enrich_global_and_horizon_notes_concat_no_punct_clash():
    """FixRound：全局 sufficient 低命中 note 与 mid 低命中 horizon note 并存 → 同键拼接含两者，
    且拼接处无"句号+分号"连用病句（分号前句尾句号去除其一）。"""
    profile = {"target": "000001", "n": 40, "hit_rate": 0.25, "sufficient_sample": True,
               "horizon_breakdown": {"mid": {"n": 4, "hit_rate": 0.25}}}
    out = pv.enrich_prediction_input({"trace": "x"}, profile)
    note = out["validation_profile"].get("note")
    assert note
    assert "该 target 同类条件历史命中率低" in note  # 全局低命中 note 在
    assert "mid" in note and "历史印证少" in note     # horizon note 在
    assert "。；" not in note                        # 无句号+分号病句

