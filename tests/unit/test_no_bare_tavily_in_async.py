"""契约：async 函数体内的 TavilyService.search 必须被 asyncio.to_thread 包裹。

辩论 R1 裁决：failover 链是同步阻塞，async 函数内裸调用会在事件循环上阻塞
整条链。用父指针表沿调用往上找最近的 to_thread 包裹，找不到即违规。
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "aistock_agent"

# 显式纯同步模块白名单（工具/服务层本身，阻塞由调用方 to_thread）
SYNC_WHITELIST = {
    "services/tavily.py",
    "services/search_providers.py",
    "services/key_pool.py",
    "tools/search_tools.py",
    "iterate/replay_layer.py",
}


def _build_parent_map(root: ast.AST) -> dict[ast.AST, ast.AST]:
    pm: dict[ast.AST, ast.AST] = {root: None}

    class V(ast.NodeVisitor):
        def generic_visit(self, node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                pm[child] = node
                self.generic_visit(child)
            ast.NodeVisitor.generic_visit(self, node)

    V().generic_visit(root)
    return pm


def _is_to_thread_wrapped(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread"


def test_no_bare_tavily_search_in_async_functions():
    violations: list[str] = []
    for py in SRC.rglob("*.py"):
        rel = py.relative_to(SRC).as_posix().replace("\\", "/")
        if rel in SYNC_WHITELIST:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        pm = _build_parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.AsyncFor | ast.AsyncWith):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                is_tavily = _is_tavily_search(fn)
                if not is_tavily:
                    continue
                wrapped = False
                cur = pm.get(sub)
                while cur is not None:
                    if isinstance(cur, ast.Call) and _is_to_thread_wrapped(cur):
                        wrapped = True
                        break
                    cur = pm.get(cur)
                if not wrapped:
                    violations.append(f"{rel}:{sub.lineno}")
    assert not violations, f"async 内裸同步 Tavily 调用: {violations}"


def _is_tavily_search(fn: ast.expr) -> bool:
    """识别 TavilyService.search / TavilyClient.search，含实例形态 TavilyService().search。

    评审 N 修订：event_scrape_sources.collect_tavily 用 TavilyService().search（实例调用，
    fn.value 是 Call 而非 Name），仅认 Name 形态会漏检。
    """
    if not (isinstance(fn, ast.Attribute) and fn.attr == "search"):
        return False
    v = fn.value
    if isinstance(v, ast.Name) and v.id in {"TavilyService", "TavilyClient"}:
        return True
    if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "TavilyService":
        return True
    return False
