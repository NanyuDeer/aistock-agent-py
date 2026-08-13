"""evaluator —— 归因相似度三档评分"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.evaluator import evaluate_attribution, extract_agent_attribution

GT = {
    "gt_id": "gt_20260731_us_market_surge",
    "confidence": "high",
    "attribution": {
        "direction": "bullish",
        "drivers": ["隔夜美股暴涨", "外盘传导"],
        "transmission_path": ["美股 → A股高开"],
        "affected_sectors": ["半导体", "算力", "新能源"],
        "source_notes": [],
    },
}

AGENT_OUT = "大盘高开 1.2%，主因为隔夜美股大涨带动风险偏好回升。半导体板块领涨 3.2%。"


def _mock_llm_extract(direction: str, drivers: list[str], sectors: list[str]) -> object:
    payload = {"direction": direction, "drivers": drivers, "sectors": sectors}
    return type("R", (), {"content": json.dumps(payload)})()


def _mock_driver_judge(hit: int, total: int, quotes: list[str] | None = None) -> object:
    payload = {"hit_count": hit, "total_count": total}
    if quotes is not None:
        payload["quotes"] = quotes
    return type("R", (), {"content": json.dumps(payload)})()


def _sample_sector_list_text() -> str:
    """带文末 SECTOR_LIST 板块清单的 agent 输出样例（渲染层确定性产出）。"""
    head = (
        "## 归因结论\n"
        "- 结论：CRO业绩超预期驱动医药板块逆势领涨。\n"
        "## 候选解释与反证\n"
        "- 候选：医药板块资金净流入，芯片概念资金流出。\n"
    )
    sector_list = (
        "<!--SECTOR_LIST_START-->\n"
        "- CRO概念\n- 重组蛋白\n- 细胞免疫治疗\n- 减肥药\n- 金属铅\n"
        "<!--SECTOR_LIST_END-->"
    )
    # 正文足够长（>4000 字符），SECTOR_LIST 落在 4000 截断点之后（复现线上截断）
    return head + "证据索引详情字段" * 600 + "\n" + sector_list


@pytest.mark.asyncio
async def test_extract_input_promotes_sector_list() -> None:
    """extract 输入必须包含 SECTOR_LIST 板块清单（置顶）。

    2026-08-13 板块维 0 命中根因：渲染文末的 SECTOR_LIST（含标准答案细分
    板块：CRO概念/重组蛋白/细胞免疫治疗）被 extract 输入 text[:4000] 截断，
    extract 只能看到正文泛化板块（医药/CRO）。板块清单置顶后 extract 能提取
    细分板块名。
    """
    from aistock_agent.iterate.evaluator import extract_agent_attribution

    text = _sample_sector_list_text()
    assert "SECTOR_LIST_START" in text
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=type(
                "R",
                (),
                {
                    "content": (
                        '{"direction": "bullish", "drivers": [], "sectors": []}'
                    )
                },
            )()
        )
        await extract_agent_attribution(text)
    prompt_arg = factory.return_value.ainvoke.call_args.args[0][1].content
    assert "重组蛋白" in prompt_arg
    assert "细胞免疫治疗" in prompt_arg


@pytest.mark.asyncio
async def test_perfect_match_scores_high() -> None:
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                _mock_llm_extract(
                    "bullish", ["隔夜美股暴涨", "外盘传导"], ["半导体", "算力", "新能源"]
                ),
                _mock_driver_judge(hit=2, total=2),
            ]
        )
        score = await evaluate_attribution(AGENT_OUT, GT)
    assert score.total >= 0.8
    assert score.direction == 0.2
    assert score.drivers == 0.5
    assert score.sectors == 0.3


@pytest.mark.asyncio
async def test_wrong_direction_loses_direction_score() -> None:
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                _mock_llm_extract("bearish", ["国内政策收紧"], ["银行"]),
                _mock_driver_judge(hit=0, total=2),
            ]
        )
        score = await evaluate_attribution(AGENT_OUT, GT)
    assert score.direction == 0.0
    assert score.sectors == 0.0
    assert score.total < 0.5


@pytest.mark.asyncio
async def test_sector_overlap_partial() -> None:
    """agent drivers 为空时 _driver_hit_score 直接返回 0.0，不调 judge LLM。"""
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("bullish", [], ["半导体", "银行", "白酒"])
        )
        score = await evaluate_attribution(AGENT_OUT, GT)
    # 板块重叠 1/3 ≈ 0.1
    assert 0.05 <= score.sectors <= 0.15


@pytest.mark.asyncio
async def test_extract_agent_attribution_returns_struct() -> None:
    """提取函数返回结构必须包含三键（供评分使用）。"""
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("bullish", ["a"], ["半导体"])
        )
        parsed = await extract_agent_attribution("大盘高开 1.2%")
    assert {"direction", "drivers", "sectors"} <= set(parsed)


"""评分重归一化 + direction_present（A12/A15/A3 修复）"""


@pytest.mark.asyncio
async def test_empty_ground_truth_scores_zero_not_full() -> None:
    """空 GT（direction=neutral + sectors=[] + drivers=[]）不得满分：total=0.0。"""
    gt = {
        "attribution": {
            "direction": "neutral",
            "drivers": [],
            "affected_sectors": [],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        # extract 返回空结构；drivers 空 → 不调 judge
        factory.return_value.ainvoke = AsyncMock(
            return_value=type(
                "R",
                (),
                {
                    "content": json.dumps(
                        {"direction": "neutral", "drivers": [], "sectors": []}
                    )
                },
            )()
        )
        score = await evaluate_attribution("任何输出", gt)
    assert score.total == 0.0
    assert score.available_weight == 0.0  # 三维全部无对比对象


@pytest.mark.asyncio
async def test_neutral_direction_excluded_from_denominator() -> None:
    """GT direction=neutral 时方向维不参与分母（direction_present=False），
    板块+驱动全中仍可达 1.0（重归一化），但方向错误不再贡献 0.2 白给。"""
    gt = {
        "attribution": {
            "direction": "neutral",
            "drivers": ["外盘传导"],
            "affected_sectors": ["半导体"],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                # extract：direction=neutral（撞 GT）、drivers/sectors 全中
                type(
                    "R",
                    (),
                    {
                        "content": json.dumps(
                            {
                                "direction": "neutral",
                                "drivers": ["外盘传导"],
                                "sectors": ["半导体"],
                            }
                        )
                    },
                )(),
                # judge：hit=1 total=1
                type("R", (), {"content": json.dumps({"hit_count": 1, "total_count": 1})})(),
            ]
        )
        score = await evaluate_attribution("外盘传导，半导体领涨", gt)
    # 可用权重 = drivers 0.5 + sectors 0.3 = 0.8；得分 = 0.5+0.3=0.8 → total=1.0
    assert score.total == 1.0
    assert score.available_weight == 0.8


"""gap_analysis 感知 present 维度（Task 6 审查 Important 修复）"""


@pytest.mark.asyncio
async def test_gap_analysis_no_phantom_gap_when_all_dims_excluded() -> None:
    """GT direction=neutral + sectors=[] + drivers=[]：三维全部被排除出评分，
    gap_analysis 不得含假缺口——应为"无显著差距"（而非必报"方向不一致"等）。"""
    gt = {
        "attribution": {
            "direction": "neutral",
            "drivers": [],
            "affected_sectors": [],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        # extract 返回 neutral（与 GT 语义匹配）；drivers 空 → 不调 judge
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("neutral", [], [])
        )
        score = await evaluate_attribution("任何输出", gt)
    assert "方向不一致" not in score.gap_analysis
    assert "板块覆盖不足" not in score.gap_analysis
    assert "驱动要素覆盖不足" not in score.gap_analysis
    assert score.gap_analysis == "无显著差距"


@pytest.mark.asyncio
async def test_gap_analysis_reports_direction_only_when_present() -> None:
    """GT direction=bullish + sectors=[] + drivers=[]：仅方向维参与评分，
    方向不匹配应报"方向不一致"；被排除的板块/驱动两维不得报假缺口。"""
    gt = {
        "attribution": {
            "direction": "bullish",
            "drivers": [],
            "affected_sectors": [],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("bearish", ["国内政策收紧"], ["银行"])
        )
        score = await evaluate_attribution("偏空解读", gt)
    assert "方向不一致" in score.gap_analysis
    assert "板块覆盖不足" not in score.gap_analysis
    assert "驱动要素覆盖不足" not in score.gap_analysis


"""judge 固定分母 + corpus 引用机械核验（A2/N5 修复）"""


@pytest.mark.asyncio
async def test_driver_hit_score_uses_fixed_denominator() -> None:
    """judge 自报 total 小于 len(truth) 时，分母仍固定为 len(truth)，防小 total 满分。"""
    gt = {
        "attribution": {
            "direction": "bullish",
            "drivers": ["a", "b", "c"],  # 3 条 truth
            "affected_sectors": [],
            "corpus": "a b c 语料",
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                # extract：direction=bearish 与 GT(bullish) 不匹配 → direction_score=0，
                # 隔离驱动维，保证下方 total<0.5 断言成立（若方向命中 total≈0.52 会误报）
                _mock_llm_extract("bearish", ["a"], []),
                # judge 恶意自报 hit=1 total=1（truth 实际 3 条）；
                # quotes=["a"] 在 corpus="a b c 语料" 中可验证 → 核验通过 hit=1，
                # 但分母固定 len(truth)=3 → 0.5*1/3≈0.1667，而非 0.5 满分
                _mock_driver_judge(1, 1, quotes=["a"]),
            ]
        )
        score = await evaluate_attribution("a", gt)
    # 驱动分 = 0.5 * 1/3 ≈ 0.1667，而非 0.5 满分
    assert score.drivers == pytest.approx(0.1667, abs=0.001)
    assert score.total < 0.5


@pytest.mark.asyncio
async def test_driver_hit_rejects_unverifiable_quote() -> None:
    """judge 声称命中但引用片段不在 corpus 中 → 该命中作废（机械核验）。"""
    gt = {
        "attribution": {
            "direction": "bullish",
            "drivers": ["隔夜美股暴涨"],
            "affected_sectors": [],
            "corpus": "财联社：A股高开，半导体领涨",  # 语料不含"隔夜美股"
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                _mock_llm_extract("bullish", ["隔夜美股暴涨"], []),
                # judge 报 hit=1 且引用"隔夜美股暴涨"（不在 corpus）
                _mock_driver_judge(1, 1, quotes=["隔夜美股暴涨"]),
            ]
        )
        score = await evaluate_attribution("隔夜美股暴涨", gt)
    assert score.drivers == 0.0  # 引用无法在 corpus 验证 → 命中作废


@pytest.mark.asyncio
async def test_driver_hit_accepts_reworded_quote_with_keywords() -> None:
    """agent 改写表述（含语料关键词、非逐字）通过机械核验。

    2026-08-13 服务器驱动维全 0 根因：N5 逐字核验误杀语义改写——agent LLM
    生成的驱动表述（如"美国FCC限制中国光模块对美出口"）与切片语料
    （"美国拟限制含光模块的中国数据中心组件对美出口"）措辞不同，逐字
    匹配必然失败 → verified=0 → 驱动维恒 0 分（即使语义完全等价）。
    放宽为关键词溯源（任意 2 字连续片段在语料中即可验证）。
    """
    gt = {
        "attribution": {
            "direction": "bullish",
            "drivers": ["美国限制进口中国光模块"],
            "affected_sectors": [],
            # 语料含"美国""光模块""出口"等关键词，但与 agent 改写表述非逐字一致
            "corpus": "财联社：美国拟限制含光模块的中国数据中心组件对美出口，光模块概念股下跌",
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                _mock_llm_extract("bullish", ["美国FCC限制中国光模块对美出口"], []),
                # judge 引用 agent 的改写表述（非语料逐字）
                _mock_driver_judge(1, 1, quotes=["美国FCC限制中国光模块对美出口"]),
            ]
        )
        score = await evaluate_attribution("美国FCC限制中国光模块对美出口", gt)
    assert score.drivers == 0.5  # 含语料关键词（美国/光模块/出口）→ 核验通过，命中保留


"""T7 M3: corpus=None 防御（str(None)='None' 是 truthy，导致误触发核验）"""


@pytest.mark.asyncio
async def test_corpus_null_does_not_trigger_verification() -> None:
    """corpus=None 时 str(None)='None' 是 truthy，导致 quotes 在 'None' 字符串中
    被误验证——合法引用 '隔夜美股暴涨' 不在 'None' 中 → verified=0 → hit 被作废。

    修复后 corpus=None → ''（falsy），if corpus: 为 False，不触发机械核验，
    hit 保留 LLM 报告值（1），drivers_score=0.5。
    """
    gt = {
        "attribution": {
            "direction": "bullish",
            "drivers": ["隔夜美股暴涨"],
            "affected_sectors": [],
            "corpus": None,  # 关键：corpus 显式为 None
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                _mock_llm_extract("bullish", ["隔夜美股暴涨"], []),
                # judge 报 hit=1，引用正常文本（不在 'None' 字符串中）
                _mock_driver_judge(1, 1, quotes=["隔夜美股暴涨"]),
            ]
        )
        score = await evaluate_attribution("隔夜美股暴涨", gt)
    # 修复前：corpus=str(None)='None'（truthy）→ '隔夜美股暴涨' not in 'None'
    #         → verified=0 → hit=0 → drivers=0.0
    # 修复后：corpus=''（falsy）→ 不触发核验 → hit=1 → drivers=0.5
    assert score.drivers == 0.5


"""T6 M2: 单维 truth 边界（仅 drivers 或仅 sectors 的重归一化）"""


@pytest.mark.asyncio
async def test_only_drivers_truth_directions_excluded() -> None:
    """GT 仅有 drivers（direction=neutral, sectors=[]）：方向维和板块维排除，
    available_weight=0.5（仅驱动维），total = drivers_score / 0.5。"""
    gt = {
        "attribution": {
            "direction": "neutral",
            "drivers": ["外盘传导"],
            "affected_sectors": [],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                _mock_llm_extract("neutral", ["外盘传导"], []),
                _mock_driver_judge(1, 1),
            ]
        )
        score = await evaluate_attribution("外盘传导", gt)
    assert score.available_weight == 0.5  # 仅驱动维 0.5
    # drivers_score = 0.5 * 1/1 = 0.5; total = 0.5 / 0.5 = 1.0
    assert score.drivers == 0.5
    assert score.total == 1.0


@pytest.mark.asyncio
async def test_only_sectors_truth_drivers_excluded() -> None:
    """GT 仅有 sectors（direction=neutral, drivers=[]）：方向维和驱动维排除，
    available_weight=0.3（仅板块维），total = sectors_score / 0.3。"""
    gt = {
        "attribution": {
            "direction": "neutral",
            "drivers": [],
            "affected_sectors": ["半导体"],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("neutral", [], ["半导体"])
        )
        score = await evaluate_attribution("半导体领涨", gt)
    assert score.available_weight == 0.3  # 仅板块维 0.3
    # sectors_score = 0.3 * 1/1 = 0.3; total = 0.3 / 0.3 = 1.0
    assert score.sectors == 0.3
    assert score.total == 1.0


"""T6 M4: 重归一化阈值影响分析（纯文档性测试，不改代码）"""


@pytest.mark.asyncio
async def test_renormalization_threshold_impact() -> None:
    """重归一化使单维 truth 更易达标，需后续校准。

    场景一（全维 truth）：direction+drivers+sectors agent 全命中 → total=1.0 >= 0.8 ✓
    场景二（单维 truth）：仅 sectors agent 全命中 → total=1.0 >= 0.8 ✓
    但场景二实际只验证了板块覆盖，达标过易——重归一化放大了剩余维度权重，
    使 0.8 达标阈值在单维场景下更易达成。
    """
    # 场景一：全维 truth（direction+drivers+sectors）
    gt_full = {
        "attribution": {
            "direction": "bullish",
            "drivers": ["外盘传导"],
            "affected_sectors": ["半导体"],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                _mock_llm_extract("bullish", ["外盘传导"], ["半导体"]),
                _mock_driver_judge(1, 1),
            ]
        )
        score_full = await evaluate_attribution("外盘传导，半导体领涨", gt_full)
    assert score_full.total >= 0.8

    # 场景二：单维 truth（仅 sectors，direction=neutral + drivers=[]）
    gt_single = {
        "attribution": {
            "direction": "neutral",
            "drivers": [],
            "affected_sectors": ["半导体"],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("neutral", [], ["半导体"])
        )
        score_single = await evaluate_attribution("半导体领涨", gt_single)
    assert score_single.total >= 0.8  # 重归一化使单维 truth 达标过易
