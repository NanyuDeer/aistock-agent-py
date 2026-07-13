"""通用字典工具 — 类型安全的嵌套字典访问

从 ``agents/workers/iterate.py`` 迁出的通用工具函数，
供 iterate_analyzer / snapshot_builder 等模块复用。
"""

from typing import cast


def get_nested_dict(data: dict[str, object], key: str) -> dict[str, object]:
    """从 JSON-parsed dict 中安全提取嵌套 dict。

    ``dict[str, object]`` 的 ``.get()`` 返回 ``object``，无法直接调用 ``.get()``。
    此函数做 isinstance 收窄 + cast，保证 mypy strict 通过。

    Args:
        data: 父级字典。
        key: 要提取的嵌套字典的键。

    Returns:
        嵌套的字典，若不存在或类型不匹配则返回空字典。
    """
    value = data.get(key)
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def get_num(data: dict[str, object], key: str, default: float) -> float:
    """从 dict 中安全提取数值（排除 bool）。

    JSON-parsed 值类型为 ``object``，不能直接做 ``<`` 比较。
    此函数做 isinstance 收窄到 ``int | float``（排除 bool），返回 ``float``。

    Args:
        data: 目标字典。
        key: 要提取的键。
        default: 提取失败时的默认值。

    Returns:
        提取到的数值（float），失败返回 default。
    """
    value = data.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    return default
