"""V4 实时运行结果影子归档与每日审计页。

为什么需要单独一层：`runner` 只负责取数与研判，P4 要求把结果落到 app-api 的
`brain_v4_runs.payload` 影子表，并生成一份本地静态审计页。归档失败绝不能推翻
已完成的研判，因此这里把“构造 payload / 调内部接口 / 生成静态页”收在一处，
并明确三条纪律：
- payload 字段集合固定，缺失字段写 None/[]，绝不编造数值；
- 只写影子归档接口，不触碰生产简报表；
- 审计页为纯静态 HTML，不含 fetch/XHR。

时间纪律：`date` / `as_of` 全部来自 `LiveRunResult`，本模块不读取系统时间。
"""

from __future__ import annotations

import html
import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from aistock_agent.brain_v4.runtime.runner import LiveRunResult

#: app-api 影子归档路由（见 `aistock-app-api/src/modules/brain-v4/internalRouter.ts`）。
ARCHIVE_PATH = "/internal/brain-v4/archive"

#: 与 runner 保持一致；归档列要求非空，但 payload 不复制这三个版本号。
_VERSION = "brain-v4-live-1"


@dataclass(frozen=True)
class ArchiveResult:
    """一次归档的出口；失败只记录分类错误，调用方继续使用原 run 结果。"""

    archived: bool
    run_id: str
    error: str | None = None
    response: dict[str, object] | None = None
    # 失败根因原文：区分“连接池未初始化”与“服务端业务拒绝”。
    error_detail: str | None = None


class ArchiveClient(Protocol):
    """可注入的归档传输；生产实现由 `NodeArchiveClient` 包装 `NodeApiClient`。"""

    async def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object] | None: ...


class NodeArchiveClient:
    """默认归档传输。

    `NodeApiClient.post` 内部已按既有约定注入 `X-Internal-Token` 与 JSON 头；
    这里保留 `headers` 参数作为可注入契约（测试据此断言鉴权头），真实实现不
    重复加头，避免与 `NodeApiClient` 的 token 配置漂移。
    """

    def __init__(self) -> None:
        # 惰性导入：测试注入假客户端时完全不需要加载 settings/data_client。
        from aistock_agent.services.data_client import NodeApiClient

        self._client = NodeApiClient()

    async def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
        error_out: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        # `NodeApiClient.post` 会把连接池未初始化抛出的 RuntimeError 吞成 None，
        # 使归档层无法区分“未初始化”与“空响应”。这里显式探测连接池状态，
        # 让 RuntimeError 带出真实原因（上层据此分类为 config_error）。
        from aistock_agent.services.http_client import HttpClientPool

        try:
            await HttpClientPool.get_client()
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from exc
        # 透传 error_out：``NodeApiClient.post`` 失败时把 stage/detail 写进出参，
        # 归档层据此暴露真实原因，不再只报 empty_response。
        return await self._client.post(path, body, error_out=error_out)


def _accepts_error_out(client: ArchiveClient) -> bool:
    """判断注入的归档客户端是否支持 ``error_out`` 出参。

    既有测试替身只实现 ``post(path, body, headers=...)``，无条件传 ``error_out``
    会 TypeError；这里按签名探测，保持向后兼容。
    """
    try:
        signature = inspect.signature(client.post)
    except (TypeError, ValueError):
        return False
    return "error_out" in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            result[key] = item
    return result


def _list_of_mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for item in value:
        mapping = _mapping(item)
        if mapping is not None:
            result.append(mapping)
    return result


def _as_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _feature_id(profile: str) -> str:
    return "market_trace" if profile == "market_trace" else "briefing"


def _judgment(result: LiveRunResult) -> dict[str, object] | None:
    if result.judgment is not None:
        return result.judgment.model_dump(mode="json")
    return _mapping(result.payload.get("judgment"))


def _subject_code(result: LiveRunResult) -> str:
    if result.judgment is not None and result.judgment.subject_code:
        return result.judgment.subject_code
    raw = _mapping(result.payload.get("judgment"))
    code = _as_str(raw.get("subject_code")) if raw is not None else None
    return code or "unknown"


def _relation(result: LiveRunResult) -> str | None:
    if result.judgment is not None:
        relation = _as_str(result.judgment.relation)
        if relation is not None:
            return relation
    return _as_str(result.payload.get("relation"))


def _collaboration_mode(result: LiveRunResult) -> str:
    return (
        _as_str(result.payload.get("collaboration_mode"))
        or _as_str(result.audit.get("mode"))
        or "UNKNOWN"
    )


def _usage(result: LiveRunResult) -> dict[str, object]:
    # tokens 取 runner 的 total_tokens；cost/latency 当前 runner 未产出，如实写 None。
    return {
        "tokens": _as_int(result.usage.get("total_tokens")),
        "cost": _as_float(result.usage.get("cost")),
        "latency_ms": _as_int(result.audit.get("latency_ms")),
    }


def _insufficient_reasons(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _audit(
    result: LiveRunResult,
    *,
    gaps: Sequence[dict[str, object]],
    revisions: Sequence[dict[str, object]],
    collaboration_mode: str,
) -> dict[str, object]:
    raw = result.audit
    gap_count = _as_int(raw.get("gap_count"))
    if gap_count is None:
        gap_count = len(gaps)
    revision_count = _as_int(raw.get("revision_count"))
    if revision_count is None:
        revision_count = len(revisions)
    return {
        # assumptions 当前 runner 未产出；缺失写 []，不从假设文本反推。
        "assumptions": _list_of_mappings(result.payload.get("assumptions")),
        "evidence_count": _as_int(raw.get("evidence_count")) or 0,
        "gap_count": gap_count,
        "revision_count": revision_count,
        "mode": _as_str(raw.get("mode")) or collaboration_mode,
        "insufficient_reasons": _insufficient_reasons(raw.get("insufficient_reasons")),
    }


def build_archive_payload(result: LiveRunResult) -> dict[str, object]:
    """构造 `POST /internal/brain-v4/archive` 的请求体。

    顶层是 `brain_v4_runs` 的列（app-api `archiveRun` 的必填字段），`payload`
    是固定十键的影子归档内容。缺失字段一律 None/[]，不得编造。
    """
    source = result.payload
    gaps = _list_of_mappings(source.get("gaps"))
    revisions = _list_of_mappings(source.get("revisions"))
    collaboration_mode = _collaboration_mode(result)

    archive_payload: dict[str, object] = {
        "judgment": _judgment(result),
        "briefing_v1": result.briefing_v1,
        "evidence_ledger": _mapping(source.get("evidence_ledger")),
        "hypotheses": _list_of_mappings(source.get("hypotheses")),
        "gaps": gaps,
        "revisions": revisions,
        "collaboration_mode": collaboration_mode,
        "six_factor": (
            None if result.six_factor is None else result.six_factor.model_dump(mode="json")
        ),
        "usage": _usage(result),
        "audit": _audit(
            result,
            gaps=gaps,
            revisions=revisions,
            collaboration_mode=collaboration_mode,
        ),
    }
    return {
        "v4_run_id": result.run_id,
        "feature_id": _feature_id(result.profile),
        "profile": result.profile,
        "subject_type": "market",
        "subject_code": _subject_code(result),
        "as_of": result.as_of.isoformat(),
        "data_cutoff": result.data_cutoff.isoformat(),
        "snapshot_id": result.snapshot_id,
        "snapshot_sha": result.snapshot_sha,
        "parent_run_id": result.parent_run_id,
        "relation": _relation(result),
        "config_version": _VERSION,
        "method_version": _VERSION,
        "model_version": _VERSION,
        "payload": archive_payload,
    }


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_error"
    if isinstance(exc, httpx.RequestError):
        return "request_error"
    # 连接池未初始化等本地配置错误会以 RuntimeError 上抛；与上游故障区分开。
    if isinstance(exc, RuntimeError):
        return "config_error"
    return "unexpected_error"


def _archive_headers() -> dict[str, str]:
    from aistock_agent.config import settings

    return {
        "X-Internal-Token": settings.internal_api_token,
        "Content-Type": "application/json",
    }


async def archive_run(
    result: LiveRunResult,
    *,
    client: ArchiveClient | None = None,
) -> ArchiveResult:
    """归档一次 V4 影子运行；失败只返回分类错误，绝不抛出裸异常。"""
    archive_client: ArchiveClient = client if client is not None else NodeArchiveClient()
    body = build_archive_payload(result)
    error_out: dict[str, object] = {}
    try:
        headers = _archive_headers()
        if _accepts_error_out(archive_client):
            response = await cast(Any, archive_client).post(
                ARCHIVE_PATH, body, headers=headers, error_out=error_out
            )
        else:
            response = await archive_client.post(
                ARCHIVE_PATH, body, headers=headers
            )
    except Exception as exc:  # noqa: BLE001 - 归档失败不得推翻已完成的研判
        return ArchiveResult(
            archived=False,
            run_id=result.run_id,
            error=_classify_error(exc),
            error_detail=str(exc),
        )
    if response is None:
        # 无响应但有 error_out 详情：暴露真实原因（截断到 200 字符），
        # empty_response 仅保留给“既无响应也无 detail”的真·空响应。
        detail = _as_str(error_out.get("detail"))
        if detail is not None:
            summary = f"{_as_str(error_out.get('stage')) or 'post_failed'}:{detail}"[:200]
            return ArchiveResult(
                archived=False,
                run_id=result.run_id,
                error=summary,
                error_detail=summary,
            )
        return ArchiveResult(archived=False, run_id=result.run_id, error="empty_response")

    # 响应可能是未解包的信封（含 code）：code != 0 或缺少 data 时按业务拒绝处理，
    # 不再笼统报 empty_response。`NodeArchiveClient` 已解包的纯 data（无 code）照常成功。
    if "code" in response:
        code = response.get("code")
        message = _as_str(response.get("message")) or "unknown"
        if code not in (0, 200, 201):
            return ArchiveResult(
                archived=False,
                run_id=result.run_id,
                error=f"rejected:{message}",
                response=response,
                error_detail=message,
            )
        if response.get("data") is None:
            return ArchiveResult(
                archived=False,
                run_id=result.run_id,
                error=f"rejected:{message}",
                response=response,
                error_detail=message,
            )
    return ArchiveResult(archived=True, run_id=result.run_id, response=response)


def _cell(value: object) -> str:
    if value is None:
        return "-"
    return html.escape(str(value))


def _count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _render_daily_report(
    report_date: str, results: Sequence[LiveRunResult]
) -> str:
    rows: list[str] = []
    for result in results:
        payload = _mapping(build_archive_payload(result)["payload"]) or {}
        usage = _mapping(payload.get("usage")) or {}
        audit = _mapping(payload.get("audit")) or {}
        reasons = audit.get("insufficient_reasons")
        reason_text = "；".join(
            item for item in reasons if isinstance(item, str)
        ) if isinstance(reasons, list) else ""
        rows.append(
            "<tr>"
            f"<td>{_cell(result.profile)}</td>"
            f"<td>{_cell(result.status)}</td>"
            f"<td>{_cell(payload.get('collaboration_mode'))}</td>"
            f"<td class=\"num\">{_count(payload.get('hypotheses'))}</td>"
            f"<td class=\"num\">{_cell(audit.get('evidence_count'))}</td>"
            f"<td class=\"num\">{_cell(audit.get('gap_count'))}</td>"
            f"<td class=\"num\">{_cell(audit.get('revision_count'))}</td>"
            f"<td class=\"num\">{_cell(usage.get('latency_ms'))}</td>"
            f"<td class=\"num\">{_cell(usage.get('tokens'))}</td>"
            f"<td class=\"num\">{_cell(usage.get('cost'))}</td>"
            f"<td>{_cell(reason_text)}</td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Brain V4 每日审计 · {html.escape(report_date)}</title>
  <style>
    body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 24px; color: #1f2328; }}
    h1 {{ font-size: 20px; }}
    .hint {{ color: #6e7781; font-size: 13px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border: 1px solid #d0d7de; padding: 6px 8px; text-align: left; }}
    th {{ background: #f6f8fa; white-space: nowrap; }}
    td.num {{ text-align: right; }}
  </style>
</head>
<body>
  <h1>Brain V4 每日审计 · {html.escape(report_date)}</h1>
  <p class="hint">影子运行只读审计；页面为静态生成，不含任何在线请求。</p>
  <table>
    <thead>
      <tr>
        <th>profile</th><th>status</th><th>mode</th><th>假设数</th><th>证据数</th>
        <th>gap</th><th>回改</th><th>latency(ms)</th><th>tokens</th><th>cost</th>
        <th>insufficient 原因</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</body>
</html>
"""


def write_daily_report(results: Sequence[LiveRunResult], out_dir: str | Path) -> Path:
    """把一批 run 写成 `<out_dir>/<date>/index.html` 静态审计页。"""
    if not results:
        raise ValueError("write_daily_report 需要至少一个运行结果")
    report_date = max(result.as_of for result in results).date().isoformat()
    target_dir = Path(out_dir) / report_date
    target_dir.mkdir(parents=True, exist_ok=True)
    index = target_dir / "index.html"
    index.write_text(
        _render_daily_report(report_date, results), encoding="utf-8"
    )
    return index


__all__ = [
    "ARCHIVE_PATH",
    "ArchiveClient",
    "ArchiveResult",
    "NodeArchiveClient",
    "archive_run",
    "build_archive_payload",
    "write_daily_report",
]
