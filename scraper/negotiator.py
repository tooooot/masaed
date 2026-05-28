#!/usr/bin/env python3
"""
مساعد المفاوض v2
Layer 1: Intent Parser  (LLM — يفهم فقط)
Layer 2: Rules Engine   (Python — يقرر فقط)
Layer 3: Response Gen   (LLM — يكتب فقط) | Telegram notify
"""
import os, json, re, time
from datetime import datetime, timezone
from bot import get_conn, wa_send, _phone_lock, ANTHROPIC_KEY
from intent_parser import parse_intent
from rules_engine import evaluate as rules_eval

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))

CANCEL_WORDS = {"لا شكرا","مو مهتم","لا يهمني","إلغاء","الغاء",
                "cancel","stop","انهاء","إنهاء","مو رايه"}

_REPLY_SYSTEM = """\
أنت مساعد عقاري محترف تتحدث بالعربية السعودية.
أجب بجملة أو جملتين فقط — طبيعية ومهنية.
لا تَعِد بأي شيء محدد. لا تذكر أرقاماً لم تسمعها.
إذا ذكر الطرف سعراً: أقرّه وقل ستتابع.
لا تُغلق الصفقة ولا تقل "تم الاتفاق".
إذا كان التفاوض قريباً من الاتفاق: قل "سأُبلغ المسؤول لإتمام الصفقة".
"""


# ── DB helpers ─────────────────────────────────────────────────────────────────

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
                lead_max_price  INT,
                owner_min_price INT,
                chat_log        JSONB DEFAULT '[]',
                expires_at      TIMESTAMPTZ DEFAULT NOW() + INTERVAL '7 days',
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for col, typ in [("lead_max_price", "INT"), ("owner_min_price", "INT")]:
            cur.execute(f"""
                ALTER TABLE sanad.masaed_negotiations
                ADD COLUMN IF NOT EXISTS {col} {typ}
            """)
        conn.commit()
    conn.close()


def _load_neg(phone: str) -> dict | None:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, lead_id, listing_id, lead_phone, listing_phone,
                   status, lead_name, listing_title, listing_city,
                   listing_price, lead_max_price, owner_min_price, chat_log
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
            'status','lead_name','listing_title','listing_city',
            'listing_price','lead_max_price','owner_min_price','chat_log']
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


# ── Response Generator (Layer 3) ───────────────────────────────────────────────

def _generate_reply(neg: dict, my_role: str, text: str, intent: dict) -> str:
    title   = neg.get("listing_title") or "العقار"
    city    = neg.get("listing_city") or ""
    price   = neg.get("listing_price")
    p_str   = f"{price:,} ر/سنة" if price else "قابل للتفاوض"
    context = f"العقار: {title}" + (f" — {city}" if city else "") + f"\nالسعر: {p_str}"

    note = ""
    if intent.get("amount"):
        note = f"\n(المتحدث ذكر: {intent['amount']:,} ر)"

    system   = _REPLY_SYSTEM + f"\n\nسياق العقار:\n{context}\nالمتحدث الآن: {my_role}"
    messages = [{"role": "user", "content": f"[{my_role}]: {text}{note}"}]

    if ANTHROPIC_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=120, system=system, messages=messages
            )
            reply = resp.content[0].text.strip()
            if reply:
                return reply
        except Exception as e:
            print(f"[NEG] Anthropic reply: {e}", flush=True)

    if DEEPSEEK_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": system}] + messages,
                max_tokens=120
            )
            reply = resp.choices[0].message.content.strip()
            if reply:
                return reply
        except Exception as e:
            print(f"[NEG] DeepSeek reply: {e}", flush=True)

    return "وصلت رسالتك، سأتابع معك قريباً."


# ── Main handler ───────────────────────────────────────────────────────────────

def _words(text: str) -> set:
    return set(re.split(r'[\s،,؟?!.،؛؟]+', text.strip().lower()))

def _has_cancel(text: str) -> bool:
    return bool(_words(text) & CANCEL_WORDS)


def _handle_active(neg: dict, phone: str, text: str) -> bool:
    neg_id  = neg["id"]
    is_lead = (phone == neg["lead_phone"])
    my_role = "مستأجر" if is_lead else "مالك"
    other   = neg["listing_phone"] if is_lead else neg["lead_phone"]

    # إلغاء صريح (فحص كلمات)
    if _has_cancel(text):
        _close(neg_id, "cancelled")
        _append_log(neg_id, my_role, text)
        wa_send(phone, "تم إنهاء التفاوض. شكراً 🙏")
        time.sleep(0.5)
        wa_send(other, "أُنهي التفاوض من الطرف الآخر.")
        return True

    # Layer 1: فهم النية
    intent = parse_intent(text)
    print(f"[NEG #{neg_id}] {my_role} intent={intent['intent']} amount={intent.get('amount')}", flush=True)

    # Layer 2: قرار حتمي
    decision = rules_eval(neg, my_role, intent)
    print(f"[NEG #{neg_id}] action={decision['action']} reason={decision['reason']}", flush=True)

    # سجّل الرسالة
    _append_log(neg_id, my_role, text)

    # حدّث ذاكرة السعر إن وُجدت
    if decision.get("update_price"):
        field = decision["update_price"]["field"]
        value = decision["update_price"]["value"]
        _update_neg(neg_id, **{field: value})
        neg[field] = value  # حدّث النسخة المحلية لـ tg_notify

    # إلغاء من قِبَل rules_engine
    if decision["action"] == "cancel":
        _close(neg_id, "cancelled")
        wa_send(phone, "تم إنهاء التفاوض. شكراً 🙏")
        time.sleep(0.5)
        wa_send(other, "أُنهي التفاوض من الطرف الآخر.")
        return True

    # Layer 3: رد تلقائي على المرسل
    reply = _generate_reply(neg, my_role, text, intent)
    _append_log(neg_id, f"bot→{my_role}", reply)
    wa_send(phone, reply)

    # رفع علم الإدارة في قاعدة البيانات
    if decision["action"] == "notify_admin":
        reason_map = {
            "near_agreement": "قريب من الاتفاق",
            "ready_to_close": "جاهز للإغلاق",
            "party_leaving":  f"{my_role} قد ينسحب",
            "many_rounds":    "تفاوض طويل",
        }
        label = reason_map.get(decision["reason"], decision["reason"])
        _update_neg(neg_id, needs_admin=True, admin_reason=label)

    return True


def handle_negotiation_message(phone: str, text: str) -> bool:
    with _phone_lock(phone):
        neg = _load_neg(phone)
        if not neg:
            return False
        return _handle_active(neg, phone, text)


# ── بدء تفاوض جديد ────────────────────────────────────────────────────────────

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
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active', NOW() + INTERVAL '7 days')
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
        f"📍 {title_str}" + (f" — {city_str}" if city_str else "") + "\n"
        f"💰 {price_str}\n\n"
        f"تحدّث معي مباشرة — أنا هنا أساعدك في التفاوض."
    )
    time.sleep(0.5)
    wa_send(listing_phone,
        f"مرحباً، أنا مساعد العقاري 🏠\n"
        f"لديك مستأجر مهتم بعقارك"
        + (f" في {city_str}" if city_str else "") + ".\n"
        f"💰 {price_str}\n\n"
        f"تحدّث معي مباشرة — أنا هنا أساعدك في التفاوض."
    )

    print(f"[NEG] Active #{neg_id}: {lead_phone} ↔ {listing_phone}", flush=True)
    return {"ok": True, "neg_id": neg_id}
