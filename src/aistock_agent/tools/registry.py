"""工具注册中心 — 按 category 集中管理工具集

agent 只需声明 category，即可获取完整工具列表，
不再手动 import + 拼接。

三种使用方式：
    # 方式1：默认导入全部
    from aistock_agent.tools.registry import get_tools
    tools = get_tools()

    # 方式2：按 category 命名控制
    tools = get_tools("morning")

    # 方式3：直接 import 具体工具名
    from aistock_agent.tools.stock_tools import get_quote

自动注册机制：
    每个 tool 模块在底部调用 ``register("category", tool)`` 自注册。
    ``tools/__init__.py`` 导入所有 tool 模块触发注册。
    新增工具只需在定义它的 tool 文件底部加一行 ``register()``，
    **不需要编辑本文件**，避免多人合并冲突。

前端 /skills 接口暴露：
    ``register()`` 默认 ``expose=True``，工具会出现在 ``GET /api/agent/skills``。
    内部工具可设 ``expose=False``，只给 agent 用，不暴露给前端。
"""

from langchain_core.tools import BaseTool

# 运行时注册表（由各 tool 模块通过 register() 填充）
_REGISTRY: dict[str, list[BaseTool]] = {}

# 暴露给前端 /skills 接口的工具（按注册顺序，自动去重）
_EXPOSED_SKILLS: list[BaseTool] = []
_EXPOSED_NAMES: set[str] = set()

# 预声明空 category（迭代 agent 无工具）
_EMPTY_CATEGORIES: set[str] = {"iterate"}


def register(category: str, tool: BaseTool, *, expose: bool = True) -> None:
    """将工具注册到指定 category

    每个 tool 模块在底部调用此函数自注册。
    同一工具可注册到多个 category（如 get_quote 同时属于 stock 和 event）。
    重复注册同一工具到同一 category 会被自动忽略（去重）。

    Args:
        category: 工具分类名（如 "morning"、"stock"、"event"）
        tool: 已装饰的 ``@tool`` 工具对象
        expose: 是否暴露给 ``GET /api/agent/skills`` 接口（默认 True）
    """
    if category not in _REGISTRY:
        _REGISTRY[category] = []
    # 去重：同一对象不重复追加
    if tool not in _REGISTRY[category]:
        _REGISTRY[category].append(tool)

    # 暴露给 /skills 接口
    if expose and tool.name not in _EXPOSED_NAMES:
        _EXPOSED_NAMES.add(tool.name)
        _EXPOSED_SKILLS.append(tool)


def get_exposed_skills() -> list[BaseTool]:
    """获取暴露给前端 /skills 接口的工具列表

    Returns:
        按注册顺序排列的去重工具列表
    """
    return _EXPOSED_SKILLS.copy()


def get_all_tools() -> list[BaseTool]:
    """获取全部工具（去重）

    Returns:
        去重后的全部工具列表，顺序按 _REGISTRY 遍历顺序
    """
    seen: set[int] = set()
    result: list[BaseTool] = []
    for tools in _REGISTRY.values():
        for tool in tools:
            if id(tool) not in seen:
                seen.add(id(tool))
                result.append(tool)
    return result


def get_tools(category: str | None = None) -> list[BaseTool]:
    """获取工具集

    Args:
        category: 工具分类名（如 "morning"、"stock"、"event"）。
                  不传或传 None → 返回全部工具（去重）。
                  传具体名称 → 返回该分类的工具列表。

    Returns:
        该分类的工具列表，未知 category 返回空列表
    """
    if category is None:
        return get_all_tools()
    return _REGISTRY.get(category, [])


# 预填充空 category
for _cat in _EMPTY_CATEGORIES:
    _REGISTRY[_cat] = []
