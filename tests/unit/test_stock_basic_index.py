"""股票名称索引单元测试 — 第二阶段：加载 / 最长匹配 / 失败降级。

覆盖：
1. ensure_loaded 从 Node 接口拉取全量股票基础信息并构建索引。
2. 最长匹配优先（"中国银行"命中时不误配"银行"）。
3. 接口失败降级为空索引（返回 []，不影响事件抓取）。
4. 空文本直接返回 []。
"""

import pytest

import aistock_agent.services.stock_basic_index as sbi


@pytest.fixture(autouse=True)
def _reset_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例前重置模块级全局状态（避免用例间污染/误触真实接口）。"""
    monkeypatch.setattr(sbi, "_names_longest_first", ())
    monkeypatch.setattr(sbi, "_loaded", False)
    monkeypatch.setattr(sbi, "_load_task", None)


@pytest.mark.asyncio
async def test_ensure_loaded_builds_index_from_api(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_list(path: str) -> list[dict[str, object]]:
        return [
            {"symbol": "600519", "name": "贵州茅台", "industry": "白酒"},
            {"symbol": "300750", "name": "宁德时代", "industry": "电池"},
        ]

    monkeypatch.setattr(sbi.node_api, "get_list", fake_get_list)

    await sbi.ensure_loaded()

    assert sbi.match_stock_names("贵州茅台回购股份") == ["贵州茅台"]
    assert sbi.match_stock_names("宁德时代获得海外订单") == ["宁德时代"]


def test_longest_match_prefers_longer_name() -> None:
    # "中国银行"命中时，被其包含的短名"银行"不应出现在结果里
    sbi.inject_names(["中国银行", "银行"])
    assert sbi.match_stock_names("中国银行分红") == ["中国银行"]


@pytest.mark.asyncio
async def test_api_failure_degrades_to_empty_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_list(path: str) -> list[dict[str, object]]:
        raise RuntimeError("network down")

    monkeypatch.setattr(sbi.node_api, "get_list", fake_get_list)

    await sbi.ensure_loaded()

    # 接口失败 → 索引为空 → 返回 []，不抛异常（不影响事件抓取）
    assert sbi.match_stock_names("贵州茅台回购股份") == []


def test_empty_text_returns_empty() -> None:
    sbi.inject_names(["贵州茅台", "宁德时代"])
    assert sbi.match_stock_names("") == []
