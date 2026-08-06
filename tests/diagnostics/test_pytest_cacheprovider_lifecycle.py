"""pytest cacheprovider 生命周期诊断测试。

背景：
    工作树中曾出现 ``addopts = "-p no:cacheprovider"`` 的未提交绕过，据称是为了规避
    "断言结束但进程不退出" 的挂起问题。本诊断用于：

    1. 稳定验证 cacheprovider 启用时 pytest 能自然退出（回归守护）。
    2. 留下可复现的最小诊断路径，便于未来挂起复现时快速定位。

调查结论（2026-07-24）：
    在基线 7c3a3e9 与当前工作树多次运行（含冷启动 .pytest_cache 删除、langsmith
    插件禁用、连续 3 次、全量 tests/ 等场景），均无法复现挂起；进程在数秒内自然
    退出。未找到根因，未猜测性改配置；仅保留本诊断作为回归守护。

    已排除项：
    - trace_loader 单元测试（tests/unit/test_trace_lookup.py，6 项）→ 自然退出
    - 全量 tests/unit/（531 项）→ 自然退出
    - 全量 tests/integration/（222 项）→ 自然退出
    - 全量 tests/e2e/（56 项）→ 自然退出
    - 全量 tests/ 聚合（826 项）→ 自然退出
    - 基线 7c3a3e9 worktree 全量 → 自然退出
    - 冷启动（删除 .pytest_cache）→ 自然退出
    - 禁用 langsmith 插件 → 自然退出
    - 连续 3 次运行 → 均自然退出

    阻塞点：
    - 挂起无法在当前环境复现；若未来在 CI 或其他环境复现，请收集：
      ``py -X faulthandler -m pytest ...`` 输出、挂起时的线程 dump
     （``py -m psutil`` 或 Process Explorer）、.pytest_cache 状态。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VENV_PYTHON = _PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
_PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable

# 挂起判定阈值：正常退出耗时约 1.5~6s，给 60s 余量已足够区分"慢"与"挂起"。
_TIMEOUT_SECONDS = 60


def _run_pytest_with_cacheprovider(
    test_target: str, child_cache_dir: Path
) -> tuple[int, float, str]:
    """以子进程运行 pytest，强制启用 cacheprovider，返回 (exit_code, elapsed, tail)。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_PROJECT_ROOT / "src")

    cmd = [
        _PYTHON,
        "-m",
        "pytest",
        test_target,
        # 清空 pyproject.toml 中可能存在的 addopts（含 -p no:cacheprovider 绕过），
        # 显式启用 cacheprovider，确保本次运行真正使用缓存插件。
        "-o",
        "addopts=",
        "-p",
        "cacheprovider",
        "-o",
        f"cache_dir={child_cache_dir.as_posix()}",
        "-q",
    ]

    import time

    start = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(_PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    elapsed = time.monotonic() - start
    tail = "\n".join((proc.stdout or "").splitlines()[-6:])
    return proc.returncode, elapsed, tail


def test_cacheprovider_probe_passes_explicit_child_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子进程必须使用调用方提供的独立 cache_dir。"""
    child_cache_dir = tmp_path / "child-pytest-cache"
    expected_cache_option = f"cache_dir={child_cache_dir.as_posix()}"

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert ["-o", expected_cache_option] in [
            cmd[index : index + 2] for index in range(len(cmd) - 1)
        ]
        child_cache_dir.mkdir()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    try:
        _run_pytest_with_cacheprovider("tests/unit/test_trace_lookup.py", child_cache_dir)
    except TypeError as error:
        pytest.fail(f"诊断 helper 必须接收显式 child_cache_dir：{error}")

    assert child_cache_dir.is_dir()


@pytest.mark.timeout(_TIMEOUT_SECONDS + 10) if os.environ.get("PYTEST_TIMEOUT") else pytest.mark.skipif(False, reason="no pytest-timeout")
def test_trace_loader_exits_naturally_with_cacheprovider(tmp_path: Path) -> None:
    """cacheprovider 启用时 trace_loader 测试必须自然退出。

    失败模式：
    - subprocess.TimeoutExpired → 进程挂起（回归捕获目标）
    - returncode not in (0, 1) → pytest 异常崩溃
    """
    try:
        child_cache_dir = tmp_path / "child-pytest-cache"
        exit_code, elapsed, tail = _run_pytest_with_cacheprovider(
            "tests/unit/test_trace_lookup.py", child_cache_dir
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            textwrap.dedent(
                """
                pytest with cacheprovider 挂起（>{seconds}s 未退出）。
                请收集 faulthandler 输出与线程 dump 进一步定位。
                """.format(seconds=_TIMEOUT_SECONDS)
            ).strip()
        )

    assert exit_code in (0, 1), (
        f"pytest 异常退出，exit_code={exit_code}，预期 0 或 1。\n输出末尾：\n{tail}"
    )
    # 给一个宽松上界，避免慢机误报；真正的挂起会被 subprocess.TimeoutExpired 捕获。
    assert elapsed < _TIMEOUT_SECONDS, (
        f"pytest 耗时 {elapsed:.1f}s 超过预期，可能接近挂起。\n输出末尾：\n{tail}"
    )
    assert child_cache_dir.is_dir(), "子进程未使用传入的独立 cache_dir。"
