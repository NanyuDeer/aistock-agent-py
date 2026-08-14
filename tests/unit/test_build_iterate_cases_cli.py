"""CLI 动态 choices 与统一分派（二期 case-sourcing）。"""

import inspect

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
