"""预测能力 Pydantic 契约 — 影响持续性推演输出。

字段名以本文件为准，后续任务不得另起近义字段。
本模块只定义数据结构，不包含任何业务逻辑或 LLM 调用。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aistock_agent.schemas.target import Target

# 时间尺度分档（对齐 alert cycle）：短 1-5 交易日 / 中 1-4 周 / 长 1-6 月
PredictionHorizonType = Literal["short", "mid", "long"]
# 当前演化阶段
PredictionPhase = Literal["building", "peaking", "decaying", "returning"]
# 条件化预判方向（§3.1，anchor 自带 direction，不依赖 horizons[].direction）
PredictionDirection = Literal["bullish", "bearish", "neutral"]
# 验证标的（§3.1 anchor.metric）
PredictionMetric = Literal["close", "high", "low", "volume", "index_close"]


class PredictionHorizon(BaseModel):
    """单档位影响持续性预测。"""

    model_config = ConfigDict(extra="forbid")

    horizon: PredictionHorizonType
    label: str = Field(
        default="",
        description="该档基准走势短语（4~6 字，如 恐慌出清为主/震荡磨底/震荡走强；"
        "洞见卡基准行“基准 · {label}”展示；旧记录为空则前端回退不渲染）。",
    )
    remaining_estimate: str  # 还能持续多久（定性估算，如 "2-4 周"）
    phase: PredictionPhase  # 当前演化阶段
    direction: Literal["bullish", "bearish", "neutral"]  # 该档位影响方向
    target: str  # 验证对象（"上证指数"/"半导体板块"）
    metric_projection: str  # 可量化预期（到期 hit/miss 对照依据）
    confidence: Literal["high", "medium", "low"]
    confidence_source: Literal["llm", "deterministic"] | None = None  # LLM 原值 or 确定性钳制


class PredictionRisk(BaseModel):
    """风险因素 — 什么会导致预测失效。"""

    model_config = ConfigDict(extra="forbid")

    factor: str
    invalidation: str
    # 失效条件"读数触发式"复核触发器（A1）— 可选，仅读数触发类风险填充
    indicator: Literal["ma20", "ma60"] | None = None
    direction: Literal["above", "below"] | None = None
    window: int | None = None
    measure: Literal["close"] | None = "close"
    snapshot_value: float | None = None  # 预测日 MA 快照（审计用）
    triggered: bool = False


class EvolutionStep(BaseModel):
    """演化路径单步 — 按档位切分的时间轴节点（供前端直接渲染时间轴）。"""

    model_config = ConfigDict(extra="forbid")

    label: str  # 档位标签（如 "短线"/"中线"/"长线"）
    text: str  # 该档位演化描述


class PredictionAnchor(BaseModel):
    """条件化预判的验证锚点（§3.1）— 到期比对的确定性标准。

    - ``horizon`` 对齐 PredictionHorizonType + HORIZON_TRADING_DAY_OFFSETS（5/20/120 交易日）
    - ``direction`` 该条件的**情景方向**（验证 scenario 命中主判依据），**自挂**不依赖
      ``horizons[].direction``：同档不同情景方向可能相反，且个股轻量预判不自建完整三档。
      必填；LLM 缺失时由归一化层确定性提取兜底（见 §4.1），不做 parse_failed。
    """

    model_config = ConfigDict(extra="forbid")

    horizon: PredictionHorizonType  # 验证周期 short / mid / long
    threshold: str  # 验证阈值（涨跌幅 %，如 "+5%"/"-3%"），到期比对用
    metric: PredictionMetric = "close"  # 验证标的，缺省 close；大盘用 index_close
    direction: PredictionDirection = "neutral"  # 情景方向；缺省 neutral，LLM 不产时归一化层兜底


class PredictionCondition(BaseModel):
    """条件化预判的"条件 → 情景"对（§3.1）。

    硬约束：condition 必须含可量化条件（放量/缩量、价位、均线、情绪温度等）；
    至少 1 条 condition 含成交量维度（呼应"分放量/缩量场景"）。
    """

    model_config = ConfigDict(extra="forbid")

    condition: str  # 触发条件（完整可量化事实描述，长句保留，供详细报告原文展示）
    label: str = Field(
        default="",
        description="路径短语名，固定两段式“{市场状态/触发条件，≈4 字，≤6} · {触发后走势，≈4 字，≤6}”，"
        "如 恐慌出清 · 下跌中继 / 缩量企稳 · 平台修复（洞见卡路径首行加粗展示；"
        "旧记录为空则前端回退用 condition 主干）。",
    )
    scenario: str  # 条件满足后的走势预判（尽量含幅度/目标位）
    anchor: PredictionAnchor  # 验证锚点（horizon + threshold + metric + direction）
    keywords: list[str] = Field(
        default_factory=list,
        description="简洁展示用关键词（1~2 个，单条 ≤10 字、硬上限 15 字，如 两市放量≥2.2万亿）；"
        "condition 本体不受影响仍为完整句。旧记录为空数组。",
    )


class OmittedHorizon(BaseModel):
    """被省略（未产出）档位的显式留痕（spec §5.3）：供产品解释与画像诊断。"""

    model_config = ConfigDict(extra="forbid")

    horizon: PredictionHorizonType
    reason: str  # LLM 产出；归一化层校验非空、非空泛

    @model_validator(mode="after")
    def _reason_not_blank(self) -> "OmittedHorizon":
        # spec §5.4：归一化层校验非空——空白/纯空格 reason 无解释价值，拒绝（空泛词
        # 由提示词约束 + LLM 侧控制，此处只挡结构空值）。
        if not self.reason.strip():
            raise ValueError("omitted reason must not be blank")
        return self


class PredictionResult(BaseModel):
    """影响持续性推演完整输出。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["3.0"]
    prediction_status: Literal["confirmed", "hypothesis", "insufficient"]
    horizons: list[PredictionHorizon] = Field(...)  # 多档位并存
    omitted_horizons: list[OmittedHorizon] = Field(default_factory=list)  # 缺档留痕（spec §5.3）
    # 条件化预判（§3.1）；旧 2.0 记录为空
    conditions: list[PredictionCondition] = Field(default_factory=list)
    target: Target | None = None  # 关联统一 Target 维度（§3.3/全局 §2）；旧记录为 None
    evolution_narrative: str  # 后续演化路径叙事（强化→衰减→回归），兼容旧展示
    # 结构化演化步骤（前端时间轴）；旧记录可能为空
    evolution_steps: list[EvolutionStep] = Field(default_factory=list)
    risks: list[PredictionRisk]
    evidence_ids: list[str]  # 只引用溯源证据，禁止编造外部事实
    attribution_summary: str | None = None  # 一句话预测结论（随报告展示）
    evidence_corroboration: dict[str, object] | None = None  # A2 独立源冲突检测结果

    @model_validator(mode="after")
    def _check_omitted_not_overlap(self) -> "PredictionResult":
        produced = {h.horizon for h in self.horizons}
        for o in self.omitted_horizons:
            if o.horizon in produced:
                raise ValueError(f"horizon {o.horizon} 同时出现在 horizons 与 omitted_horizons")
        return self

    @model_validator(mode="after")
    def _require_horizons(self) -> "PredictionResult":
        if not self.horizons:
            raise ValueError("horizons must not be empty")
        return self
