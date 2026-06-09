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
                  USE_ANTHROPIC, get_contact, get_contact_registrations, build_memory_context,
                  build_party_profile, wa_send_media, BASE_URL as _BASE_URL)
from intent_parser import parse_intent, amounts_in

DEEPSEEK_KEY    = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))
ADMIN_WA_PHONE  = os.getenv("MASAED_WA_PHONE", "")     # رقم الإدارة يتلقى إشعارات
BASE_URL        = os.getenv("MASAED_BASE_URL", "https://masaed.wardyat.net")

# ── Retry wa_send ──────────────────────────────────────────────────────────────

def _classify_outbound(text: str) -> str:
    """يصنّف نيّة ردّ الوسيط الصادر (للطبقة الثالثة الصادرة في صفحة خلف الكواليس)."""
    t = text or ""
    if "تم إتمام الصفقة" in t:                       return "إتمام"
    if "اقتراح الوسط" in t:                          return "اقتراح وسط"
    if ("بانتظار «موافق»" in t or "ما زال بانتظار" in t
            or "ما زال قائم" in t):                  return "تذكير"
    if "🔔" in t and "يطلب" in t:                    return "ترحيل"
    if "بخصوص استفسارك" in t:                        return "ردّ معلومة"
    if "أنا الوسيط بين" in t:                        return "شرح وساطة"
    if "وصل عرضك" in t:                              return "تأكيد عرض"
    if "تم تسجيل موافقتك" in t or "وصلت موافقتك" in t: return "تأكيد قبول"
    if "سؤال وجيه" in t and "أستوضح" in t:           return "ترحيل سؤال"
    if "أُنهي التفاوض" in t or "تم إنهاء التفاوض" in t: return "إنهاء"
    if "وضّح لي" in t and "السعر النهائي" in t:       return "طلب توضيح"
    return "ردّ وسيط"


import threading as _threading
_lang_ctx = _threading.local()   # خريطة {phone: lang} للتفاوض الجاري (للترجمة)


def wa_send(phone: str, text: str, retries: int = 3) -> bool:
    """wa_send مع 3 محاولات. 🌍 يترجم تلقائياً لرسالة لغة الطرف إن كان سياق
    التفاوض نشطاً (نقطة الترجمة الوحيدة — لا تفوت أي رسالة)."""
    try:
        _m = getattr(_lang_ctx, "map", None)
        _lang = _m.get(phone) if _m else None
        if _lang:
            import i18n
            if not i18n.is_arabic(_lang):
                text = i18n.localize(text, _lang)
                print(f"[I18N→{phone}] {_lang}: {text[:40]}", flush=True)
    except Exception as _e:
        print(f"[I18N] فشل الترجمة: {_e}", flush=True)
    for attempt in range(retries):
        try:
            _wa_send_raw(phone, text)
            try:                                    # الطبقة الثالثة الصادرة: سجّل نيّة الوسيط
                import route_trace
                route_trace.add("صادر", phone, "negotiate",
                                _classify_outbound(text), "💬 المفاوض", text)
            except Exception:
                pass
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


# سمات العقار التي قد يسأل عنها طرف — إن لم تكن بالحقائق المخزّنة نطلبها من الآخر
_ATTR_WORDS = ("مصعد", "موقف", "دور", "الطابق", "طابق", "مفروش", "مساحة", "اطلال",
               "إطلال", "حديقة", "مسبح", "تكييف", "مكيف", "مطبخ", "عمر العقار",
               "صيانة", "فواتير", "كهرباء", "ماء", "واجهة", "خادمة", "مستودع",
               "اثاث", "أثاث", "حمام", "حمامات", "صالة")


def _asks_unknown_attr(text: str, facts: str) -> bool:
    """سؤال عن سمة عقار غير مذكورة في الحقائق المخزّنة → يستلزم سؤال الطرف الآخر."""
    facts = facts or ""
    hits = [w for w in _ATTR_WORDS if w in text]
    return bool(hits) and any(w not in facts for w in hits)


def _record_incidental_price(neg: dict, is_lead: bool, amount: int, conn):
    """يسجّل سعراً ذُكر ضمن رسالة (لا في فرع عرض صريح) مع حارس اتجاهي:
    سقف المستأجر لا يرتفع وأرضية المالك لا تنخفض ضمنياً — تفادياً للعروض
    المشروطة («٤٠٥٠٠ أو ٤٠ نقداً») التي تزيّف التقارب. يُرجع المبلغ المسجَّل أو None."""
    field = "lead_max_price" if is_lead else "owner_min_price"
    cur = neg.get(field)
    if cur is not None:
        if (is_lead and amount > cur) or ((not is_lead) and amount < cur):
            return None                        # حركة عكسية ضمنية → تجاهل
    if cur != amount:
        _update_neg(neg["id"], conn, **{field: amount}); neg[field] = amount
    return amount


# صيغ متنوّعة لسؤال السعر — نُدوّرها كي لا تتكرر الرسالة نفسها حرفياً
_PRICE_PROMPTS = (
    "وش السعر اللي يناسبك للإيجار السنوي؟",
    "وش آخر رقم تقبل تقفل عليه؟",
    "كم الرقم اللي ترتاح له ونمشّيها؟",
    "على أي مبلغ نتفق نهائياً؟",
)


def _price_prompt(neg: dict) -> str:
    """صيغة سؤال سعر تختلف عن سابقتها (مانع تكرار)."""
    n = sum(1 for e in neg.get("chat_log", []) if str(e.get("role", "")).startswith("bot"))
    return _PRICE_PROMPTS[n % len(_PRICE_PROMPTS)]


# علامات القبول الناعم: موافقة على السعر ولو أُلحقت بشرط/سؤال
_SOFT_ACCEPT_MARKERS = ("موافق", "أوافق", "اوافق", "مقبول", "تمام", "ماشي", "نقفل",
                        "نقفلها", "تمت", "تمّت", "نتفق", "اتفقنا", "أقبل", "اقبل",
                        "زي ما قلت", "زي ماقلت", "زي ما اقترح", "نمشيها", "نمشّيها",
                        "خلاص", "اوكي", "أوكي", "نعتبرها تمت", "نختمها")


def _is_soft_accept(text: str, proposed_price: int) -> bool:
    """قبول ناعم: ذكر السعر المقترح نفسه مع علامة موافقة، ولو ألحق شرطاً/سؤالاً.
    لا يُعدّ قبولاً إن ذكر مبلغاً مختلفاً (فذاك تفاوض/عرض مضاد لا قبول)."""
    if not proposed_price or not any(m in text for m in _SOFT_ACCEPT_MARKERS):
        return False
    amts = amounts_in(text)
    if amts:                                   # ذكر مبلغاً → يجب أن يكون المقترح وحده
        return proposed_price in amts and all(a == proposed_price for a in amts)
    return True                                # علامة موافقة بلا رقم → قبول للمقترح


# أسئلة يطرحها المالك عن الباحث/المستأجر (عن الشخص لا العقار)
_SEEKER_ATTR = ("وظيف", "شغل", "عمل", "راتب", "دخل", "كفيل", "كفال",
                "عدد", "أفراد", "افراد", "اطفال", "أطفال", "عائلت", "عيال",
                "متى تسكن", "متى بتسكن", "موعد السكن", "مدة", "كم سنة", "كم سنه",
                "تدفع", "الدفع", "شيك", "كاش", "دفعات", "جنسي", "حيوان", "بيت شعر")


# علامات تأليف/تردّد/وعد بالسؤال في رد النموذج → دليل أنه لا يملك المعلومة (يُحظر التأليف)
_HEDGE_MARKERS = ("ما أعرف", "ما اعرف", "لا أعرف", "لا اعرف", "لست متأكد", "لست متأكّد",
                  "غير متأكد", "أعتقد", "اعتقد", "ربما", "يمكن أن", "سأتأكد", "سأتحقق",
                  "بأتأكد", "بأتحقق", "خلني أتأكد", "خلني أتحقق", "لا تتوفر", "غير متوفر",
                  "ما عندي معلومة", "ما لدي", "أظن", "اظن", "على ما أعتقد",
                  # وعود الإحالة (يقولها بدل أن يفعلها) — نلتقطها لنُحيل فعلاً:
                  "أسأل المالك", "اسأل المالك", "أسأل الطرف", "أسأل صاحب", "بسأل المالك",
                  "بأسأل", "راح أسأل", "أستفسر", "استفسر", "أرد عليك", "أرجع لك",
                  "أوافيك", "أتأكد من المالك", "أتأكد من الطرف", "نسأل المالك")


def _is_hedge(reply: str) -> bool:
    """هل الرد يحمل تردّداً/تأليفاً (لا يملك الإجابة فعلاً)؟"""
    return any(m in reply for m in _HEDGE_MARKERS)


# أسئلة السعر/التفاوض → يجيب عليها المفاوض مباشرة (لا تُحال)
_PRICE_Q = ("سعر", "السعر", "الايجار", "الإيجار", "تنزل", "تنزيل", "المبلغ",
            "خصم", "تخفيض", "قابل", "دفعات", "الدفع", "شهري", "سنوي")
# كلمات شائعة نتجاهلها عند فحص «هل الإجابة في الحقائق»
_STOP = {"هل", "فيه", "يوجد", "في", "الحي", "الشقة", "العقار", "عندك", "عندكم",
         "كم", "وش", "ايش", "هذا", "هذه", "قريب", "قريبة", "من", "على", "الى",
         "عن", "ولا", "أو", "او", "مع", "كذا", "متوفر", "متوفرة"}


def _answerable_from_facts(text: str, neg: dict) -> bool:
    """هل تظهر كلمة مفتاحية من السؤال في الحقائق المخزّنة؟ (فيمكن الإجابة دون تأليف)."""
    facts = " ".join(str(neg.get(k) or "") for k in
                     ("listing_facts", "listing_title", "listing_city"))
    words = [w for w in re.findall(r"[؀-ۿ]{3,}", text) if w not in _STOP]
    return any(w in facts for w in words) if words else False


def _needs_relay(my_role: str, text: str, neg: dict) -> bool:
    """
    بروتوكول جلب المعلومة الناقصة (صارم): يُحال السؤال للطرف الآخر إن لم تكن
    إجابته متوفّرة لدينا — منعاً لأي تأليف. (يُستدعى داخل فرع intent=='question').
    """
    if any(k in text for k in _PRICE_Q):
        return False                       # سؤال سعر/تفاوض → المفاوض يجيب
    if my_role == "مستأجر":               # يسأل عن العقار/الحي
        facts = " ".join(str(neg.get(k) or "") for k in
                         ("listing_facts", "listing_title", "listing_city"))
        # سأل عن سمة عقار محدّدة (مصعد/موقف/فواتير…) غير موجودة في الحقائق → أحِل (منع تأليف)
        if _asks_unknown_attr(text, facts):
            return True
        # أو لم تظهر أي كلمة من السؤال في الحقائق إطلاقاً → أحِل
        return not _answerable_from_facts(text, neg)
    # المالك يسأل عن الباحث (معلومات الباحث ليست لدينا غالباً → أحِل)
    return any(w in text for w in _SEEKER_ATTR) or not _answerable_from_facts(text, neg)


# ── System Prompts (الأقوال) — معزولة في prompts.py ───────────────────────────
from prompts import (role_ctx as _role_ctx, sys_intro as _sys_intro,
                     sys_reject as _sys_reject, sys_negotiator as _sys_negotiator)


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
            ("pending_req",     "JSONB"),
            ("alt_offers",      "JSONB"),
            ("phase",           "TEXT"),
            ("seeker_reg",      "INT DEFAULT 0"),
            ("owner_reg",       "INT DEFAULT 0"),
            ("lead_lang",       "TEXT"),
            ("listing_lang",    "TEXT"),
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
                   needs_admin, admin_notified, listing_facts, chat_log,
                   pending_req, phase, seeker_reg, owner_reg, lead_lang, listing_lang
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
        'pending_req','phase','seeker_reg','owner_reg','lead_lang','listing_lang',
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
    # 🔧 نظام التطوير: راجِع المحادثة الحقيقية عند إغلاقها (خيط منفصل)
    try:
        import reviewer
        reviewer.review_negotiation_async(neg_id)
    except Exception as _re:
        print(f"[REVIEWER] تعذّر إطلاق مراجعة #{neg_id}: {_re}", flush=True)


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
    if ANTHROPIC_KEY and USE_ANTHROPIC:
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
    # ── مساعد الحافظ الذكي: ملف العميل المضغوط (حقائق فقط) ───────────────────
    profile = neg.get("_profile")
    if profile:
        lines.append("\n" + profile)
    else:
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
    # ── 🎭 حقن نبرة المزاج (المرحلة ١): يصوغ النموذج بالنبرة المناسبة لا ببرود ──
    try:
        from strategy import mood_guidance
        context += "\n\n" + mood_guidance(text, intent)
    except Exception:
        pass
    role_ctx    = _role_ctx(my_role)
    other_party = "المستأجر" if my_role == "مالك" else "المالك"
    intent_type = intent.get("intent", "other")
    is_identity = intent.get("_identity", False)

    if is_identity:
        source = "وجدت رقمك من إعلانك في حراج" if my_role == "مالك" \
                 else "وجدت رقمك من طلبك المُسجَّل للبحث عن سكن"
        system = _sys_intro().format(role_ctx=role_ctx, source=source, context=context)
    elif intent_type == "reject":
        system = _sys_reject().format(role_ctx=role_ctx, context=context)
    else:
        # الأسئلة والعموم: prompt المفاوض الموحّد الموجّه بالهدف
        system = _sys_negotiator().format(role_ctx=role_ctx, other_party=other_party, context=context)

    # ردّ الـLLM يُولَّد بالعربية، وتُترجَم لغة الطرف تلقائياً في wa_send (نقطة واحدة)
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
    """يقترح سعر إقفال عند التقارب. عند التداخل (المستأجر يقبل ≥ حدّ المالك)
    نُسوّي عند حدّ المالك مباشرةً — لا منتصف أعلى منه — لإقفال أسرع وأعدل."""
    if lead_max >= owner_min:
        mid = owner_min                       # تداخل → سعر المالك يُرضي الطرفين
    else:
        mid = round((lead_max + owner_min) / 2 / 500) * 500
    gap   = owner_min - lead_max
    neg_id = neg["id"]

    # إن سبق اقتراح نفس السعر: لا نُعيد إرسال أي شيء (ولا نصفّر الموافقات) — صمت
    # تام، ويطوي المُنادي التذكيرَ في ردّه الواحد بدل رسالة تنبيه متكرّرة مستقلّة.
    if neg.get("proposed_price") == mid:
        return False

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
    return True                                 # اقتراح جديد أُرسِل للطرفين


def _maybe_propose(neg: dict, conn) -> bool:
    """إن توفّر سعرا الطرفين وتقاربا → أطلق اقتراح الوسط (يُستدعى بعد أي التقاط
    سعر عَرَضي حتى لا يتعطّل الاقتراح حين يُلحق الطرفان أسئلة بكل رسالة).
    يُرجع True إن حصل اقتراح/تنبيه."""
    lmax = neg.get("lead_max_price")
    omin = neg.get("owner_min_price")
    if not (lmax and omin):
        return False
    gap  = omin - lmax
    ref  = neg.get("listing_price") or omin
    near = (lmax >= omin) or (gap <= 1500) or (ref and gap / ref <= 0.12)
    if not near:
        return False
    return _propose_middle(neg, lmax, omin, conn)   # True فقط لو اقتراح جديد


def _reminded_recently(neg: dict) -> bool:
    """هل ذكّرنا بالمقترح القائم ضمن آخر ٤ رسائل بوت؟ (لكبح تكرار التذكير)."""
    recent = [e.get("text", "") for e in neg.get("chat_log", [])
              if str(e.get("role", "")).startswith("bot")][-4:]
    return any(("بانتظار موافقتك" in t) or ("ما زال قائماً" in t) for t in recent)


def _standing_tail(neg: dict) -> str:
    """ذيل تذكير بالمقترح القائم — يُطوى داخل ردّ واحد، ومرّة كل نافذة فقط."""
    p = neg.get("proposed_price")
    if not p or _reminded_recently(neg):
        return ""
    return f"\nوالمقترح {p:,} ر ما زال بانتظار موافقتك 🤝"


# ── جلب الناقص + الصور (Feature: relay & media) ─────────────────────────────────

def _listing_photos(neg: dict, conn) -> list:
    """صور العقار — أولاً من الإعلان المسحوب (masaed_listings.photos) بالـid الفعلي،
    ثم بالهاتف، ثم تسجيل المالك. تُرسَل من القاعدة لا تُطلب من المالك."""
    def _urls(raw):
        out = []
        for p in (raw or []):
            if isinstance(p, dict) and p.get("url"):
                out.append(p["url"])
            elif isinstance(p, str) and p.startswith("http"):
                out.append(p)
        return out
    pv = list(set(_pvar(neg["listing_phone"])))
    with conn.cursor() as cur:
        # 1) الإعلان المسحوب بالـid الفعلي للصفقة (الأدقّ — listing_id موثوق)
        if neg.get("listing_id"):
            cur.execute("SELECT photos FROM sanad.masaed_listings WHERE id=%s", (neg["listing_id"],))
            r = cur.fetchone()
            if r and _urls(r[0]):
                return _urls(r[0])
        # 2) بالهاتف (احتياط لو listing_id تسجيلاً)
        cur.execute("""SELECT photos FROM sanad.masaed_listings WHERE phone = ANY(%s)
                       AND photos IS NOT NULL ORDER BY id DESC LIMIT 1""", (pv,))
        r = cur.fetchone()
        if r and _urls(r[0]):
            return _urls(r[0])
        # 3) تسجيل المالك (احتياط أخير)
        cur.execute("""SELECT photos FROM sanad.masaed_registrations
                       WHERE phone = ANY(%s) AND type='listing' AND photos IS NOT NULL
                         AND jsonb_array_length(photos) > 0
                       ORDER BY created_at DESC LIMIT 1""", (pv,))
        r = cur.fetchone()
        if r:
            return _urls(r[0])
    return []


def _relay_info_request(neg: dict, my_role: str, text: str, conn):
    """أعد صياغة طلب الطرف وأرسله للطرف الآخر، وسجّل طلباً معلّقاً لإغلاق الحلقة."""
    is_lead     = (my_role == "مستأجر")
    asker_phone = neg["lead_phone"] if is_lead else neg["listing_phone"]
    other_phone = neg["listing_phone"] if is_lead else neg["lead_phone"]
    asker       = "المستأجر" if is_lead else "المالك"
    reworded = _llm(
        "أنت وسيط عقاري. أعد صياغة طلب الطرف التالي كرسالة قصيرة مهذّبة موجّهة "
        "للطرف الآخر لجلب المعلومة. أرجع نص الرسالة فقط بلا مقدمات.",
        f"[{asker} يطلب]: {text}", max_tokens=120
    ) or text
    msg = f"🔔 {asker} يطلب:\n{reworded}\n\nياليت تزوّدني بها وأنقلها له فوراً."
    _append_log(neg["id"], f"relay→{'مالك' if is_lead else 'مستأجر'}", msg, conn)
    # سجّل الطلب المعلّق: من سأل + سؤاله — لإرجاع رد الطرف الآخر إليه لاحقاً
    _update_neg(neg["id"], conn, pending_req=json.dumps({
        "asker_role": asker, "asker_phone": asker_phone, "question": text[:200],
    }))
    wa_send(other_phone, msg)


# ── Main handler ───────────────────────────────────────────────────────────────

def _choice_from(text: str, n: int):
    """يستخرج اختيار الباحث (رقم 1..n) من رسالته بعد عرض البدائل. يُرجع الفهرس (0-based) أو None."""
    t = (text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    for m in re.finditer(r'(?<!\d)([1-9])(?!\d)', t):
        v = int(m.group(1))
        if 1 <= v <= n:
            return v - 1
    for k, v in {"الاول": 1, "الأول": 1, "اول": 1, "الثاني": 2, "ثاني": 2,
                 "الثالث": 3, "ثالث": 3}.items():
        if k in (text or "") and v <= n:
            return v - 1
    return None


def _link_alternative(neg: dict, lead_id, chosen: dict, conn):
    """يربط اختيار الباحث بالبديل: ينشئ توفيقاً pending جاهزاً للمحاكاة (تحت البوّابة)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sanad.masaed_matches
                    (lead_id, listing_id, score, reason, status,
                     req_phone, lst_phone, lst_price, req_city, lst_city)
                VALUES (%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s)
                ON CONFLICT (lead_id, listing_id) DO UPDATE SET
                    status='pending', score=GREATEST(sanad.masaed_matches.score,90),
                    reason=EXCLUDED.reason, updated_at=NOW()
            """, (lead_id or 0, chosen.get("id"), 90, "اختاره الباحث من البدائل",
                  neg.get("lead_phone"), chosen.get("phone"), chosen.get("price"),
                  neg.get("listing_city"), chosen.get("city")))
            conn.commit()
        print(f"[ALT-LINK] الباحث {neg.get('lead_phone')} اختار العرض {chosen.get('id')} → توفيق pending", flush=True)
    except Exception as e:
        print(f"[ALT-LINK] {e}", flush=True)


def _seeker_alternatives(lead_phone: str, current_listing_id, conn, limit: int = 3) -> list:
    """أفضل عروض أخرى مطابقة لنفس الباحث (عدا العرض الحالي) — لترشيحها عند طلب بدائل."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l.id, l.title, l.price, l.city, l.phone
                FROM sanad.masaed_matches m
                JOIN sanad.masaed_listings l ON l.id = m.listing_id
                WHERE m.req_phone = %s AND m.listing_id <> %s AND m.status = 'pending'
                  AND l.phone IS NOT NULL
                ORDER BY m.score DESC NULLS LAST LIMIT %s
            """, (lead_phone, current_listing_id or 0, limit))
            return [{"id": r[0], "title": r[1], "price": r[2], "city": r[3], "phone": r[4]}
                    for r in cur.fetchall()]
    except Exception as e:
        print(f"[ALT] تعذّر جلب البدائل: {e}", flush=True)
        return []


def _pvar(p):
    """صيغتا الهاتف السعودي (966 و0) للمطابقة في الاستعلامات."""
    p = (p or "").strip()
    v = {p}
    if p.startswith("966") and len(p) > 3:
        v.add("0" + p[3:])
    elif p.startswith("0") and len(p) > 1:
        v.add("966" + p[1:])
    return list(v)


def _safe_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


# ── 🌍 اللغة: كشف لغة كل طرف وإرسال رسائل المراحل بلغته ──────────────────────
def _party_lang(neg, phone):
    return neg.get("lead_lang") if phone == neg.get("lead_phone") else neg.get("listing_lang")


def _detect_party_lang(neg, phone, text, conn):
    """يكشف لغة الطرف من رسالته إن لم تُخزَّن، ويحفظها."""
    is_lead = (phone == neg.get("lead_phone"))
    col = "lead_lang" if is_lead else "listing_lang"
    if neg.get(col) or not (text or "").strip():
        return
    import i18n
    lang = i18n.detect_lang(text)
    neg[col] = lang
    _update_neg(neg["id"], conn, **{col: lang})
    if not i18n.is_arabic(lang):
        print(f"[I18N #{neg['id']}] {'lead' if is_lead else 'listing'} = {lang}", flush=True)


def _say(neg, phone, text_ar):
    """يرسل رسالة مرحلة؛ الترجمة تتم تلقائياً في wa_send عبر خريطة اللغة."""
    wa_send(phone, text_ar)


def _fetch_understanding(role, phone, conn):
    """فهم الطرف المستخرَج من إعلانه (من كاش masaed_comprehension)."""
    try:
        with conn.cursor() as cur:
            if role == "seeker":
                cur.execute("""SELECT id FROM sanad.masaed_leads WHERE phone = ANY(%s)
                               AND listing_type='wanted' ORDER BY scraped_at DESC LIMIT 1""", (_pvar(phone),))
                r = cur.fetchone()
                if not r:
                    return {}
                cur.execute("SELECT profile FROM sanad.masaed_comprehension WHERE source='lead' AND ext_id=%s", (str(r[0]),))
            else:
                cur.execute("""SELECT id FROM sanad.masaed_listings WHERE phone = ANY(%s)
                               ORDER BY id DESC LIMIT 1""", (_pvar(phone),))
                r = cur.fetchone()
                if not r:
                    return {}
                cur.execute("SELECT profile FROM sanad.masaed_comprehension WHERE source='listing' AND ext_id=%s", (str(r[0]),))
            p = cur.fetchone()
            return (p[0] if p and p[0] else {})
    except Exception as e:
        print(f"[REG] fetch understanding: {e}", flush=True)
        return {}


def _reg_fields(profile, role):
    """من الهوية الموحّدة."""
    import identity
    return identity.reg_fields(profile, role)


def _create_party_registration(role, phone, neg, profile, conn):
    """مساعد المسجّل: ينشئ تسجيلاً للطرف في قاعدتنا ويُرجع رابط صفحته الاحترافية."""
    base = os.getenv("MASAED_BASE_URL", "https://masaed.wardyat.net")
    profile = profile or {}
    try:
        with conn.cursor() as cur:
            if role == "seeker":
                cur.execute("""INSERT INTO sanad.masaed_registrations
                        (phone,name,type,slug,city,district,property_type,rooms,budget_annual,status)
                        VALUES (%s,%s,'wanted',%s,%s,%s,%s,%s,%s,'complete')
                        ON CONFLICT (slug) DO UPDATE SET status='complete', updated_at=NOW() RETURNING id""",
                    (phone, neg.get("lead_name") or "باحث", f"neg-seeker-{phone}",
                     profile.get("city") or neg.get("listing_city"), profile.get("district"),
                     profile.get("property_type"), _safe_int(profile.get("rooms")),
                     _safe_int(profile.get("price"))))
            else:
                cur.execute("""INSERT INTO sanad.masaed_registrations
                        (phone,name,type,slug,city,district,property_type,rooms,price_annual,status)
                        VALUES (%s,%s,'listing',%s,%s,%s,%s,%s,%s,'complete')
                        ON CONFLICT (slug) DO UPDATE SET status='complete', updated_at=NOW() RETURNING id""",
                    (phone, "مالك", f"neg-owner-{phone}",
                     profile.get("city") or neg.get("listing_city"), profile.get("district"),
                     profile.get("property_type"), _safe_int(profile.get("rooms")),
                     _safe_int(profile.get("price")) or neg.get("listing_price")))
            rid = cur.fetchone()[0]
            conn.commit()
        return f"{base}/p/{rid}"
    except Exception as e:
        print(f"[REG] create registration: {e}", flush=True)
        return None


def _start_negotiation_phase(neg, conn):
    """انتقال من التسجيل إلى التفاوض بعد اكتمال تسجيل الطرفين."""
    import identity
    _update_neg(neg["id"], conn, phase="negotiating")
    neg["phase"] = "negotiating"
    _say(neg, neg["lead_phone"], identity.negotiation_start("seeker"))
    _say(neg, neg["listing_phone"], identity.negotiation_start("owner"))
    print(f"[NEG #{neg['id']}] المرحلة: تسجيل → تفاوض", flush=True)


_REG_CONFIRM_WORDS = {"صحيح", "صح", "مضبوط", "تمام", "نعم", "ايه", "ايوه", "ايوا", "اي",
                      "زين", "اوك", "اوكي", "تم", "اكيد", "أكيد", "موافق", "صحيحه", "صحيحة",
                      "ok", "okay", "yes", "correct", "right"}


def _reg_identity_reply(role):
    src   = "طلبك المنشور على حراج" if role == "seeker" else "إعلانك المنشور على حراج"
    other = "عرض يناسبك" if role == "seeker" else "مستأجر مناسب"
    return (f"أنا «مساعد» — وسيط عقاري إلكتروني 🤝 لاحظت {src}، وحبيت أوصّلك لـ{other}. "
            f"خدمتنا مجانية ومتابعتك تهمّنا. تحب نكمل؟")


def _reg_calm_reply(role):
    src = "طلبك" if role == "seeker" else "إعلانك"
    return (f"أعتذر منك بصدق 🙏 ما قصدي أزعجك. أنا «مساعد» وسيط إلكتروني، لقيت {src} على حراج "
            f"وحبيت أساعدك توصل لاتفاق مناسب. راحتك أهم — تحب نكمل بهدوء، ولا أتوقّف؟")


def _looks_like_confirm(text, intent):
    if intent.get("intent") == "accept":
        return True
    words = set((text or "").replace("،", " ").replace(".", " ").split())
    return bool(words & _REG_CONFIRM_WORDS)


def _handle_registration(neg, phone, text, conn):
    """📝 مرحلة التسجيل قبل التفاوض — مع فهم النية/الهوية/المزاج (لا تقدّم أعمى):
    موافقة → تأكيد البيانات + صفحة → عند اكتمال الطرفين يبدأ التفاوض."""
    import identity
    from intent_parser import parse_intent
    neg_id  = neg["id"]
    is_lead = (phone == neg["lead_phone"])
    role    = "seeker" if is_lead else "owner"
    role_ar = "مستأجر" if is_lead else "مالك"
    step_col = "seeker_reg" if is_lead else "owner_reg"
    step = neg.get(step_col) or 0
    _append_log(neg_id, role_ar, text, conn)

    if _has_cancel(text):
        _close(neg_id, "cancelled", conn)
        _say(neg, phone, identity.cancel_to_party())
        _say(neg, neg["listing_phone"] if is_lead else neg["lead_phone"], identity.cancel_to_other())
        return True

    intent = parse_intent(text)

    # 🎭 مزاج غاضب/منزعج → احتواء + تعريف، بلا تقدّم في التسجيل (من الفهم العميق)
    _mood = intent.get("mood") or "neutral"
    if _mood in ("angry", "frustrated"):
        _say(neg, phone, _reg_calm_reply(role))
        return True

    # 🧠 سؤال الهوية «من أنت / كيف حصلت على رقمي» → أجب، بلا تقدّم
    if intent.get("_identity"):
        _say(neg, phone, _reg_identity_reply(role))
        return True

    # ✅ مسجّل بالفعل وينتظر الطرف الآخر → لا تُعِد طلب التسجيل (إصلاح حلقة الإعادة)
    if step >= 2:
        _say(neg, phone, identity.waiting_other())
        return True

    if step == 0:
        # ردّ الرغبة على المبادرة → اطلب تأكيد البيانات والنواقص
        prof = {"city": neg.get("listing_city"), "price": neg.get("listing_price")}
        prof.update({k: v for k, v in _fetch_understanding(role, phone, conn).items() if v not in (None, "")})
        known, missing = _reg_fields(prof, role)
        msg = identity.registration_confirm(role, known, missing)
        _update_neg(neg_id, conn, **{step_col: 1})
        _append_log(neg_id, f"bot→{role_ar}", msg, conn)
        _say(neg, phone, msg)
        return True

    # step >= 1: السؤال الصريح يُجاب بإيجاز (لا تفاوض سعر في التسجيل بعد)؛
    # أما التأكيد/البيانات/التصحيح/الميزانية → تُكمل التسجيل.
    if intent.get("intent") == "question" and not _looks_like_confirm(text, intent):
        reply = ("نكمّل تسجيلك أول 👍 أكّد البيانات فوق (أرسل «نعم»)، أو صحّح/أضف الناقص — "
                 "وبعدها أجاوبك على كل استفساراتك بالتفصيل.")
        _append_log(neg_id, f"bot→{role_ar}", reply, conn)
        _say(neg, phone, reply)
        return True

    # تأكيد/بيانات/تصحيح → أنشئ التسجيل وأرسل الصفحة، ثم تحقق من اكتمال الطرفين
    prof = _fetch_understanding(role, phone, conn)
    page = _create_party_registration(role, phone, neg, prof, conn)
    _update_neg(neg_id, conn, **{step_col: 2})
    neg[step_col] = 2
    done = identity.registration_done(role, page)
    _append_log(neg_id, f"bot→{role_ar}", done, conn)
    _say(neg, phone, done)

    other_step = (neg.get("owner_reg") if is_lead else neg.get("seeker_reg")) or 0
    if other_step >= 2:
        _start_negotiation_phase(neg, conn)
    else:
        _say(neg, phone, identity.waiting_other())
    return True


def _handle_active(neg: dict, phone: str, text: str, conn, media_url: str = None) -> bool:
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

    # ── 🔁 اختار الباحث بديلاً (رقم) بعد عرض البدائل → اربطه تلقائياً (تحت البوّابة) ──
    if is_lead:
        with conn.cursor() as cur:
            cur.execute("SELECT alt_offers, lead_id FROM sanad.masaed_negotiations WHERE id=%s", (neg_id,))
            _r = cur.fetchone()
        _alts_saved = (_r[0] if _r else None) or []
        _lead_id = _r[1] if _r else None
        if _alts_saved:
            _ci = _choice_from(text, len(_alts_saved))
            if _ci is not None:
                chosen = _alts_saved[_ci]
                import identity
                _append_log(neg_id, my_role, text, conn)
                _link_alternative(neg, _lead_id, chosen, conn)
                wa_send(phone, identity.alternative_chosen(chosen.get("title")))
                _close(neg_id, "cancelled", conn)
                wa_send(neg["listing_phone"], identity.cancel_to_other())
                try:
                    _notify_admin(neg, "chose_alternative", conn)
                except Exception:
                    pass
                return True

    # 🧠 تحليل النيّة مرّة واحدة لكل رسالة (يُعاد استخدامه أدناه — يقلّل حمل LLM للثلث)
    intent = parse_intent(text)

    # ── 🔁 الباحث يطلب عروضاً أخرى / لم يرغب بهذا العرض → نرشّح بدائل مطابقة ──
    if is_lead and intent.get("intent") == "want_alternatives" and not _wants_viewing(text):
        import identity
        _append_log(neg_id, my_role, text, conn)
        alts = _seeker_alternatives(neg["lead_phone"], neg.get("listing_id"), conn)
        if alts:
            lines = "\n".join(
                f"{i+1}) {a.get('title') or 'عرض'}"
                + (f" — {int(a['price']):,} ر/سنة" if a.get('price') else "")
                + (f" — {a['city']}" if a.get('city') else "")
                for i, a in enumerate(alts))
            wa_send(phone, identity.alternatives_offer(lines))
            _update_neg(neg_id, conn, alt_offers=json.dumps(alts, ensure_ascii=False))
            try:
                _notify_admin(neg, "wants_alternatives", conn)
            except Exception as _e:
                print(f"[NEG #{neg_id}] إشعار البدائل: {_e}", flush=True)
        else:
            wa_send(phone, identity.alternatives_none())
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

    # ── تتبع الموافقة على سعر مقترح (قبول صريح أو ناعم) ───────────────────────
    if neg.get("proposed_price"):
        intent_q = intent
        soft = _is_soft_accept(text, neg["proposed_price"])
        if intent_q["intent"] == "accept" or soft:
            p_str = f"{neg['proposed_price']:,}"
            # 🛑 قبول مصحوب بسؤال/شرط جوهري ليس قبولاً نهائياً: أحِل السؤال ولا تُغلق
            # (يمنع «إعلان إتمام الصفقة» بينما الطرف ما زال يستفسر عن معلومة جوهرية.)
            _facts = " ".join(str(neg.get(k) or "") for k in
                              ("listing_facts", "listing_title", "listing_city"))
            has_followup = (_asks_unknown_attr(text, _facts)
                            or any(w in text for w in _SEEKER_ATTR))
            if has_followup:
                _append_log(neg_id, my_role, text, conn)
                _relay_info_request(neg, my_role, text, conn)
                wa_send(phone, f"قبل ما نثبّت المقترح ({p_str} ر)، خلّني آخذ لك جواب "
                               f"استفسارك من الطرف الآخر وأرجع لك فوراً 👌")
                return True

            # قبول نظيف بلا أسئلة معلّقة → سجّله، وأغلق إن اتفق الطرفان
            field  = "lead_accepted" if is_lead else "owner_accepted"
            _update_neg(neg_id, conn, **{field: True})
            neg[field] = True
            _append_log(neg_id, my_role, text, conn)
            if soft and intent_q["intent"] != "accept":
                print(f"[NEG #{neg_id}] قبول ناعم من {my_role} على {neg['proposed_price']:,}", flush=True)

            lead_ok  = neg.get("lead_accepted")  or is_lead
            owner_ok = neg.get("owner_accepted") or (not is_lead)

            if lead_ok and owner_ok:
                print(f"[NEG #{neg_id}] FSM: closing → agreed (price={neg['proposed_price']:,})", flush=True)
                _close(neg_id, "agreed", conn, agreed_price=neg["proposed_price"])
                msg = (f"🎉 تم إتمام الصفقة على {p_str} ر/سنة بنجاح ✅\n"
                       f"مبروك! سيتم التواصل لإكمال إجراءات العقد.")
                wa_send(phone, msg)
                wa_send(other, msg)
                _notify_admin(neg, "ready_to_close", conn)
            else:
                wa_send(phone,
                    f"تم تسجيل موافقتك على {p_str} ر/سنة ✅\n"
                    f"ننتظر رد الطرف الآخر.")
            return True

    # ── حالة الـFSM (intent مُحلَّل مسبقاً مرّة واحدة أعلاه) ───────────────────
    _state_before = fsm_state(neg)
    print(f"[NEG #{neg_id}] state={_state_before} {my_role} intent={intent['intent']} "
          f"amount={intent.get('amount')} firm={intent.get('is_firm')}", flush=True)

    _append_log(neg_id, my_role, text, conn)

    # ── 🎭 طبقة المشاعر (المرحلة ١): مزاج غاضب/محبط → احتواء دافئ، لا برود ولا سعر ──
    #    تُقدَّم على منطق التفاوض، إلا إن كان قبولاً/عرض سعر صريحاً (نية إيجابية واضحة).
    try:
        from strategy import detect_mood, warm_reply
        _mood = detect_mood(text, intent)
    except Exception:
        _mood = "neutral"
    if (_mood in ("angry", "frustrated")
            and intent["intent"] not in ("accept", "price_offer")
            and not _has_cancel(text)):
        n = sum(1 for e in neg.get("chat_log", []) if str(e.get("role", "")).startswith("bot"))
        reply = warm_reply(_mood, n)
        print(f"[NEG #{neg_id}] 🎭 احتواء ({_mood}) — بلا دفع للسعر", flush=True)
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        if _mood == "angry":                       # غضب شديد → أبلغ المسؤول بهدوء
            _notify_admin(neg, "party_upset", conn)
        return True

    # ── إغلاق الحلقة: ردّ الطرف الذي سألناه عن معلومة معلّقة → أوصِله للسائل ────
    #    يُسلَّم الرد حتى لو تضمّن سعراً (نلتقط السعر أيضاً لمواصلة التقارب).
    pending = neg.get("pending_req")
    if (pending and phone != pending.get("asker_phone")
            and not _has_cancel(text) and not _wants_phone(text)):
        asker_phone = pending["asker_phone"]
        q = (pending.get("question") or "").strip()
        asker_role  = "مالك" if asker_phone == neg["listing_phone"] else "مستأجر"
        body = text.strip() or "(أرسل لك ملفاً)"
        fwd = (f"بخصوص استفسارك" + (f' ("{q[:50]}")' if q else "") + ":\n"
               f"الطرف الآخر يقول: {body}")
        _append_log(neg_id, f"answer→{asker_role}", fwd, conn)
        wa_send(asker_phone, fwd)
        if media_url:                       # مرّر أي صورة/ملف أرفقه الطرف المسؤول
            wa_send_media(asker_phone, media_url, caption="من الطرف الآخر")
        _update_neg(neg_id, conn, pending_req=None)
        # التقط أي سعر ورد ضمن الرد لمواصلة التقارب (بحارس اتجاهي)
        amt = intent.get("amount")
        captured = ""
        if amt and 3000 <= amt <= 999000:
            rec = _record_incidental_price(neg, is_lead, amt, conn)
            if rec is not None:
                captured = f" وسجّلت سعرك ({rec:,} ر) ✅"
        # تقاربا؟ أطلق اقتراح الوسط (يكفي إشعار قصير لأن الاقتراح يصل للطرفين)
        if _maybe_propose(neg, conn):          # اقتراح جديد أُرسِل للطرفين
            ack = f"تمام، وصلني ونقلته له ✅{captured}\nوبما إننا قريبين، أرسلت لكما اقتراح السعر — ردّ «موافق» لإتمامها 🤝"
        elif neg.get("proposed_price"):        # اقتراح قائم → تذكير مطويّ مرّة كل نافذة
            ack = f"تمام، وصلني ونقلته له ✅{captured}{_standing_tail(neg)}"
        else:
            ack = f"تمام، وصلني ونقلته له ✅{captured} نكمّل — {_price_prompt(neg)}"
        _append_log(neg_id, f"bot→{my_role}", ack, conn)
        wa_send(phone, ack)
        return True

    other_role = "المالك" if my_role == "مستأجر" else "المستأجر"

    # ── حارس صارم ضد التأليف (مهما كانت النية): سؤال عن سمة غير متوفّرة → أحِل ──
    #    يلتقط أي سعر ورد بالرسالة بصمت، ثم يُحيل السؤال للطرف الآخر (لا نخمّن إجابة).
    facts_all = " ".join(str(neg.get(k) or "") for k in
                         ("listing_facts", "listing_title", "listing_city"))
    asks_unknown = ((my_role == "مستأجر" and _asks_unknown_attr(text, facts_all))
                    or (my_role == "مالك" and any(w in text for w in _SEEKER_ATTR)))
    if asks_unknown and not _wants_viewing(text) and not _wants_phone(text):
        amt = intent.get("amount")
        rec = None
        if amt and 3000 <= amt <= 999000:
            rec = _record_incidental_price(neg, is_lead, amt, conn)
        _relay_info_request(neg, my_role, text, conn)
        _head = (f"سؤال وجيه 👍 أستوضحه من {other_role} وأوافيك فوراً."
                 + (f" وسجّلت رقمك ({rec:,} ر) ✅" if rec else ""))
        # تقاربا؟ أطلق اقتراح الوسط على الطرفين بدل مجرّد إعادة سؤال السعر
        if _maybe_propose(neg, conn):          # اقتراح جديد أُرسِل للطرفين
            reply = _head + "\nوبما إننا قريبين، أرسلت لكما اقتراح السعر — ردّ «موافق» لإتمامها 🤝"
        elif neg.get("proposed_price"):        # اقتراح قائم → تذكير مطويّ مرّة كل نافذة
            reply = _head + _standing_tail(neg)
        else:
            reply = _head + f" وعشان نتقدّم بالتوازي — {_price_prompt(neg)}"
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True

    # ── قبول صريح ───────────────────────────────────────────────────────────
    if intent["intent"] == "accept":
        # قبول مقترن بسعر صريح ولا اقتراح وسط بعد → عامله كعرض سعر (يلتقط/يقارب/يقفل)
        if intent.get("amount") and not neg.get("proposed_price"):
            intent = {**intent, "intent": "price_offer"}
        else:
            reply = "ممتاز! وصلت موافقتك ✅\nسأُبلغ المسؤول لإتمام إجراءات الصفقة."
            _append_log(neg_id, f"bot→{my_role}", reply, conn)
            wa_send(phone, reply)
            _notify_admin(neg, "ready_to_close", conn)
            return True

    # ── عرض سعر — حفظ + فحص تقارب → relay أو propose_middle ───────────────
    if intent["intent"] == "price_offer" and intent.get("amount"):
        amount = intent["amount"]
        field  = "lead_max_price" if is_lead else "owner_min_price"
        cur    = neg.get(field)

        # حارس العروض المشروطة: رسالة فيها أكثر من رقم («٤٠٥٠٠ أو ٤٠ نقداً») وغير
        # حازمة وتحرّكها عكسي (يرفع سقف المستأجر/يُسقط أرضية المالك) → اطلب توضيحاً
        # بدل اعتماد رقم ملتبس قد يزيّف التقارب.
        adverse = (cur is not None and
                   ((is_lead and amount > cur) or ((not is_lead) and amount < cur)))
        if adverse and not intent.get("is_firm") and len(amounts_in(text)) > 1:
            reply = (f"وضّح لي من فضلك السعر النهائي اللي تثبت عليه (رقم واحد) "
                     f"وأبني عليه مباشرة 👍")
            _append_log(neg_id, f"bot→{my_role}", reply, conn)
            wa_send(phone, reply)
            return True

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
                made = _propose_middle(neg, lmax, omin, conn)
                _notify_admin(neg, "near_agreement", conn)
                if not made:                   # اقتراح قائم بالفعل → ردّ واحد للمُرسِل (بفحص نافذة)
                    tail = _standing_tail(neg)
                    r = (f"وصلك المقترح {neg['proposed_price']:,} ر —{tail}" if tail
                         else "وصلني 👍 ننتظر تأكيد الطرفين على المقترح.")
                    _append_log(neg_id, f"bot→{my_role}", r, conn)
                    wa_send(phone, r)
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

    # ── طلب صور/معاينة → أرسل المخزّن فعلياً، وإلا اطلبه من الطرف الآخر ─────────
    if _wants_viewing(text):
        photos = _listing_photos(neg, conn)
        sent = 0
        for url in photos[:6]:
            if wa_send_media(phone, url):
                sent += 1
        asking_more = any(k in text for k in ("اضاف", "إضاف", "المزيد", "اكثر", "أكثر",
                                              "ثاني", "غيرها", "زياده", "زيادة", "واجهه", "واجهة"))
        if sent and not asking_more:
            reply = ("هذي صور العقار المتوفرة عندي 📸 "
                     "وعشان نتقدّم — وش السعر اللي يناسبك للإيجار السنوي؟")
        else:
            # 🔁 لا صور مخزّنة → تفادَ الحلقة: المالك يُجاب بذكاء، والمستأجر يُسأل
            # المالك مرّة واحدة فقط ثم تُعرَض المعاينة.
            asked_before = any(("صور" in (e.get("text") or ""))
                               and str(e.get("role", "")).startswith(("relay", "answer"))
                               for e in neg.get("chat_log", [])[-10:])
            if my_role == "مالك":
                # المالك يتحدّث عن الصور (حيرة/لا صور) → لا تُحِله كطلب جديد للمستأجر
                reply = ("المستأجر طلب صور العقار 📷 — إن عندك صور أرسلها هنا وأنقلها له، "
                         "وإلا ننسّق له معاينة على الطبيعة. وعشان نمشّي — وش أفضل سعر تقبله؟")
            elif not asked_before:
                _relay_info_request(neg, my_role, text, conn)   # اسأل المالك مرّة واحدة
                reply = ("طلبت من المالك صور العقار وأوافيك فور وصولها 👌 وإن ما توفّرت "
                         "أنسّق لك معاينة على الطبيعة. وبالتوازي — وش السعر اللي يناسبك؟")
            else:
                reply = ("ما توفّرت صور للعقار حالياً 📷 — أنسّق لك معاينة على الطبيعة بدلاً منها؟ 🗓️ "
                         "وعشان نتقدّم — وش السعر اللي يناسبك؟")
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True

    # ── سؤال عن معلومة لدى الطرف الآخر (عقار للمستأجر / الباحث للمالك) → relay ──
    if intent["intent"] == "question" and _needs_relay(my_role, text, neg):
        _relay_info_request(neg, my_role, text, conn)
        reply = (f"سؤال وجيه 👍 أستوضحه من {other_role} وأوافيك فوراً. "
                 f"وعشان نتقدّم بالتوازي — وش السعر اللي يناسبك للإيجار السنوي؟")
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True

    # ── إحباط/شكوى موجّهة للبوت → احتواء دافئ بلا دفعٍ للسعر (المرحلة ١) ────────
    if _is_meta_complaint(text):
        reply = ("آسف منك بصدق إذا ضايقتك 🙏 ما كان قصدي. "
                 "راحتك أهم — تحب نكمّل بهدوء، أو أتوقّف؟ أنا تحت أمرك.")
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
    # بروتوكول جلب المعلومة الناقصة (صارم — لا تأليف):
    # إمّا أصدر النموذج [[ASK_OTHER]]، أو سؤالٌ ظهر فيه تردّد/تأليف → اسأل الطرف الآخر
    if "ASK_OTHER" in reply or _is_hedge(reply):
        _relay_info_request(neg, my_role, text, conn)
        reply = (f"سؤال وجيه 👍 أستوضحه من {other_role} وأوافيك فوراً. "
                 f"وعشان نتقدّم بالتوازي — وش السعر اللي يناسبك للإيجار السنوي؟")
        _append_log(neg_id, f"bot→{my_role}", reply, conn)
        wa_send(phone, reply)
        return True
    # حارس التكرار: لا نرسل نفس رد البوت السابق حرفياً
    last_bot = next((e.get("text") for e in reversed(neg.get("chat_log", []))
                     if str(e.get("role", "")).startswith("bot")), None)
    if last_bot and reply.strip() == last_bot.strip():
        reply = "خلّنا نركّز على السعر 👍 وش الرقم اللي يناسبك للإيجار السنوي؟"
    _append_log(neg_id, f"bot→{my_role}", reply, conn)
    wa_send(phone, reply)
    return True


def handle_negotiation_message(phone: str, text: str, media_url: str = None) -> bool:
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
                neg["_profile"] = build_party_profile(phone, conn)  # ملف الحافظ المضغوط
            except Exception:
                pass
            # 🌍 كشف لغة الطرف وتفعيل خريطة الترجمة لكل رسائل هذا التفاوض
            try:
                _detect_party_lang(neg, phone, text or "", conn)
                _lang_ctx.map = {neg["lead_phone"]: neg.get("lead_lang"),
                                 neg["listing_phone"]: neg.get("listing_lang")}
            except Exception as _le:
                print(f"[I18N] كشف اللغة: {_le}", flush=True)
            try:
                # 📝 مرحلة التسجيل (قبل التفاوض): مبادرة→موافقة→تسجيل→تفاوض
                if neg.get("phase") == "registering":
                    return _handle_registration(neg, phone, text or "", conn)
                return _handle_active(neg, phone, text or "", conn, media_url)
            finally:
                _lang_ctx.map = None


# ── بدء تفاوض جديد ────────────────────────────────────────────────────────────

def start_negotiation(lead_id: int, listing_id: int,
                      lead_phone: str, listing_phone: str,
                      lead_name: str = None, listing_title: str = None,
                      listing_city: str = None, listing_price: int = None,
                      send_intro: bool = True, require_gate: bool = True,
                      lead_url: str = None, listing_url: str = None) -> dict:
    ensure_table()

    if lead_phone == listing_phone:
        return {"ok": False, "error": "لا يمكن التفاوض مع نفس الرقم"}

    # 🚦 البوّابة الإلزامية: لا تواصل حقيقي قبل محاكاة معتمدة (fail-closed).
    # نقطة الاختناق الوحيدة لكل مسارات البدء (lab/start، matches/approve، negotiate/start).
    if require_gate:
        try:
            import deal_gate
            if not deal_gate.check(lead_phone, listing_phone):
                return {
                    "ok": False,
                    "gate": "blocked",
                    "error": ("⛔ هذه الصفقة لم تُحاكَ وتُعتمَد بعد. شغّل «محاكاة الأطراف»، "
                              "راجِع النتيجة، ثم اعتمدها قبل بدء التواصل الحقيقي."),
                }
        except Exception as _e:
            print(f"[GATE] تعذّر فحص البوّابة — منع احترازي: {_e}", flush=True)
            return {"ok": False, "gate": "error",
                    "error": "تعذّر التحقق من بوّابة الاعتماد — مُنع البدء احترازاً."}

    with _conn_ctx() as conn:
        # ملاحظة: لم يعد شرط التسجيل المسبق مطلوباً — مرحلة التسجيل داخل المفاوض
        # (phase='registering') تتكفّل بتسجيل الطرفين أثناء المحادثة قبل التفاوض.
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
                     listing_facts, status, phase, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active','registering', NOW() + INTERVAL '7 days')
                RETURNING id
            """, (lead_id, listing_id, lead_phone, listing_phone,
                  lead_name, listing_title, listing_city, listing_price, facts or None))
            neg_id = cur.fetchone()[0]
            conn.commit()

    # استهلك الاعتماد: اعتماد واحد = بدء واحد؛ إعادة البدء تتطلّب محاكاة واعتماداً جديدين
    if require_gate:
        try:
            import deal_gate
            deal_gate.consume(lead_phone, listing_phone, neg_id)
        except Exception as _e:
            print(f"[GATE] فشل consume بعد البدء: {_e}", flush=True)

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

    if send_intro:
        # 🔗 حلّ الرابط الموحّد — بالـID الفعلي للصفقة (الإعلان الصحيح بالضبط)، لا
        # بالهاتف-الأحدث (الذي يخطئ حين يملك الطرف أكثر من إعلان). الأولوية:
        # (١) رابط مُمرَّر صراحةً، (٢) بالـid مع التحقّق أنه يخصّ هاتف الطرف،
        # (٣) احتياط أخير بالهاتف فقط حين لا id.
        _su = lead_url
        _so = listing_url
        _lp_v = set(_pvar(listing_phone)); _sp_v = set(_pvar(lead_phone))
        try:
            with _conn_ctx() as _c, _c.cursor() as _cur:
                if not _so and listing_id:
                    _cur.execute("SELECT url, phone FROM sanad.masaed_listings WHERE id=%s", (listing_id,))
                    _r = _cur.fetchone()
                    if _r and _r[1] and _r[1] in _lp_v:   # الإعلان يخصّ هذا المالك
                        _so = _r[0]
                if not _su and lead_id:
                    _cur.execute("SELECT url, phone FROM sanad.masaed_leads WHERE id=%s", (lead_id,))
                    _r = _cur.fetchone()
                    if _r and _r[1] and _r[1] in _sp_v:
                        _su = _r[0]
                # احتياط: بالهاتف فقط إن بقي ناقصاً (حالات بلا id حقيقي)
                if not _su:
                    _cur.execute("""SELECT url FROM sanad.masaed_leads WHERE phone = ANY(%s)
                                    AND listing_type='wanted' ORDER BY scraped_at DESC LIMIT 1""", (list(_sp_v),))
                    _r = _cur.fetchone(); _su = _r[0] if _r else None
                if not _so:
                    _cur.execute("""SELECT url FROM sanad.masaed_listings WHERE phone = ANY(%s)
                                    ORDER BY id DESC LIMIT 1""", (list(_lp_v),))
                    _r = _cur.fetchone(); _so = _r[0] if _r else None
        except Exception as _e:
            print(f"[NEG] جلب روابط المبادرة: {_e}", flush=True)

        # المبادرة من الهوية الموحّدة (identity) — مصدر واحد للموقع والواتساب.
        import identity
        wa_send(lead_phone, identity.outreach("seeker", _su, lead_name_resolved or None))
        wa_send(listing_phone, identity.outreach("owner", _so, listing_name_resolved or None))

    print(f"[NEG] Active #{neg_id}: {lead_phone} ↔ {listing_phone}", flush=True)
    return {"ok": True, "neg_id": neg_id}
