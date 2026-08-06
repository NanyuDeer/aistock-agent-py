"""自选股洞察归因 Worker：上下文 → 候选 → LLM 受约束选择 → 校验 → 规则兜底。

编排链（PRD 第 9 节第三阶段）：
1. ``InsightNodeClient.get_event_context`` 读取 Node 冻结的事件上下文；未就绪
   （返回 None）时产出 ``retryable_snapshot_not_ready=True``，由 Consumer 不 ack，
   等待 pending reclaim 重新执行。
2. 正文缺失：不生成主因结论，直接发布 ``unconfirmed``（PRD §12：来源缺少正文）。
3. ``extract_candidates`` 确定性抽取候选 → ``_llm_select`` LLM 结构化受约束选择。
4. ``_resolve``：LLM 输出必须经 ``validate_attribution`` 校验（候选存在 / 非
   suppressed / label 长度 / 证据锚定原文），不过则 ``rule_fallback_select`` 兜底。
5. 注入 event_id / analysis_version 后返回 ``InsightOutcome``。
"""

import json

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.agents.workers.stock_trace import (
    _first_json_object,
    _recover_tool_payload,
)
from aistock_agent.config import settings
from aistock_agent.prompts.workers.insight import INSIGHT_ATTRIBUTION_PROMPT
from aistock_agent.schemas.insight import InsightAttributionOutput
from aistock_agent.services.insight_candidate import CandidateFactor, extract_candidates
from aistock_agent.services.insight_client import InsightNodeClient
from aistock_agent.services.insight_validator import (
    rule_fallback_select,
    validate_attribution,
)
from aistock_agent.services.llm import get_deep_think, get_quick_think

logger = structlog.get_logger()


class InsightOutcome:
    """``analyze`` 返回契约：快照未就绪可重试；result 为待写入的洞察结果载荷。"""

    def __init__(
        self,
        result: dict[str, object],
        retryable_snapshot_not_ready: bool = False,
    ) -> None:
        self.result = result
        self.retryable_snapshot_not_ready = retryable_snapshot_not_ready


def _recover_from_raw(raw: object) -> dict[str, object] | None:
    """从 include_raw 的 raw 消息恢复结构化载荷（json content 优先，其次 tool_calls）。

    与 stock_trace 同源的解析兜底：``with_structured_output(include_raw=True)`` 在
    LLM 输出无法解析（``parsed`` 为 None）时，``raw`` 携带原始消息，需要自行恢复。
    """
    content = getattr(raw, "content", None)
    if isinstance(content, str) and content.strip():
        try:
            value = json.loads(_first_json_object(content))
        except (ValueError, json.JSONDecodeError):
            pass
        else:
            if isinstance(value, dict):
                return value
    try:
        return _recover_tool_payload(raw)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _format_details_from_drivers(
    payload: InsightAttributionOutput, by_id: dict[str, CandidateFactor]
) -> str:
    """display_report.details：只含已选因素与证据（参照 Task 10 兜底格式，简洁可读）。"""
    if payload.primary_driver is None:
        return ""
    primary = payload.primary_driver
    primary_cand = by_id[primary.candidate_id]
    parts = [
        f"主导因素：{primary.label}（{primary_cand.category}）。"
        f"证据：{primary_cand.evidence_quote}"
    ]
    parts.extend(
        f"次因：{d.label}（{by_id[d.candidate_id].category}）。"
        f"证据：{by_id[d.candidate_id].evidence_quote}"
        for d in payload.secondary_drivers
    )
    return "".join(parts)


class InsightWorker:
    """自选股洞察归因 worker：确定性候选 + LLM 受约束选择 + 规则兜底。"""

    def __init__(self, client: InsightNodeClient | None = None) -> None:
        self._client = client or InsightNodeClient()

    async def analyze(
        self, event_id: str, analysis_version: str
    ) -> InsightOutcome:
        """读取上下文 → 候选 → LLM → 校验 → 兜底 → 注入身份字段。"""
        ctx = await self._client.get_event_context(event_id)
        if not ctx:
            # 上下文未就绪（Node 侧快照冻结前）：返回可重试标记，由 consumer 不 ack 等 reclaim
            return InsightOutcome({}, retryable_snapshot_not_ready=True)
        title = str(ctx.get("title") or "")
        raw_keywords = ctx.get("keywords")
        keywords = [str(k) for k in raw_keywords] if isinstance(raw_keywords, list) else []
        content = str(ctx.get("content") or "")
        if not content:
            # 无正文：发布 unconfirmed（PRD §12：来源缺少正文，不生成主因结论）
            result = rule_fallback_select([], content, title)
        else:
            candidates = extract_candidates(title, keywords, content)
            payload = await self._llm_select(candidates, title, content)
            result = self._resolve(payload, candidates, title, content)
        # 身份字段统一注入（所有非 retryable 返回路径）：Node 侧 INSERT 的
        # event_id / analysis_version 为 NOT NULL，缺失会导致 post_result 落库失败
        # 且返回 None 不抛异常 → consumer 静默 ack → 结果被丢弃（前端永远 pending）
        result["event_id"] = event_id
        result["analysis_version"] = analysis_version
        return InsightOutcome(result)

    async def _llm_select(
        self, candidates: list[CandidateFactor], title: str, content: str
    ) -> InsightAttributionOutput | None:
        """LLM 结构化受约束选择；任何失败返回 None（由调用方走规则兜底）。"""
        try:
            # DeepSeek base_url 时禁用 thinking：其默认 Thinking Mode 拒绝强制
            # tool_choice（同 stock_trace），结构化输出需显式关闭
            if settings.insight_llm_model == "quick_think":
                llm = get_quick_think()
            else:
                base_url = (
                    settings.deep_think_base_url or settings.openai_base_url
                ).lower()
                extra_body = (
                    {"thinking": {"type": "disabled"}}
                    if "deepseek" in base_url
                    else None
                )
                llm = get_deep_think(extra_body=extra_body)
            structured = llm.with_structured_output(
                InsightAttributionOutput,
                method=settings.insight_structured_output_method,
                include_raw=True,
            )
            candidates_json = [c.model_dump(mode="json") for c in candidates]
            prompt = INSIGHT_ATTRIBUTION_PROMPT.replace(
                "{{TITLE}}", title
            ).replace(
                "{{CANDIDATES_JSON}}",
                json.dumps(candidates_json, ensure_ascii=False),
            )
            response = await structured.ainvoke(
                [SystemMessage(content=prompt), HumanMessage(content=content)]
            )
            return self._parse_response(response)
        except Exception as exc:
            logger.error("insight_llm_failed", error=str(exc), exc_info=True)
            return None

    @staticmethod
    def _parse_response(response: object) -> InsightAttributionOutput | None:
        """include_raw 的 dict 响应：parsed 优先，None 时从 raw 恢复（json/tool payload）。"""
        if not isinstance(response, dict):
            return None
        parsed = response.get("parsed")
        if isinstance(parsed, InsightAttributionOutput):
            return parsed
        if isinstance(parsed, dict):
            return InsightAttributionOutput.model_validate(parsed)
        raw = response.get("raw")
        if raw is not None:
            recovered = _recover_from_raw(raw)
            if recovered is not None:
                return InsightAttributionOutput.model_validate(recovered)
        return None

    def _resolve(
        self,
        payload: InsightAttributionOutput | None,
        candidates: list[CandidateFactor],
        title: str,
        content: str,
    ) -> dict[str, object]:
        """LLM 成功且校验通过 → llm 结果；否则 → 规则兜底。"""
        if payload is not None and validate_attribution(
            payload, candidates, title, content
        ):
            return self._from_llm(payload, candidates)
        return rule_fallback_select(candidates, content, title)

    def _from_llm(
        self, payload: InsightAttributionOutput, candidates: list[CandidateFactor]
    ) -> dict[str, object]:
        """LLM 结果落地：category 取候选（LLM 不得指定），label/confidence 用 LLM 的。"""
        if payload.attribution_status == "unconfirmed":
            # validate_attribution 已保证 unconfirmed 仅在无有效候选时合法；无候选可
            # 落地，产出标准 unconfirmed 载荷（与规则兜底一致），保障载荷同构
            return rule_fallback_select([], "", "")
        primary = payload.primary_driver
        if primary is None:
            return rule_fallback_select([], "", "")
        by_id = {c.id: c for c in candidates}
        primary_cand = by_id[primary.candidate_id]
        return {
            "attribution_status": "confirmed",
            "confidence": primary.confidence,
            "primary_driver": {
                "label": primary.label,
                "category": primary_cand.category,
                "confidence": primary.confidence,
                "evidence_quote": primary_cand.evidence_quote,
                "source_ids": [""],  # Node 侧按 event_id 关联 source_id
            },
            "secondary_drivers": [
                {
                    "label": d.label,
                    "category": by_id[d.candidate_id].category,
                    "confidence": d.confidence,
                    "evidence_quote": by_id[d.candidate_id].evidence_quote,
                    "source_ids": [""],
                }
                for d in payload.secondary_drivers
            ],
            "display_report": {
                "summary": f"主导因素：{primary.label}",
                "details": _format_details_from_drivers(payload, by_id),
            },
            "podcast_brief": f"{primary.label}为主要推动因素。",
            "validation_status": "llm",
            "model_provider": settings.insight_llm_model,
        }

    async def write_result(self, result: dict[str, object]) -> dict[str, object] | None:
        """回传洞察归因结果给 Node 持久化。

        返回值必须透传：``post_result`` 内部捕获 HTTP/业务错误返回 None 不抛异常，
        Consumer 依赖该返回值判定是否 report completed（None 时进入失败重试路径）。
        """
        return await self._client.post_result(result)

    async def report_job(
        self, job_id: str, status: str, error: str | None = None
    ) -> dict[str, object] | None:
        """更新 Job 状态；返回 Node PATCH 响应。

        返回值必须透传：Consumer 依赖响应中的 ``attempt_count`` 判定是否进 DLQ
        （insight_consumer._attempt_count），丢弃会导致失败消息无限重试。
        """
        return await self._client.report_job(job_id, status, error)
