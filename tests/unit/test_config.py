"""config.py 配置测试 — 验证新增字段默认值 + env 覆盖 + llm.py 读取配置

测试覆盖：
- 新增配置字段的默认值
- 环境变量覆盖默认值
- get_quick_think / get_deep_think 从 config 读取 temperature/max_tokens
- get_quick_think 在 env 覆盖时读取覆盖值（验收标准）
"""

from unittest.mock import patch

import pytest

from aistock_agent.config import Settings


# =============================================================================
# 默认值测试
# =============================================================================


class TestConfigDefaults:
    """验证新增字段的默认值"""

    def test_quick_think_temperature_default(self):
        s = Settings()
        assert s.quick_think_temperature == 0.1

    def test_quick_think_max_tokens_default(self):
        s = Settings()
        assert s.quick_think_max_tokens == 2000

    def test_deep_think_temperature_default(self):
        s = Settings()
        assert s.deep_think_temperature == 0.3

    def test_deep_think_max_tokens_default(self):
        s = Settings()
        assert s.deep_think_max_tokens == 4000

    def test_redis_max_connections_default(self):
        s = Settings()
        assert s.redis_max_connections == 10

    def test_http_timeout_seconds_default(self):
        s = Settings()
        assert s.http_timeout_seconds == 10.0

    def test_langsmith_enabled_default(self):
        s = Settings()
        assert s.langsmith_enabled is False

    def test_langsmith_api_key_default(self):
        s = Settings()
        assert s.langsmith_api_key is None

    def test_langsmith_project_default(self):
        s = Settings()
        assert s.langsmith_project == "aistock-agent"

    def test_node_api_base_url_default(self):
        """node_api_base_url 已存在，验证默认值不变"""
        s = Settings()
        assert s.node_api_base_url == "http://localhost:3000"

    def test_log_level_exists(self):
        """log_level 已存在，验证可访问；默认值必须为大写 INFO。

        Python 的 logging.basicConfig(level=...) 对级别名大小写敏感，
        小写 "info" 会导致级别解析失败（logging.getLevelName("info") 返回字符串而非整数）。
        Task 4 的 structlog 配置也期望大写级别名，故默认值必须为 "INFO"。
        """
        s = Settings()
        assert isinstance(s.log_level, str)
        assert s.log_level == "INFO"


# =============================================================================
# 环境变量覆盖测试
# =============================================================================


class TestConfigEnvOverride:
    """验证环境变量可覆盖默认值"""

    def test_quick_think_temperature_override(self, monkeypatch):
        monkeypatch.setenv("QUICK_THINK_TEMPERATURE", "0.5")
        assert Settings().quick_think_temperature == 0.5

    def test_quick_think_max_tokens_override(self, monkeypatch):
        monkeypatch.setenv("QUICK_THINK_MAX_TOKENS", "3000")
        assert Settings().quick_think_max_tokens == 3000

    def test_deep_think_temperature_override(self, monkeypatch):
        monkeypatch.setenv("DEEP_THINK_TEMPERATURE", "0.7")
        assert Settings().deep_think_temperature == 0.7

    def test_deep_think_max_tokens_override(self, monkeypatch):
        monkeypatch.setenv("DEEP_THINK_MAX_TOKENS", "8000")
        assert Settings().deep_think_max_tokens == 8000

    def test_redis_max_connections_override(self, monkeypatch):
        monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "50")
        assert Settings().redis_max_connections == 50

    def test_http_timeout_seconds_override(self, monkeypatch):
        monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "30.0")
        assert Settings().http_timeout_seconds == 30.0

    def test_langsmith_enabled_override(self, monkeypatch):
        monkeypatch.setenv("LANGSMITH_ENABLED", "true")
        assert Settings().langsmith_enabled is True

    def test_langsmith_api_key_override(self, monkeypatch):
        monkeypatch.setenv("LANGSMITH_API_KEY", "ls_xxx")
        assert Settings().langsmith_api_key == "ls_xxx"

    def test_langsmith_project_override(self, monkeypatch):
        monkeypatch.setenv("LANGSMITH_PROJECT", "my-project")
        assert Settings().langsmith_project == "my-project"


# =============================================================================
# LLM 工厂读取配置测试
# =============================================================================


class TestLlmFactoryReadsConfig:
    """验证 get_quick_think / get_deep_think 从 config 读取 temperature/max_tokens"""

    def test_get_quick_think_passes_config_temperature(self):
        """get_quick_think 将 settings.quick_think_temperature 传给 ChatOpenAI"""
        from aistock_agent.services.llm import get_quick_think

        with patch("aistock_agent.services.llm.ChatOpenAI") as mock_chat, \
             patch("aistock_agent.services.llm.settings") as mock_settings:
            mock_settings.quick_think_model = "gpt-4o-mini"
            mock_settings.openai_api_key = "sk-test"
            mock_settings.openai_base_url = "https://api.openai.com/v1"
            mock_settings.quick_think_temperature = 0.1
            mock_settings.quick_think_max_tokens = 2000

            get_quick_think()

            mock_chat.assert_called_once()
            _, kwargs = mock_chat.call_args
            assert kwargs["temperature"] == 0.1
            assert kwargs["max_tokens"] == 2000

    def test_get_deep_think_passes_config_temperature(self):
        """get_deep_think 将 settings.deep_think_temperature 传给 ChatOpenAI"""
        from aistock_agent.services.llm import get_deep_think

        with patch("aistock_agent.services.llm.ChatOpenAI") as mock_chat, \
             patch("aistock_agent.services.llm.settings") as mock_settings:
            mock_settings.deep_think_model = "gpt-4o"
            mock_settings.openai_api_key = "sk-test"
            mock_settings.openai_base_url = "https://api.openai.com/v1"
            mock_settings.deep_think_temperature = 0.3
            mock_settings.deep_think_max_tokens = 4000

            get_deep_think()

            mock_chat.assert_called_once()
            _, kwargs = mock_chat.call_args
            assert kwargs["temperature"] == 0.3
            assert kwargs["max_tokens"] == 4000

    def test_get_quick_think_env_override_temperature(self, monkeypatch):
        """验收标准: env QUICK_THINK_TEMPERATURE=0.5 → ChatOpenAI temperature=0.5"""
        monkeypatch.setenv("QUICK_THINK_TEMPERATURE", "0.5")
        test_settings = Settings()

        from aistock_agent.services.llm import get_quick_think

        with patch("aistock_agent.services.llm.settings", test_settings), \
             patch("aistock_agent.services.llm.ChatOpenAI") as mock_chat:
            get_quick_think()

            _, kwargs = mock_chat.call_args
            assert kwargs["temperature"] == 0.5

    def test_get_quick_think_env_override_max_tokens(self, monkeypatch):
        """env QUICK_THINK_MAX_TOKENS=5000 → ChatOpenAI max_tokens=5000"""
        monkeypatch.setenv("QUICK_THINK_MAX_TOKENS", "5000")
        test_settings = Settings()

        from aistock_agent.services.llm import get_quick_think

        with patch("aistock_agent.services.llm.settings", test_settings), \
             patch("aistock_agent.services.llm.ChatOpenAI") as mock_chat:
            get_quick_think()

            _, kwargs = mock_chat.call_args
            assert kwargs["max_tokens"] == 5000

    def test_get_deep_think_env_override_temperature(self, monkeypatch):
        """env DEEP_THINK_TEMPERATURE=0.9 → ChatOpenAI temperature=0.9"""
        monkeypatch.setenv("DEEP_THINK_TEMPERATURE", "0.9")
        test_settings = Settings()

        from aistock_agent.services.llm import get_deep_think

        with patch("aistock_agent.services.llm.settings", test_settings), \
             patch("aistock_agent.services.llm.ChatOpenAI") as mock_chat:
            get_deep_think()

            _, kwargs = mock_chat.call_args
            assert kwargs["temperature"] == 0.9
