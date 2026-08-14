"""build_iterate_cases.py — 迭代切片生成 CLI（二期 case-sourcing）。

用法：
  python scripts/build_iterate_cases.py --agent <agent_id> \
      [--window-days N] [--force] [--data-dir PATH]

--agent choices 动态取自 iterate/adapters.py 的 iterable_agent_ids()（注册即生效）。
产片统一走 build_cases_for_adapter（sourcing → build_case → GT → 校验 → 回滚）。
只在服务器沙盒/生产环境运行（依赖 Node 生产数据源与 LLM key）。
"""

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aistock_agent.config import settings  # noqa: E402
from aistock_agent.iterate.adapters import (  # noqa: E402
    CaseSourceSpec,
    get_adapter,
    iterable_agent_ids,
)
from aistock_agent.iterate.case_builder import get_data_dir  # noqa: E402
from aistock_agent.iterate.case_pipeline import build_cases_for_adapter  # noqa: E402
from aistock_agent.services.http_client import HttpClientPool  # noqa: E402
from aistock_agent.services.redis_pool import RedisPool  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    """CLI 参数解析器（--agent choices 动态取自 adapter 注册表）。"""
    parser = argparse.ArgumentParser(description="迭代切片生成")
    parser.add_argument("--agent", required=True, choices=iterable_agent_ids())
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--force", action="store_true", help="跳过一致性校验强制落盘")
    parser.add_argument("--data-dir", type=Path, default=None)
    return parser


async def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)

    # CLI 独立运行时初始化连接池（FastAPI 服务在 lifespan 初始化，脚本没有
    # lifespan；node_api 依赖 HttpClientPool，晨报缓存/GT 生成依赖 RedisPool）。
    # 修复：2026-08-13 服务器手动产片报 "HttpClientPool not initialized"。
    await RedisPool.init(settings.redis_url, max_connections=settings.redis_max_connections)
    await HttpClientPool.init(timeout=settings.http_timeout_seconds)

    data_dir = args.data_dir or get_data_dir()
    adapter = get_adapter(args.agent)
    if args.window_days != 30:
        # CLI 显式覆盖产片源参数（默认 30 即用 adapter 登记值）
        new_sources = tuple(
            CaseSourceSpec(spec.provider, {**spec.params, "window_days": args.window_days})
            if spec.provider == "telegraph_keyword_scan"
            else spec
            for spec in adapter.case_sources
        )
        adapter = replace(adapter, case_sources=new_sources)
    try:
        result = await build_cases_for_adapter(adapter, data_dir=data_dir, force=args.force)
        generated, rejected = result["generated"], result["rejected"]
        print(f"{adapter.agent_id} case 生成：{generated} 个，拒绝 {rejected} 个")
        for r in cast("list[str]", result["reasons"]):
            print(f"  - {r}")
        return 0
    finally:
        await HttpClientPool.close()
        await RedisPool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
