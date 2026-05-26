-- مساعد العقاري — Database Schema v1
-- Runs inside the "sanad" PostgreSQL schema (shared with Sanad platform)
-- Connection: sanad-postgres:5432 / db: sanad / user: sanad

-- ── Sessions ────────────────────────────────────────────────────────────────
-- Each row is one anonymous negotiation session between a requester and owner.
-- Neither party knows the other's phone number; مساعد is the sole intermediary.

CREATE TABLE IF NOT EXISTS sanad.masaed_sessions (
    id               SERIAL PRIMARY KEY,
    requester_phone  TEXT NOT NULL,           -- e.g. 966550858330
    owner_phone      TEXT NOT NULL,           -- e.g. 966548060060
    status           TEXT DEFAULT 'active',   -- active | closed | expired
    context          TEXT,                    -- JSON blob: property, requirements, negotiation state
    message_count    INTEGER DEFAULT 0,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_masaed_sessions_phones ON sanad.masaed_sessions (requester_phone, owner_phone);
CREATE INDEX IF NOT EXISTS idx_masaed_sessions_status ON sanad.masaed_sessions (status);

-- ── Messages ─────────────────────────────────────────────────────────────────
-- Audit log of every message processed by مساعد (inbound only; AI replies are
-- not stored here — extend if needed).

CREATE TABLE IF NOT EXISTS sanad.masaed_messages (
    id          SERIAL PRIMARY KEY,
    session_id  INTEGER REFERENCES sanad.masaed_sessions(id),
    from_phone  TEXT NOT NULL,
    to_phone    TEXT NOT NULL,
    text        TEXT NOT NULL,
    direction   TEXT,                         -- '🔍 المهتم' | '🏠 صاحب العقار'
    sent_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_masaed_messages_session ON sanad.masaed_messages (session_id);

-- ── Context JSON structure (reference) ───────────────────────────────────────
-- Stored as TEXT in masaed_sessions.context; cast to JSONB when querying.
--
-- {
--   "negotiation_stage": "info_gathering | negotiating | near_deal | closed",
--   "property": {
--     "type": "apartment",
--     "city": "Jeddah",
--     "district": "Al-Rawdah",
--     "bedrooms": 3,
--     "bathrooms": 2,
--     "floor": 3,
--     "furnished": false,
--     "rent": 2800,
--     "currency": "SAR",
--     "available_from": "2025-07-01"
--   },
--   "requirements": {
--     "city": "Jeddah",
--     "budget": 2200,
--     "bedrooms": 3
--   },
--   "last_offer": {
--     "from": "owner",
--     "price": 2500,
--     "terms": "3 months advance"
--   }
-- }

-- ── Example: create a test session ───────────────────────────────────────────
-- INSERT INTO sanad.masaed_sessions (requester_phone, owner_phone)
-- VALUES ('966550858330', '966548060060');
