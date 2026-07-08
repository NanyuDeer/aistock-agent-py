"""应用配置 — pydantic-settings 读取环境变量"""

import json
import random
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    """全局配置，从 .env 或环境变量读取"""

    # Node.js 后端地址
    node_api_base_url: str = "http://localhost:3000"

    # LLM
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    quick_think_model: str = "gpt-4o-mini"
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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

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
