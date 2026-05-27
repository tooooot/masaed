#!/usr/bin/env python3
"""
مساعد المفاوض — يدير الحوار بين المالك والمستأجر عبر واتساب
"""
import os, json, re
from datetime import datetime, timezone
from bot import get_conn, wa_send, _phone_lock, ANTHROPIC_KEY

# ── Constants ─────────────────────────────────────────────────────────────────
NEG_TIMEOUT_HOURS = 24

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
        # Add new columns if upgrading from old schema
        for col, defn in [
            ("confirmations", "JSONB DEFAULT '{\"lead\":false,\"listing\":false}'"),
            ("expires_at",    "TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours'"),
        ]:
            cur.execute(f"""
                ALTER TABLE sanad.masaed_negotiations
                ADD COLUMN IF NOT EXISTS {col} {defn}
            """)
        conn.commit()
    conn.close()


def _load_neg(phone: str) -> dict | None:
    """Return active/pending negotiation for this phone (not expired)."""
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


# ── AI negotiator ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT_NEG = """أنت "مساعد المفاوض" — وسيط عقاري بين طرفين: مالك ومستأجر.

كل رسالة تصلك تحمل تسمية [مالك] أو [مستأجر] لتعرف من يتكلم.
ردودك السابقة موسومة بـ [bot].

مهمتك:
- انقل جوهر الرسالة للطرف الآخر بلياقة (لا تنقل حرفياً دائماً)
- لا تكشف رقم أي طرف
- اقترح تسوية وسطى عند الاختلاف في السعر
- كن محايداً وشجّع الطرفين

أعد JSON فقط — لا نص خارجه:
{
  "reply_to_sender":  "ما تقوله للمرسل",
  "forward_to_other": "ما ترسله للطرف الآخر (فارغ إذا لا شيء ينقل)",
  "status":           "active",
  "agreed_price":     null
}

⚠️ لا تضع status="agreed" إلا إذا كتب **كلا الطرفين** موافقة صريحة في هذه الجلسة.
"""


def _build_history(log: list) -> list:
    """Build AI message history from the full chat log (both sides visible)."""
    history = []
    for entry in log[-16:]:
        role = entry.get("role", "")
        text = entry.get("text", "")
        if role == "bot":
            history.append({"role": "assistant", "content": text})
        else:
            history.append({"role": "user", "content": f"[{role}]: {text}"})
    return history


def _parse_neg_response(raw: str) -> dict | None:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group())
            return {
                "reply_to_sender":  d.get("reply_to_sender", ""),
                "forward_to_other": d.get("forward_to_other", ""),
                "status":           d.get("status", "active"),
                "agreed_price":     d.get("agreed_price"),
            }
        except Exception:
            pass
    return None


def _ai_negotiate(log: list, my_role: str, text: str, context: str) -> dict:
    history = _build_history(log)
    history.append({"role": "user", "content": f"[{my_role}]: {text}"})
    system  = SYSTEM_PROMPT_NEG + f"\n\nسياق الصفقة:\n{context}"

    # Try Anthropic first
    if ANTHROPIC_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            msgs = history[:]
            if not msgs or msgs[-1]["role"] == "assistant":
                msgs.append({"role": "user", "content": "(انتظر)"})
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400, system=system, messages=msgs
            )
            result = _parse_neg_response(resp.content[0].text.strip())
            if result:
                return result
        except Exception as e:
            print(f"[NEG] Anthropic failed ({e}), falling back to DeepSeek", flush=True)

    # Fallback: DeepSeek
    ds_key = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))
    if ds_key:
        try:
            import openai
            client = openai.OpenAI(api_key=ds_key, base_url="https://api.deepseek.com/v1")
            msgs   = [{"role": "system", "content": system}] + history
            resp   = client.chat.completions.create(
                model="deepseek-chat", messages=msgs,
                response_format={"type": "json_object"}, max_tokens=400
            )
            result = _parse_neg_response(resp.choices[0].message.content.strip())
            if result:
                return result
        except Exception as e:
            print(f"[NEG] DeepSeek also failed: {e}", flush=True)

    # Hard fallback — just relay message without AI
    return {
        "reply_to_sender":  "✅ تم استلام رسالتك وسيتم إبلاغ الطرف الآخر.",
        "forward_to_other": text,
        "status":           "active",
        "agreed_price":     None
    }


# ── Confirmation phase ─────────────────────────────────────────────────────────
def _handle_pending(neg: dict, phone: str, text: str) -> bool:
    """Handle yes/no during pending phase. Returns True when handled."""
    neg_id    = neg["id"]
    is_lead   = (phone == neg["lead_phone"])
    side      = "lead" if is_lead else "listing"
    other     = neg["listing_phone"] if is_lead else neg["lead_phone"]
    my_label  = "مستأجر" if is_lead else "مالك"
    confirmations = dict(neg["confirmations"])

    t = text.strip().lower()

    # Cancel
    if any(w in t for w in CANCEL_WORDS):
        _close(neg_id, "cancelled")
        wa_send(phone, "تم الإلغاء. يمكنك التواصل معنا في أي وقت 🙏")
        wa_send(other, f"عذراً، أحد الطرفين اعتذر عن هذه الصفقة.")
        _append_log(neg_id, my_label, text)
        return True

    # Confirm
    if any(w in t for w in CONFIRM_WORDS):
        confirmations[side] = True
        _update_neg(neg_id, confirmations=json.dumps(confirmations))
        _append_log(neg_id, my_label, text)

        if confirmations["lead"] and confirmations["listing"]:
            # Both confirmed → activate
            _update_neg(neg_id, status="active")
            city  = neg.get("listing_city") or ""
            title = neg.get("listing_title") or "العقار"
            price = neg.get("listing_price")
            p_str = f"{price:,} ر/سنة" if price else "قابل للتفاوض"
            msg = (f"✅ بدأ التفاوض الرسمي!\n"
                   f"📍 {title} — {city}\n💰 {p_str}\n\n"
                   f"تفضّل بطرح أسئلتك أو عرضك.")
            wa_send(neg["lead_phone"],    msg)
            wa_send(neg["listing_phone"], msg)
        else:
            wa_send(phone, "✅ تم تسجيل موافقتك. بانتظار موافقة الطرف الآخر...")
        return True

    # Unrecognized → remind
    wa_send(phone, "رد بـ **نعم** للموافقة أو **لا** للرفض.")
    return True


# ── Active negotiation ─────────────────────────────────────────────────────────
def _handle_active(neg: dict, phone: str, text: str) -> bool:
    neg_id    = neg["id"]
    is_lead   = (phone == neg["lead_phone"])
    my_role   = "مستأجر" if is_lead else "مالك"
    other     = neg["listing_phone"] if is_lead else neg["lead_phone"]

    t = text.strip().lower()

    # Cancel
    if any(w in t for w in CANCEL_WORDS):
        _close(neg_id, "cancelled")
        wa_send(phone, "تم إنهاء التفاوض. شكراً لتعاونك 🙏")
        wa_send(other, "أُنهي التفاوض من الطرف الآخر. نأسف على ذلك.")
        _append_log(neg_id, my_role, text)
        return True

    context = (
        f"العقار: {neg.get('listing_title','')} في {neg.get('listing_city','')}\n"
        f"السعر المطلوب: "
        + (f"{neg['listing_price']:,}" if neg.get('listing_price') else "غير محدد")
        + f" ر/سنة\nالمرسل: {my_role}"
    )

    result = _ai_negotiate(neg["chat_log"], my_role, text, context)

    _append_log(neg_id, my_role, text)
    if result["reply_to_sender"]:
        _append_log(neg_id, "bot", result["reply_to_sender"])
        wa_send(phone, result["reply_to_sender"])

    if result["forward_to_other"]:
        other_role = "مالك" if is_lead else "مستأجر"
        fwd = f"💬 {other_role}:\n{result['forward_to_other']}"
        _append_log(neg_id, "bot", fwd)
        wa_send(other, fwd)

    # Only close as agreed if AI explicitly says so
    # (requires real agreement signals from both sides in context)
    if result["status"] == "agreed":
        agreed_price = result.get("agreed_price")
        _close(neg_id, "agreed", agreed_price)
        p_str = f" بسعر {agreed_price:,} ر/سنة" if agreed_price else ""
        msg = f"🎉 تم الاتفاق{p_str}!\nسيتواصل معك الطرف الآخر لإتمام الإجراءات."
        wa_send(neg["lead_phone"],    msg)
        wa_send(neg["listing_phone"], msg)
        print(f"[NEG] Deal agreed #{neg_id}", flush=True)

    elif result["status"] == "failed":
        _close(neg_id, "failed")
        wa_send(phone, "نأسف، لم يتم الاتفاق. سنبحث لك عن بدائل أخرى 🔍")

    return True


# ── Public handler ─────────────────────────────────────────────────────────────
def handle_negotiation_message(phone: str, text: str) -> bool:
    """
    Route message to negotiator if phone is in active/pending negotiation.
    Returns True if handled, False if phone has no active negotiation.
    Uses _phone_lock to prevent race conditions.
    """
    with _phone_lock(phone):
        neg = _load_neg(phone)
        if not neg:
            return False

        if neg["status"] == "pending":
            return _handle_pending(neg, phone, text)
        elif neg["status"] == "active":
            return _handle_active(neg, phone, text)

    return False


# ── Start negotiation ──────────────────────────────────────────────────────────
def start_negotiation(lead_id: int, listing_id: int,
                      lead_phone: str, listing_phone: str,
                      lead_name: str = None, listing_title: str = None,
                      listing_city: str = None, listing_price: int = None) -> dict:
    """Create pending negotiation and send confirmation requests to both parties."""
    ensure_table()

    if lead_phone == listing_phone:
        return {"ok": False, "error": "لا يمكن التفاوض مع نفس الرقم"}

    # Check not already in active/pending negotiation on same listing
    existing = _load_neg(lead_phone)
    if existing and existing["listing_id"] == listing_id:
        return {"ok": False, "error": "التفاوض جارٍ بالفعل", "neg_id": existing["id"]}

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_negotiations
                (lead_id, listing_id, lead_phone, listing_phone,
                 lead_name, listing_title, listing_city, listing_price,
                 status, confirmations,
                 expires_at)
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
    city_str  = listing_city  or "غير محددة"
    name_str  = f" يا {lead_name}" if lead_name else ""

    wa_send(lead_phone,
        f"مرحباً{name_str}! 🏠 أنا مساعد المفاوض.\n"
        f"وجدت عرضاً يناسب طلبك:\n"
        f"📍 {city_str} — {listing_title or 'عقار للإيجار'}\n"
        f"💰 {price_str}\n\n"
        f"هل أنت مهتم؟ رد بـ **نعم** للبدء أو **لا** للتخطي."
    )
    wa_send(listing_phone,
        f"مرحباً! 🏠 أنا مساعد المفاوض.\n"
        f"لديك شخص مهتم بعقارك في {city_str}.\n"
        f"سأدير الحوار بينكما للوصول لاتفاق.\n\n"
        f"هل أنت متاح الآن؟ رد بـ **نعم** للبدء أو **لا** للتخطي."
    )

    print(f"[NEG] Pending #{neg_id}: {lead_phone} ↔ {listing_phone}", flush=True)
    return {"ok": True, "neg_id": neg_id}
