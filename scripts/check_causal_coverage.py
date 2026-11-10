"""机制图覆盖度校验 —— 用真实案例反推「边够不够」。

M0 的验收标准不是「画了多少条边」，而是「能不能串通真实案例的传导路径」。
本脚本把 11-04 / 11-05 的已知路径编码成断言，改边后重跑即可。

用法：
    .venv/Scripts/python.exe -m scripts.check_causal_coverage
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aistock_agent.services.causal_graph import load_market_graph  # noqa: E402

# (起点, 终点, 这条路径对应的现实传导)
CASES: dict[str, dict[str, object]] = {
    "2026-11-04 政策日": {
        "context": "央行下调回购增持再贷款利率 0.25pp + 长鑫科技 08:45 提议回购；"
        "当日创业板 +6.40%、科创50 近 +10%、半导体设备 ETF +13%",
        "required_paths": [
            ("POLICY_RATE", "SEC_SEMI", "降息降低回购成本 → 龙头回购 → 半导体走强"),
            ("EARNINGS_EXPECT", "SEC_SEMI", "三季报兑现 → 盈利预期上修 → 半导体走强"),
            ("SEC_MEMORY", "SEC_SEMI", "存储涨价 → 半导体景气"),
        ],
    },
    "2026-11-05 结构分化": {
        "context": "韩国 KOSPI 重挫 6%、SK海力士跌超 10%、中期选举后美债收益率跌至八周低点；"
        "当日沪深300 +0.29% vs 国证2000 +2.40%，炸板 48 家",
        "required_paths": [
            ("US10Y", "IDX_GEM", "美债收益率下行 → 成长股估值修复"),
            ("IDX_SOX", "SEC_SEMI", "费城半导体 → A 股半导体"),
            ("SEC_SEMI_KR", "SEC_SEMI", "韩国半导体重挫 → A 股半导体承压（被国产替代对冲）"),
            ("THEME_DOMESTIC_SUB", "SEC_SEMI", "国产替代叙事 → 对冲外部利空"),
        ],
    },
}


def main() -> int:
    graph = load_market_graph()
    total = 0
    missing: list[tuple[str, str, str, str]] = []

    print(f"机制图：{len(graph.nodes)} 节点 / {len(graph.edges)} 条边\n")

    for case_name, case in CASES.items():
        print(f"【{case_name}】")
        print(f"  {case['context']}")
        for source, target, meaning in case["required_paths"]:  # type: ignore[misc]
            total += 1
            ok = graph.path_exists(source, target)
            mark = "OK  " if ok else "缺失"
            print(f"  [{mark}] {source} → {target}   {meaning}")
            if not ok:
                missing.append((case_name, source, target, meaning))
        print()

    if missing:
        print(f"覆盖度：{total - len(missing)}/{total} 通过，缺 {len(missing)} 条路径")
        print("\n需要补的边：")
        for _, source, target, meaning in missing:
            print(f"  - {source} → … → {target}（{meaning}）")
        return 1

    print(f"覆盖度：{total}/{total} 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
