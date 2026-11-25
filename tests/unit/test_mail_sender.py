"""mail_sender —— 配置解析、SMTP 发送、重试与附件 RFC 2231"""

from email.mime.multipart import MIMEMultipart
from unittest.mock import patch

import pytest

from aistock_agent.config import settings
from aistock_agent.services.mail_sender import _build_message, resolve_mail_config, send_mail


@pytest.fixture
def _isolate_smtp_settings(monkeypatch: object) -> None:
    """隔离测试对 settings/env 的修改，避免泄漏到其他用例。"""
    for key in ("QQ_SMTP_USER", "QQ_SMTP_AUTH", "QQ_SMTP_TO"):
        monkeypatch.delenv(key, raising=False)  # type: ignore[attr-defined]


def test_resolve_mail_config_prefers_iterate_settings(
    _isolate_smtp_settings: object, monkeypatch: object
) -> None:
    original = (settings.iterate_smtp_host, settings.iterate_smtp_user, settings.iterate_mail_to)
    try:
        settings.iterate_smtp_host = "smtp.example.com"
        settings.iterate_smtp_user = "iter@example.com"
        settings.iterate_mail_to = "to@example.com"
        monkeypatch.setenv("QQ_SMTP_USER", "qq@example.com")  # type: ignore[attr-defined]
        host, port, user, auth, to_addr = resolve_mail_config(smtp_auth="auth-key")
        assert host == "smtp.example.com"
        assert user == "iter@example.com"
        assert to_addr == "to@example.com"
        assert port == settings.iterate_smtp_port
    finally:
        settings.iterate_smtp_host, settings.iterate_smtp_user, settings.iterate_mail_to = original


def test_resolve_mail_config_falls_back_to_qq_env(monkeypatch: object) -> None:
    monkeypatch.setenv("QQ_SMTP_USER", "qq@example.com")  # type: ignore[attr-defined]
    monkeypatch.setenv("QQ_SMTP_AUTH", "secret-auth")
    monkeypatch.setenv("QQ_SMTP_TO", "boss@example.com")
    host, port, user, auth, to_addr = resolve_mail_config()
    assert user == "qq@example.com"
    assert auth == "secret-auth"
    assert to_addr == "boss@example.com"
    # 完全缺省时回退 smtp.qq.com:465
    assert host == "smtp.qq.com"
    assert port == 465


def test_send_mail_unconfigured_returns_false(_isolate_smtp_settings: object) -> None:
    settings.iterate_smtp_user = ""
    settings.iterate_mail_to = ""
    assert send_mail("t", "<p>hi</p>") is False


def test_send_mail_success(_isolate_smtp_settings: object) -> None:
    settings.iterate_smtp_user = "iter@example.com"
    settings.iterate_smtp_password = "auth"
    settings.iterate_mail_to = "to@example.com"
    with patch("aistock_agent.services.mail_sender.smtplib.SMTP_SSL") as mock_smtp:
        assert send_mail("主题", "<p>正文</p>") is True
    mock_smtp.assert_called_once()


def test_send_mail_retries_then_fails(_isolate_smtp_settings: object) -> None:
    settings.iterate_smtp_user = "iter@example.com"
    settings.iterate_smtp_password = "auth"
    settings.iterate_mail_to = "to@example.com"
    with patch(
        "aistock_agent.services.mail_sender.smtplib.SMTP_SSL",
        side_effect=OSError("conn"),
    ) as mock_smtp:
        assert send_mail("主题", "<p>正文</p>") is False
    assert mock_smtp.call_count == 3


def test_build_message_html_body_and_attachment_rfc2231(tmp_path: object) -> None:
    """HTML 正文 + 附件 MIME 类型 + 中文文件名 RFC 2231（对齐同事交接的踩坑点）。"""

    from email.mime.text import MIMEText

    attach = tmp_path / "每日报告.md"
    attach.write_text("# 报告", encoding="utf-8")

    msg = _build_message(
        user="iter@example.com",
        to_addr="to@example.com",
        subject="主题",
        body_html="<p>正文</p>",
        attachments=(str(attach),),
    )
    assert isinstance(msg, MIMEMultipart)
    parts = msg.get_payload()
    # 正文 HTML
    assert any(isinstance(p, MIMEText) and p.get_content_type() == "text/html" for p in parts)
    # 附件带正确 Content-Disposition（RFC 2231 filename*）
    disposition = next(
        str(p.get("Content-Disposition")) for p in parts if p.get_content_type() != "text/html"
    )
    assert "attachment" in disposition
    assert "filename*=" in disposition  # RFC 2231 编码的中文文件名
