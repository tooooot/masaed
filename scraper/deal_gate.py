#!/usr/bin/env python3
"""
🚦 بوّابة الصفقة الإلزامية — لا تواصل حقيقي قبل محاكاة معتمدة.

المبدأ (طلب المستخدم): كل صفقة تُحاكى أولاً، يراجع المستخدم نتيجة المحاكاة،
وإن رضي يعتمدها → عندها فقط يُسمح ببدء التواصل الحقيقي بين الطرفين.

التخزين: جدول sanad.masaed_deal_gate. المفتاح = (seeker_phone, owner_phone) مُطبّعَين.
الحالات: pending_review → approved / rejected → consumed (بعد بدء التفاوض فعلاً).

نقطة الاختناق: negotiator.start_negotiation تستدعي check() قبل أي إرسال،
فلا يمكن تجاوز البوّابة من أي مسار (lab/start، matches/approve، negotiate/start).
السياسة fail-closed: أي خطأ في الفحص = منع (الخط الأحمر للواتساب).
"""
import json
import re

# مهلة صلاحية الاعتماد (ساعات): بعدها يلزم محاكاة واعتماد جديدان
APPROVAL_TTL_HOURS = 24


def norm(raw) -> str:
    """تطبيع رقم سعودي إلى 966XXXXXXXXX — مطابق لـ scraper_api.normalize_phone."""
    clean = re.sub(r"[^0-9]", "", str(raw or ""))
    if clean.startswith("00966"):
        return "966" + clean[5:]
    if clean.startswith("966"):
        return clean
    if clean.startswith("0"):
        return "966" + clean[1:]
    if clean.startswith("5"):
        return "966" + clean
    return clean


def _get_conn():
    from bot import get_conn
    return get_conn()


def ensure_table(conn=None):
    own = conn is None
    if own:
        conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sanad.masaed_deal_gate (
                    id            SERIAL PRIMARY KEY,
                    seeker_phone  TEXT NOT NULL,
                    owner_phone   TEXT NOT NULL,
                    listing_id    INTEGER,
                    sim_job_id    TEXT,
                    sim_score     INTEGER,
                    sim_status    TEXT,
                    fact_errors   INTEGER DEFAULT 0,
                    gate_status   TEXT NOT NULL DEFAULT 'pending_review',
                    sim_summary   JSONB,
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    decided_at    TIMESTAMPTZ,
                    decided_by    TEXT,
                    UNIQUE (seeker_phone, owner_phone)
                )
            """)
            conn.commit()
    finally:
        if own:
            conn.close()


def record(seeker_phone, owner_phone, listing_id, job_id, summary,
           gate_status="pending_review", by=None, conn=None):
    """سجّل/حدّث صف بوّابة (upsert على المفتاح). يضبط decided_at للقرارات النهائية."""
    s, o = norm(seeker_phone), norm(owner_phone)
    summary = summary or {}
    score = summary.get("score")
    sim_status = summary.get("final_status")
    facts = summary.get("fact_errors", 0)
    decided = gate_status in ("approved", "rejected")
    own = conn is None
    if own:
        conn = _get_conn()
    try:
        ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sanad.masaed_deal_gate
                    (seeker_phone, owner_phone, listing_id, sim_job_id,
                     sim_score, sim_status, fact_errors, gate_status, sim_summary,
                     created_at, decided_at, decided_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        NOW(), CASE WHEN %s THEN NOW() ELSE NULL END, %s)
                ON CONFLICT (seeker_phone, owner_phone) DO UPDATE SET
                    listing_id  = EXCLUDED.listing_id,
                    sim_job_id  = EXCLUDED.sim_job_id,
                    sim_score   = EXCLUDED.sim_score,
                    sim_status  = EXCLUDED.sim_status,
                    fact_errors = EXCLUDED.fact_errors,
                    gate_status = EXCLUDED.gate_status,
                    sim_summary = EXCLUDED.sim_summary,
                    decided_at  = CASE WHEN %s THEN NOW() ELSE NULL END,
                    decided_by  = EXCLUDED.decided_by
                RETURNING id
            """, (s, o, listing_id, job_id, score, sim_status, facts,
                  gate_status, json.dumps(summary, ensure_ascii=False, default=str),
                  decided, by, decided))
            gid = cur.fetchone()[0]
            conn.commit()
        return gid
    finally:
        if own:
            conn.close()


def check(seeker_phone, owner_phone, conn=None):
    """يُرجع صف الاعتماد الصالح (approved + ضمن المهلة) أو None. fail-closed عند الخطأ."""
    s, o = norm(seeker_phone), norm(owner_phone)
    own = conn is None
    if own:
        conn = _get_conn()
    try:
        ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, sim_score, sim_status, decided_at
                FROM sanad.masaed_deal_gate
                WHERE seeker_phone=%s AND owner_phone=%s
                  AND gate_status='approved'
                  AND decided_at > NOW() - make_interval(hours => %s)
            """, (s, o, APPROVAL_TTL_HOURS))
            row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "sim_score": row[1],
                "sim_status": row[2], "decided_at": row[3]}
    finally:
        if own:
            conn.close()


def status(seeker_phone, owner_phone, conn=None):
    """الحالة الحالية للبوّابة (أي حالة) — للواجهة. يُرجع gate_status='none' إن لم توجد."""
    s, o = norm(seeker_phone), norm(owner_phone)
    own = conn is None
    if own:
        conn = _get_conn()
    try:
        ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT gate_status, sim_score, sim_status, fact_errors,
                       decided_at, created_at,
                       (gate_status='approved'
                        AND decided_at > NOW() - make_interval(hours => %s)) AS valid
                FROM sanad.masaed_deal_gate
                WHERE seeker_phone=%s AND owner_phone=%s
            """, (APPROVAL_TTL_HOURS, s, o))
            row = cur.fetchone()
        if not row:
            return {"gate_status": "none", "valid": False}
        return {"gate_status": row[0], "sim_score": row[1], "sim_status": row[2],
                "fact_errors": row[3], "decided_at": row[4], "created_at": row[5],
                "valid": bool(row[6])}
    finally:
        if own:
            conn.close()


def consume(seeker_phone, owner_phone, neg_id=None, conn=None):
    """بعد بدء التفاوض فعلاً: علّم 'consumed' كي يلزم اعتماد جديد لإعادة البدء."""
    s, o = norm(seeker_phone), norm(owner_phone)
    own = conn is None
    if own:
        conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE sanad.masaed_deal_gate
                SET gate_status='consumed', decided_at=NOW()
                WHERE seeker_phone=%s AND owner_phone=%s AND gate_status='approved'
            """, (s, o))
            conn.commit()
    except Exception as e:
        print(f"[GATE] فشل consume: {e}", flush=True)
    finally:
        if own:
            conn.close()
