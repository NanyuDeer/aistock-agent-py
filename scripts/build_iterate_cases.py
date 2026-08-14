"""build_iterate_cases.py — 迭代切片生成 CLI（二期 case-sourcing，三期 --date 历史回补）。

用法：
  python scripts/build_iterate_cases.py --agent <agent_id> \
      [--window-days N] [--date YYYY-MM-DD] [--force] [--data-dir PATH]

--agent choices 动态取自 iterate/adapters.py 的 iterable_agent_ids()（注册即生效）。
--window-days 仅对 telegraph_keyword_scan 产片源生效；--date 仅对
market_close_snapshot 产片源生效（review 历史回补，Task 2 已支持 params["date"]）。
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
    parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="扫描窗口天数；仅对 telegraph_keyword_scan 产片源生效（review 等无该 provider 的 agent 忽略）",  # noqa: E501
    )
    parser.add_argument(
        "--date",
        default=None,
        help="历史交易日 YYYY-MM-DD（仅对 market_close_snapshot 产片源生效，review 历史回补用）",
    )
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
    # CLI 显式覆盖产片源参数（--window-days 默认 30 即用 adapter 登记值；--date
    # 缺省 None 即走最近交易日）。两参数分别只对对应 provider 生效，合并进一次
    # new_sources 构造循环（对每个 spec 按 provider 名分别注入，避免两次 replace
    # 互相覆盖）；adapter 无对应 provider 时显式告警避免误解（final review I-1）。
    override_window_days = args.window_days != 30
    override_date = args.date is not None
    if override_window_days or override_date:
        if override_window_days and not any(
            spec.provider == "telegraph_keyword_scan" for spec in adapter.case_sources
        ):
            print(
                f"warning: --window-days {args.window_days} 仅对 telegraph_keyword_scan "
                f"产片源生效，adapter {adapter.agent_id} 无该 provider，参数被忽略",
                file=sys.stderr,
            )
        if override_date and not any(
            spec.provider == "market_close_snapshot" for spec in adapter.case_sources
        ):
            print(
                f"warning: --date {args.date} 仅对 market_close_snapshot 产片源生效，"
                f"adapter {adapter.agent_id} 无该 provider，参数被忽略",
                file=sys.stderr,
            )
        new_sources = tuple(
            CaseSourceSpec(
                spec.provider,
                {
                    **spec.params,
                    **(
                        {"window_days": args.window_days}
                        if override_window_days and spec.provider == "telegraph_keyword_scan"
                        else {}
                    ),
                    **(
                        {"date": args.date}
                        if override_date and spec.provider == "market_close_snapshot"
                        else {}
                    ),
                },
            )
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
