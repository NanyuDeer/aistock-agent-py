"""pytest 配置 — 共享 fixtures"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _ensure_tmp_repo_git(tmp_path: Path) -> None:
    """C-3（2026-08-14）：迭代集成测试以 tmp_path 作为 repo_root 时须视为
    git 仓库（_check_repo_environment 判定 has_git），否则变体轮被跳过。"""
    (tmp_path / ".git").mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def _reset_sse_appstatus():
    """每个测试前重置 sse-starlette 的类级 AppStatus。

    AppStatus.should_exit_event 是类级单例，首个 SSE 响应会创建绑定到当前事件循环的
    anyio.Event；后续测试在新事件循环上复用会触发 "bound to a different event loop"。
    """
    from sse_starlette.sse import AppStatus
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    yield


@pytest.fixture
def mock_node_api():
    """mock NodeApiClient.get，返回预设数据"""
    with patch("aistock_agent.services.data_client.NodeApiClient") as mock_cls:
        instance = mock_cls.return_value
        instance.get = AsyncMock()
        yield instance.get


@pytest.fixture
def mock_yfinance():
    """mock yfinance 数据"""
    with patch("aistock_agent.tools.market_tools.yf") as mock_yf:
        yield mock_yf


@pytest.fixture
def mock_tavily():
    """mock TavilyClient。

    patch services/tavily.py 模块级 ``from tavily import TavilyClient`` 绑定的
    ``TavilyClient`` 名称。tavily_finance_search 已迁移到 tools/search_tools.py，
    实际调用委托给 services.tavily.TavilyService.search()，后者在模块级引用
    TavilyClient，因此必须 patch ``aistock_agent.services.tavily.TavilyClient``
    而非源模块 ``tavily.TavilyClient``。
    """
    with patch("aistock_agent.services.tavily.TavilyClient") as mock_cls:
        yield mock_cls


@pytest.fixture
def mock_redis():
    """mock Redis client（通过 RedisPool 注入到 services.cache）。

    morning_agent 的 _get/_set_cached_briefing 委托给 services.cache，
    services.cache 通过 RedisPool.get_client() 获取客户端。
    此 fixture patch services.cache.RedisPool，注入 mock_client。
    """
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=None)
        mock_client.setex = AsyncMock()
        mock_client.aclose = AsyncMock()
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        yield mock_client


@pytest.fixture
def iterate_data_dir(tmp_path: Path) -> Path:
    """iterate 数据目录指向临时目录，并把 fixtures/iterate 下固定数据分发到对应子目录。

    分发规则：含 case_id 字段的切片 → data/cases/{agent_id}/{case_id}.json
    （切片按 agent 归档，并以 case_id 命名落盘，保证 case_path/list_cases 可定位）；
    含 gt_id 字段的标准答案 → data/ground_truths/（标准答案）。reporter/ground_truth 的
    list_pending_review 只读 ground_truths/，不读 cases/，必须正确分发。
    """
    import json
    import shutil

    from aistock_agent.config import settings

    original = settings.iterate_data_dir
    settings.iterate_data_dir = str(tmp_path)
    fixture_root = Path(__file__).parent / "fixtures" / "iterate"
    if fixture_root.exists():
        for p in fixture_root.glob("*.json"):
            payload = json.loads(p.read_text(encoding="utf-8"))
            # 标准答案按 gt_id 字段识别（gt 文件也含 case_id，不能按 case_id 分流）
            if "gt_id" in payload:
                dest = tmp_path / "ground_truths"
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest / p.name)
            elif "case_id" in payload:
                agent = payload.get("agent_id", "review")
                dest = tmp_path / "cases" / str(agent)
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest / f"{payload['case_id']}.json")
    yield tmp_path
    settings.iterate_data_dir = original
