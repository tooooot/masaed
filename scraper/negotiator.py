#!/usr/bin/env python3
"""
مساعد المفاوض — وكيل عقاري ذكي يتحدث مع كل طرف بشكل مستقل
المبدأ:
  - كل طرف يتحدث مع "مساعد" مباشرة، لا مع الطرف الآخر
  - مساعد يدير المعلومات بين الطرفين بذكاء وبقرار
  - الإدارة هي من تبدأ التفاوض وتغلق الصفقة
"""
import os, json, re, time
from datetime import datetime, timezone
from bot import get_conn, wa_send, _phone_lock, ANTHROPIC_KEY

CANCEL_WORDS = {"لا شكرا","مو مهتم","لا يهمني","إلغاء","الغاء",
                "cancel","stop","انهاء","إنهاء","مو رايه"}

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))

SYSTEM_PROMPT = """\
أنت "مساعد العقاري" — وكيل عقاري محترف.
تتحدث مع طرف واحد في كل مرة: إما المالك أو المستأجر.
لديك سجل كامل بمحادثتك مع الطرفين.

مهمتك:
- تحدّث بشكل طبيعي مع من يكلمك: أجب، اقترح، وضّح
- انقل المعلومات المهمة للطرف الآخر متى رأيت ذلك مناسباً (ليس كل رسالة)
- اقترح تسويات وسطى عند اختلاف السعر
- لا تكشف رقم هاتف أي طرف

قواعد صارمة:
- لا تقل أبداً "تم الاتفاق" أو ما يشير لإغلاق الصفقة — هذا قرار الإدارة فقط
- إذا وصل الطرفان لاتفاق واضح، قل: "يبدو أنكما قريبان من الاتفاق، سأُبلغ الإدارة لإتمام الصفقة"

أعد JSON فقط — لا نص خارجه:
{
  "reply": "ردّك على من أرسل الرسالة الآن",
  "notify_other": "رسالة تُرسل للطرف الآخر إن لزم — null إذا لا حاجة"
}
"""


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


# ── AI agent ──────────────────────────────────────────────────────────────────

def _build_messages(log: list, current_role: str, current_text: str, context: str) -> list:
    """Build AI message list from full chat log."""
    system_with_ctx = SYSTEM_PROMPT + f"\n\nسياق الصفقة:\n{context}\nالمتحدث الآن: {current_role}"

    # Build conversation history
    history = []
    for entry in log[-20:]:
        role = entry.get("role", "")
        text = entry.get("text", "")
        if role in ("مالك", "مستأجر"):
            history.append({"role": "user", "content": f"[{role}]: {text}"})
        elif role.startswith("bot"):
            history.append({"role": "assistant", "content": text})

    # Add current message
    history.append({"role": "user", "content": f"[{current_role}]: {current_text}"})

    # Ensure valid alternation (first must be user)
    if history and history[0]["role"] == "assistant":
        history = history[1:]

    return system_with_ctx, history


def _parse_response(raw: str) -> dict | None:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group())
            return {
                "reply":        str(d.get("reply") or "").strip(),
                "notify_other": str(d.get("notify_other") or "").strip() or None,
            }
        except Exception:
            pass
    return None


def _ai_respond(neg: dict, my_role: str, text: str) -> dict:
    title = neg.get("listing_title") or "العقار"
    city  = neg.get("listing_city") or ""
    price = neg.get("listing_price")
    p_str = f"{price:,} ر/سنة" if price else "قابل للتفاوض"
    context = f"العقار: {title}" + (f" — {city}" if city else "") + f"\nالسعر: {p_str}"

    system, messages = _build_messages(neg["chat_log"], my_role, text, context)

    # Anthropic
    if ANTHROPIC_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300, system=system, messages=messages
            )
            result = _parse_response(resp.content[0].text.strip())
            if result and result["reply"]:
                return result
        except Exception as e:
            print(f"[NEG] Anthropic: {e}", flush=True)

    # DeepSeek fallback
    if DEEPSEEK_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
            msgs = [{"role": "system", "content": system}] + messages
            resp = client.chat.completions.create(
                model="deepseek-chat", messages=msgs,
                response_format={"type": "json_object"}, max_tokens=300
            )
            result = _parse_response(resp.choices[0].message.content.strip())
            if result and result["reply"]:
                return result
        except Exception as e:
            print(f"[NEG] DeepSeek: {e}", flush=True)

    # Fallback: plain acknowledgment, hold message
    return {"reply": "وصلت رسالتك، سأتابع معك قريباً.", "notify_other": None}


# ── معالج الرسائل النشطة ───────────────────────────────────────────────────────

def _handle_active(neg: dict, phone: str, text: str) -> bool:
    neg_id  = neg["id"]
    is_lead = (phone == neg["lead_phone"])
    my_role = "مستأجر" if is_lead else "مالك"
    other   = neg["listing_phone"] if is_lead else neg["lead_phone"]

    # إلغاء صريح
    if _has_cancel(text):
        _close(neg_id, "cancelled")
        _append_log(neg_id, my_role, text)
        wa_send(phone, "تم إنهاء التفاوض. شكراً 🙏")
        time.sleep(0.5)
        wa_send(other, "أُنهي التفاوض من الطرف الآخر.")
        return True

    # سجّل رسالة المرسل
    _append_log(neg_id, my_role, text)

    # مساعد يرد
    result = _ai_respond(neg, my_role, text)

    # الرد على المرسل
    if result["reply"]:
        _append_log(neg_id, f"bot→{my_role}", result["reply"])
        wa_send(phone, result["reply"])

    # إبلاغ الطرف الآخر إن قرر المساعد ذلك
    if result["notify_other"]:
        time.sleep(0.5)
        _append_log(neg_id, f"bot→{'مالك' if is_lead else 'مستأجر'}", result["notify_other"])
        wa_send(other, result["notify_other"])

    print(f"[NEG #{neg_id}] {my_role}: {text[:50]} → reply:{bool(result['reply'])} notify:{bool(result['notify_other'])}", flush=True)
    return True


# ── معالج عام ─────────────────────────────────────────────────────────────────

def handle_negotiation_message(phone: str, text: str) -> bool:
    with _phone_lock(phone):
        neg = _load_neg(phone)
        if not neg:
            return False
        return _handle_active(neg, phone, text)


# ── بدء تفاوض — الإدارة هي من تقرر ──────────────────────────────────────────

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
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s, 'active', NOW() + INTERVAL '7 days')
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

    wa_send(lead_phone,
        f"مرحباً{name_str}، أنا مساعد العقاري 🏠\n"
        f"ربطتك بعرض يناسب طلبك:\n"
        f"📍 {title_str}" + (f" — {city_str}" if city_str else "") + f"\n"
        f"💰 {price_str}\n\n"
        f"تحدّث معي مباشرة — أنا هنا أساعدك في التفاوض."
    )
    time.sleep(0.5)
    wa_send(listing_phone,
        f"مرحباً، أنا مساعد العقاري 🏠\n"
        f"لديك مستأجر مهتم بعقارك"
        + (f" في {city_str}" if city_str else "") + f".\n"
        f"💰 {price_str}\n\n"
        f"تحدّث معي مباشرة — أنا هنا أساعدك في التفاوض."
    )

    print(f"[NEG] Active #{neg_id}: {lead_phone} ↔ {listing_phone}", flush=True)
    return {"ok": True, "neg_id": neg_id}
