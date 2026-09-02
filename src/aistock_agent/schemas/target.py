"""四环 × 三粒度统一 Target 维度 —— 纯数据模型。

本模块只定义数据结构，不包含业务逻辑（与 schemas 包其他模块一致）。
Target 是四环（溯源/预判/验证/迭代）全部以 ``internal_id`` 通行的唯一标识维度；
TargetProfile 收拢"这类怎么分析"的粒度差异（数据源/prompt/快照构造/默认周期/阈值），
消除散落在四环的 ``if kind == ...`` 分支。

见 ``docs/specs/2026-08-31-四环三粒度复用架构-design.md`` §2。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TargetKind = Literal["index", "sector", "stock"]


class Target(BaseModel):
    """四环统一分析对象——只承载"分析谁"（标识），"这类怎么分析"在 TargetProfile。

    数据卫生（§2.1）：
    - ``internal_id`` 稳定标识：画像 key / 缓存 key / 日志 一律用它，不用 name
    - ``code`` 带交易所后缀的 ts_code（``000001.SH`` / ``000001.SZ``），6 位裸码只做展示层
    - ``name`` 可改名（板块合并/改名只更新 name），不影响 internal_id
    """

    model_config = ConfigDict(extra="forbid")

    kind: TargetKind
    internal_id: str
    code: str | None = None
    name: str


class TargetProfile(BaseModel):
    """四环粒度差异注册表——做"这类怎么分析"的数据化配置，不是框架。

    kind 级配置：三粒度差异集中一处，四环统一 ``get_profile(target)``；
    加第五粒度（如 ETF/行业指数）只追加一条记录（YAGNI，见 §8.7）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: TargetKind
    # 溯源
    trace_prompt_template: str            # 归因 prompt 分粒度
    evidence_sources: list[str]           # 证据数据源列表
    # 预判
    snapshot_builder: str                 # 输入快照构造器名
    default_horizons: list[str] = Field(
        default_factory=lambda: ["short", "mid", "long"]
    )
    # 验证
    kline_fetcher: str                    # 数据获取函数名（get_index_kline/...）
    benchmark: str | None = None          # 超额收益基准（个股用；index/sector 为 None）
    # 迭代
    case_sourcer: str                     # 产片源函数名
    # 触发迭代的命中率阈值：float 全局默认；或 dict 按 horizon×场景 分层，
    # 形如 {"<horizon>": {"<scenario>": float, "_default": float}, "_default": float}，
    # 统一取值走 get_iterate_threshold（services/target_profile.py，见 §2.3 注释）。
    score_threshold: float | dict[str, object] = 0.5
