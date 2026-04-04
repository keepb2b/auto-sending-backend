-- ============================================================
-- Supabase Schema for Sales Automation
-- Run this in: Supabase Dashboard > SQL Editor > New Query
-- ============================================================

-- ── Companies ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS companies (
    id            SERIAL PRIMARY KEY,
    company_name  TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    website       TEXT UNIQUE,
    form_url      TEXT,
    address       TEXT,
    description   TEXT,
    status        TEXT DEFAULT '新規',
    created_at    TIMESTAMP DEFAULT NOW(),
    last_contact  TIMESTAMP,
    email_status  TEXT
);

-- ── Templates ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS templates (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    subject     TEXT,
    body        TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
);

-- ── Campaigns ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS campaigns (
    id           SERIAL PRIMARY KEY,
    company_id   INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    template_id  INTEGER REFERENCES templates(id) ON DELETE SET NULL,
    status       TEXT DEFAULT '送信待ち',
    sent_at      TIMESTAMP,
    opened       BOOLEAN DEFAULT FALSE,
    replied      BOOLEAN DEFAULT FALSE
);

-- ── Replies ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS replies (
    id           SERIAL PRIMARY KEY,
    company_id   INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    from_email   TEXT,
    subject      TEXT,
    body         TEXT,
    received_at  TIMESTAMP DEFAULT NOW(),
    read         BOOLEAN DEFAULT FALSE,
    message_id   TEXT
);

-- Dedupe inbound messages (optional; app checks before insert)
CREATE INDEX IF NOT EXISTS idx_replies_message_id ON replies (message_id) WHERE message_id IS NOT NULL AND message_id <> '';

-- ── Schedules ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schedules (
    id             SERIAL PRIMARY KEY,
    name           TEXT DEFAULT 'デフォルトスケジュール',
    send_time      TEXT DEFAULT '10:00',
    daily_limit    INTEGER DEFAULT 500,
    enabled        BOOLEAN DEFAULT TRUE,
    scrape_time    TEXT DEFAULT '09:00',
    followup_days  INTEGER DEFAULT 3,
    created_at     TIMESTAMP DEFAULT NOW(),
    last_run       TIMESTAMP
);

-- Insert default schedule row
INSERT INTO schedules (name) VALUES ('デフォルトスケジュール')
ON CONFLICT DO NOTHING;

-- ── Indexes ──────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_companies_status  ON companies(status);
CREATE INDEX IF NOT EXISTS idx_companies_email   ON companies(email);
CREATE INDEX IF NOT EXISTS idx_campaigns_company ON campaigns(company_id);
CREATE INDEX IF NOT EXISTS idx_replies_company   ON replies(company_id);

-- ── Row Level Security (RLS) ─────────────────────────────────
-- Enable RLS and allow full access via service_role key (used by backend)

ALTER TABLE companies  ENABLE ROW LEVEL SECURITY;
ALTER TABLE templates  ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaigns  ENABLE ROW LEVEL SECURITY;
ALTER TABLE replies    ENABLE ROW LEVEL SECURITY;
ALTER TABLE schedules  ENABLE ROW LEVEL SECURITY;

-- Allow all operations for authenticated service role
DROP POLICY IF EXISTS "service_role full access" ON companies;
DROP POLICY IF EXISTS "service_role full access" ON templates;
DROP POLICY IF EXISTS "service_role full access" ON campaigns;
DROP POLICY IF EXISTS "service_role full access" ON replies;
DROP POLICY IF EXISTS "service_role full access" ON schedules;

CREATE POLICY "service_role full access" ON companies  FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role full access" ON templates  FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role full access" ON campaigns  FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role full access" ON replies    FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role full access" ON schedules  FOR ALL USING (true) WITH CHECK (true);
