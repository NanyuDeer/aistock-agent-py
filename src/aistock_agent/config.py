"""应用配置 — pydantic-settings 读取环境变量"""

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

    # Redis
    redis_url: str = "redis://localhost:6379/1"

    # Tavily
    tavily_api_key: str = ""

    # 服务
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # 内网鉴权
    internal_api_token: str = "change-me-in-production"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
