"""快照数据模型 — snapshot / rolling_stats / manifest 的 TypedDict 定义

这些类型用于 snapshot_builder 的类型标注和 JSON 结构文档化。
运行时 JSON 读写不依赖这些类型（直接操作 dict），但代码层用这些类型做类型安全。
"""

from typing import TypedDict


class SectorDeviation(TypedDict):
    """单个板块的方向-强度偏差"""
    morning_score: int
    review_score: int
    deviation: int


class AttributionComparison(TypedDict):
    """单个板块的归因一致性"""
    similarity: int
    morning_cause: str
    review_cause: str


class Dimension1Coverage(TypedDict):
    """维度一：关注点重叠度"""
    overlap_hits: list[str]
    missing_in_morning: list[str]
    over_focused: list[str]
    hit_rate: float
    new_coverage_rate: float


class Dimension2Direction(TypedDict):
    """维度二：方向-强度偏差"""
    sectors: dict[str, SectorDeviation]
    direction_accuracy: float
    mean_deviation: float
    abs_mean_deviation: float


class Dimension3Attribution(TypedDict):
    """维度三：归因一致性"""
    sectors: dict[str, AttributionComparison]
    attribution_match_rate: float


class Dimension4Sentiment(TypedDict):
    """维度四：情绪基调"""
    morning_sentiment: float
    review_sentiment: float
    bias: float


class SnapshotData(TypedDict):
    """完整快照结构（snapshot_T.json）"""
    date: str
    morning_file: str
    review_file: str
    dimension_1_coverage: Dimension1Coverage
    dimension_2_direction: Dimension2Direction
    dimension_3_attribution: Dimension3Attribution
    dimension_4_sentiment: Dimension4Sentiment


class MARollingStats(TypedDict):
    """单个 MA 窗口的滚动指标"""
    hit_rate: float
    direction_accuracy: float
    mean_deviation: float
    attribution_match_rate: float
    sentiment_bias: float


class RollingStatsData(TypedDict):
    """rolling_stats.json 结构"""
    updated_at: str
    ma5: MARollingStats
    ma10: MARollingStats
    ma20: MARollingStats


class ManifestRecord(TypedDict):
    """manifest.json 中单条记录"""
    date: str
    snapshot_file: str
    hit_rate: float
    direction_accuracy: float
    mean_deviation: float
    attribution_match_rate: float
    sentiment_bias: float


class ManifestData(TypedDict):
    """manifest.json 结构"""
    records: list[ManifestRecord]
