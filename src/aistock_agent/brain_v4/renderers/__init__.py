"""简报出口投影层（Task 9 Step 4）。

只把已有 `BriefingResult` 投影为 App / Web / text / audio 四种展示形态：
- headline、direction、relation、risk level、evidence IDs 与 confidence 保持不变；
- audio 可以缩短措辞，但不得新增 claim 或改变 confidence；
- 本层不写研究逻辑、不调用工具、不读取系统时间。
"""

from .briefing import Channel, RenderedBriefing, render

__all__ = ["Channel", "RenderedBriefing", "render"]
