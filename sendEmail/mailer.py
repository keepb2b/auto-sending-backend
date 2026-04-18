# -*- coding: utf-8 -*-
"""SMTP bulk sender used by backend main.send_emails_bulk."""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Same .env as main.py; override=True so backend .env wins over stale shell env (matches main).
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


def _env_str(key: str, default: str = "") -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


class EmailSender:
    def __init__(self) -> None:
        self.host = _env_str("SMTP_HOST", "smtp.gmail.com") or "smtp.gmail.com"
        port_raw = _env_str("SMTP_PORT", "587") or "587"
        try:
            self.port = int(port_raw)
        except ValueError:
            logger.warning("Invalid SMTP_PORT %r; using 587", port_raw)
            self.port = 587
        self.user = _env_str("SMTP_USER")
        self.password = _env_str("SMTP_PASSWORD")
        self.from_addr = _env_str("SMTP_FROM") or self.user

    def _send_sync(self, to_email: str, subject: str, body: str) -> bool:
        if not self.user or not self.password or not to_email:
            return False
        from_addr = self.from_addr or self.user
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = str(Header(subject, "utf-8"))
        msg["From"] = from_addr
        msg["To"] = to_email
        try:
            if self.port == 465:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=90) as smtp:
                    smtp.login(self.user, self.password)
                    smtp.sendmail(from_addr, [to_email], msg.as_string())
            else:
                with smtplib.SMTP(self.host, self.port, timeout=90) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                    smtp.login(self.user, self.password)
                    smtp.sendmail(from_addr, [to_email], msg.as_string())
            return True
        except Exception as e:
            logger.warning(
                "SMTP send failed host=%s port=%s user=%s to=%s: %s",
                self.host,
                self.port,
                self.user or "(empty)",
                to_email,
                e,
            )
            return False

    async def send_email(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        company_name: str = "",
    ) -> bool:
        _ = company_name
        return await asyncio.to_thread(self._send_sync, to_email, subject, body)
