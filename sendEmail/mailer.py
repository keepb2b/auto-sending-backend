# -*- coding: utf-8 -*-
"""SMTP bulk sender used by backend main.send_emails_bulk."""
from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


class EmailSender:
    def __init__(self) -> None:
        self.host = (os.getenv("SMTP_HOST") or "smtp.gmail.com").strip()
        self.port = int(os.getenv("SMTP_PORT") or "587")
        self.user = (os.getenv("SMTP_USER") or "").strip()
        self.password = (os.getenv("SMTP_PASSWORD") or "").strip()
        self.from_addr = (os.getenv("SMTP_FROM") or self.user).strip()

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
        except Exception:
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
