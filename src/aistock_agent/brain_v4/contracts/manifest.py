"""特性/能力 manifest 契约。

每个 feature 或 capability 在自己的目录维护 `manifest.yaml`，不存在中央热点文件。
manifest 是注册表唯一事实源：CI 聚合后生成 `generated/registry.lock`，运行期只读取
通过校验的 Registry，feature 模块不得改动中央 builder 或中央 YAML。

字段口径见 docs/智慧大脑-V4-团队接入与迁移规范.md §4。
"""

from typing import Literal

from pydantic import Field

from . import V4Contract

ManifestKind = Literal["feature", "capability"]
ManifestStatus = Literal["draft", "shadow", "active", "retired"]


class ManifestBudget(V4Contract):
    """单个功能的时间与 token 预算；必须显式给出，禁止默认。"""

    timeout_s: int = Field(gt=0)
    token_budget: int = Field(gt=0)


class ManifestDegradation(V4Contract):
    """缺失父判断或可选能力失败时的降级口径。"""

    missing_parent: str = "insufficient"
    optional_capability_failure: str = "partial"


class LegacyDeletion(V4Contract):
    """旧链 adapter 的调用方与可删除条件，防止影子迁移留下无主代码。"""

    adapter: str
    callers: list[str] = Field(default_factory=list)
    delete_when: str


class FeatureManifest(V4Contract):
    """一个 feature/capability 的完整声明。"""

    id: str
    kind: ManifestKind
    owner: str
    status: ManifestStatus
    profiles: list[str] = Field(default_factory=list)
    contracts: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    cutoff: str
    budgets: ManifestBudget
    degradation: ManifestDegradation = Field(default_factory=ManifestDegradation)
    active_owner: str
    shadow_owner: str | None = None
    legacy_deletion: LegacyDeletion | None = None
