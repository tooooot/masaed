#!/usr/bin/env python3
"""
مساعد المفاوض — وسيط بين المالك والمستأجر عبر واتساب
المبدأ: البوت ناقل فقط، لا يفاوض ولا يقرر ولا يغلق صفقة بدون إذن الإدارة
"""
import os, json, time
from datetime import datetime, timezone
from bot import get_conn, wa_send, _phone_lock

CONFIRM_WORDS = {"نعم","أيوه","ايوه","اوك","ok","yes","موافق","أكيد","اكيد","يلا","تمام"}
CANCEL_WORDS  = {"لا","كلا","لأ","لا شكرا","مو مهتم","لا يهمني","إلغاء","الغاء",
                 "cancel","stop","انهاء","إنهاء","خلاص","مو رايه"}


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
                status          TEXT DEFAULT 'pending',
                confirmations   JSONB DEFAULT '{"lead":false,"listing":false}',
                agreed_price    INT,
                lead_name       TEXT,
                listing_title   TEXT,
                listing_city    TEXT,
                listing_price   INT,
                chat_log        JSONB DEFAULT '[]',
                expires_at      TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours',
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for col, defn in [
            ("confirmations", "JSONB DEFAULT '{\"lead\":false,\"listing\":false}'"),
            ("expires_at",    "TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours'"),
        ]:
            cur.execute(f"ALTER TABLE sanad.masaed_negotiations ADD COLUMN IF NOT EXISTS {col} {defn}")
        conn.commit()
    conn.close()


def _load_neg(phone: str) -> dict | None:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, lead_id, listing_id, lead_phone, listing_phone,
                   status, confirmations, lead_name, listing_title,
                   listing_city, listing_price, chat_log, expires_at
            FROM sanad.masaed_negotiations
            WHERE (lead_phone = %s OR listing_phone = %s)
              AND status IN ('pending','active')
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC LIMIT 1
        """, (phone, phone))
        row = cur.fetchone()
    conn.close()
    if not row:
        return None
    cols = ['id','lead_id','listing_id','lead_phone','listing_phone',
            'status','confirmations','lead_name','listing_title',
            'listing_city','listing_price','chat_log','expires_at']
    d = dict(zip(cols, row))
    d['confirmations'] = d['confirmations'] or {"lead": False, "listing": False}
    d['chat_log']      = d['chat_log'] or []
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


# ── مرحلة التأكيد (pending) ────────────────────────────────────────────────────

def _handle_pending(neg: dict, phone: str, text: str) -> bool:
    neg_id        = neg["id"]
    is_lead       = (phone == neg["lead_phone"])
    side          = "lead" if is_lead else "listing"
    other         = neg["listing_phone"] if is_lead else neg["lead_phone"]
    my_label      = "مستأجر" if is_lead else "مالك"
    confirmations = dict(neg["confirmations"])
    t             = text.strip().lower()

    if any(w in t for w in CANCEL_WORDS):
        _close(neg_id, "cancelled")
        _append_log(neg_id, my_label, text)
        wa_send(phone, "تم الإلغاء. يمكنك التواصل معنا في أي وقت 🙏")
        time.sleep(0.5)
        wa_send(other, "عذراً، أحد الطرفين اعتذر عن هذه الصفقة.")
        return True

    if any(w in t for w in CONFIRM_WORDS):
        confirmations[side] = True
        _update_neg(neg_id, confirmations=json.dumps(confirmations))
        _append_log(neg_id, my_label, text)

        if confirmations["lead"] and confirmations["listing"]:
            _update_neg(neg_id, status="active")
            title = neg.get("listing_title") or "العقار"
            city  = neg.get("listing_city") or ""
            price = neg.get("listing_price")
            p_str = f"{price:,} ر/سنة" if price else "قابل للتفاوض"
            msg = (
                f"✅ بدأ التفاوض!\n"
                f"📍 {title}" + (f" — {city}" if city else "") + f"\n"
                f"💰 {p_str}\n\n"
                f"ابدأ بطرح أسئلتك أو عرضك مباشرة.\n"
                f"رسائلك ستصل للطرف الآخر عبر مساعد."
            )
            wa_send(neg["lead_phone"],    msg)
            time.sleep(0.5)
            wa_send(neg["listing_phone"], msg)
        else:
            wa_send(phone, "✅ سُجّلت موافقتك. بانتظار موافقة الطرف الآخر...")
        return True

    wa_send(phone, "رد بـ *نعم* للموافقة أو *لا* للرفض.")
    return True


# ── مرحلة التفاوض النشط (active) — ناقل بحت ──────────────────────────────────

def _handle_active(neg: dict, phone: str, text: str) -> bool:
    """
    البوت ناقل فقط:
    • يُعيد إرسال كل رسالة للطرف الآخر بتسمية المُرسِل
    • لا يفاوض ولا يقترح ولا يغلق الصفقة
    • فقط كلمة الإلغاء تُنهي من طرف واحد
    • الإغلاق كـ"متفق" يتم من الإدارة حصراً
    """
    neg_id   = neg["id"]
    is_lead  = (phone == neg["lead_phone"])
    my_role  = "مستأجر" if is_lead else "مالك"
    other    = neg["listing_phone"] if is_lead else neg["lead_phone"]
    t        = text.strip().lower()

    # إلغاء: الطرف يخرج باختياره
    if any(w in t for w in CANCEL_WORDS):
        _close(neg_id, "cancelled")
        _append_log(neg_id, my_role, text)
        wa_send(phone, "تم إنهاء التفاوض. شكراً 🙏")
        time.sleep(0.5)
        wa_send(other, "أُنهي التفاوض من الطرف الآخر.")
        return True

    # سجّل رسالة المُرسِل
    _append_log(neg_id, my_role, text)

    # أرسلها للطرف الآخر مع تسمية المُرسِل
    fwd = f"💬 *{my_role}:*\n{text}"
    time.sleep(0.5)
    wa_send(other, fwd)
    _append_log(neg_id, "relay", fwd)

    print(f"[NEG #{neg_id}] relay {my_role} → other: {text[:60]}", flush=True)
    return True


# ── معالج عام ─────────────────────────────────────────────────────────────────

def handle_negotiation_message(phone: str, text: str) -> bool:
    with _phone_lock(phone):
        neg = _load_neg(phone)
        if not neg:
            return False
        if neg["status"] == "pending":
            return _handle_pending(neg, phone, text)
        if neg["status"] == "active":
            return _handle_active(neg, phone, text)
    return False


# ── بدء تفاوض جديد ────────────────────────────────────────────────────────────

def start_negotiation(lead_id: int, listing_id: int,
                      lead_phone: str, listing_phone: str,
                      lead_name: str = None, listing_title: str = None,
                      listing_city: str = None, listing_price: int = None) -> dict:
    ensure_table()

    if lead_phone == listing_phone:
        return {"ok": False, "error": "لا يمكن التفاوض مع نفس الرقم"}

    existing = _load_neg(lead_phone)
    if existing and existing["listing_id"] == listing_id:
        return {"ok": False, "error": "التفاوض جارٍ بالفعل", "neg_id": existing["id"]}

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_negotiations
                (lead_id, listing_id, lead_phone, listing_phone,
                 lead_name, listing_title, listing_city, listing_price,
                 status, confirmations, expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                    'pending','{"lead":false,"listing":false}',
                    NOW() + INTERVAL '24 hours')
            RETURNING id
        """, (lead_id, listing_id, lead_phone, listing_phone,
              lead_name, listing_title, listing_city, listing_price))
        neg_id = cur.fetchone()[0]
        conn.commit()
    conn.close()

    price_str = f"{listing_price:,} ر/سنة" if listing_price else "سعر قابل للتفاوض"
    city_str  = listing_city or "غير محددة"
    name_str  = f" يا {lead_name}" if lead_name else ""

    wa_send(lead_phone,
        f"مرحباً{name_str}! 🏠\n"
        f"لديك عرض يناسب طلبك:\n"
        f"📍 {city_str} — {listing_title or 'عقار للإيجار'}\n"
        f"💰 {price_str}\n\n"
        f"رد بـ *نعم* إذا أنت مهتم، أو *لا* للتخطي."
    )
    time.sleep(0.5)
    wa_send(listing_phone,
        f"مرحباً! 🏠\n"
        f"شخص مهتم بعقارك في {city_str}.\n"
        f"سيُدار الحوار بينكما عبر مساعد بشكل سري.\n\n"
        f"رد بـ *نعم* للبدء، أو *لا* للتخطي."
    )

    print(f"[NEG] Pending #{neg_id}: {lead_phone} ↔ {listing_phone}", flush=True)
    return {"ok": True, "neg_id": neg_id}
