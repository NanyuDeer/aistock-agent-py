"""板块 Target 构造（Spec D · Target 数据卫生 §2.1）。

板块画像 key 一律用稳定 internal_id（= resolved.ts_code），不按 name，防板块改名断画像。
"""

from aistock_agent.schemas.target import Target  # 真实定义：schemas/target.py
from aistock_agent.services.prediction_targets import _SECTOR_MARKERS


def sector_target_strategy(sector_name: str) -> str:
    """确定性分类：信号词命中 → sector，否则 unknown（供校验，不走 LLM）。"""
    return "sector" if any(m in sector_name for m in _SECTOR_MARKERS) else "unknown"


def sector_target_from_resolved(sector_name: str, resolved: dict[str, str]) -> Target:
    """从 resolve_sector_target 结果构造可靠 Target。

    resolved 必须含 ts_code（稳定标识）；缺 ts_code 视为解析失败（抛 ValueError，
    属于「调用方拿未解析结果硬构造」的编程错误，用 fail-fast 提示而非静默降级）。
    """
    ts_code = resolved.get("ts_code")
    if not ts_code:
        raise ValueError(f"sector_target_from_resolved: missing ts_code for {sector_name}")
    display_name = resolved.get("name") or sector_name
    return Target(kind="sector", internal_id=ts_code, code=ts_code, name=display_name)
