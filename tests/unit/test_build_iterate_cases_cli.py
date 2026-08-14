"""CLI 动态 choices 与统一分派（二期 case-sourcing；三期 --date 历史回补）。"""

import asyncio
import inspect
from unittest.mock import AsyncMock

from aistock_agent.iterate.adapters import iterable_agent_ids


def test_cli_choices_dynamic_from_registry(monkeypatch) -> None:
    import scripts.build_iterate_cases as cli

    parser = cli._build_parser()
    agent_action = next(a for a in parser._actions if a.dest == "agent")
    assert agent_action.choices == iterable_agent_ids()


def test_main_has_no_agent_branch() -> None:
    import scripts.build_iterate_cases as cli

    source = inspect.getsource(cli.main)
    assert "args.agent ==" not in source, "main 不得按 agent_id 分支"


def _run_cli_main(monkeypatch, argv: list[str]) -> tuple[int, object]:
    """以 pool/build 全 patch 方式执行 cli.main，返回 (exit_code, 传给 build 的 adapter)。

    三期 --date 断言需要观察 main 传给 build_cases_for_adapter 的 adapter 是否携带
    注入的 params；真实调用会连 Redis/HTTP 与 LLM，必须整体 patch（AsyncMock 兜底
    pool init/close，--data-dir 指向占位路径避免触碰真实数据目录）。
    """
    import scripts.build_iterate_cases as cli

    captured: dict[str, object] = {}

    async def fake_build(
        adapter: object, *, data_dir: object, force: bool
    ) -> dict[str, object]:
        captured["adapter"] = adapter
        return {"generated": 0, "rejected": 0, "case_ids": [], "reasons": []}

    monkeypatch.setattr(cli.RedisPool, "init", AsyncMock())
    monkeypatch.setattr(cli.HttpClientPool, "init", AsyncMock())
    monkeypatch.setattr(cli.RedisPool, "close", AsyncMock())
    monkeypatch.setattr(cli.HttpClientPool, "close", AsyncMock())
    monkeypatch.setattr(cli, "build_cases_for_adapter", fake_build)

    exit_code = asyncio.run(cli.main([*argv, "--data-dir", "tmp"]))
    return exit_code, captured["adapter"]


def test_cli_has_date_option() -> None:
    """三期：--date 参数存在且注入 review 产片源。"""
    import scripts.build_iterate_cases as cli

    parser = cli._build_parser()
    date_action = next(a for a in parser._actions if a.dest == "date")
    assert date_action is not None
    source = inspect.getsource(cli.main)
    assert "market_close_snapshot" in source  # main 按 provider 名注入 date


def test_cli_date_injects_market_close_snapshot_params(monkeypatch) -> None:
    """三期：--date 注入 review（market_close_snapshot）产片源 params。"""
    from aistock_agent.iterate.adapters import IterableAgentAdapter

    exit_code, adapter = _run_cli_main(
        monkeypatch, ["--agent", "review", "--date", "2026-07-31"]
    )
    assert exit_code == 0
    assert isinstance(adapter, IterableAgentAdapter)
    assert len(adapter.case_sources) == 1
    spec = adapter.case_sources[0]
    assert spec.provider == "market_close_snapshot"
    assert spec.params["date"] == "2026-07-31"


def test_cli_date_warns_stderr_for_agent_without_provider(monkeypatch, capsys) -> None:
    """三期：非目标 agent（event_analyst）传 --date → stderr warning（与 --window-days 同模式）。"""
    from aistock_agent.iterate.adapters import IterableAgentAdapter

    exit_code, adapter = _run_cli_main(
        monkeypatch, ["--agent", "event_analyst", "--date", "2026-07-31"]
    )
    err = capsys.readouterr().err
    assert "--date" in err
    assert "market_close_snapshot" in err
    assert exit_code == 0
    assert isinstance(adapter, IterableAgentAdapter)
    assert all("date" not in spec.params for spec in adapter.case_sources)


def test_cli_both_window_days_and_date_inject_in_single_loop(monkeypatch, capsys) -> None:
    """三期：两参数同时传都生效——合并进一次 new_sources 构造，按 provider 名分别注入，互不覆盖。"""
    from aistock_agent.iterate.adapters import (
        ITERABLE_AGENTS,
        CaseSourceSpec,
        IterableAgentAdapter,
    )

    monkeypatch.setitem(
        ITERABLE_AGENTS,
        "dual_provider",
        IterableAgentAdapter(
            agent_id="dual_provider",
            module_path="aistock_agent.agents.workers.review",
            case_sources=(
                CaseSourceSpec("telegraph_keyword_scan", {"window_days": 30}),
                CaseSourceSpec("market_close_snapshot"),
            ),
        ),
    )
    exit_code, adapter = _run_cli_main(
        monkeypatch,
        ["--agent", "dual_provider", "--window-days", "7", "--date", "2026-07-31"],
    )
    assert exit_code == 0
    assert isinstance(adapter, IterableAgentAdapter)
    params_by_provider = {spec.provider: spec.params for spec in adapter.case_sources}
    assert params_by_provider["telegraph_keyword_scan"]["window_days"] == 7
    assert params_by_provider["market_close_snapshot"]["date"] == "2026-07-31"
    assert "date" not in params_by_provider["telegraph_keyword_scan"]
    assert "window_days" not in params_by_provider["market_close_snapshot"]
    assert capsys.readouterr().err == ""  # 两个 provider 都在 → 无 warning


def test_cli_window_days_still_injects_after_merge(monkeypatch) -> None:
    """回归：合并重构后 --window-days 对 event_analyst 的注入行为不变。"""
    from aistock_agent.iterate.adapters import IterableAgentAdapter

    exit_code, adapter = _run_cli_main(
        monkeypatch, ["--agent", "event_analyst", "--window-days", "7"]
    )
    assert exit_code == 0
    assert isinstance(adapter, IterableAgentAdapter)
    spec = adapter.case_sources[0]
    assert spec.provider == "telegraph_keyword_scan"
    assert spec.params["window_days"] == 7
    assert "date" not in spec.params
