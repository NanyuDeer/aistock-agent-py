"""V4 公共契约包。

统一约束（见 docs/智慧大脑-V4-团队接入与迁移规范.md §5）：
- 全部 `extra="forbid"`，未知字段必须报错；
- 时间边界 `as_of` / `data_cutoff` / `snapshot_id` 由调用方显式传入；
- 任何字段不得默认取系统当前时间。
"""

from pydantic import BaseModel, ConfigDict


class V4Contract(BaseModel):
    """V4 契约基类：拒绝未知字段，避免静默吞掉上游新增字段。"""

    model_config = ConfigDict(extra="forbid")
