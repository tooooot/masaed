#!/usr/bin/env python3
"""
مساعد المفاوض — وسيط بين المالك والمستأجر عبر واتساب
المبدأ:
  - الإدارة هي من تقرر بدء التفاوض من لوحة التحكم (ضغط الزر = الموافقة)
  - البوت يُبلّغ الطرفين ويبدأ فوراً — لا يستأذنهم
  - البوت ناقل فقط، لا يفاوض ولا يقرر ولا يغلق صفقة
  - الإغلاق يتم من الإدارة حصراً عبر /negotiate/<id>/agree
"""
import os, json, re, time
from datetime import datetime, timezone
from bot import get_conn, wa_send, _phone_lock

CANCEL_WORDS = {"لا","كلا","لأ","لا شكرا","مو مهتم","لا يهمني","إلغاء","الغاء",
                "cancel","stop","انهاء","إنهاء","خلاص","مو رايه"}

def _words(text: str) -> set:
    return set(re.split(r'[\s،,؟?!.،؛؟]+', text.strip().lower()))

def _has_cancel(text: str) -> bool:
    return bool(_words(text) & CANCEL_WORDS)


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_table():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sanad.masaed_negotiations (
                id              SERIAL PRIMARY KEY,
                lead_id         INT,
                listing_id      INT,
                lead_phone      TEXT,
                listing_phone   TEXT,
                status          TEXT DEFAULT 'active',
                agreed_price    INT,
                lead_name       TEXT,
                listing_title   TEXT,
                listing_city    TEXT,
                listing_price   INT,
                chat_log        JSONB DEFAULT '[]',
                expires_at      TIMESTAMPTZ DEFAULT NOW() + INTERVAL '7 days',
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
    conn.close()


def _load_neg(phone: str) -> dict | None:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, lead_id, listing_id, lead_phone, listing_phone,
                   status, lead_name, listing_title,
                   listing_city, listing_price, chat_log
            FROM sanad.masaed_negotiations
            WHERE (lead_phone = %s OR listing_phone = %s)
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC LIMIT 1
        """, (phone, phone))
        row = cur.fetchone()
    conn.close()
    if not row:
        return None
    cols = ['id','lead_id','listing_id','lead_phone','listing_phone',
            'status','lead_name','listing_title','listing_city','listing_price','chat_log']
    d = dict(zip(cols, row))
    d['chat_log'] = d['chat_log'] or []
    return d


def _update_neg(neg_id: int, **fields):
    if not fields:
        return
    conn = get_conn()
    with conn.cursor() as cur:
        sets = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(
            f"UPDATE sanad.masaed_negotiations SET {sets}, updated_at = NOW() WHERE id = %s",
            list(fields.values()) + [neg_id]
        )
        conn.commit()
    conn.close()


def _append_log(neg_id: int, role: str, text: str):
    ts = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_negotiations
            SET chat_log   = chat_log || %s::jsonb,
                updated_at = NOW()
            WHERE id = %s
        """, (json.dumps([{"role": role, "text": text, "ts": ts}]), neg_id))
        conn.commit()
    conn.close()


def _close(neg_id: int, status: str, agreed_price: int = None):
    _update_neg(neg_id, status=status, agreed_price=agreed_price)


# ── الناقل (active) ────────────────────────────────────────────────────────────

def _handle_active(neg: dict, phone: str, text: str) -> bool:
    neg_id  = neg["id"]
    is_lead = (phone == neg["lead_phone"])
    my_role = "مستأجر" if is_lead else "مالك"
    other   = neg["listing_phone"] if is_lead else neg["lead_phone"]

    # إلغاء بطلب صريح من الطرف
    if _has_cancel(text):
        _close(neg_id, "cancelled")
        _append_log(neg_id, my_role, text)
        wa_send(phone, "تم إنهاء التفاوض. شكراً 🙏")
        time.sleep(0.5)
        wa_send(other, "أُنهي التفاوض من الطرف الآخر.")
        return True

    # سجّل ثم أعد الإرسال للطرف الآخر
    _append_log(neg_id, my_role, text)
    fwd = f"💬 *{my_role}:*\n{text}"
    time.sleep(0.5)
    wa_send(other, fwd)
    _append_log(neg_id, "relay", fwd)

    print(f"[NEG #{neg_id}] {my_role} → other: {text[:60]}", flush=True)
    return True


# ── معالج عام ─────────────────────────────────────────────────────────────────

def handle_negotiation_message(phone: str, text: str) -> bool:
    with _phone_lock(phone):
        neg = _load_neg(phone)
        if not neg:
            return False
        return _handle_active(neg, phone, text)


# ── بدء تفاوض — الإدارة هي من تقرر ─────────────────────────────────────────

def start_negotiation(lead_id: int, listing_id: int,
                      lead_phone: str, listing_phone: str,
                      lead_name: str = None, listing_title: str = None,
                      listing_city: str = None, listing_price: int = None) -> dict:
    ensure_table()

    if lead_phone == listing_phone:
        return {"ok": False, "error": "لا يمكن التفاوض مع نفس الرقم"}

    existing = _load_neg(lead_phone)
    if existing and existing.get("listing_id") == listing_id:
        return {"ok": False, "error": "التفاوض جارٍ بالفعل", "neg_id": existing["id"]}

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_negotiations
                (lead_id, listing_id, lead_phone, listing_phone,
                 lead_name, listing_title, listing_city, listing_price,
                 status, expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                    'active', NOW() + INTERVAL '7 days')
            RETURNING id
        """, (lead_id, listing_id, lead_phone, listing_phone,
              lead_name, listing_title, listing_city, listing_price))
        neg_id = cur.fetchone()[0]
        conn.commit()
    conn.close()

    price_str = f"{listing_price:,} ر/سنة" if listing_price else "قابل للتفاوض"
    city_str  = listing_city or ""
    title_str = listing_title or "عقار للإيجار"
    name_str  = f" {lead_name}" if lead_name else ""

    # المستأجر: إبلاغ مباشر بدون استئذان
    wa_send(lead_phone,
        f"مرحباً{name_str} 🏠\n"
        f"ربطك مساعد العقاري بعرض يناسب طلبك:\n"
        f"📍 {title_str}" + (f" — {city_str}" if city_str else "") + f"\n"
        f"💰 {price_str}\n\n"
        f"تواصل مباشرة — رسائلك تصل للمالك عبر مساعد."
    )
    time.sleep(0.5)

    # المالك: إبلاغ مباشر بدون استئذان
    wa_send(listing_phone,
        f"مرحباً 🏠\n"
        f"ربطك مساعد العقاري بمستأجر مهتم بعقارك"
        + (f" في {city_str}" if city_str else "") + f".\n"
        f"💰 {price_str}\n\n"
        f"تواصل مباشرة — رسائلك تصل للمستأجر عبر مساعد."
    )

    print(f"[NEG] Active #{neg_id}: {lead_phone} ↔ {listing_phone}", flush=True)
    return {"ok": True, "neg_id": neg_id}
