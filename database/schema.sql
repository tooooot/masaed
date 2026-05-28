-- مساعد العقاري — Database Schema v3 (Unified)
-- Single schema that matches negotiator.py v3 exactly
-- Runs inside the "sanad" PostgreSQL schema
-- Connection: sanad-postgres:5432 / db: sanad / user: sanad

-- ── Main negotiations table (replaces masaed_sessions) ───────────────────────
CREATE TABLE IF NOT EXISTS sanad.masaed_negotiations (
    id              SERIAL PRIMARY KEY,
    lead_id         INT,                            -- من masaed_leads
    listing_id      INT,                            -- من listing db
    lead_phone      TEXT NOT NULL,                  -- رقم المستأجر
    listing_phone   TEXT NOT NULL,                  -- رقم المالك
    status          TEXT DEFAULT 'active',          -- active | cancelled | closed

    -- ── بيانات العقار ──
    lead_name       TEXT,
    listing_title   TEXT,
    listing_city    TEXT,
    listing_price   INT,                            -- السعر الأول من الإعلان

    -- ── التفاوض ──
    lead_max_price  INT,                            -- أعلى عرض من المستأجر
    owner_min_price INT,                            -- أقل سعر من المالك
    proposed_price  INT,                            -- السعر الوسط المقترح
    lead_accepted   BOOLEAN DEFAULT false,
    owner_accepted  BOOLEAN DEFAULT false,
    agreed_price    INT,                            -- السعر النهائي عند الاتفاق

    -- ── إدارة ──
    needs_admin     BOOLEAN DEFAULT false,
    admin_reason    TEXT,
    admin_notified  BOOLEAN DEFAULT false,

    -- ── بيانات مساعدة ──
    listing_facts   TEXT,                           -- معلومات العقار المحملة مسبقاً
    chat_log        JSONB DEFAULT '[]',             -- سجل الرسائل: [{role, text, ts}]

    -- ── زمن ──
    expires_at      TIMESTAMPTZ DEFAULT NOW() + INTERVAL '7 days',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_masaed_neg_phones
    ON sanad.masaed_negotiations (lead_phone, listing_phone);
CREATE INDEX IF NOT EXISTS idx_masaed_neg_status
    ON sanad.masaed_negotiations (status);
CREATE INDEX IF NOT EXISTS idx_masaed_neg_created
    ON sanad.masaed_negotiations (created_at DESC);


-- ── Registrations (التحقق من هوية المستخدمين) ────────────────────────────
CREATE TABLE IF NOT EXISTS sanad.masaed_registrations (
    id          SERIAL PRIMARY KEY,
    phone       TEXT UNIQUE NOT NULL,
    role        TEXT NOT NULL,                      -- 'tenant' | 'owner'
    status      TEXT DEFAULT 'active',              -- active | verified | abandoned
    otp_code    TEXT,
    otp_sent_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_masaed_reg_phone
    ON sanad.masaed_registrations (phone);


-- ── Contact memory (tracking and notes) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS sanad.masaed_contacts (
    id          SERIAL PRIMARY KEY,
    phone       TEXT UNIQUE NOT NULL,
    name        TEXT,
    notes       TEXT,
    last_seen   TIMESTAMPTZ DEFAULT NOW(),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_masaed_contact_phone
    ON sanad.masaed_contacts (phone);


-- ── Example setup ────────────────────────────────────────────────────────────
-- Register two test users
-- INSERT INTO sanad.masaed_registrations (phone, role, status, verified_at)
-- VALUES
--   ('966550858330', 'tenant', 'verified', NOW()),
--   ('966548060060', 'owner', 'verified', NOW());
--
-- Start a negotiation
-- INSERT INTO sanad.masaed_negotiations
--   (lead_id, listing_id, lead_phone, listing_phone,
--    listing_title, listing_city, listing_price, status)
-- VALUES
--   (1, 100, '966550858330', '966548060060',
--    'شقة 3 غرف', 'جدة', 2800, 'active');
