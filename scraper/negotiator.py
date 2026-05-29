#!/usr/bin/env python3
"""
مساعد المفاوض v3
- Fast intent detection (regex-first, LLM للغامض فقط)
- Connection pool (اتصال واحد لكل رسالة بدل 5)
- نقل العروض بين الطرفين (relay)
- إشعار WA للإدارة عند needs_admin
- منع تكرار رسائل الانتظار
- رسالة افتتاح تشرح المصدر
- wa_send مع retry
- CANCEL_WORDS بحدود كلمات صحيحة
"""
import os, json, re, time, html
from contextlib import contextmanager
from datetime import datetime, timezone

from bot import (get_conn, wa_send as _wa_send_raw, _phone_lock, ANTHROPIC_KEY,
                  get_contact, get_contact_registrations, build_memory_context)
from intent_parser import parse_intent

DEEPSEEK_KEY    = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))
ADMIN_WA_PHONE  = os.getenv("MASAED_WA_PHONE", "")     # رقم الإدارة يتلقى إشعارات
BASE_URL        = os.getenv("MASAED_BASE_URL", "https://masaed.wardyat.net")

# ── Retry wa_send ──────────────────────────────────────────────────────────────

def wa_send(phone: str, text: str, retries: int = 3) -> bool:
    """wa_send مع 3 محاولات وتراجع أسي."""
    for attempt in range(retries):
        try:
            _wa_send_raw(phone, text)
            return True
        except Exception as e:
            print(f"[WA RETRY {attempt+1}/{retries}] {phone}: {e}", flush=True)
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
    return False


# ── Connection context manager ─────────────────────────────────────────────────

@contextmanager
def _conn_ctx():
    """اتصال واحد لكل معالجة رسالة — يُغلق دائماً."""
    conn = get_conn()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Cancel detection ───────────────────────────────────────────────────────────

_CANCEL_WORDS_AR = {
    "لا شكرا", "مو مهتم", "لا يهمني", "إلغاء", "الغاء",
    "انهاء", "إنهاء", "مو رايه", "مش مهتم", "مو متاح",
    "ما أرغب", "ما ارغب",
}
_CANCEL_EN_EXACT = {"cancel", "stop"}


def _has_cancel(text: str) -> bool:
    t = text.strip().lower()
    # كلمات كاملة فقط (لا substrings)
    words = set(re.split(r"[\s،,؟?!.،؛؟\n]+", t))
    if words & _CANCEL_WORDS_AR:
        return True
    # إنجليزي: الرسالة كاملة فقط
    return t in _CANCEL_EN_EXACT


# ── كشف الطلبات الخاصة + شكوى الإحباط (قواعد حتمية قبل الـLLM) ───────────────────

_PHONE_REQ = ("اعطني رقم", "عطني رقم", "ابغى رقم", "ابي رقم", "وش رقم", "ايش رقم",
              "رقمه", "رقمها", "رقم المالك", "رقم المستأجر", "رقم الطرف", "كلمه على", "اتصل فيه")
_VIEW_REQ  = ("صور", "الصوره", "صورة", "معاينة", "اعاين", "أعاين", "ابغى اشوف",
              "ابي اشوف", "نشوف الشقه", "زياره", "زيارة", "أزور", "موعد معاينة")
# إحباط/سب موجّه للبوت — ليس رفضاً للصفقة
_META_COMPLAINT = ("لا تكرر", "تكرر الرسائل", "تكرر الرسايل", "غبي", "انقلع", "اقفل",
                   "ما تفهم", "مايفهم", "فاشل", "سخيف", "زهقت", "تعبتني", "مايصير")


def _is_meta_complaint(text: str) -> bool:
    return any(k in text for k in _META_COMPLAINT)


def _wants_phone(text: str) -> bool:
    return any(k in text for k in _PHONE_REQ)


def _wants_viewing(text: str) -> bool:
    return any(k in text for k in _VIEW_REQ)


# ── System Prompts ─────────────────────────────────────────────────────────────

def _role_ctx(my_role: str) -> str:
    """
    تعليمة مشتركة لكل prompt: من تكلّم ومن هو الطرف الآخر.
    تمنع قول "مالك" للمالك أو "مستأجر" للمستأجر كشخص ثالث.
    """
    other = "المستأجر" if my_role == "مالك" else "المالك"
    return (
        f"⚠️ أنت الآن تخاطب: {my_role} مباشرة.\n"
        f"الطرف الآخر غائب عن هذه المحادثة: {other}.\n"
        f"قواعد صارمة:\n"
        f"- خاطبه بـ(أنت/لك/معك) — لا تذكر \"{my_role}\" كشخص ثالث أبداً\n"
        f"- إذا أردت الإشارة للطرف الآخر قل \"{other}\"\n"
        f"- مثال خاطئ (للمالك): \"خلنا ننسق مع المالك\" ← المالك هو من أمامك!\n"
        f"- مثال صحيح (للمالك): \"خلنا ننسق موعد معك\" أو \"سأنسق مع المستأجر\"\n"
    )


_SYS_INTRO = """\
أنت "مساعد" — وسيط عقاري إلكتروني يربط ملاك العقارات بالباحثين عن سكن.
المستخدم يسألك عن هويتك أو كيف حصلت على رقمه.
{role_ctx}
أجب بوضوح في ٢-٣ جمل:
١. عرّف نفسك: "أنا مساعد العقاري، وسيط إلكتروني"
٢. اشرح المصدر: {source}
٣. اذكر العرض المتاح باختصار ثم ادفع نحو السعر مباشرة (مثال: "السعر المطروح ٤٥ ألف — وش رأيك؟").
ملاحظة: أنت الوسيط — لا تشارك أرقام هواتف، وتنسّق المعاينة بنفسك.
{context}
"""

_SYS_REJECT = """\
أنت "مساعد" — وسيط عقاري. المتحدث يُبدي تحفظاً أو رفضاً للصفقة نفسها.
{role_ctx}
- تعاطف بهدوء
- لا تُلحّ
- اترك الباب مفتوحاً: "إذا تغيّر رأيك نحن هنا"
- جملة أو جملتان
{context}
"""

# ── prompt المفاوض الموحّد: موجّه بالهدف والحالة (يُستخدم للأسئلة والعموم) ──────
_SYS_NEGOTIATOR = """\
أنت "مساعد" — وسيط عقاري سعودي محترف ونشِط، مهمتك إتمام صفقة إيجار هذا العقار تحديداً.
{role_ctx}

🎯 هدفك الوحيد: الوصول لاتفاق على سعر إيجار سنوي يرضي الطرفين، ثم إغلاق الصفقة.

كيف تتصرف:
- بادِر وكن متحمّساً وواثقاً — قُد المحادثة نحو رقم، لا تنتظر ولا تعرض قوائم خدمات.
- إن لم يُطرح سعر بعد: اذكر السعر المطروح واسأل الطرف صراحةً عن رقمه أو قبوله.
- إن وُجد عرض على الطاولة (انظر "حالة التفاوض"): علّق عليه وادفع نحو تقارب برقم محدّد.
- أجب على سؤاله من البيانات المتاحة فقط، باختصار، ثم أعِد توجيهه نحو السعر.
- جُمل قصيرة (٢-٤)، عامية سعودية طبيعية، شخصية ودودة ثابتة.

🚧 حدود صارمة:
- أنت الوسيط: لا تُعطِ رقم هاتف الطرف الآخر إطلاقاً. إن طُلب قل: "أنا الوسيط بينكما، أنقل كل شيء وأنسّق المعاينة — ما يحتاج تبادل أرقام".
- طلب الصور/المعاينة: قل إنك ستنسّق مع {other_party} وترتّب ذلك — لا تعِد بصور فوراً ولا تخترع آلية.
- لا تعرض خدمات غير موجودة (نشر إعلانات، تسويق، "شبكتنا"). مهمتك هذه الصفقة فقط.
- لا تخترع معلومات، ولا تكرّر ردك السابق حرفياً.
{context}
"""


# ── DB helpers (يقبلون conn اختياري) ──────────────────────────────────────────

def ensure_table(conn=None):
    own = conn is None
    if own:
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
                proposed_price  INT,
                lead_accepted   BOOLEAN DEFAULT false,
                owner_accepted  BOOLEAN DEFAULT false,
                needs_admin     BOOLEAN DEFAULT false,
                admin_reason    TEXT,
                admin_notified  BOOLEAN DEFAULT false,
                listing_facts   TEXT,
                chat_log        JSONB DEFAULT '[]',
                expires_at      TIMESTAMPTZ DEFAULT NOW() + INTERVAL '7 days',
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for col, typ in [
            ("lead_max_price",  "INT"),
            ("owner_min_price", "INT"),
            ("proposed_price",  "INT"),
            ("lead_accepted",   "BOOLEAN DEFAULT false"),
            ("owner_accepted",  "BOOLEAN DEFAULT false"),
            ("needs_admin",     "BOOLEAN DEFAULT false"),
            ("admin_reason",    "TEXT"),
            ("admin_notified",  "BOOLEAN DEFAULT false"),
            ("listing_facts",   "TEXT"),
        ]:
            cur.execute(
                f"ALTER TABLE sanad.masaed_negotiations "
                f"ADD COLUMN IF NOT EXISTS {col} {typ}"
            )
        conn.commit()
    if own:
        conn.close()


def _load_neg(phone: str, conn) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, lead_id, listing_id, lead_phone, listing_phone,
                   status, lead_name, listing_title, listing_city,
                   listing_price, lead_max_price, owner_min_price,
                   proposed_price, lead_accepted, owner_accepted,
                   needs_admin, admin_notified, listing_facts, chat_log
            FROM sanad.masaed_negotiations
            WHERE (lead_phone = %s OR listing_phone = %s)
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC LIMIT 1
        """, (phone, phone))
        row = cur.fetchone()
    if not row:
        return None
    cols = [
        'id','lead_id','listing_id','lead_phone','listing_phone',
        'status','lead_name','listing_title','listing_city',
        'listing_price','lead_max_price','owner_min_price',
        'proposed_price','lead_accepted','owner_accepted',
        'needs_admin','admin_notified','listing_facts','chat_log',
    ]
    d = dict(zip(cols, row))
    d['chat_log'] = d['chat_log'] or []
    return d


def _load_listing_facts(listing_id: int, conn) -> str:
    if not listing_id:
        return ""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT title, body, rooms, property_type, price, city
                FROM sanad.masaed_listings WHERE id = %s
            """, (listing_id,))
            row = cur.fetchone()
        if not row:
            return ""
        title, body, rooms, prop_type, price, city = row
        body_clean = html.unescape(body or "")
        body_clean = re.sub(r'<[^>]+>', '', body_clean)
        body_clean = re.sub(r'https?://\S+', '', body_clean)
        body_clean = re.sub(r'\d+\.\d{5,},\d+\.\d{5,}', '', body_clean)
        body_clean = re.sub(r'\n{3,}', '\n\n', body_clean).strip()
        if len(body_clean) > 600:
            body_clean = body_clean[:600] + "…"
        parts = [f"العقار: {title}"]
        if city:     parts.append(f"المدينة: {city}")
        if rooms:    parts.append(f"الغرف: {rooms}")
        if prop_type:parts.append(f"النوع: {prop_type}")
        if price:    parts.append(f"السعر: {price:,} ر/سنة")
        if body_clean: parts.append(f"\nالتفاصيل:\n{body_clean}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[NEG] listing_facts: {e}", flush=True)
        return ""


def _update_neg(neg_id: int, conn, **fields):
    if not fields:
        return
    with conn.cursor() as cur:
        sets = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(
            f"UPDATE sanad.masaed_negotiations "
            f"SET {sets}, updated_at = NOW() WHERE id = %s",
            list(fields.values()) + [neg_id]
        )
    conn.commit()


def _append_log(neg_id: int, role: str, text: str, conn):
    ts = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_negotiations
            SET chat_log   = chat_log || %s::jsonb,
                updated_at = NOW()
            WHERE id = %s
        """, (json.dumps([{"role": role, "text": text, "ts": ts}]), neg_id))
    conn.commit()


def _close(neg_id: int, status: str, conn, agreed_price: int = None):
    _update_neg(neg_id, conn, status=status, agreed_price=agreed_price)


# ── Admin notification ─────────────────────────────────────────────────────────

def _notify_admin(neg: dict, reason: str, conn):
    """إشعار إدارة واحدة عبر WA — لا تكرار."""
    neg_id = neg["id"]

    # منع التكرار
    if neg.get("admin_notified") and neg.get("admin_reason") == reason:
        return

    label = {
        "near_agreement": "قريب من الاتفاق",
        "ready_to_close": "جاهز للإغلاق",
        "party_leaving":  "طرف قد ينسحب",
        "many_rounds":    "تفاوض طويل",
    }.get(reason, reason)

    _update_neg(neg_id, conn,
                needs_admin=True,
                admin_reason=label,
                admin_notified=True)
    neg["needs_admin"]    = True
    neg["admin_reason"]   = label
    neg["admin_notified"] = True

    if not ADMIN_WA_PHONE:
        return

    title = neg.get("listing_title") or "عقار"
    lmax  = neg.get("lead_max_price")
    omin  = neg.get("owner_min_price")
    gap_line = ""
    if lmax and omin:
        gap  = omin - lmax
        mid  = round((lmax + omin) / 2 / 500) * 500
        gap_line = (
            f"\n💰 المستأجر: {lmax:,} | المالك: {omin:,}"
            f"\n📊 الفجوة: {gap:,} ر | الوسط: {mid:,} ر"
        )

    msg = (
        f"🔔 مساعد — يحتاج قرار\n"
        f"#{neg_id}: {title}\n"
        f"⚡ {label}{gap_line}\n"
        f"👉 {BASE_URL}"
    )
    wa_send(ADMIN_WA_PHONE, msg)


# ── LLM response generator ─────────────────────────────────────────────────────

def _llm(system: str, user_msg: str, max_tokens: int = 150) -> str | None:
    if ANTHROPIC_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=20.0)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens, temperature=0.3, system=system,
                messages=[{"role": "user", "content": user_msg}]
            )
            return resp.content[0].text.strip() or None
        except Exception as e:
            print(f"[NEG] Anthropic: {e}", flush=True)

    if DEEPSEEK_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1", timeout=20.0)
            resp = client.chat.completions.create(
                model="deepseek-chat", temperature=0.3,
                messages=[{"role": "system", "content": system},
                          {"role": "user",   "content": user_msg}],
                max_tokens=max_tokens
            )
            return resp.choices[0].message.content.strip() or None
        except Exception as e:
            print(f"[NEG] DeepSeek: {e}", flush=True)

    return None


def _build_context(neg: dict) -> str:
    title  = neg.get("listing_title") or "العقار"
    city   = neg.get("listing_city") or ""
    price  = neg.get("listing_price")
    p_str  = f"{price:,} ر/سنة" if price else "قابل للتفاوض"
    lines  = ["\nسياق العقار:", f"العقار: {title}"]
    if city:  lines.append(f"المدينة: {city}")
    lines.append(f"السعر: {p_str}")
    facts = neg.get("listing_facts") or ""
    if facts:
        lines.append(facts)
    # ── مساعد الحافظ: أضف ذاكرة العميل إن وُجدت ─────────────────────────────
    contact = neg.get("_contact") or {}
    if contact.get("name"):
        lines.append(f"\nاسم المتحدث: {contact['name']}")
    if contact.get("notes"):
        lines.append(f"ملاحظات: {contact['notes']}")

    # ── حالة التفاوض الحالية: ليعرف الـLLM أين وصلت العروض وما هدفه التالي ──────
    lmax = neg.get("lead_max_price")
    omin = neg.get("owner_min_price")
    prop = neg.get("proposed_price")
    state = []
    if lmax: state.append(f"- أعلى سعر عرضه المستأجر: {lmax:,} ر")
    if omin: state.append(f"- أقل سعر يقبله المالك: {omin:,} ر")
    if prop: state.append(f"- السعر المقترح حالياً: {prop:,} ر")
    if lmax and omin: state.append(f"- الفجوة بينهما: {omin - lmax:,} ر")
    if state:
        lines.append("\nحالة التفاوض (استخدمها لتدفع نحو الإغلاق):")
        lines.extend(state)
    else:
        lines.append("\nحالة التفاوض: لا توجد عروض سعرية بعد — مهمتك أن تستخرج رقماً من الطرف الآن.")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# آلة حالات التفاوض (Negotiation FSM)
# الحالة = دالّة محضة على بيانات الصف (مصدر حقيقة واحد، لا انجراف).
# الكود يفرض المسار والانتقالات؛ الـLLM يصوغ فقط ضمن هدف الحالة الحالية.
# ══════════════════════════════════════════════════════════════════════════════

STATE_OPENING  = "opening"    # لا أسعار بعد
STATE_RELAY    = "relay"      # طرف واحد طرح سعراً
STATE_CONVERGE = "converge"   # كلا السعرين معروفان والفجوة قائمة
STATE_CLOSING  = "closing"    # سعر وسط مقترح، بانتظار القبول
STATE_AGREED   = "agreed"
STATE_CANCELLED = "cancelled"


def fsm_state(neg: dict) -> str:
    """اشتقّ حالة التفاوض من بيانات الصف."""
    st = neg.get("status")
    if st in (STATE_AGREED, STATE_CANCELLED):
        return st
    if neg.get("proposed_price"):
        return STATE_CLOSING
    lmax, omin = neg.get("lead_max_price"), neg.get("owner_min_price")
    if lmax and omin:
        return STATE_CONVERGE
    if lmax or omin:
        return STATE_RELAY
    return STATE_OPENING


def fsm_goal(state: str, neg: dict, my_role: str) -> str:
    """الهدف الحتمي للحالة الحالية — يُحقن في صياغة الـLLM (لا يقرّر المسار، يوجّه الصياغة)."""
    if state == STATE_CLOSING:
        p = neg.get("proposed_price") or 0
        return (f"يوجد سعر وسط مقترح ({p:,} ر/سنة). هدفك: احصل على موافقة صريحة "
                f"(\"موافق/نعم\") على هذا الرقم تحديداً — لا تفتح مواضيع أخرى.")
    if state == STATE_CONVERGE:
        return ("كلا الطرفين طرح سعراً والفجوة قائمة. هدفك: قرّب وجهتي النظر "
                "وادفع نحو رقم وسطٍ محدّد.")
    if state == STATE_RELAY:
        mine = neg.get("lead_max_price") if my_role == "مستأجر" else neg.get("owner_min_price")
        if mine:
            return ("هذا الطرف طرح سعره والطرف الآخر لم يردّ بعد. هدفك: طمئنه "
                    "أنك نقلت عرضه وتنتظر ردّ الطرف الآخر — دون وعود زائدة.")
        return ("الطرف الآخر طرح سعراً وهذا الطرف لم يطرح بعد. هدفك المباشر: "
                "اطلب من هذا الطرف رقمه الصريح للإيجار السنوي.")
    # OPENING
    return ("لا توجد أسعار مطروحة بعد. هدفك المباشر: استخرج رقماً صريحاً من هذا "
            "الطرف الآن (كم تقبل/تعرض للإيجار السنوي؟).")


def _generate_reply(neg: dict, my_role: str, text: str, intent: dict) -> str:
    context     = _build_context(neg)
    # ── حقن هدف الحالة الحالية: الـLLM يصوغ ضمن هدف الـFSM لا أكثر ──────────────
    _state = fsm_state(neg)
    context += f"\n\n🎯 حالة التفاوض الآن: [{_state}] — هدفك في هذا الرد: {fsm_goal(_state, neg, my_role)}"
    role_ctx    = _role_ctx(my_role)
    other_party = "المستأجر" if my_role == "مالك" else "المالك"
    intent_type = intent.get("intent", "other")
    is_identity = intent.get("_identity", False)

    if is_identity:
        source = "وجدت رقمك من إعلانك في حراج" if my_role == "مالك" \
                 else "وجدت رقمك من طلبك المُسجَّل للبحث عن سكن"
        system = _SYS_INTRO.format(role_ctx=role_ctx, source=source, context=context)
    elif intent_type == "reject":
        system = _SYS_REJECT.format(role_ctx=role_ctx, context=context)
    else:
        # الأسئلة والعموم: prompt المفاوض الموحّد الموجّه بالهدف
        system = _SYS_NEGOTIATOR.format(role_ctx=role_ctx, other_party=other_party, context=context)

    return _llm(system, f"[{my_role}]: {text}", max_tokens=400) or "وصلت رسالتك، نكمّل — وش السعر اللي يناسبك؟"


# ── Relay price between parties ────────────────────────────────────────────────

def _relay_price(neg: dict, sender_role: str, amount: int, conn):
    """ينقل العرض للطرف الآخر فوراً."""
    is_lead  = sender_role == "مستأجر"
    other    = neg["listing_phone"] if is_lead else neg["lead_phone"]
    title    = neg.get("listing_title") or "العقار"
    lmax     = neg.get("lead_max_price")
    omin     = neg.get("owner_min_price")

    if is_lead:
        # أخبر المالك بعرض المستأجر
        prev = f" (عرضه السابق: {lmax:,} ر)" if lmax and lmax != amount else ""
        msg = (
            f"🔔 المستأجر يعرض {amount:,} ريال للعقار{prev}.\n"
            f"هل تقبل؟ أرسل موافق أو عرض مختلف."
        )
    else:
        # أخبر المستأجر بموقف المالك
        prev = f" (طلبك: {lmax:,} ر)" if lmax else ""
        msg = (
            f"🔔 المالك يقبل بحد أدنى {amount:,} ريال{prev}.\n"
            f"هل تقبل؟ أرسل موافق أو اقترح سعراً."
        )

    _append_log(neg["id"], f"relay→{'مالك' if is_lead else 'مستأجر'}", msg, conn)
    wa_send(other, msg)


def _propose_middle(neg: dict, lead_max: int, owner_min: int, conn):
    """يقترح سعراً وسطاً للطرفين عند التقارب."""
    mid   = round((lead_max + owner_min) / 2 / 500) * 500
    gap   = owner_min - lead_max
    neg_id = neg["id"]

    _update_neg(neg_id, conn, proposed_price=mid,
                lead_accepted=False, owner_accepted=False)
    neg["proposed_price"] = mid

    msg_lead = (
        f"📊 اقتراح الوسط: {mid:,} ريال/سنة\n"
        f"(طلبك: {lead_max:,} | المالك: {owner_min:,} | فجوة: {gap:,} ر)\n"
        f"أرسل موافق للقبول أو سعراً مختلفاً."
    )
    msg_owner = (
        f"📊 اقتراح الوسط: {mid:,} ريال/سنة\n"
        f"(المستأجر: {lead_max:,} | طلبك: {owner_min:,} | فجوة: {gap:,} ر)\n"
        f"أرسل موافق للقبول أو سعراً مختلفاً."
    )

    _append_log(neg_id, "bot→مستأجر", msg_lead, conn)
    _append_log(neg_id, "bot→مالك",   msg_owner, conn)
    wa_send(neg["lead_phone"],    msg_lead)
    wa_send(neg["listing_phone"], msg_owner)


# ── Main handler ───────────────────────────────────────────────────────────────

def _handle_active(neg: dict, phone: str, text: str, conn) -> bool:
    neg_id   = neg["id"]
    is_lead  = (phone == neg["lead_phone"])
    my_role  = "مستأجر" if is_lead else "مالك"
    other    = neg["listing_phone"] if is_lead else neg["lead_phone"]

    # ── إلغاء صريح ────────────────────────────────────────────────────────────
    if _has_cancel(text):
        _close(neg_id, "cancelled", conn)
        _append_log(neg_id, my_role, text, conn)
        wa_send(phone, "تم إنهاء التفاوض. شكراً 🙏")
        wa_send(other, "أُنهي التفاوض من الطرف الآخر.")
        return True

    # ── قراءة proposed_price من DB (لتجنب race condition) ────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT proposed_price, lead_accepted, owner_accepted, needs_admin
            FROM sanad.masaed_negotiations WHERE id = %s
        """, (neg_id,))
        row = cur.fetchone()
    if row:
        neg["proposed_price"]  = row[0]
        neg["lead_accepted"]   = row[1]
        neg["owner_accepted"]  = row[2]
        neg["needs_admin"]     = row[3]

    # ── تتبع الموافقة على سعر مقترح ──────────────────────────────────────────
    if neg.get("proposed_price"):
        intent_q = parse_intent(text)
        if intent_q["intent"] == "accept":
            field  = "lead_accepted" if is_lead else "owner_accepted"
            _update_neg(neg_id, conn, **{field: True})
            neg[field] = True
            _append_log(neg_id, my_role, text, conn)

            lead_ok  = neg.get("lead_accepted")  or is_lead
            owner_ok = neg.get("owner_accepted") or (not is_lead)
            p_str    = f"{neg['proposed_price']:,}"

            if lead_ok and owner_ok:
                # انتقال FSM: closing → agreed (إتمام تلقائي عند اتفاق الطرفين)
                print(f"[NEG #{neg_id}] FSM: closing → agreed (price={neg['proposed_price']:,})", flush=True)
                _close(neg_id, "agreed", conn, agreed_price=neg["proposed_price"])
                msg = (f"🎉 تم إتمام الصفقة على {p_str} ر/سنة بنجاح ✅\n"
                       f"مبروك! سيتم التواصل لإكمال إجراءات العقد.")
                wa_send(phone, msg)
                wa_send(other, msg)
                _notify_admin(neg, "ready_to_close", conn)  # إشعار للعلم فقط
            else:
                wa_send(phone,
                    f"تم تسجيل موافقتك على {p_str} ر/سنة ✅\n"
                    f"ننتظر رد الطرف الآخر.")
            return True

    # ── parse intent + سجّل حالة الـFSM الحالية ───────────────────────────────
    intent = parse_intent(text)
    _state_before = fsm_state(neg)
    print(f"[NEG #{neg_id}] state={_state_before} {my_role} intent={intent['intent']} "
          f"amount={intent.get('amount')} firm={intent.get('is_firm')}", flush=True)

    _append_log(neg_id, my_role, text, conn)

    # ── قبول صريح (بدون proposed_price) ─────────────────────────────────────
    if intent["intent"] == "accept":
        reply = "ممتاز! وصلت موافقتك ✅\nسأُبلغ المسؤول لإتمام إجراءات الصفقة."
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        _notify_admin(neg, "ready_to_close", conn)
        return True

    # ── عرض سعر — حفظ + فحص تقارب → relay أو propose_middle ───────────────
    if intent["intent"] == "price_offer" and intent.get("amount"):
        amount = intent["amount"]
        field  = "lead_max_price" if is_lead else "owner_min_price"
        _update_neg(neg_id, conn, **{field: amount})
        neg[field] = amount

        lmax = neg.get("lead_max_price")
        omin = neg.get("owner_min_price")

        if lmax and omin:
            gap  = omin - lmax
            ref  = neg.get("listing_price") or omin
            near = (lmax >= omin) or (gap <= 1500) or (ref and gap / ref <= 0.12)

            if near:
                # انتقال FSM: converge → closing (اقتراح وسط مباشرة)
                print(f"[NEG #{neg_id}] FSM: {_state_before} → closing (propose_middle)", flush=True)
                _propose_middle(neg, lmax, omin, conn)
                _notify_admin(neg, "near_agreement", conn)
                return True
            print(f"[NEG #{neg_id}] FSM: {_state_before} → converge (gap={gap:,})", flush=True)

        # بعيدان أو طرف واحد: relay للطرف الآخر + تأكيد للمُرسِل
        print(f"[NEG #{neg_id}] FSM: → relay ({my_role} عرض {amount:,})", flush=True)
        _relay_price(neg, my_role, amount, conn)
        reply = f"وصل عرضك ({amount:,} ر) ✅ سأتابع مع الطرف الآخر وأعود إليك."
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True

    other_role = "المالك" if my_role == "مستأجر" else "المستأجر"

    # ── رفض حازم للصفقة (وليس إحباطاً من البوت) ───────────────────────────────
    if (intent["intent"] == "reject"
            and intent.get("is_firm")
            and intent.get("sentiment") == "negative"
            and not _is_meta_complaint(text)):
        _notify_admin(neg, "party_leaving", conn)
        reply = "أفهم موقفك. سأطّلع المسؤول وسنعود إليك إن وُجد حل مناسب."
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True

    # ── طلب رقم الطرف الآخر → اشرح الوساطة (لا تشارك أرقاماً) ──────────────────
    if _wants_phone(text):
        reply = (f"أنا الوسيط بينك وبين {other_role} 🤝 أنقل لك كل التفاصيل وأنسّق "
                 f"المعاينة مباشرة — ما يحتاج تبادل أرقام. وعشان نتقدّم، وش آخر سعر تقبله؟")
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True

    # ── طلب صور/معاينة → تنسيق فعلي (لا وعود وهمية ولا هلوسة) ──────────────────
    if _wants_viewing(text):
        what = "الصور وموعد المعاينة" if "صور" in text else "موعد المعاينة"
        reply = (f"تمام 👌 أنسّق مع {other_role} بخصوص {what} وأرجع لك. "
                 f"وعشان نمشّي الأمور بالتوازي — وش السعر اللي يناسبك للإيجار السنوي؟")
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True

    # ── إحباط/شكوى موجّهة للبوت → إعادة تفاعل لطيفة نحو الهدف (لا تصعيد) ────────
    if _is_meta_complaint(text):
        reply = ("آسف إذا ضايقتك 🙏 خلّنا نركّز على المهم: "
                 "وش السعر اللي تشوفه مناسب للإيجار السنوي ونتفاهم عليه؟")
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True

    # ── كثرة الرسائل ─────────────────────────────────────────────────────────
    rounds = sum(1 for e in neg.get("chat_log", [])
                 if e.get("role") in ("مستأجر", "مالك"))
    if rounds >= 8 and rounds % 3 == 0 and not neg.get("needs_admin"):
        _notify_admin(neg, "many_rounds", conn)
        reply = "شكراً على صبرك. سأُطّلع المسؤول وسيتابع معك قريباً."
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True

    # ── رد تلقائي (LLM) ───────────────────────────────────────────────────────
    reply = _generate_reply(neg, my_role, text, intent)
    # حارس التكرار: لا نرسل نفس رد البوت السابق حرفياً
    last_bot = next((e.get("text") for e in reversed(neg.get("chat_log", []))
                     if str(e.get("role", "")).startswith("bot")), None)
    if last_bot and reply.strip() == last_bot.strip():
        reply = "خلّنا نركّز على السعر 👍 وش الرقم اللي يناسبك للإيجار السنوي؟"
    _append_log(neg_id, f"bot→{my_role}", reply, conn)
    wa_send(phone, reply)
    return True


def handle_negotiation_message(phone: str, text: str) -> bool:
    with _phone_lock(phone):
        with _conn_ctx() as conn:
            neg = _load_neg(phone, conn)
            if not neg:
                return False
            # حمّل listing_facts مرة واحدة
            if not neg.get("listing_facts"):
                facts = _load_listing_facts(neg.get("listing_id"), conn)
                if facts:
                    _update_neg(neg["id"], conn, listing_facts=facts)
                    neg["listing_facts"] = facts
            # ── مساعد الحافظ: حدّث last_seen وأضف للذاكرة ──────────────────
            try:
                contact = get_contact(phone)   # upsert last_seen
                neg["_contact"] = contact      # للاستخدام في _generate_reply
            except Exception:
                pass
            return _handle_active(neg, phone, text, conn)


# ── بدء تفاوض جديد ────────────────────────────────────────────────────────────

def start_negotiation(lead_id: int, listing_id: int,
                      lead_phone: str, listing_phone: str,
                      lead_name: str = None, listing_title: str = None,
                      listing_city: str = None, listing_price: int = None) -> dict:
    ensure_table()

    if lead_phone == listing_phone:
        return {"ok": False, "error": "لا يمكن التفاوض مع نفس الرقم"}

    with _conn_ctx() as conn:
        # ── شرط التسجيل: كلا الطرفين يجب أن يكونا في masaed_registrations ──
        with conn.cursor() as cur:
            cur.execute("""
                SELECT phone FROM sanad.masaed_registrations
                WHERE phone IN (%s, %s) AND status != 'abandoned'
            """, (lead_phone, listing_phone))
            registered = {r[0] for r in cur.fetchall()}

        unregistered = []
        if lead_phone    not in registered: unregistered.append(lead_phone)
        if listing_phone not in registered: unregistered.append(listing_phone)

        if unregistered:
            return {
                "ok": False,
                "error": "يجب تسجيل الطرفين قبل بدء التفاوض",
                "unregistered": unregistered,
            }

        existing = _load_neg(lead_phone, conn)
        if existing and existing.get("listing_id") == listing_id:
            return {"ok": False, "error": "التفاوض جارٍ بالفعل", "neg_id": existing["id"]}

        # حمّل بيانات الإعلان لحفظها مسبقاً
        facts = ""
        if listing_id:
            facts = _load_listing_facts(listing_id, conn)

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sanad.masaed_negotiations
                    (lead_id, listing_id, lead_phone, listing_phone,
                     lead_name, listing_title, listing_city, listing_price,
                     listing_facts, status, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active', NOW() + INTERVAL '7 days')
                RETURNING id
            """, (lead_id, listing_id, lead_phone, listing_phone,
                  lead_name, listing_title, listing_city, listing_price, facts or None))
            neg_id = cur.fetchone()[0]
            conn.commit()

    price_str = f"{listing_price:,} ر/سنة" if listing_price else "قابل للتفاوض"
    city_str  = listing_city or ""
    title_str = listing_title or "عقار للإيجار"

    # ── مساعد الحافظ: حمّل بيانات الطرفين ───────────────────────────────────
    lead_contact    = get_contact(lead_phone)     # upsert + last_seen
    listing_contact = get_contact(listing_phone)

    lead_name_resolved    = (lead_name
                             or lead_contact.get("name")
                             or "")
    listing_name_resolved = listing_contact.get("name") or ""

    # تحقق إذا أي طرف مسجّل في المسجّل (v1)
    lead_regs    = get_contact_registrations(lead_phone)
    listing_regs = get_contact_registrations(listing_phone)
    lead_reg_note    = " (مسجّل ✅)" if lead_regs    else " (غير مسجّل)"
    listing_reg_note = " (مسجّل ✅)" if listing_regs else " (غير مسجّل)"
    print(f"[NEG] lead{lead_reg_note} listing{listing_reg_note}", flush=True)

    # رسالة المستأجر
    greeting_lead = f"مرحباً {lead_name_resolved} 👋" if lead_name_resolved else "مرحباً 👋"
    wa_send(lead_phone,
        f"{greeting_lead}، أنا مساعد العقاري — وسيط إلكتروني.\n"
        f"وجدت طلبك المُسجَّل وربطتك بعرض يناسبه:\n"
        f"📍 {title_str}" + (f" — {city_str}" if city_str else "") + "\n"
        f"💰 {price_str}\n\n"
        f"تحدّث معي مباشرة — أنا الوسيط بينك وبين المالك."
    )

    # رسالة المالك
    greeting_listing = f"مرحباً {listing_name_resolved} 👋" if listing_name_resolved else "مرحباً 👋"
    wa_send(listing_phone,
        f"{greeting_listing}، أنا مساعد العقاري — وسيط إلكتروني.\n"
        f"وجدت إعلانك في حراج وربطتك بمستأجر مهتم"
        + (f" في {city_str}" if city_str else "") + ".\n"
        f"💰 {price_str}\n\n"
        f"تحدّث معي مباشرة — أنا الوسيط بينك وبين المستأجر."
    )

    print(f"[NEG] Active #{neg_id}: {lead_phone} ↔ {listing_phone}", flush=True)
    return {"ok": True, "neg_id": neg_id}
