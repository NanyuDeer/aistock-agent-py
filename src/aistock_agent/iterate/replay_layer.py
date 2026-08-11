"""数据回放层 —— 让待迭代 agent 在子进程内只能看到 T 时刻及之前的信息。

机制：环境变量 REPLAY_CASE_ID / REPLAY_AGENT 开启回放模式；apply_replay_patches
按 adapter.data_deps 逻辑名替换数据获取函数（monkeypatch），并把所有写副作用
（DB/Redis/归档）替换为 no-op。不改待迭代 agent 的 run() 逻辑，进程退出即清理。
"""

import json
import os
from collections.abc import Awaitable, Callable

import structlog

from aistock_agent.iterate.adapters import IterableAgentAdapter
from aistock_agent.iterate.case_builder import load_case

logger = structlog.get_logger()

# 逻辑名 → 可 patch 的模块路径（框架级工具映射，非 agent 特判）。
# 注意：这是"模块属性"patch，只对 agent 代码直接调用的模块级函数生效。
# review/event 的工具经 registry 持有的 BaseTool 调用（原始函数在 import 时被捕获），
# 模块属性 patch 拦截不到，真正的隔离在服务层（见 _SERVICE_ISOLATION_TARGETS）：
# - review 的隔离由 market 绑定 patch（build_market_trace_snapshot）承担；
# - news/global 目标对 review 是惰性的——review.py 从不直接调用
#   get_cls_news / get_global_markets，保留仅为兼容直接调用路径；
# - "search"：tavily_finance_search 直接调用时读切片语料（受限"可搜索"语料）。
_REPLAY_PATCH_TARGETS: dict[str, str] = {
    "news": "aistock_agent.tools.news_tools.get_cls_news",
    "market": "aistock_agent.agents.workers.review.build_market_trace_snapshot",
    "global": "aistock_agent.tools.market_tools.get_global_markets",
    "search": "aistock_agent.tools.search_tools.tavily_finance_search",
}

# 服务层隔离（C1 根因修复）：event 工具经 registry 持有的 BaseTool 调用，
# 模块属性 patch 无法拦截其捕获的原始函数；但所有 event 工具的真实网络调用
# 最终汇入服务层——NodeApiClient.get/get_list/post（data_client.py）与
# TavilyService.search（tavily.py）。按类方法替换（实例调用同样命中，本代码库
# 已用 NodeApiClient.save_analysis_report 验证）。值 = 替换实现 kind：
# - "node_read"：get/get_list 通用回放读——path 含 news/telegraph/search 时返回
#   切片 cls_telegraph 语料（与 news_tools 消费的响应形状一致），其余返回 None（隔离）；
# - "node_noop"：post 写操作 no-op（返回 None，回放只读——None 让调用方走
#   优雅降级路径，避免被误判为"写成功"）；
# - "tavily_search"：TavilyService.search 是同步静态方法（tavily_finance_search
#   直接 `result = TavilyService.search(...)` 无 await），必须同步替换，
#   返回切片语料（受限"可搜索"语料），不发网络请求。
_SERVICE_ISOLATION_TARGETS: dict[str, str] = {
    "aistock_agent.services.data_client.NodeApiClient.get": "node_read",
    "aistock_agent.services.data_client.NodeApiClient.get_list": "node_read",
    "aistock_agent.services.data_client.NodeApiClient.post": "node_noop",
    "aistock_agent.services.tavily.TavilyService.search": "tavily_search",
}

# 写副作用 → no-op（回放只读，禁止污染主数据）。
# 注意：review.py / event.py 用 from-import 绑定这些名字（event.py:29-31
# `from aistock_agent.services.cache import get_cached_event, set_cached_event` /
# `from aistock_agent.services.event_persister import persist_event_report`），
# patch 必须指向绑定模块 aistock_agent.agents.workers.<agent>.<name> 而非源模块；
# NodeApiClient 是类方法，replacement 必须接受任意参数（含 self）。
# 分两类：
# - sync 目标：archiver 的归档函数是同步函数，review.py 直接调用
#   `archive_market_trace_snapshot(snapshot)` / `if not archive_review(...)`，
#   用 async no-op 会产生未 await 的协程（RuntimeWarning 且恒真），必须同步替换。
# - async 目标：`await set_cached_review(...)` / `await set_cached_event(...)` /
#   `await persist_event_report(...)` / `await node_api.save_*`，异步替换。
_SYNC_SIDE_EFFECT_TARGETS: tuple[str, ...] = (
    "aistock_agent.agents.workers.review.archive_market_trace_snapshot",
    "aistock_agent.agents.workers.review.archive_review",
)
_ASYNC_SIDE_EFFECT_TARGETS: tuple[str, ...] = (
    "aistock_agent.agents.workers.review.set_cached_review",
    "aistock_agent.agents.workers.event.set_cached_event",
    "aistock_agent.agents.workers.event.persist_event_report",
    "aistock_agent.services.data_client.NodeApiClient.save_analysis_report",
    "aistock_agent.services.data_client.NodeApiClient.save_token_usage",
)

# 缓存读隔离：回放必须强制走完整流水线。review.py:963 `await get_cached_review(report_date)`
# 与 event.py:601 `await get_cached_event(user_msg)` 若真实 Redis 命中会返回生产全量数据，
# 破坏 T 窗口隔离承诺，因此 patch 为"无缓存"（返回 None）。同样 patch 绑定模块名。
_CACHE_READ_ISOLATION_TARGETS: tuple[str, ...] = (
    "aistock_agent.agents.workers.review.get_cached_review",
    "aistock_agent.agents.workers.event.get_cached_event",
)

_PATCHED_PATHS: set[str] = set()


def get_replay_case_id() -> str | None:
    return os.environ.get("REPLAY_CASE_ID") or None


def get_replay_agent_id() -> str | None:
    return os.environ.get("REPLAY_AGENT") or None


def is_replay_mode() -> bool:
    return get_replay_case_id() is not None


def load_replay_snapshot() -> dict[str, object] | None:
    """按 REPLAY_CASE_ID 加载切片 window_before。"""
    case_id = get_replay_case_id()
    if not case_id:
        return None
    try:
        case = load_case(case_id)
    except FileNotFoundError:
        logger.warning("iterate_replay_case_missing", case_id=case_id)
        return None
    window = case.get("window_before")
    return window if isinstance(window, dict) else None


def apply_replay_patches(adapter: IterableAgentAdapter) -> None:
    """按 adapter.data_deps 应用数据回放 patch + 副作用 no-op patch。"""
    snapshot = load_replay_snapshot()
    if snapshot is None:
        raise RuntimeError("replay snapshot not found: REPLAY_CASE_ID invalid")

    for logic_name, field_name in adapter.data_deps.items():
        target_path = _REPLAY_PATCH_TARGETS.get(logic_name)
        if target_path is None:
            continue  # 未声明回放的工具保持原逻辑（如知识图谱查询）
        _patch_async(target_path, _make_reader(field_name, snapshot, logic_name))

    # C1：event 工具经 registry 持有的 BaseTool 调用（模块属性 patch 拦截不到），
    # 服务层隔离在此统一应用（对 review 也生效：review.py 的 run(state) 只调
    # save_analysis_report/save_prediction，前者已 no-op、后者经 post 替换间接 no-op）。
    for target, kind in _SERVICE_ISOLATION_TARGETS.items():
        if kind == "node_read":
            _patch_async(target, _make_service_node_reader(snapshot))
        elif kind == "node_noop":
            _patch_async(target, _make_none_noop)
        else:  # tavily_search：同步替换（TavilyService.search 调用处无 await）
            _patch_sync(target, _make_tavily_search_reader(snapshot))

    for target in _SYNC_SIDE_EFFECT_TARGETS:
        _patch_sync(target, _make_sync_noop)

    for target in _ASYNC_SIDE_EFFECT_TARGETS:
        _patch_async(target, _make_noop)

    for target in _CACHE_READ_ISOLATION_TARGETS:
        _patch_async(target, _make_no_cache)

    logger.info("iterate_replay_patches_applied", agent_id=adapter.agent_id)


def remove_replay_patches() -> None:
    """移除全部回放 patch（子进程退出即进程回收，此函数供测试复用）。"""
    for path in list(_PATCHED_PATHS):
        try:
            owner, attr = _import_owner(path)
            original = getattr(owner, "_REPLAY_ORIGINAL", {}).get(attr)
            if original is not None:
                setattr(owner, attr, original)
        except Exception:  # noqa: BLE001
            pass
    _PATCHED_PATHS.clear()


def _import_owner(target_path: str) -> tuple[object, str]:
    """解析 patch 目标，返回 (持有对象, 属性名)。

    常规目标是 'module.attr'；NodeApiClient 是类而非模块，'module.Class.method'
    无法直接 __import__，须先导入最长的可导入模块前缀，再 getattr 逐级下钻。
    """
    parts = target_path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            owner: object = __import__(".".join(parts[:i]), fromlist=[parts[i]])
        except ModuleNotFoundError:
            continue
        for part in parts[i:-1]:
            owner = getattr(owner, part)
        return owner, parts[-1]
    raise ModuleNotFoundError(target_path)


def _patch_async(target_path: str, replacement: Callable[..., Awaitable[object]]) -> None:
    """把 target_path 引用的函数替换为 async replacement，并保留原函数供恢复。"""
    _patch(target_path, replacement)


def _patch_sync(target_path: str, replacement: Callable[..., object]) -> None:
    """把 target_path 引用的函数替换为 sync replacement，并保留原函数供恢复。"""
    _patch(target_path, replacement)


def _patch(target_path: str, replacement: Callable[..., object]) -> None:
    """把 target_path 引用的函数替换为 replacement，并保留原函数供恢复。"""
    owner, attr = _import_owner(target_path)
    original = getattr(owner, attr, None)
    originals = getattr(owner, "_REPLAY_ORIGINAL", None)
    if originals is None:
        originals = {}
        setattr(owner, "_REPLAY_ORIGINAL", originals)
    originals[attr] = original
    setattr(owner, attr, replacement)
    _PATCHED_PATHS.add(target_path)


async def _make_noop(*args: object, **kwargs: object) -> bool:
    """async 副作用 no-op：接受任意参数（含实例方法 self），返回 True 表示成功。

    调用方用 `if not await set_cached_review(...)` 判断成败（review.py:1130），
    返回 None 会被 `not None` 判为失败 → 回放降级，因此必须返回 True。
    """
    return True


async def _make_none_noop(*args: object, **kwargs: object) -> None:
    """async 写操作 no-op（返回 None）：NodeApiClient.post 在回放下不发请求。

    post 的真实调用方以 None 判定"未写入/请求失败"（如 event_persister.py:69
    `if result is None: return False`），返回 None 保持优雅降级且不污染主数据；
    不能返回 True——会被调用方误判为写成功。
    """
    return None


def _make_sync_noop(*args: object, **kwargs: object) -> bool:
    """sync 副作用 no-op：archive_review / archive_market_trace_snapshot 是同步函数。

    review.py 直接 `archive_market_trace_snapshot(snapshot)` 和
    `if not archive_review(...)`（review.py:1039/1120），必须同步返回 True；
    返回协程会产生 RuntimeWarning 且恒真（真值判断失效）。
    """
    return True


async def _make_no_cache(*args: object, **kwargs: object) -> None:
    """缓存读隔离 no-op：get_cached_review 恒返回 None（"无缓存"）。

    review.py:963 先读缓存再决定是否走完整流水线；返回 None 强制走
    完整回放路径，避免真实 Redis 命中引入生产数据破坏 T 窗口隔离。
    """
    return None


def _is_news_service_path(path: str) -> bool:
    """判断 NodeApiClient 路径是否命中回放语料（news/telegraph/search 端点）。"""
    return any(marker in path for marker in ("news", "telegraph", "search"))


def _make_service_node_reader(
    snapshot: dict[str, object],
) -> Callable[..., Awaitable[object]]:
    """构造 NodeApiClient.get/get_list 的服务层回放读。

    event 工具（search_cls_news / get_news_fulltext / get_quote 等）经 registry
    持有的 BaseTool 调用 node_api.get(...)（实例方法，类补丁生效）。按 path 匹配：
    - 含 news/telegraph/search：返回切片 cls_telegraph 语料，形状与 news_tools
      消费的响应一致——search/latest 端点消费 {"items": [...]}（_format_news_list），
      fulltext 端点消费 {"title", "content"}；
    - 其余路径（如 /internal/quote/...）：返回 None（隔离，不发真实请求）。
    """

    async def reader(*args: object, **kwargs: object) -> object:
        # 实例调用 node_api.get(path) 时普通函数会被绑定，self 落在 args[0]，
        # 因此 path 在 args[1]；兼容类级/关键字调用（args[0] / kwargs["path"]）。
        path_kwarg = kwargs.get("path")
        if isinstance(path_kwarg, str):
            path = path_kwarg
        elif len(args) >= 2:
            path = str(args[1])
        elif args:
            path = str(args[0])
        else:
            path = ""
        logger.debug("iterate_replay_service_node_read_isolated", path=path)
        if not _is_news_service_path(path):
            return None
        raw = snapshot.get("cls_telegraph")
        records = raw if isinstance(raw, list) else []
        if "fulltext" in path:
            record = next((r for r in records if isinstance(r, dict)), None)
            if record is None:
                return None
            return {"title": record.get("title", ""), "content": record.get("content", "")}
        return {"items": [r for r in records if isinstance(r, dict)]}

    return reader


def _make_tavily_search_reader(
    snapshot: dict[str, object],
) -> Callable[..., dict[str, object]]:
    """构造 TavilyService.search 的服务层回放替换：返回切片语料，不发网络请求。

    TavilyService.search 是同步静态方法（tavily_finance_search 直接
    `result = TavilyService.search(...)` 后 `result.get("results")`，无 await），
    因此必须同步返回 dict；async 替换会产生未 await 的协程（同 _make_sync_noop
    的坑）。类补丁对类级/实例调用均生效。返回 {"results": [切片记录]}，工具按
    Tavily 形状消费（title/content/url），并显式声明"搜索数据受限"。
    """

    def reader(*args: object, **kwargs: object) -> dict[str, object]:
        logger.debug("iterate_replay_tavily_search_isolated")
        raw = snapshot.get("cls_telegraph")
        records = raw if isinstance(raw, list) else []
        results = [
            {
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "url": r.get("url", ""),
            }
            for r in records
            if isinstance(r, dict)
        ]
        return {
            "results": results,
            "note": "回放模式：搜索数据受限（切片内 T 时刻前电报语料）",
        }

    return reader


def _make_reader(
    field_name: str,
    snapshot: dict[str, object],
    logic_name: str,
) -> Callable[..., Awaitable[object]]:
    """构造从切片字段读取数据的 async 函数（兼容原函数任意参数）。"""

    async def reader(*args: object, **kwargs: object) -> object:
        raw = snapshot.get(field_name)
        if logic_name == "news":
            return _format_news(raw)
        if logic_name == "search":
            return _format_search(raw)
        if logic_name == "global":
            return _format_global(raw)
        if logic_name == "market":
            return _parse_market_snapshot(raw)
        return json.dumps(raw, ensure_ascii=False) if raw is not None else "[]"

    return reader


def _format_news(raw: object) -> str:
    records = raw if isinstance(raw, list) else []
    lines = [
        f"- {r.get('time', '')} {r.get('title', '')}: {r.get('content', '')}"
        for r in records
        if isinstance(r, dict)
    ]
    return "\n".join(lines) if lines else "暂无电报数据"


def _format_global(raw: object) -> str:
    records = raw if isinstance(raw, list) else []
    lines = [
        f"- {r.get('ticker', '')} {r.get('change_pct', '')}% (asof {r.get('asof', '')})"
        for r in records
        if isinstance(r, dict)
    ]
    return "\n".join(lines) if lines else "暂无全球市场数据"


def _format_search(raw: object) -> str:
    """搜索类工具的回放版：不发网络请求，只返回切片内电报语料（受限"可搜索"语料）。

    设计文档 6.2：搜索类工具加日期范围过滤（结果时间 ≤ T）。回放模式下
    T 时刻后的搜索结果在切片生成时已被过滤，这里直接返回切片内语料即可，
    显式声明"搜索数据受限"，避免 agent 误以为获得了全网搜索能力。
    """
    records = raw if isinstance(raw, list) else []
    lines = [
        f"- {r.get('title', '')}: {r.get('content', '')}（财联社电报 {r.get('time', '')}）"
        for r in records
        if isinstance(r, dict)
    ]
    header = "回放模式：搜索数据受限，以下为切片内 T 时刻前电报语料\n"
    if not lines:
        return "回放模式：搜索数据受限，暂无切片内语料"
    return header + "\n".join(lines)


def _parse_market_snapshot(raw: object) -> object:
    """把切片 market_snapshot JSON 解析为 MarketTraceSnapshot 实例（review 消费）。

    校验失败直接抛 ValueError 让回放快速失败——返回原始 dict 会在
    validate_snapshot_discovery 处以隐晦的类型错误崩溃，难以定位。
    """
    from aistock_agent.schemas.market_trace import MarketTraceSnapshot

    if not isinstance(raw, dict):
        raise ValueError("replay market_snapshot must be a dict")
    try:
        return MarketTraceSnapshot.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"replay market_snapshot invalid: {e}") from e
