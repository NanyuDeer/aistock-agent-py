"""应用配置 — pydantic-settings 读取环境变量"""

import random

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """全局配置，从 .env 或环境变量读取"""

    # Node.js 后端地址
    node_api_base_url: str = "http://localhost:3000"

    # LLM
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    quick_think_model: str = "gpt-4o-mini"
    deep_think_model: str = "gpt-4o"

    # Redis（与 Node.js 共用实例，db=1 避免 key 冲突）
    redis_url: str = "redis://localhost:6379/1"

    # Tavily API 池（逗号分隔，支持多成员共享额度）
    # 优先使用 TAVILY_API_KEYS；若只有单人，用 TAVILY_API_KEY 兼容
    tavily_api_key: str = ""
    tavily_api_keys: str = ""

    # 服务（Python 8000 / Node.js 3000）
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # 内网鉴权
    internal_api_token: str = "change-me-in-production"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

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
