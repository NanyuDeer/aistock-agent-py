"""影响时长分流策略（spec 2026-09-03-动态档位 §5.1）：driver 分类 → required/optional 白名单。

纯函数、无 IO、可单测。required 档 LLM 不可裁（防模型偷懒全 short）；optional 档有据才产。
越白名单产档由 prediction_service.apply_horizon_policy 确定性裁剪。
"""
from dataclasses import dataclass

DRIVER_TYPES: tuple[str, ...] = (
    "policy_macro", "trend_fundamental", "sector_rotation", "event_shock", "transient_market",
)


@dataclass(frozen=True)
class HorizonPolicy:
    required: tuple[str, ...]   # 必须产出（含 short）
    optional: tuple[str, ...]   # 有依据才产


def infer_horizon_policy(driver_type: str, target_kind: str) -> HorizonPolicy:
    """spec §5.1 表。target_kind: index|sector|stock。"""
    if driver_type == "policy_macro":
        return HorizonPolicy(required=("short", "mid", "long"), optional=())
    if driver_type == "trend_fundamental":
        # 产业趋势中期确定性高；长期是否成立留 LLM 依证据收窄
        return HorizonPolicy(required=("short", "mid"), optional=("long",))
    if driver_type == "sector_rotation":
        # 风格轮动扩散看中期持续性，无长期逻辑
        return HorizonPolicy(required=("short",), optional=("mid",))
    if driver_type == "event_shock":
        # 业绩/公告成色决定是否升 mid；未知一律仅 short
        return HorizonPolicy(required=("short",), optional=("mid",))
    # transient_market（含未知回落）：单日异动/情绪脉冲，默认不产 mid/long
    return HorizonPolicy(required=("short",), optional=())


def classify_driver(category_label: str | None, target_kind: str) -> str:
    """把上游候选/溯源类别标签归一到 driver_type；未知名回落 transient_market。

    category_label 样例映射（v1，keyword 匹配；可随上游标签体系扩充）：
    政策/产业政策/宏观/利率/地缘 → policy_macro；
    景气拐点/趋势/基本面/产业趋势/渗透率 → trend_fundamental；
    轮动/扩散/风格/资金主线/主线 → sector_rotation；
    业绩/财报/公告/突发/超预期 → event_shock；
    其余/None → transient_market。
    """
    if not category_label:
        return "transient_market"
    label = str(category_label)
    policy_keys = ("政策", "宏观", "利率", "地缘", "产业政策")
    trend_keys = ("景气", "趋势", "基本面", "渗透率", "拐点")
    rotation_keys = ("轮动", "扩散", "风格", "主线", "资金主线")
    shock_keys = ("业绩", "财报", "公告", "突发", "超预期", "预告")
    if any(k in label for k in policy_keys):
        return "policy_macro"
    if any(k in label for k in trend_keys):
        return "trend_fundamental"
    if any(k in label for k in rotation_keys):
        return "sector_rotation"
    if any(k in label for k in shock_keys):
        return "event_shock"
    return "transient_market"
