# -*- coding: utf-8 -*-
import os
import sys

# Use project venv when present so `python3 main.py` works (PEP 668 blocks system pip).
if sys.prefix == sys.base_prefix:
    _venv_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python3")
    if os.path.isfile(_venv_py):
        os.execv(_venv_py, [_venv_py, os.path.abspath(__file__)] + sys.argv[1:])

from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import asyncio
import importlib.util
import psycopg2, psycopg2.errors, psycopg2.extras
from datetime import datetime
from functools import lru_cache
from urllib.parse import urlparse, parse_qs, unquote
from dotenv import load_dotenv

# Backend .env must win over stale DATABASE_URL in the shell / Windows user env
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(_ENV_PATH, override=True)

def _parse_database_url(url: str) -> dict:
    """
    Build psycopg2 keyword args from DATABASE_URL. Uses rsplit('@', 1) on netloc so
    usernames like user@domain.com or passwords containing @ (when last @ separates host) parse correctly.
    Prefer user=postgres or postgres.<project_ref> for Supabase — not an email address.
    """
    raw = (url or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("DATABASE_URL is empty")
    parsed = urlparse(raw if "://" in raw else f"postgresql://{raw}")
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")

    netloc = parsed.netloc
    if not netloc:
        raise ValueError("DATABASE_URL missing host (netloc)")

    if "@" in netloc:
        auth, hostport = netloc.rsplit("@", 1)
    else:
        auth, hostport = "", netloc

    user = password = None
    if auth:
        c = auth.find(":")
        if c == -1:
            user = unquote(auth)
        else:
            user = unquote(auth[:c])
            password = unquote(auth[c + 1 :])

    if hostport.startswith("["):
        end = hostport.find("]")
        host = hostport[1:end]
        rest = hostport[end + 1 :].lstrip(":")
        port = int(rest) if rest else 5432
    else:
        if ":" in hostport:
            host, p = hostport.rsplit(":", 1)
            port = int(p)
        else:
            host, port = hostport, 5432

    dbname = (parsed.path or "/postgres").lstrip("/") or "postgres"
    q = parse_qs(parsed.query)
    sslmode = (q.get("sslmode") or ["prefer"])[0]

    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
        "sslmode": sslmode,
        "connect_timeout": 15,
    }


def _log_database_url_target():
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        print("DATABASE_URL: (not set)")
        return
    try:
        p = _parse_database_url(raw)
        print(f"DATABASE_URL -> {p['host']}:{p['port']} (user={p.get('user') or '?'})")
    except Exception as e:
        print(f"DATABASE_URL: (invalid - {e})")

_log_database_url_target()

app = FastAPI(title="Sales Automation API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "data", "templates")
os.makedirs(TEMPLATES_DIR, exist_ok=True)


@lru_cache(maxsize=1)
def _email_sender_class():
    """Load sendEmail/mailer.py by path so imports work regardless of cwd or sys.path."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sendEmail", "mailer.py")
    spec = importlib.util.spec_from_file_location("backend_automation_mailer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load mailer from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.EmailSender


@lru_cache(maxsize=1)
def _reply_checker_module():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sendEmail", "reply_checker.py")
    spec = importlib.util.spec_from_file_location("backend_reply_checker", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load reply_checker from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise HTTPException(503, "DATABASE_URL not set in .env")
    try:
        kw = _parse_database_url(url)
        return psycopg2.connect(**kw)
    except Exception as e:
        raise HTTPException(503, f"DB connection failed: {e}")

def rows_to_dicts(cur):
    cols = [d[0] for d in cur.description]
    result = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        result.append(d)
    return result


def _normalize_company_dict(d: dict) -> dict:
    """Align DB values (EN/JP mix) with frontend / Supabase expectations."""
    if not d:
        return d
    out = dict(d)
    st = out.get("status")
    if st is not None:
        s = str(st).strip()
        low = s.lower()
        if low in ("new", "") or s == "New":
            out["status"] = "新規"
        elif s in ("Sent",):
            out["status"] = "メール送信済み"
        elif s in ("Replied",):
            out["status"] = "返信あり"
    es = out.get("email_status")
    if es is None or (isinstance(es, str) and es.strip() == ""):
        out["email_status"] = None
    else:
        e = str(es).strip()
        if e in ("Success", "送信成功"):
            out["email_status"] = "送信成功"
        elif e in ("Failed", "送信失敗"):
            out["email_status"] = "送信失敗"
    return out


def _company_status_db_variants(status: Optional[str]):
    """All status strings that may appear in DB for one logical filter."""
    if not status or not str(status).strip():
        return None
    s = str(status).strip()
    equiv = {
        "新規": ("新規", "New", "new"),
        "New": ("新規", "New", "new"),
        "new": ("新規", "New", "new"),
        "メール送信済み": ("メール送信済み", "Sent"),
        "Sent": ("メール送信済み", "Sent"),
        "返信あり": ("返信あり", "Replied"),
        "Replied": ("返信あり", "Replied"),
        "フォーム送信済み": ("フォーム送信済み",),
    }
    if s in equiv:
        return list(equiv[s])
    for k, vals in equiv.items():
        if s.lower() == k.lower():
            return list(vals)
    return [s]


def _fetch_companies_for_bulk_email(cur, target_status: str) -> list:
    """
    Load all matching companies from PostgreSQL (e.g. Supabase) in one query.
    Only rows with a non-empty email are returned; SMTP sends to that address, not the website URL.
    """
    st_vars = _company_status_db_variants(target_status) or ["新規", "New", "new"]
    ph = ",".join(["%s"] * len(st_vars))
    cur.execute(
        f"""SELECT * FROM companies
            WHERE status IN ({ph})
              AND email IS NOT NULL
              AND TRIM(email) != ''""",
        tuple(st_vars),
    )
    return [_normalize_company_dict(r) for r in rows_to_dicts(cur)]


def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                id SERIAL PRIMARY KEY, company_name TEXT NOT NULL,
                email TEXT, phone TEXT, website TEXT UNIQUE,
                form_url TEXT, address TEXT, description TEXT,
                status TEXT DEFAULT 'New', memo TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                last_contact TIMESTAMP, email_status TEXT);
            CREATE TABLE IF NOT EXISTS templates (
                id SERIAL PRIMARY KEY, name TEXT NOT NULL,
                subject TEXT, body TEXT,
                created_at TIMESTAMP DEFAULT NOW());
            CREATE TABLE IF NOT EXISTS campaigns (
                id SERIAL PRIMARY KEY,
                company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                template_id INTEGER REFERENCES templates(id) ON DELETE SET NULL,
                status TEXT DEFAULT 'pending', sent_at TIMESTAMP,
                opened BOOLEAN DEFAULT FALSE, replied BOOLEAN DEFAULT FALSE);
            CREATE TABLE IF NOT EXISTS replies (
                id SERIAL PRIMARY KEY, company_id INTEGER,
                from_email TEXT, subject TEXT, body TEXT,
                received_at TIMESTAMP DEFAULT NOW(), read BOOLEAN DEFAULT FALSE,
                message_id TEXT);
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY, name TEXT DEFAULT 'Default',
                send_time TEXT DEFAULT '10:00', daily_limit INTEGER DEFAULT 500,
                enabled BOOLEAN DEFAULT TRUE, scrape_time TEXT DEFAULT '09:00',
                followup_days INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT NOW(), last_run TIMESTAMP);
        """)
        cur.execute("INSERT INTO schedules (id) VALUES (1) ON CONFLICT DO NOTHING;")
        cur.execute("SELECT COUNT(*) FROM templates")
        (template_count,) = cur.fetchone()
        if (template_count or 0) == 0:
            cur.execute(
                """INSERT INTO templates (name, subject, body) VALUES (%s, %s, %s)""",
                (
                    "デフォルト",
                    "{{company_name}}様へのご連絡",
                    """お世話になっております。

{{company_name}} ご担当者様

突然のご連絡失礼いたします。ご返信いただければ幸いです。

よろしくお願いいたします。""",
                ),
            )
            print("Seeded default email template (templates was empty)")
        cur.execute("ALTER TABLE replies ADD COLUMN IF NOT EXISTS message_id TEXT;")
        conn.commit(); cur.close(); conn.close()
        print("DB tables ready")
    except HTTPException as e:
        print(f"DB init skipped: {e.detail}")
    except Exception as e:
        print(f"DB init error: {e}")

init_db()

# ── Models ────────────────────────────────────────────────────────────────────
class Company(BaseModel):
    company_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    form_url: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None
    status: str = "New"
    memo: Optional[str] = None

class Template(BaseModel):
    name: str; subject: str; body: str

class Campaign(BaseModel):
    company_id: int; template_id: int

class ScheduleSettings(BaseModel):
    enabled: bool; send_time: str; daily_limit: int
    scrape_time: Optional[str] = "09:00"
    followup_days: Optional[int] = 3

class BulkSendRequest(BaseModel):
    template_id: Optional[int] = None
    target_status: Optional[str] = "新規"
    subject: Optional[str] = None
    body: Optional[str] = None
    service_name: Optional[str] = None
    user_name: Optional[str] = None


def _apply_email_template_vars(
    text: Optional[str],
    company_name: str,
    service_name: str = "",
    user_name: str = "",
) -> str:
    if not text:
        return ""
    c = company_name or ""
    s = service_name or ""
    u = user_name or ""
    return (
        str(text)
        .replace("{{company_name}}", c)
        .replace("{{service_name}}", s)
        .replace("{{user_name}}", u)
        .replace("【会社名】", c)
        .replace("【サービス名】", s)
        .replace("◯◯", u)
    )


# Same placeholders as frontend emailBulkDefaults — used when `templates` table has no row
_BULK_FALLBACK_SUBJECT = "件名：空きスペースの収益化についてのご提案"
_BULK_FALLBACK_BODY = """お世話になっております。
{{company_name}}ご担当者様

突然のご連絡失礼いたします。
{{service_name}}の{{user_name}}と申します。

現在、空きスペースを「無人で収益化」できる{{service_name}}のご提案でご連絡いたしました。

近年、
・使われていない会議室
・空き時間のスタジオ
・稼働していない店舗スペース
などの"遊休資産"を活用し、収益化する動きが増えております。

一方で、
「管理の手間がかかる」
「予約・決済・鍵の管理がバラバラ」
といった理由から、導入に踏み切れないケースも多いのが現状です。

そこで弊社では、
予約・決済・入退室管理（スマートロック）を一括で管理できる仕組みを提供しており、
完全無人でのスペース運営を実現しております。

▼導入メリット
・空き時間の収益化（新たな収入源の創出）
・人件費ゼロでの運用
・既存スペースの有効活用

すでに同様のモデルで運用されている事例も増えており、
初期コストを抑えながらスモールスタートが可能です。

もし少しでもご関心がございましたら、
簡単な資料をお送りさせていただきますので、
「資料希望」とご返信いただけますと幸いです。

何卒よろしくお願いいたします。

――――――――――――――――
署名
――――――――――――――――
※本メールは、公開情報をもとにお送りしております。
今後のご案内が不要な場合は、お手数ですが「配信停止」とご返信ください。"""


class ScrapeRequest(BaseModel):
    keywords: List[str]
    results_per_keyword: Optional[int] = 20

@app.get("/")
def read_root():
    return {"message": "Sales Automation API running", "mode": "PostgreSQL"}

# ── Companies ─────────────────────────────────────────────────────────────────
@app.get("/api/companies")
def get_companies(status: Optional[str] = None):
    conn = get_db()
    try:
        cur = conn.cursor()
        if status:
            variants = _company_status_db_variants(status)
            if variants:
                ph = ",".join(["%s"] * len(variants))
                cur.execute(
                    f"SELECT * FROM companies WHERE status IN ({ph}) ORDER BY id",
                    tuple(variants),
                )
            else:
                cur.execute("SELECT * FROM companies ORDER BY id")
        else:
            cur.execute("SELECT * FROM companies ORDER BY id")
        return [_normalize_company_dict(r) for r in rows_to_dicts(cur)]
    finally: conn.close()

@app.post("/api/companies")
def create_company(company: Company):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO companies (company_name,email,phone,website,form_url,
            address,description,status,memo,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (company.company_name,company.email,company.phone,company.website,company.form_url,
             company.address,company.description,company.status,company.memo,datetime.now()))
        new_id = cur.fetchone()[0]; conn.commit()
        return {"id": new_id, **company.dict()}
    finally: conn.close()

@app.put("/api/companies/{company_id}")
def update_company(company_id: int, company: Company):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""UPDATE companies SET company_name=%s,email=%s,phone=%s,website=%s,
            form_url=%s,address=%s,description=%s,status=%s,memo=%s WHERE id=%s""",
            (company.company_name,company.email,company.phone,company.website,company.form_url,
             company.address,company.description,company.status,company.memo,company_id))
        if cur.rowcount == 0: raise HTTPException(404, "Company not found")
        conn.commit(); return {"message": "Updated"}
    finally: conn.close()

@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM companies WHERE id=%s", (company_id,)); conn.commit()
        return {"message": "Deleted"}
    finally: conn.close()

# ── Templates ─────────────────────────────────────────────────────────────────
def save_template_file(tid, name, subject, body):
    safe = "".join(c if c.isalnum() or c in (' ','-','_') else '_' for c in name).strip()
    with open(os.path.join(TEMPLATES_DIR, f"{tid:03d}_{safe}.txt"), "w", encoding="utf-8") as f:
        f.write(f"Subject: {subject}\n{'='*60}\n{body}")

@app.get("/api/templates")
def get_templates():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM templates ORDER BY id")
        return rows_to_dicts(cur)
    finally: conn.close()

@app.post("/api/templates")
def create_template(template: Template):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO templates (name,subject,body,created_at) VALUES (%s,%s,%s,%s) RETURNING id",
                    (template.name,template.subject,template.body,datetime.now()))
        new_id = cur.fetchone()[0]; conn.commit()
        save_template_file(new_id, template.name, template.subject, template.body)
        return {"id": new_id, **template.dict()}
    finally: conn.close()

@app.put("/api/templates/{template_id}")
def update_template(template_id: int, template: Template):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE templates SET name=%s,subject=%s,body=%s WHERE id=%s",
                    (template.name,template.subject,template.body,template_id))
        if cur.rowcount == 0: raise HTTPException(404, "Template not found")
        conn.commit()
        save_template_file(template_id, template.name, template.subject, template.body)
        return {"message": "Updated"}
    finally: conn.close()

@app.delete("/api/templates/{template_id}")
def delete_template(template_id: int):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM templates WHERE id=%s", (template_id,)); conn.commit()
        return {"message": "Deleted"}
    finally: conn.close()

# ── Campaigns ─────────────────────────────────────────────────────────────────
@app.get("/api/campaigns")
def get_campaigns():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM campaigns ORDER BY id")
        return rows_to_dicts(cur)
    finally: conn.close()

@app.post("/api/campaigns")
def create_campaign(campaign: Campaign):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO campaigns (company_id,template_id,status,opened,replied)
            VALUES (%s,%s,'pending',FALSE,FALSE) RETURNING id""",
            (campaign.company_id,campaign.template_id))
        new_id = cur.fetchone()[0]; conn.commit()
        return {"id": new_id, "message": "Campaign created"}
    finally: conn.close()

# ── Schedule ──────────────────────────────────────────────────────────────────
@app.get("/api/schedule")
async def get_schedule():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM schedules WHERE id=1")
        row = cur.fetchone()
        if not row:
            return {"id":1,"name":"Default","send_time":"10:00","daily_limit":500,
                    "enabled":True,"scrape_time":"09:00","followup_days":3,"last_run":None}
        cols = [d[0] for d in cur.description]
        s = dict(zip(cols, row))
        return {"id":s["id"],"name":s["name"],"send_time":s["send_time"],
                "daily_limit":s["daily_limit"],"enabled":bool(s["enabled"]),
                "scrape_time":s.get("scrape_time") or "09:00",
                "followup_days":s.get("followup_days") or 3,
                "last_run":s["last_run"].isoformat() if s.get("last_run") else None}
    finally: conn.close()

@app.post("/api/schedule")
async def update_schedule(settings: ScheduleSettings):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO schedules (id,send_time,daily_limit,enabled,scrape_time,followup_days)
            VALUES (1,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET send_time=EXCLUDED.send_time,
            daily_limit=EXCLUDED.daily_limit,enabled=EXCLUDED.enabled,
            scrape_time=EXCLUDED.scrape_time,followup_days=EXCLUDED.followup_days""",
            (settings.send_time,settings.daily_limit,settings.enabled,
             settings.scrape_time,settings.followup_days))
        conn.commit()
        return {"message":"Schedule saved","settings":settings.dict()}
    finally: conn.close()

# ── Replies ───────────────────────────────────────────────────────────────────
@app.get("/api/replies")
async def get_replies():
    """Only replies whose From address matches a company row with email_status 送信成功 (bulk send success)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.id, s.company_id, s.from_email, s.subject, s.body, s.received_at, s.read, s.message_id, s.company_name
            FROM (
                SELECT DISTINCT ON (r.id) r.id, r.company_id, r.from_email, r.subject, r.body, r.received_at, r.read, r.message_id,
                       COALESCE(c.company_name, '') AS company_name
                FROM replies r
                INNER JOIN companies c ON LOWER(TRIM(c.email)) = LOWER(TRIM(r.from_email))
                WHERE TRIM(COALESCE(c.email_status, '')) = %s
                  AND c.email IS NOT NULL AND TRIM(c.email) != ''
                ORDER BY r.id, c.id
            ) s
            ORDER BY s.received_at DESC NULLS LAST
            """,
            ("送信成功",),
        )
        return rows_to_dicts(cur)
    finally: conn.close()

@app.put("/api/replies/{reply_id}/read")
async def mark_reply_read(reply_id: int):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE replies SET read=TRUE WHERE id=%s", (reply_id,)); conn.commit()
        return {"message": "Marked as read"}
    finally: conn.close()


@app.delete("/api/replies/{reply_id}")
def delete_reply(reply_id: int):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM replies WHERE id=%s", (reply_id,))
        conn.commit()
        return {"message": "Deleted"}
    finally: conn.close()


@app.post("/api/replies/check")
async def check_replies_endpoint():
    try:
        mod = _reply_checker_module()
        result = await asyncio.to_thread(mod.fetch_and_store_replies)
    except ImportError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    if not result.get("ok"):
        raise HTTPException(502, result.get("error") or "Reply fetch failed")
    return result

# ── Send emails ───────────────────────────────────────────────────────────────
@app.post("/api/send-emails-bulk")
async def send_emails_bulk(request: Optional[BulkSendRequest] = Body(default=None)):
    """
    1) Resolve subject/body (request JSON and/or `templates` table).
    2) Fetch every matching company from the DB (Supabase/Postgres via DATABASE_URL) with one SELECT.
    3) For each company, substitute template variables, then send one SMTP message to that row's `email`.
    """
    EmailSender = _email_sender_class();

   



    target_status = (request.target_status if request and request.target_status else None) or "新規"
    svc = (request.service_name or "").strip() if request else ""
    usr = (request.user_name or "").strip() if request else ""
    use_subj = (
        request.subject.strip()
        if request and request.subject and str(request.subject).strip()
        else None
    )
    use_body = (
        request.body.strip()
        if request and request.body and str(request.body).strip()
        else None
    )
    conn = get_db()
    try:
        cur = conn.cursor()
        template = None
        if use_subj is None or use_body is None:
            row = None
            try:
                if request and request.template_id:
                    cur.execute("SELECT * FROM templates WHERE id=%s", (request.template_id,))
                else:
                    cur.execute("SELECT * FROM templates ORDER BY id LIMIT 1")
                row = cur.fetchone()
            except psycopg2.errors.UndefinedTable:
                conn.rollback()
                row = None
            if not row:
                template = {"subject": _BULK_FALLBACK_SUBJECT, "body": _BULK_FALLBACK_BODY}
            else:
                template = dict(zip([d[0] for d in cur.description], row))

        if use_subj is not None and use_body is not None:
            base_subject, base_body = use_subj, use_body
        else:
            base_subject = use_subj if use_subj is not None else str(template["subject"])
            base_body = use_body if use_body is not None else str(template["body"])

        targets = _fetch_companies_for_bulk_email(cur, target_status)
        if not targets:
            return {"message": "No targets", "sent": 0, "failed": 0, "total": 0}

        total = len(targets)
        sender = EmailSender()
        if not (sender.user and sender.password):
            raise HTTPException(
                503,
                "SMTP_USER and SMTP_PASSWORD must be set in auto-sending-backend/.env to send mail.",
            )
        sent = failed = 0
        for company in targets:
            try:
                cn = str(company["company_name"])
                subj = _apply_email_template_vars(base_subject, cn, svc, usr)
                body = _apply_email_template_vars(base_body, cn, svc, usr)
                email = company["email"]

                if email == "送信成功":
                    continue;
                ok = await sender.send_email(to_email=company["email"],
                    subject=subj, body=body,
                    company_name=cn)
                if ok:
                    cur.execute(
                        "UPDATE companies SET status=%s,last_contact=%s,email_status=%s WHERE id=%s",
                        ("メール送信済み", datetime.now(), "送信成功", company["id"]),
                    )
                    sent += 1
                else:
                    cur.execute(
                        "UPDATE companies SET email_status=%s WHERE id=%s",
                        ("送信失敗", company["id"]),
                    )
                    failed += 1
            except Exception: failed += 1
        conn.commit()
        return {
            "message": f"{sent} emails sent",
            "sent": sent,
            "failed": failed,
            "total": total,
        }
    finally: conn.close()

# ── Scrape ────────────────────────────────────────────────────────────────────
@app.post("/api/scrape")
async def scrape_companies_endpoint(request: ScrapeRequest):
    if not request.keywords or all(k.strip() == "" for k in request.keywords):
        raise HTTPException(400, "Please provide keywords")
    automation_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python_app", "automation")
    sys.path.insert(0, automation_dir)
    from scraper import CompanyScraper
    scraper = CompanyScraper()
    try:
        df = scraper.scrape_companies([k.strip() for k in request.keywords if k.strip()],
                                      results_per_keyword=request.results_per_keyword)
    finally: scraper.close()
    if df.empty: return {"message": "No companies found", "added": 0, "skipped": 0, "total": 0}

    conn = get_db()
    try:
        cur = conn.cursor(); added = skipped = 0
        for _, row in df.iterrows():
            website = row.get("website") or ""
            if not website: skipped += 1; continue
            cur.execute("""INSERT INTO companies (company_name,email,phone,website,form_url,
                address,description,status,memo,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (website) DO NOTHING""",
                (row.get("company_name"),row.get("email"),row.get("phone"),website,
                 row.get("form_url"),row.get("address"),row.get("description"),
                 row.get("status") or "新規",row.get("memo") or "",datetime.now()))
            if cur.rowcount > 0: added += 1
            else: skipped += 1
        cur.execute("SELECT COUNT(*) FROM companies"); total = cur.fetchone()[0]
        conn.commit()
        return {"message": f"{added} companies added", "added": added, "skipped": skipped, "total": total}
    finally: conn.close()

# ── CSV migration ─────────────────────────────────────────────────────────────
@app.post("/api/upload-csv")
async def upload_csv_to_db():
    import pandas as pd
    from psycopg2.extras import execute_values
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    conn = get_db(); results = {}
    try:
        cur = conn.cursor()
        for csv_file, insert_sql, row_fn in [
            ("companies.csv",
             "INSERT INTO companies (company_name,email,phone,website,form_url,address,description,status,memo,created_at,last_contact,email_status) VALUES %s ON CONFLICT (website) DO NOTHING",
             lambda r:(r.get("company_name"),r.get("email"),r.get("phone"),r.get("website"),r.get("form_url"),
                       r.get("address"),r.get("description"),r.get("status") or "New",r.get("memo"),
                       r.get("created_at") or None,r.get("last_contact") or None,r.get("email_status"))),
            ("templates.csv",
             "INSERT INTO templates (name,subject,body,created_at) VALUES %s ON CONFLICT DO NOTHING",
             lambda r:(r.get("name"),r.get("subject"),r.get("body"),r.get("created_at") or None)),
        ]:
            path = os.path.join(data_dir, csv_file)
            if os.path.exists(path):
                df = pd.read_csv(path, encoding="utf-8-sig", keep_default_na=False).replace("", None)
                rows = [row_fn(row) for _, row in df.iterrows()]
                if rows: execute_values(cur, insert_sql, rows)
                results[csv_file] = {"uploaded": cur.rowcount, "total": len(rows)}
        conn.commit()
        return {"message": "CSV uploaded to PostgreSQL", "results": results}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally: conn.close()

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
