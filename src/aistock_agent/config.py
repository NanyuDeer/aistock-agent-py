"""应用配置 — pydantic-settings 读取环境变量"""

import json
import os
import random
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode


def _parse_string_list(v: object) -> object:
    """支持逗号分隔和 JSON 数组两种环境变量格式（cors_origins / holidays_extra 共用）。

    NoDecode 阻止 pydantic-settings 预先 JSON 解析，原始字符串传入 before-validator。
    此处先尝试 JSON 解析（处理 ["a","b"] 格式），失败则按逗号分割（处理 a,b 格式）。
    """
    if isinstance(v, str):
        # 先尝试 JSON 数组格式（["a","b"]）
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        # 退回逗号分隔格式（a,b）
        return [item.strip() for item in v.split(",") if item.strip()]
    return v


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
    # Embedding — 行业语义匹配（对齐硬约束：必须独立配置，禁用 LLM 端点做 embedding）
    # 未配置时仅 fallback 到 openai_*（供测试）；生产必须显式配置支持 embedding 的服务。
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "text-embedding-3-small"
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
    # 自选股洞察上线后默认停用旧 stock_trace consumer；
    # STOCK_TRACE_CONSUMER_ENABLED=true 可临时恢复。
    stock_trace_consumer_enabled: bool = False
    # Tool calling provides a schema-bound response across OpenAI-compatible models.
    stock_trace_structured_output_method: Literal[
        "function_calling", "json_mode", "json_schema"
    ] = "function_calling"

    # ===== 自选股洞察（watchlist insight）=====
    # 独立 Redis db，与 stock_trace db=2 隔离
    insight_redis_url: str = "redis://localhost:6379/3"
    insight_consumer_group: str = "watchlist-insight-workers"
    insight_consumer_block_ms: int = 1000
    # Pending 消息达到该空闲时间后，可由任意 Consumer 接管重试。
    insight_pending_claim_idle_ms: int = 3000
    insight_max_attempts: int = 3
    # Consumer 集成模式开关：true=在主进程 lifespan 内启动（同 stock_trace）
    insight_consumer_enabled: bool = True
    # 归因 LLM 受约束选择用 json_mode 结构化输出（PRD 第 9 节）
    insight_structured_output_method: Literal[
        "function_calling", "json_mode", "json_schema"
    ] = "json_mode"
    # 主因 label 主题概括关键词长度上限（PRD：建议 ≤12 字）
    insight_label_max_chars: int = 12
    # 归因任务轻，默认 quick_think
    insight_llm_model: Literal["quick_think", "deep_think"] = "quick_think"
    # 午盘触发后补抓窗口（分钟，PRD §8：15-20 分钟；Node 侧 cron 使用）
    insight_refetch_minutes: int = 20

    # HTTP 超时（main.py lifespan 传给 HttpClientPool.init）
    http_timeout_seconds: float = 10.0

    # Tavily API 池（逗号分隔，支持多成员共享额度）
    # 优先使用 TAVILY_API_KEYS；若只有单人，用 TAVILY_API_KEY 兼容
    tavily_api_key: str = ""
    tavily_api_keys: str = ""

    # 抖音视频转写（硅基流动 SenseVoice；E:/changer_learning 已验证）
    douyin_api_key: str = ""
    # 可选：显式指定 ffmpeg/ffprobe 路径（默认走 PATH 查找）
    ffmpeg_binary: str = ""
    ffprobe_binary: str = ""

    # 服务（Python 8080 / Node.js 3000）
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    # 内网鉴权
    internal_api_token: str = "change-me-in-production"

    # CORS 允许的源列表（api/middleware.py setup_middleware 读取）
    # 支持逗号分隔（CORS_ORIGINS=http://a,http://b）或 JSON 数组格式（CORS_ORIGINS=["a","b"]）
    # NoDecode 阻止 pydantic-settings 预先 JSON 解析，交给 _parse_cors_origins 统一处理
    cors_origins: Annotated[list[str], NoDecode] = ["*"]

    # 补充节假日表（HOLIDAYS_EXTRA，YYYY-MM-DD 逗号分隔或 JSON 数组，复用 cors_origins 解析模式）
    # 用途：chinese_calendar 1.11.0 仅覆盖 2004-2026，2027 起 is_trading_day 走越年 fallback
    # （只跳周末，精度损失）。此列表提供覆盖范围之外的补充休市日，让越年判定恢复精度。
    # 空列表时 is_trading_day 行为与拆分前逐字节一致。
    holidays_extra: Annotated[list[str], NoDecode] = []

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
    # sqlite busy_timeout（秒；sqlite3 默认 5.0）。多 worker 并发写短暂争用时
    # sqlite 抛 "database is locked"；30s 覆盖争用窗口（Phase 5 Task 3 低成本先行项）。
    sqlite_busy_timeout: float = 30.0

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

    # ===== 迭代 Agent 自动闭环（iterate）=====
    # 默认关闭；服务器沙盒 .env 设 ITERATE_ENABLED=true 开启
    iterate_enabled: bool = False
    # 数据目录（切片/标准答案/实验/报告，均 gitignore）
    iterate_data_dir: str = "data"
    # 每日消费/报告：工作日 17:00（产片 16:30 之后；错开 16:00 prediction_validate）
    iterate_cron: str = "0 17 * * 1-5"
    # 产片：工作日 16:30（收盘快照 15:35 之后；错开 16:00 prediction_validate）
    iterate_case_build_cron: str = "30 16 * * 1-5"
    iterate_max_rounds: int = 5            # 每案例变体轮数上限
    iterate_target_score: float = 0.8      # 归因相似度达标值
    iterate_max_daily_cases: int = 3       # 每日消费历史案例上限
    iterate_round_timeout_seconds: int = 600  # 每轮实验子进程超时
    # C-3（2026-08-14）：禁止作为迭代仓库根的路径黑名单（fail-closed），
    # 防止对非 git 目录恢复基线失败后静默污染
    iterate_forbidden_repo_roots: list[str] = []
    # A-1（2026-08-14）：judge 上线前校准闸门（默认关闭）——开启时
    # calibration.calibration_passed() 必须达标（命中率 >= 0.8）
    iterate_calibration_required: bool = False
    # ── 五期校准：no_improvement 终止（裁决书 D4/N3 落地）──
    # delta 未配置（None）→ 禁用（现状：stalled 仅观测）；compute_delta.py 校准后
    # 配置 ITERATE_NO_IMPROVE_DELTA 启用。
    iterate_no_improve_delta: float | None = None
    no_improve_max_stalls: int = 4
    # SMTP 报告（QQ 邮箱授权码；2026-08-14 加 QQ_SMTP_* 别名——.env.development
    # 用 QQ_SMTP_USER/AUTH/TO 键名，与字段名不匹配导致 settings 读不到 →
    # mail_not_configured，报告无法发送；LLM key 正常因 OPENAI_API_KEY 恰好匹配）
    iterate_smtp_host: str = ""
    iterate_smtp_port: int = 465
    iterate_smtp_user: str = Field(
        default="", validation_alias=AliasChoices("QQ_SMTP_USER", "ITERATE_SMTP_USER")
    )
    iterate_smtp_password: str = Field(
        default="",
        validation_alias=AliasChoices("QQ_SMTP_AUTH", "ITERATE_SMTP_PASSWORD"),
    )
    iterate_mail_to: str = Field(
        default="", validation_alias=AliasChoices("QQ_SMTP_TO", "ITERATE_MAIL_TO")
    )
    # ---- evening_chain 事件驱动重构（spec: 2026-07-29）----
    # quick review：15:30 收盘后基于腾讯实时行情立即产出
    scheduler_review_quick_cron: str = "30 15 * * 1-5"
    # full review：20:30 Tushare 完整数据覆盖 quick
    scheduler_review_full_cron: str = "30 20 * * 1-5"
    scheduler_prediction_validate_cron: str = "0 16 * * 1-5"  # 预测到期验证：工作日 16:00
    # 预测验证统计出口（D3，与验证解耦独立调度）：16:05 验证落库后汇总命中率/baseline
    scheduler_prediction_stats_cron: str = "5 16 * * 1-5"
    # ── 统一事件抓取中台调度（2026-08-12；2026-08-13 盘前全量 07:30→08:45） ──
    scheduler_event_scrape_cron: str = "45 8 * * 1-5"  # 盘前档：08:45 全量（紧邻晨报 08:50）
    scheduler_event_scrape_intraday_cron: str = (
        "0 10-14 * * 1-5"  # 盘中档：10:00-14:00 每小时（含 12:00，午间公告/新闻增量）
    )
    scheduler_event_scrape_early_cron: str = (
        "45 8 * * 1-5"  # 早间刷新：08:45（晨报 08:50 前最后一刷，与盘前档合并）
    )
    scheduler_event_scrape_close_cron: str = "5 15 * * 1-5"   # 收盘汇总：15:05（复盘/播报消费）
    # ── 事件抓取中台 LLM 评分（Phase-2，2026-08-13） ──
    event_scoring_llm_enabled: bool = False          # 总开关（默认关闭灰度开启）
    event_scoring_candidate_threshold: int = 3       # 规则评分候选门槛（>=3 送 LLM）
    event_scoring_quick_batch_size: int = 20         # quick_think 批量粗筛每批条数
    event_scoring_cache_ttl: int = 86400             # 评分缓存 TTL（秒，24h）
    # ── GI 盘中纯增量更新（2026-08-14） ──
    gi_incremental_enabled: bool = False             # 总开关（默认关闭灰度开启）
    # 每日 quick_think 比较次数上限（达上限后仅规则判断）
    gi_max_llm_calls_per_day: int = 10
    gi_compare_epsilon: float = 0.1                  # 代理分接近阈值（|Δ|<=ε 触发 LLM 决胜）
    gi_top_k: int = 3                                # 每方向 Top-K 候选池大小
    # gi_state:{date} Redis TTL（秒，当日 24:00 过期）
    gi_state_ttl: int = 86400
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

    # 事件分析流水线（event_analysis_pipeline）整体超时（秒）。
    # 覆盖 Event Conduction → Global Importance 全链路，超时记录日志但不中断 broadcast。
    event_analysis_pipeline_timeout_seconds: int = 900

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
        return _parse_string_list(v)

    @field_validator("holidays_extra", mode="before")
    @classmethod
    def _parse_holidays_extra(cls, v: object) -> object:
        """HOLIDAYS_EXTRA 解析：复用 cors_origins 的逗号分隔/JSON 数组模式。

        补充节假日表（YYYY-MM-DD 列表）：HOLIDAYS_EXTRA=2027-01-01,2027-10-01
        或 HOLIDAYS_EXTRA=["2027-01-01","2027-10-01"]。
        """
        return _parse_string_list(v)

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
