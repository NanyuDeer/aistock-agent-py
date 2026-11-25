"""observability.logging 单测 — structlog 配置与 get_logger

验证：
- setup_logging 配置 JSONRenderer（最后一个 processor）
- setup_logging 配置 TimeStamper（iso 时间戳）
- setup_logging 含 merge_contextvars（支持 request_id 注入）
- get_logger 返回 BoundLogger
- 实际日志输出为 JSON，含 timestamp/level/event 字段
- bind_contextvars 绑定的 request_id 出现在 JSON 输出中（merge_contextvars 生效）
- level 过滤：WARNING 时 debug 不输出，DEBUG 时输出
- 无效 level 回退到 INFO
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest
import structlog

from aistock_agent.observability.logging import get_logger, setup_logging


@pytest.fixture(autouse=True)
def _reset_logging():
    """每个测试前后重置 structlog 全局配置为 INFO，避免互相污染。"""
    setup_logging("INFO")
    yield
    setup_logging("INFO")


def test_setup_logging_uses_json_renderer():
    """setup_logging 后最后一个 processor 是 JSONRenderer"""
    setup_logging("INFO")
    config = structlog.get_config()
    last_processor = config["processors"][-1]
    assert isinstance(last_processor, structlog.processors.JSONRenderer)


def test_setup_logging_includes_timestamper():
    """setup_logging 含 TimeStamper（iso 时间戳）"""
    setup_logging("INFO")
    config = structlog.get_config()
    has_timestamper = any(
        isinstance(p, structlog.processors.TimeStamper)
        for p in config["processors"]
    )
    assert has_timestamper


def test_setup_logging_includes_merge_contextvars():
    """setup_logging 含 merge_contextvars（支持 request_id 等上下文变量）"""
    setup_logging("INFO")
    config = structlog.get_config()
    has_merge = any(
        p is structlog.contextvars.merge_contextvars
        for p in config["processors"]
    )
    assert has_merge


def test_setup_logging_includes_add_log_level():
    """setup_logging 含 add_log_level（输出 level 字段）"""
    setup_logging("INFO")
    config = structlog.get_config()
    has_level = any(
        p is structlog.processors.add_log_level
        for p in config["processors"]
    )
    assert has_level


def test_get_logger_returns_bound_logger():
    """get_logger 返回可调用的 BoundLogger（有 info/debug/warning 方法）"""
    log = get_logger("test_logger")
    assert log is not None
    assert hasattr(log, "info")
    assert hasattr(log, "debug")
    assert hasattr(log, "warning")


def test_log_output_is_json_with_required_fields():
    """实际日志输出为 JSON，含 timestamp/level/event 字段"""
    setup_logging("INFO")
    buf = io.StringIO()
    with redirect_stdout(buf):
        get_logger("test").info("my_event", user_id=42)
    line = buf.getvalue().strip()
    assert line, "expected JSON log output, got empty string"
    data = json.loads(line)
    assert data["event"] == "my_event"
    assert data["level"] == "info"
    assert "timestamp" in data
    assert data["user_id"] == 42


def test_log_output_includes_request_id_from_contextvars():
    """bind_contextvars 绑定的 request_id 出现在 JSON 输出中（merge_contextvars 生效）。

    全局约束要求 structlog JSON 输出含 timestamp/level/event/request_id 字段。
    merge_contextvars 在处理器链中，但需验证：通过 contextvars 绑定的 request_id
    确实被合并进最终 JSON 输出。
    """
    setup_logging("INFO")
    structlog.contextvars.bind_contextvars(request_id="test-123")
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            get_logger("test").info("ctx_event")
        data = json.loads(buf.getvalue().strip())
        assert data["event"] == "ctx_event"
        assert data["request_id"] == "test-123"
    finally:
        structlog.contextvars.clear_contextvars()


def test_log_output_for_warning_level():
    """warning 级别日志输出 level=warning"""
    setup_logging("INFO")
    buf = io.StringIO()
    with redirect_stdout(buf):
        get_logger("test").warning("warn_event")
    data = json.loads(buf.getvalue().strip())
    assert data["event"] == "warn_event"
    assert data["level"] == "warning"


def test_level_filter_warning_hides_debug():
    """WARNING 级别时 debug 日志被过滤（不输出）"""
    setup_logging("WARNING")
    buf = io.StringIO()
    with redirect_stdout(buf):
        get_logger("test").debug("debug_event")
    assert buf.getvalue() == ""


def test_level_filter_debug_shows_debug():
    """DEBUG 级别时 debug 日志正常输出"""
    setup_logging("DEBUG")
    buf = io.StringIO()
    with redirect_stdout(buf):
        get_logger("test").debug("debug_event")
    data = json.loads(buf.getvalue().strip())
    assert data["event"] == "debug_event"
    assert data["level"] == "debug"


def test_invalid_level_falls_back_to_info():
    """无效 level 字符串回退到 INFO（不抛异常）"""
    setup_logging("NOT_A_LEVEL")
    # INFO 级别：info 应正常输出
    buf = io.StringIO()
    with redirect_stdout(buf):
        get_logger("test").info("still_works")
    data = json.loads(buf.getvalue().strip())
    assert data["event"] == "still_works"


def test_log_output_includes_traceback_for_exc_info():
    """logger.exception 时输出真实 traceback（format_exc_info 生效，回归：曾只留 exc_info=true）"""
    setup_logging("INFO")
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            raise ValueError("boom")
        except ValueError:
            get_logger("test").exception("err_event")
    line = buf.getvalue().strip()
    assert line
    data = json.loads(line)
    assert data["event"] == "err_event"
    assert data["level"] == "error"
    rendered = json.dumps(data, ensure_ascii=False)
    assert "Traceback" in rendered
    assert "ValueError: boom" in rendered


def test_setup_logging_is_idempotent():
    """多次调用 setup_logging 不抛异常，配置以最后一次为准"""
    setup_logging("DEBUG")
    setup_logging("INFO")
    setup_logging("WARNING")
    # 最终为 WARNING：debug 不输出
    buf = io.StringIO()
    with redirect_stdout(buf):
        get_logger("test").debug("should_be_filtered")
    assert buf.getvalue() == ""
