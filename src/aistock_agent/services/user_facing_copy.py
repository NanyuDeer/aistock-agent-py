"""用户可见文案的 L2 工程词黑名单（spec §12.1/§12.6）。

只装工程实现词，**不装炒股域词汇**（主线 / 超额 / 均线 / 涨停 / 放量 …）——
用户是股民不是程序员，禁掉业务词等于禁掉他们的语言。
"""
from __future__ import annotations

# 顺序即报错顺序，便于测试稳定断言
BANNED_ENGINEERING = (
    "base",
    "index",
    "blocking",
    "certainty",
    "pct",
    "ma5",
    "ma10",
    "slot",
    "target_date",
    "升档",
    "闸门",
    "档位",
    "阻塞",
)


def find_engineering_terms(text: str) -> list[str]:
    """返回命中的工程词（按黑名单顺序、去重）。"""
    low = (text or "").lower()
    return [w for w in BANNED_ENGINEERING if w in low]


def check_copy(text: str) -> None:
    """用户可见文案守卫：命中任一工程词即抛 ValueError。"""
    hits = find_engineering_terms(text)
    if hits:
        raise ValueError(f"用户可见文案含工程词 {hits}：{text!r}")
