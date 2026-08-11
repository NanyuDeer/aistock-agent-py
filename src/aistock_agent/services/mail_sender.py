"""mail_sender —— 通用 QQ 邮箱 SMTP 邮件发送（复用已验证模式）。

对接同事交接的 QQ 邮箱发送流程（smtp.qq.com:465 SSL + 授权码 + HTML 正文
+ 附件 RFC 2231 中文文件名），仅用 Python 标准库（smtplib + email）。

配置解析顺序（任一空则回退下一级）：
1. 显式参数（send_mail 的 smtp_* / to_addr 覆盖）
2. settings.iterate_smtp_*（迭代闭环既有配置）
3. 环境变量 QQ_SMTP_USER / QQ_SMTP_AUTH / QQ_SMTP_TO（同事交接约定）
host/port 缺省回退 smtp.qq.com:465。
"""

import os
import smtplib
import time
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import cast

import structlog

from aistock_agent.config import settings

logger = structlog.get_logger()

#: 附件 MIME 类型映射（缺省 application/octet-stream 会导致附件变 .bin）
_MIME_TYPES: dict[str, tuple[str, str]] = {
    ".xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".xls": ("application", "vnd.ms-excel"),
    ".pdf": ("application", "pdf"),
    ".csv": ("text", "csv"),
    ".zip": ("application", "zip"),
}

_SMTP_RETRIES = 3
_SMTP_RETRY_DELAY_SECONDS = 2


def resolve_mail_config(
    *,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_user: str | None = None,
    smtp_auth: str | None = None,
    to_addr: str | None = None,
) -> tuple[str, int, str, str, str]:
    """解析 SMTP 配置，返回 (host, port, user, auth, to_addr)。

    显式参数优先，其次 settings.iterate_smtp_*，最后环境变量 QQ_SMTP_*。
    host/port 完全缺省时回退 smtp.qq.com:465。
    """
    host = smtp_host or settings.iterate_smtp_host or "smtp.qq.com"
    port = smtp_port or settings.iterate_smtp_port or 465
    user = smtp_user or settings.iterate_smtp_user or os.environ.get("QQ_SMTP_USER", "")
    auth = smtp_auth or settings.iterate_smtp_password or os.environ.get("QQ_SMTP_AUTH", "")
    to_addr = to_addr or settings.iterate_mail_to or os.environ.get("QQ_SMTP_TO", "")
    return host, port, user, auth, to_addr


def send_mail(
    subject: str,
    body_html: str,
    *,
    attachments: tuple[str, ...] = (),
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_user: str | None = None,
    smtp_auth: str | None = None,
    to_addr: str | None = None,
) -> bool:
    """发送 HTML 邮件（可选附件）；失败重试 3 次；配置缺失返回 False。"""
    host, port, user, auth, to_addr_final = resolve_mail_config(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_auth=smtp_auth,
        to_addr=to_addr,
    )
    if not (user and auth and to_addr_final):
        logger.warning("mail_not_configured")
        return False

    msg = _build_message(user, to_addr_final, subject, body_html, attachments)

    for attempt in range(1, _SMTP_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                server.login(user, auth)
                server.sendmail(user, [to_addr_final], msg.as_string())
            logger.info("mail_sent", subject=subject)
            return True
        except (OSError, smtplib.SMTPException) as exc:  # noqa: PERF203
            logger.warning("mail_smtp_failed", attempt=attempt, error=str(exc))
            if attempt < _SMTP_RETRIES:
                time.sleep(_SMTP_RETRY_DELAY_SECONDS)
    return False


def _build_message(
    user: str,
    to_addr: str,
    subject: str,
    body_html: str,
    attachments: tuple[str, ...],
) -> MIMEMultipart:
    """构造 MIMEMultipart：HTML 正文 + 附件（MIME 映射 + RFC 2231 中文文件名）。"""
    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = cast("str", Header(subject, "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    for path_str in attachments:
        path = Path(path_str)
        if not path.exists():
            logger.warning("mail_attachment_missing", path=str(path))
            continue
        main_type, sub_type = _MIME_TYPES.get(path.suffix.lower(), ("application", "octet-stream"))
        with path.open("rb") as f:
            part = MIMEBase(main_type, sub_type)
            part.set_payload(f.read())
            encoders.encode_base64(part)
        # RFC 2231：中文文件名必须用 tuple 形式，否则乱码 / 丢失
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", path.name))
        msg.attach(part)
    return msg
