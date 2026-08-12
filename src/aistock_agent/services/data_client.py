"""数据客户端 — httpx AsyncClient → Node.js /internal/* API

Python 服务不拥有 A 股数据，通过回调 Node.js 获取。
httpx.AsyncClient 由 ``HttpClientPool`` 全局复用（lifespan 管理）。
"""

import json
from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import quote

import httpx
import structlog

from aistock_agent.config import settings
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.redis_pool import RedisPool

logger = structlog.get_logger()


@dataclass(frozen=True)
class ReviewReportReadResult:
    """市场复盘工件读取结果，仅供 trace_loader 使用。"""

    status: Literal["found", "not_found", "unavailable"]
    report: dict[str, object] | None = None


HotBurstReadStatus = Literal["available", "empty", "unavailable"]


@dataclass(frozen=True)
class HotBurstReadResult:
    """机构调研热门股读取结果，区分正常空结果与数据源不可用。"""

    status: HotBurstReadStatus
    data: dict[str, object] | None = None


IndustryChainStatus = Literal[
    "found",
    "not_found",
    "authentication_failed",
    "upstream_failed",
    "timeout",
    "request_failed",
    "invalid_response",
]


@dataclass(frozen=True)
class IndustryChainReadResult:
    """IndustryKG 行业链读取结果，保留失败原因供工具层展示。"""

    status: IndustryChainStatus
    data: dict[str, object] | None = None
    source: str | None = None


def _is_valid_industry_node(
    node: object, *, requires_leading_stocks: bool = False
) -> bool:
    """验证 IndustryKG 节点的最小交付契约。"""
    if not isinstance(node, dict):
        return False

    industry_id = node.get("id")
    name = node.get("name")
    if not (
        isinstance(industry_id, str)
        and industry_id.strip()
        and isinstance(name, str)
        and name.strip()
    ):
        return False

    return not requires_leading_stocks or isinstance(node.get("leadingStocks"), list)


class NodeApiClient:
    """Node.js 内部 API 客户端"""

    def __init__(self) -> None:
        self._base_url = settings.node_api_base_url.rstrip("/")
        self._token = settings.internal_api_token

    async def get(self, path: str) -> dict[str, object] | None:
        """GET 请求 Node.js 内部 API

        Args:
            path: 路径，如 /internal/quote/600519

        Returns:
            业务数据（已解包 `data` 字段）；请求失败或业务码非 200 返回 None。
            仅返回 dict 类型——Node.js ``data`` 为列表时返回 None，
            列表端点请用 :meth:`get_list`。
        """
        data = await self._request(path)
        return data if isinstance(data, dict) else None

    async def get_list(self, path: str) -> list[dict[str, object]] | None:
        """GET 请求 Node.js 内部 API（列表型端点）

        部分接口（如 ``/internal/monitor/:symbol``、``/internal/graph/concepts``）
        的 ``data`` 字段是数组而非对象，``get`` 会因 ``isinstance(data, dict)``
        判否返回 None。本方法专门解包列表型响应。

        Args:
            path: 路径，如 /internal/monitor/600519

        Returns:
            业务数据列表；请求失败、业务码非 200 或 data 非列表返回 None
        """
        data = await self._request(path)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return None

    async def get_hot_burst_data(self, path: str) -> HotBurstReadResult:
        """读取热门机构调研数据，保留成功空结果与读取失败的语义差异。"""
        url = f"{self._base_url}{path}"
        headers = {"X-Internal-Token": self._token}

        try:
            client = await HttpClientPool.get_client()
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 200:
                logger.error("hot_burst_read_invalid_response", url=url)
                return HotBurstReadResult("unavailable")

            data = payload.get("data")
            if data is None:
                return HotBurstReadResult("empty")
            if not isinstance(data, dict):
                logger.error("hot_burst_read_invalid_data", url=url)
                return HotBurstReadResult("unavailable")
            return HotBurstReadResult("available", data)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "hot_burst_read_http_error",
                url=url,
                status=exc.response.status_code,
            )
        except httpx.RequestError as exc:
            logger.error("hot_burst_read_request_error", url=url, error=str(exc))
        except Exception as exc:
            logger.error("hot_burst_read_unexpected_error", url=url, error=str(exc))

        return HotBurstReadResult("unavailable")

    async def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> dict[str, object] | None:
        """POST 请求 Node.js 内部 API

        Args:
            path: 路径，如 /internal/analysis-reports
            body: JSON 请求体

        Returns:
            业务数据（已解包 data 字段）；请求失败或业务码非 0/200/201 返回 None。
            仅返回 dict 类型——Node.js ``data`` 为列表时返回 None。
        """
        data = await self._post_request(path, body, timeout=timeout)
        return data if isinstance(data, dict) else None

    async def semantic_search_industries(
        self, embedding: list[float], threshold: float = 0.7, limit: int = 5
    ) -> list[dict[str, object]]:
        """pgvector 语义搜索行业（事件传导 Step 3 首层行业定位）

        调用 Node.js /internal/industries/semantic-search，
        在 industry_embeddings 表中做 cosine similarity 搜索。

        Args:
            embedding: 1536 维查询向量（OpenAI text-embedding-3-small）
            threshold: 相似度阈值 (0-1)，默认 0.7
            limit: 返回数量上限，默认 5

        Returns:
            匹配行业列表 [{code, name, similarity}]，失败返回空列表
        """
        data = await self.post("/internal/industries/semantic-search", {
            "embedding": embedding,
            "threshold": threshold,
            "limit": limit,
        })
        industries = data.get("industries") if data else None
        if isinstance(industries, list):
            return [item for item in industries if isinstance(item, dict)]
        return []

    async def _post_request(
        self, path: str, body: dict[str, object], *, timeout: float | None = None
    ) -> object | None:
        """POST 请求 Node.js 内部 API，返回解包后的 data 字段。

        ``post`` 的共享实现：统一处理 HTTP 错误、业务码校验、payload 解包。
        """
        url = f"{self._base_url}{path}"
        headers = {
            "X-Internal-Token": self._token,
            "Content-Type": "application/json",
        }

        try:
            client = await HttpClientPool.get_client()
            if timeout is None:
                resp = await client.post(url, json=body, headers=headers)
            else:
                resp = await client.post(url, json=body, headers=headers, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()

            if not isinstance(payload, dict):
                logger.error(
                    "node_api_post_unexpected_payload",
                    url=url,
                    payload=str(payload)[:200],
                )
                return None
            if payload.get("code") not in (0, 200, 201):
                logger.error("node_api_post_business_error", url=url, code=payload.get("code"),
                             message=payload.get("message"))
                return None
            return payload.get("data")
        except httpx.HTTPStatusError as e:
            logger.error(
                "node_api_post_http_error",
                url=url,
                status=e.response.status_code,
                response_body=e.response.text[:500],
            )
        except httpx.RequestError as e:
            logger.error("node_api_post_request_error", url=url, error=str(e))
        except Exception as e:
            logger.error("node_api_post_unexpected_error", url=url, error=str(e))

        return None

    async def delete(self, path: str) -> dict[str, object] | None:
        """DELETE 请求 Node.js 内部 API

        Args:
            path: 路径，如 /internal/analysis-reports/cleanup

        Returns:
            业务数据（已解包 `data` 字段）；请求失败返回 None。
        """
        url = f"{self._base_url}{path}"
        headers = {"X-Internal-Token": self._token}

        try:
            client = await HttpClientPool.get_client()
            resp = await client.delete(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

            if not isinstance(payload, dict):
                return None
            if payload.get("code") != 200:
                logger.error("node_api_delete_error", url=url, code=payload.get("code"))
                return None
            return payload.get("data") if isinstance(payload.get("data"), dict) else None
        except httpx.HTTPStatusError as e:
            logger.error("node_api_delete_http_error", url=url, status=e.response.status_code)
        except httpx.RequestError as e:
            logger.error("node_api_delete_request_error", url=url, error=str(e))
        except Exception as e:
            logger.error("node_api_delete_unexpected_error", url=url, error=str(e))

        return None

    async def patch(self, path: str, body: dict[str, object]) -> dict[str, object] | None:
        """PATCH Node 内部 API，并返回已解包的对象 data。"""
        url = f"{self._base_url}{path}"
        headers = {"X-Internal-Token": self._token, "Content-Type": "application/json"}
        try:
            client = await HttpClientPool.get_client()
            response = await client.patch(url, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 200:
                logger.error("node_api_patch_business_error", url=url)
                return None
            data = payload.get("data")
            return data if isinstance(data, dict) else None
        except httpx.HTTPStatusError as exc:
            logger.error("node_api_patch_http_error", url=url, status=exc.response.status_code)
        except httpx.RequestError as exc:
            logger.error("node_api_patch_request_error", url=url, error=str(exc))
        except Exception as exc:
            logger.error("node_api_patch_unexpected_error", url=url, error=str(exc))
        return None

    async def _request(self, path: str) -> object | None:
        """GET 请求 Node.js 内部 API，返回解包后的 data 字段（dict/list/标量）。

        ``get`` / ``get_list`` 的共享实现：统一处理 HTTP 错误、业务码校验、
        payload 解包。返回原始 ``data``（可能为 dict / list / None），
        由调用方按需做类型收敛。
        """
        url = f"{self._base_url}{path}"
        headers = {"X-Internal-Token": self._token}

        try:
            client = await HttpClientPool.get_client()
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

            # Node.js 约定返回 { code: 200, data: {...} }，解包 data 字段
            if not isinstance(payload, dict):
                logger.error("node_api_unexpected_payload", url=url, payload=str(payload)[:200])
                return None
            if payload.get("code") != 200:
                logger.error("node_api_business_error", url=url, code=payload.get("code"),
                             message=payload.get("message"))
                return None
            return payload.get("data")
        except httpx.HTTPStatusError as e:
            logger.error("node_api_http_error", url=url, status=e.response.status_code)
        except httpx.RequestError as e:
            logger.error("node_api_request_error", url=url, error=str(e))
        except Exception as e:
            logger.error("node_api_unexpected_error", url=url, error=str(e))

        return None

    # ─── Agent 分析报告持久化 ───

    async def save_analysis_report(
        self,
        report_type: str,
        report_date: str,
        content: object,
        user_id: str | None = None,
        data_source: str | None = None,
        status: str = "completed",
        generation_time_ms: int | None = None,
        model_version: str | None = None,
        error_message: str | None = None,
        update_cache: bool = True,  # P2：chat_analysis 传 False（公共列表排除，D15）
    ) -> dict[str, object] | None:
        """持久化 Agent 分析报告（upsert）

        Args:
            report_type: 报告类型 (morning/wind_leader/hot_burst/review/stock/alert/iterate)
            report_date: 报告日期 (YYYY-MM-DD)
            content: 报告内容（任意 JSON 可序列化对象）
            user_id: 用户ID（公共报告为 None）
            data_source: 数据源
            status: 状态 (completed/failed)
            generation_time_ms: 生成耗时(毫秒)
            model_version: 模型版本
            error_message: 错误信息
            update_cache: 是否同步写 Python report_cache（前端公共报告列表）。
                默认 True 保持既有行为；chat_analysis 传 False（不进公共列表，D15 覆盖语义）。

        Returns:
            Node.js 返回的 { id, report_type, report_date, created_at } 或 None
        """
        payload: dict[str, object] = {
            "report_type": report_type,
            "report_date": report_date,
            "content": content,
            "status": status,
        }
        if user_id is not None:
            payload["user_id"] = user_id
        if data_source is not None:
            payload["data_source"] = data_source
        if generation_time_ms is not None:
            payload["generation_time_ms"] = generation_time_ms
        if model_version is not None:
            payload["model_version"] = model_version
        if error_message is not None:
            payload["error_message"] = error_message

        result = await self.post("/internal/analysis-reports", payload)
        # 同步写入内存缓存（前端报告列表查询用）；chat_analysis 不进公共列表（D15 排除）
        if update_cache:
            try:
                from aistock_agent.services.report_cache import set_report  # noqa: PLC0415
                set_report(report_type, report_date, payload)
            except Exception:
                pass
        if result:
            logger.info(
                "analysis_report_saved",
                report_type=report_type,
                report_date=report_date,
                user_id=user_id,
            )
        return result

    async def save_token_usage(
        self,
        *,
        user_id: str,
        session_id: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        question: str | None = None,
    ) -> dict[str, object] | None:
        """记录一次对话 token 用量（P10 线 2，ws.py 计费回调）。

        与 save_analysis_report 同模式：``post`` 已吞异常返回 None，
        调用方再包一层 try/except 记日志即可——落库失败不阻断对话
        （"永不 500"铁律）。

        Returns:
            Node 返回的 {id} 或 None（失败）。
        """
        payload: dict[str, object] = {
            "user_id": user_id,
            "session_id": session_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "question": question,
        }
        result = await self.post("/internal/usage/records", payload)
        if result:
            logger.info(
                "token_usage.saved",
                user_id=user_id,
                session_id=session_id,
                total_tokens=total_tokens,
            )
        return result

    async def put(self, path: str, body: dict[str, object]) -> dict[str, object] | None:
        """PUT Node 内部 API，并返回已解包的对象 data。"""
        url = f"{self._base_url}{path}"
        headers = {"X-Internal-Token": self._token, "Content-Type": "application/json"}
        try:
            client = await HttpClientPool.get_client()
            response = await client.put(url, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 200:
                logger.error("node_api_put_business_error", url=url)
                return None
            data = payload.get("data")
            return data if isinstance(data, dict) else None
        except httpx.HTTPStatusError as exc:
            logger.error("node_api_put_http_error", url=url, status=exc.response.status_code)
        except httpx.RequestError as exc:
            logger.error("node_api_put_request_error", url=url, error=str(exc))
        except Exception as exc:
            logger.error("node_api_put_unexpected_error", url=url, error=str(exc))
        return None

    # ─── 预测能力落库与验证（大盘溯源预测 → prediction_records）───

    async def save_prediction(self, payload: dict[str, object]) -> dict[str, object] | None:
        """持久化预测记录（POST /internal/predictions）。

        与 save_analysis_report 同模式：``post`` 已吞异常返回 None，
        调用方再包 try/except——落库失败不阻断溯源报告（"永不 500"）。
        """
        result = await self.post("/internal/predictions", payload)
        if result:
            logger.info(
                "prediction.saved",
                source_type=payload.get("source_type"),
                source_id=payload.get("source_id"),
            )
        return result

    async def list_pending_predictions(self) -> list[dict[str, object]]:
        """读取全部 pending 预测记录（到期验证扫描用）。"""
        return await self.get_list("/internal/predictions?status=pending") or []

    async def update_prediction_verification(
        self,
        prediction_id: int,
        horizon: str,
        entry: dict[str, object],
    ) -> dict[str, object] | None:
        """回写单档位到期验证结果（PUT /internal/predictions/:id/verification）。"""
        body: dict[str, object] = {"horizon": horizon}
        body.update(entry)
        return await self.put(f"/internal/predictions/{prediction_id}/verification", body)

    async def get_analysis_report(
        self,
        report_type: str,
        report_date: str,
        user_id: str | None = None,
    ) -> dict[str, object] | None:
        """查询 Agent 分析报告

        Args:
            report_type: 报告类型
            report_date: 报告日期 (YYYY-MM-DD)
            user_id: 用户ID（公共报告为 None）

        Returns:
            报告数据 dict（含 content, status 等字段）或 None（不存在）
        """
        if user_id:
            path = f"/internal/analysis-reports/{report_type}/{report_date}/{user_id}"
        else:
            path = f"/internal/analysis-reports/{report_type}/{report_date}"

        return await self.get(path)

    async def list_analysis_reports(
        self,
        report_type: str,
        report_date: str,
    ) -> list[dict[str, object]]:
        """读取同一交易日、同一类型的全部已持久化报告。"""
        path = f"/internal/analysis-reports/{report_type}/{report_date}/list"
        return await self.get_list(path) or []

    async def get_quick_snapshot(self) -> dict[str, object] | None:
        """拉取 Node 端 quick snapshot（15:30 腾讯实时行情版）。

        Returns:
            dict（snapshot_kind='quick'），或 None（未就绪/服务不可用）。
            与 get("/internal/market/close-snapshot") 不同，此接口 15:30 后立即可用。
        """
        return await self.get("/internal/market/quick-snapshot")

    async def get_last_close_snapshot(self) -> dict[str, object] | None:
        """拉取最近一个已完成交易日的收盘快照（跳过时钟门禁）。

        在盘中或开盘前（如凌晨）需要昨日收盘数据时调用，close-snapshot
        返回 409 时可降级到此接口。

        Returns:
            dict（status='complete'），或 None（数据不可用/服务异常）。
        """
        return await self.get("/internal/market/last-close-snapshot")

    async def get_review_analysis_report(
        self,
        report_date: date,
    ) -> ReviewReportReadResult:
        """读取已持久化的 review 工件并保留不存在与服务失败的差异。

        该接口故意只接受 ``datetime.date``，并通过 ``isoformat`` 固定构造路径，
        防止调用方输入进入 URL 路径后发生目录穿越。它不改变通用
        :meth:`get_analysis_report` 的 ``None`` 兼容语义。
        """
        if type(report_date) is not date:
            raise TypeError("report_date 必须是 datetime.date")

        url = (
            f"{self._base_url}/internal/analysis-reports/review/"
            f"{report_date.isoformat()}"
        )
        headers = {"X-Internal-Token": self._token}

        try:
            client = await HttpClientPool.get_client()
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("review_report_read_request_error", url=url, error=str(exc))
            return ReviewReportReadResult("unavailable")
        except Exception as exc:
            logger.error("review_report_read_unexpected_error", url=url, error=str(exc))
            return ReviewReportReadResult("unavailable")

        if response.status_code == 404:
            return ReviewReportReadResult("not_found")
        if response.status_code != 200:
            logger.error(
                "review_report_read_http_error",
                url=url,
                status=response.status_code,
            )
            return ReviewReportReadResult("unavailable")

        try:
            payload = response.json()
        except Exception as exc:
            logger.error("review_report_read_invalid_json", url=url, error=str(exc))
            return ReviewReportReadResult("unavailable")

        if not isinstance(payload, dict) or payload.get("code") != 200:
            logger.error("review_report_read_invalid_response", url=url)
            return ReviewReportReadResult("unavailable")
        report = payload.get("data")
        if not isinstance(report, dict):
            logger.error("review_report_read_invalid_data", url=url)
            return ReviewReportReadResult("unavailable")
        return ReviewReportReadResult("found", report)

    async def get_industry_chain(self, industry_name: str) -> IndustryChainReadResult:
        """读取 IndustryKG 行业链，并保留 HTTP 与响应结构的失败分类。"""
        encoded_name = quote(industry_name, safe="")
        url = f"{self._base_url}/internal/industry/{encoded_name}/chain?depth=1"
        headers = {"X-Internal-Token": self._token}

        try:
            client = await HttpClientPool.get_client()
            response = await client.get(url, headers=headers)
            if response.status_code == 404:
                return IndustryChainReadResult("not_found")
            if response.status_code == 403:
                return IndustryChainReadResult("authentication_failed")
            if response.status_code != 200:
                logger.error(
                    "industry_chain_read_http_error",
                    url=url,
                    status=response.status_code,
                )
                return IndustryChainReadResult("upstream_failed")

            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 200:
                logger.error("industry_chain_read_invalid_response", url=url)
                return IndustryChainReadResult("invalid_response")

            data = payload.get("data")
            if not isinstance(data, dict):
                logger.error("industry_chain_read_invalid_data", url=url)
                return IndustryChainReadResult("invalid_response")

            source = data.get("source")
            industry = data.get("industry")
            upstream = data.get("upstream")
            downstream = data.get("downstream")
            if (
                source != "IndustryKGService"
                or not isinstance(upstream, list)
                or not isinstance(downstream, list)
                or not _is_valid_industry_node(industry)
                or not all(
                    _is_valid_industry_node(item, requires_leading_stocks=True)
                    for item in upstream
                )
                or not all(
                    _is_valid_industry_node(item, requires_leading_stocks=True)
                    for item in downstream
                )
            ):
                logger.error("industry_chain_read_invalid_data", url=url)
                return IndustryChainReadResult("invalid_response")

            return IndustryChainReadResult("found", data, source)
        except httpx.TimeoutException as exc:
            logger.error("industry_chain_read_timeout", url=url, error=str(exc))
            return IndustryChainReadResult("timeout")
        except httpx.RequestError as exc:
            logger.error("industry_chain_read_request_error", url=url, error=str(exc))
            return IndustryChainReadResult("request_failed")
        except Exception as exc:
            logger.error("industry_chain_read_invalid_response", url=url, error=str(exc))
            return IndustryChainReadResult("invalid_response")

    async def cleanup_expired_reports(self) -> int:
        """清理过期报告

        Returns:
            已删除的报告数量（失败返回 0）
        """
        result = await self.delete("/internal/analysis-reports/cleanup")
        deleted_count = result.get("deleted_count") if result else None
        return deleted_count if isinstance(deleted_count, int) else 0

    async def get_user_profile(self, user_id: str) -> dict[str, object] | None:
        """按 user_id 拉取用户画像（Phase 4-3 全局用户记忆）。

        调用 Node.js ``GET /internal/user-profile/{user_id}``；Redis TTL 5min
        缓存（``user_profile:{user_id}``，JSON 序列化），防对话每轮重复拉取。

        Args:
            user_id: 用户 openid（P0 可信 user_id）。

        Returns:
            profile dict（nickname / investment_preferences / risk_tolerance /
            updated_at）；空画像返回 ``{}``（区别于拉取失败）；失败返回 None
            （"永不 500"：对话入口失败仅 warning 不阻断）。
        """
        cache_key = f"user_profile:{user_id}"
        try:
            client = await RedisPool.get_client()
            cached = await client.get(cache_key)
            if cached:
                raw = cached.decode() if isinstance(cached, bytes) else str(cached)
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
        except Exception:
            logger.debug("get_user_profile_cache_miss", exc_info=True)

        data = await self.get(f"/internal/user-profile/{user_id}")
        if not isinstance(data, dict):
            logger.warning("get_user_profile_failed", user_id=user_id)
            return None

        try:
            client = await RedisPool.get_client()
            await client.setex(
                cache_key, _USER_PROFILE_TTL_SECONDS, json.dumps(data, ensure_ascii=False)
            )
        except Exception:
            logger.debug("get_user_profile_cache_write_failed", exc_info=True)
        return data


# 用户画像缓存 TTL（5 分钟；失败/空画像同样缓存，避免每轮重复拉取）
_USER_PROFILE_TTL_SECONDS = 300


# 全局单例
node_api = NodeApiClient()
