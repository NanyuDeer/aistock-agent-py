"""应用配置 — pydantic-settings 读取环境变量"""

import json
import os
import random
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    """全局配置，从 .env.{APP_ENV} 或环境变量读取"""

    # Node.js 后端地址
    node_api_base_url: str = "http://localhost:3000"

    # LLM — quick_think（默认 API）
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    quick_think_model: str = "gpt-4o-mini"
    # LLM — deep_think（可使用不同 API，如 DeepSeek 直连）
    # 若未配置则 fallback 到 openai_api_key / openai_base_url
    deep_think_api_key: str = ""
    deep_think_base_url: str = ""
    deep_think_model: str = "gpt-4o"
    # 双模型参数 — 从配置读取，避免硬编码（services/llm.py 引用）
    quick_think_temperature: float = 0.1
    quick_think_max_tokens: int = 2000
    deep_think_temperature: float = 0.3
    deep_think_max_tokens: int = 4000

    # Redis（与 Node.js 共用实例，db=1 避免 key 冲突）
    redis_url: str = "redis://localhost:6379/1"
    # 连接池最大连接数（main.py lifespan 传给 RedisPool.init）
    redis_max_connections: int = 10
    # Stock Trace 使用 Node Outbox 所在的 Redis DB；独立 Consumer 进程使用该连接。
    stock_trace_redis_url: str = "redis://localhost:6379/2"
    stock_trace_consumer_group: str = "stock-trace-workers"
    stock_trace_consumer_block_ms: int = 1000
    # Pending 消息达到该空闲时间后，可由任意 Consumer 接管重试。
    stock_trace_pending_claim_idle_ms: int = 3000
    stock_trace_max_attempts: int = 3
    # Consumer 集成模式开关：true=在 agent-py 主进程 lifespan 内启动（一次重启即可），
    # false=需独立进程运行（python -m aistock_agent.workers.stock_trace_consumer）。
    # 生产环境推荐 true；开发/测试可设 false 避免意外消费 Stream。
    stock_trace_consumer_enabled: bool = True
    # Tool calling provides a schema-bound response across OpenAI-compatible models.
    stock_trace_structured_output_method: Literal[
        "function_calling", "json_mode", "json_schema"
    ] = "function_calling"

    # HTTP 超时（main.py lifespan 传给 HttpClientPool.init）
    http_timeout_seconds: float = 10.0

    # Tavily API 池（逗号分隔，支持多成员共享额度）
    # 优先使用 TAVILY_API_KEYS；若只有单人，用 TAVILY_API_KEY 兼容
    tavily_api_key: str = ""
    tavily_api_keys: str = ""

    # 服务（Python 8000 / Node.js 3000）
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # 内网鉴权
    internal_api_token: str = "change-me-in-production"

    # CORS 允许的源列表（api/middleware.py setup_middleware 读取）
    # 支持逗号分隔（CORS_ORIGINS=http://a,http://b）或 JSON 数组格式（CORS_ORIGINS=["a","b"]）
    # NoDecode 阻止 pydantic-settings 预先 JSON 解析，交给 _parse_cors_origins 统一处理
    cors_origins: Annotated[list[str], NoDecode] = ["*"]

    # 健康检查：是否在 /health/ready 中探测 LLM 连通性。
    # 默认关闭——避免 readiness 探针每次消耗 token；需探测时设 HEALTH_CHECK_LLM=true。
    health_check_llm: bool = False

    # 是否将 /chat/* 路由切换到新 CHAT 子图（compile_chat_graph）。
    # 默认 False 走老路径；上线时切 True，出问题立即切回。
    chat_graph_enabled: bool = False

    # LangSmith 追踪（默认关闭，生产按需开启）
    langsmith_enabled: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "aistock-agent"

    # LangGraph checkpointer 后端（memory / sqlite / redis）
    # memory=MemorySaver（开发默认，已可用）；sqlite/redis 需安装对应
    # langgraph-checkpoint 子包，未安装时 get_checkpointer 优雅降级到 MemorySaver
    checkpointer_backend: str = "memory"
    # sqlite backend 的数据库路径（仅 checkpointer_backend="sqlite" 时使用）
    sqlite_path: str = ".langgraph.db"

    # 定时调度（APScheduler AsyncIOScheduler，集成到 main.py lifespan）
    # 关闭后 lifespan 不启动调度器（开发/测试环境可设 SCHEDULER_ENABLED=false）
    scheduler_enabled: bool = True
    scheduler_morning_cron: str = "50 8 * * 1-5"       # 晨报：工作日 08:50
    scheduler_review_cron: str = "30 15 * * 1-5"       # 复盘：工作日 15:30
    scheduler_snapshot_cron: str = "35 15 * * 1-5"     # 快照：工作日 15:35
    scheduler_iterate_cron: str = "40 15 * * 1-5"      # 迭代：工作日 15:40
    # 播报链路：工作日 09:00（morning→wind_leader→hot_burst→broadcast）
    scheduler_broadcast_cron: str = "0 9 * * 1-5"
    scheduler_timezone: str = "Asia/Shanghai"
    # ---- evening_chain 事件驱动重构（spec: 2026-07-29）----
    # quick review：15:30 收盘后基于腾讯实时行情立即产出
    scheduler_review_quick_cron: str = "30 15 * * 1-5"
    # full review：20:30 Tushare 完整数据覆盖 quick
    scheduler_review_full_cron: str = "30 20 * * 1-5"
    # EventBus 配置
    event_bus_max_retries: int = 3
    event_bus_deadletter_prefix: str = "dlq:"
    event_bus_consumer_group: str = "evening_chain"
    event_stream_max_len: int = 10000
    # Feature Flag：quick snapshot 开关（false 时走旧 _run_evening_chain_task）
    quick_snapshot_enabled: bool = False

    # 隔离 QA：保留原始字符串，再由 qa_mode_enabled 做与 Node 一致的严格判断。
    # QA_RUN_ID 绑定单次隔离资源，避免误把任意日期任务写入 QA 库。
    qa_mode: str = ""
    qa_run_id: str = ""

    # 市场事件推送阈值（晨报生成后自动识别重大涨跌并推送）
    # 对称阈值：上涨 >= 阈值 或 下跌 <= -阈值 才视为"重磅"
    market_event_up_threshold: float = 1.5      # 指数涨幅 ≥ 1.5%
    market_event_down_threshold: float = -1.5   # 指数跌幅 ≤ -1.5%
    market_event_max_pushes: int = 2            # 每次晨报最多推送条数

    # 现象发现：同向指数异动与市场广度。
    # broad_rally / broad_decline 基础条件：至少 N 个核心指数同向超过 change_pct，且广度比例达标。
    phenomenon_broad_index_count: int = 4
    phenomenon_broad_index_change_pct: float = 0.8
    # 备选：全部核心指数大幅同向异动。
    phenomenon_broad_all_index_change_pct: float = 1.5
    # 上涨（下跌）家数与总家数之比的最小值。
    phenomenon_broad_breadth_ratio: float = 0.55

    # 现象发现：同向异动的辅助确认条件。
    # 涨跌停家数差（用于 broad_rally / broad_decline 的辅助确认）。
    phenomenon_broad_limit_count_gap: int = 20
    # 成交额变化百分比（用于 broad_rally / broad_decline 的辅助确认）。
    phenomenon_broad_turnover_change_pct: float = 10.0

    # 现象发现：风格分化阈值。
    # CSI300 与 CSI1000 反向涨跌幅阈值（方向相反且绝对值均超过此值）。
    phenomenon_style_divergence_change_pct: float = 0.5

    # 现象发现：板块集中异动阈值。
    # 板块相对大盘中位数的绝对异动阈值。
    phenomenon_sector_abs_change_pct: float = 3.0
    # 板块集中时市场广度需在此区间内（中性）。
    phenomenon_sector_neutral_breadth_min_ratio: float = 0.40
    phenomenon_sector_neutral_breadth_max_ratio: float = 0.60

    # 现象发现：情绪极端阈值。
    # 涨停家数门槛。
    phenomenon_sentiment_limit_up_count: int = 50
    # 跌停家数门槛。
    phenomenon_sentiment_limit_down_count: int = 30
    # 炸板家数与涨停家数的最小比例。
    phenomenon_sentiment_broken_ratio: float = 0.35
    # 最高连板门槛（情绪确认条件）。
    phenomenon_sentiment_highest_board: int = 5

    # 现象发现：规则评分与严重度。
    # 规则进入 detected 的最小评分。
    phenomenon_min_match_score: int = 2
    # severity=high 的最小评分。
    phenomenon_high_severity_score: int = 3

    model_config = {
        "env_file": f".env.{os.getenv('APP_ENV', 'development')}",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level_uppercase(cls, v: object) -> str:
        """强制 log_level 为大写，防御 LOG_LEVEL=info 等环境变量小写覆盖。"""
        if isinstance(v, str):
            return v.upper()
        return str(v)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: object) -> object:
        """支持逗号分隔和 JSON 数组两种环境变量格式。

        NoDecode 阻止 pydantic-settings 预先 JSON 解析，原始字符串传入此
        before-validator。此处先尝试 JSON 解析（处理 ["a","b"] 格式），
        失败则按逗号分割（处理 http://a,http://b 格式）。
        """
        if isinstance(v, str):
            # 先尝试 JSON 数组格式（CORS_ORIGINS=["http://a","http://b"]）
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            # 退回逗号分隔格式（CORS_ORIGINS=http://a,http://b）
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @property
    def qa_mode_enabled(self) -> bool:
        """仅接受与 Node 一致的精确 ``QA_MODE=true``。"""
        return self.qa_mode == "true"

    def get_tavily_key(self) -> str:
        """从 API 池中随机选取一个可用的 Tavily Key。

        优先读取 TAVILY_API_KEYS（逗号分隔的多 key 池），
        若未配置则降级到 TAVILY_API_KEY 单 key。
        """
        pool_raw = self.tavily_api_keys.strip()
        if pool_raw:
            keys = [k.strip() for k in pool_raw.split(",") if k.strip()]
            if keys:
                return random.choice(keys)
        return self.tavily_api_key


settings = Settings()
