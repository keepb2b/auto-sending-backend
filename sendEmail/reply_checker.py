# -*- coding: utf-8 -*-
"""
Fetch inbound mail via IMAP, match senders to companies, insert into `replies` (Supabase/Postgres),
and mark matched companies as 返信あり.

Env (backend/.env):
  DATABASE_URL     — required (same as main app)
  IMAP_HOST        — default imap.gmail.com
  IMAP_PORT        — default 993
  IMAP_USER        — default SMTP_USER
  IMAP_PASSWORD    — default SMTP_PASSWORD
  IMAP_MAILBOX     — default INBOX
  reply_fetch_days / REPLY_FETCH_DAYS — how far back to search (default 14; use 1 for recent mail)
  SMTP_FROM / SMTP_USER — used to skip messages from our own address
  REPLY_IMPORT_REQUIRE_PRIOR_SEND — default 1/true: only import when sender matches a company row whose
    status is メール送信済み or 返信あり (or Sent/Replied). Set 0/false to import any inbox mail from
    addresses that exist in companies (old behavior).
"""
from __future__ import annotations

import hashlib
import html
import imaplib
import os
import re
import ssl
from datetime import datetime, timezone, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr
from pathlib import Path
from typing import Any

import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_since_str(days: int) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    return f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}"


def _decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw or ""


def _normalize_email(addr: str) -> str:
    return (addr or "").strip().lower()


def _env_truthy(name: str, default: bool = True) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _company_eligible_for_reply_import(status: str | None) -> bool:
    """True if we already sent this lead an email from the app (or they already replied once)."""
    if not status:
        return False
    s = str(status).strip()
    if s in ("メール送信済み", "返信あり"):
        return True
    return s.lower() in ("sent", "replied")


def _is_auto_generated_reply(msg) -> bool:
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto not in ("no", "none"):
        return True
    xar = (msg.get("X-Autoreply") or "").strip().lower()
    if xar in ("yes", "true", "1"):
        return True
    return False


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# Gmail / Apple Mail / many clients (English UI): "On Sat, Apr 4, 2026 at 1:47 AM <x@y.com> wrote:"
_GMAIL_ON_WROTE = re.compile(
    r"(?:^|[\s\r\n])On (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+.+?\s+wrote:\s*",
    re.IGNORECASE | re.DOTALL,
)
# Same pattern with numeric dates (some locales)
_ON_NUMDATE_WROTE = re.compile(
    r"(?:^|[\s\r\n])On \d{1,2}[./-]\d{1,2}[./-]\d{2,4}.+?\s+wrote:\s*",
    re.IGNORECASE | re.DOTALL,
)


def _strip_reply_quotations(text: str) -> str:
    """Keep only the new reply; drop quoted thread (Gmail 'On … wrote:', Outlook blocks, >-quoted lines)."""
    if not text:
        return text
    t = text.strip()

    cut = t.find("-----Original Message-----")
    if cut != -1:
        t = t[:cut].strip()

    m = _GMAIL_ON_WROTE.search(t)
    if m:
        t = t[: m.start()].strip()
    else:
        m2 = _ON_NUMDATE_WROTE.search(t)
        if m2:
            t = t[: m2.start()].strip()

    lines = t.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and lines[-1].lstrip().startswith(">"):
        lines.pop()
    t = "\n".join(lines).strip()

    lines = t.splitlines()
    while lines and lines[0].lstrip().startswith(">"):
        lines.pop(0)
    return "\n".join(lines).strip()


def _extract_body(msg) -> str:
    plain = ""
    html_part = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html_part:
                html_part = text
        if plain.strip():
            return plain
        return _html_to_text(html_part) if html_part else ""
    ctype = msg.get_content_type()
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except Exception:
        text = payload.decode("utf-8", errors="replace")
    if ctype == "text/html":
        return _html_to_text(text)
    return text


def _message_stable_id(msg, from_addr: str, subject: str, raw_msg: bytes) -> str:
    mid = (msg.get("Message-ID") or "").strip()
    if mid:
        return mid
    h = hashlib.sha256(f"{from_addr}|{subject}|{len(raw_msg)}".encode("utf-8", errors="replace")).hexdigest()
    return f"local-{h}"


def _parse_received_at(msg) -> datetime:
    for key in ("Date",):
        raw = msg.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            continue
    return datetime.utcnow()


def _ensure_replies_schema(cur) -> None:
    cur.execute("ALTER TABLE replies ADD COLUMN IF NOT EXISTS message_id TEXT;")


def fetch_and_store_replies() -> dict[str, Any]:
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        return {"ok": False, "error": "DATABASE_URL not set", "imported": 0, "skipped": 0}

    host = (os.getenv("IMAP_HOST") or "imap.gmail.com").strip()
    port = int(os.getenv("IMAP_PORT") or "993")
    user = (os.getenv("IMAP_USER") or os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("IMAP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip()
    mailbox = (os.getenv("IMAP_MAILBOX") or "INBOX").strip() or "INBOX"
    days = int(
        os.getenv("reply_fetch_days") or os.getenv("REPLY_FETCH_DAYS") or "14"
    )

    our_addrs = {
        _normalize_email(os.getenv("SMTP_FROM") or ""),
        _normalize_email(os.getenv("SMTP_USER") or ""),
        _normalize_email(user),
    }
    our_addrs.discard("")

    if not user or not password:
        return {
            "ok": False,
            "error": "IMAP_USER/IMAP_PASSWORD (or SMTP_USER/SMTP_PASSWORD) not set",
            "imported": 0,
            "skipped": 0,
        }

    since = _imap_since_str(days)
    imported = 0
    skipped = 0
    errors: list[str] = []

    try:
        ctx = ssl.create_default_context()
        imap = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        imap.login(user, password)
        imap.select(mailbox, readonly=True)
        status, data = imap.search(None, f"(SINCE {since})")
        if status != "OK" or not data or not data[0]:
            imap.logout()
            conn = psycopg2.connect(dsn)
            try:
                cur = conn.cursor()
                _ensure_replies_schema(cur)
                conn.commit()
            finally:
                conn.close()
            return {"ok": True, "imported": 0, "skipped": 0, "message": "No messages in range"}

        ids = data[0].split()
        # Cap work per run
        ids = ids[-200:] if len(ids) > 200 else ids

        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        _ensure_replies_schema(cur)
        conn.commit()

        for num in ids:
            try:
                st, part = imap.fetch(num, "(RFC822)")
                if st != "OK" or not part:
                    skipped += 1
                    continue
                raw = None
                for response_part in part:
                    if isinstance(response_part, tuple) and len(response_part) >= 2:
                        raw = response_part[1]
                        break
                if not isinstance(raw, (bytes, bytearray)):
                    skipped += 1
                    continue
                raw = bytes(raw)
                msg = message_from_bytes(raw)
                from_raw = msg.get("From") or ""
                _, from_addr = parseaddr(from_raw)
                from_addr = (from_addr or "").strip()
                if not from_addr:
                    skipped += 1
                    continue
                if _normalize_email(from_addr) in our_addrs:
                    skipped += 1
                    continue
                if _is_auto_generated_reply(msg):
                    skipped += 1
                    continue

                subject = _decode_header_value(msg.get("Subject"))
                body = _strip_reply_quotations(_extract_body(msg))
                mid = _message_stable_id(msg, from_addr, subject, raw)

                cur.execute("SELECT 1 FROM replies WHERE message_id = %s", (mid,))
                if cur.fetchone():
                    skipped += 1
                    continue

                received_at = _parse_received_at(msg)
                require_prior = _env_truthy("REPLY_IMPORT_REQUIRE_PRIOR_SEND", True)
                cur.execute(
                    "SELECT id, company_name, status FROM companies WHERE LOWER(TRIM(email)) = %s LIMIT 1",
                    (_normalize_email(from_addr),),
                )
                row = cur.fetchone()
                if require_prior:
                    if not row:
                        skipped += 1
                        continue
                    company_status = row[2]
                    if not _company_eligible_for_reply_import(company_status):
                        skipped += 1
                        continue
                    company_id = int(row[0])
                else:
                    company_id = int(row[0]) if row else None

                cur.execute(
                    """
                    INSERT INTO replies (company_id, from_email, subject, body, received_at, read, message_id)
                    VALUES (%s, %s, %s, %s, %s, FALSE, %s)
                    """,
                    (company_id, from_addr, subject, body or "(本文なし)", received_at, mid),
                )
                if company_id is not None:
                    cur.execute(
                        "UPDATE companies SET status = %s WHERE id = %s",
                        ("返信あり", company_id),
                    )
                imported += 1
            except Exception as ex:
                errors.append(str(ex)[:200])
                skipped += 1

        conn.commit()
        cur.close()
        conn.close()
        imap.logout()
    except imaplib.IMAP4.error as e:
        return {"ok": False, "error": f"IMAP error: {e}", "imported": imported, "skipped": skipped}
    except Exception as e:
        return {"ok": False, "error": str(e), "imported": imported, "skipped": skipped}

    out: dict[str, Any] = {
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "message": f"Imported {imported} replies, skipped {skipped}",
    }
    if errors:
        out["warnings"] = errors[:5]
    return out


if __name__ == "__main__":
    import json

    r = fetch_and_store_replies()
    print(json.dumps(r, ensure_ascii=False))
