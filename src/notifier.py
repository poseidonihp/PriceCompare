from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import requests

from .config import EmailConfig, TelegramConfig

logger = logging.getLogger(__name__)


def send_telegram(message: str, config: TelegramConfig) -> bool:
    if not config.enabled:
        return False
    url = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    payload = {
        "chat_id": config.chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Error enviando Telegram: %s", exc)
        return False
    return True


def send_email(subject: str, body_markdown: str, config: EmailConfig, body_html: str | None = None) -> bool:
    if not config.enabled:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.from_addr
    msg["To"] = config.to_addr
    msg.set_content(body_markdown)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as server:
            server.starttls()
            server.login(config.from_addr, config.app_password)
            server.send_message(msg)
    except Exception as exc:
        logger.error("Error enviando email: %s", exc)
        return False
    return True
