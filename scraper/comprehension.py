#!/usr/bin/env python3
"""
🧠 طبقة الفهم العميق (هجين) — LLM يفهم النص الكامل للطلب/الإعلان.

الهجين: regex (score_match) يفرز بالجملة بسرعة ورخص؛ هذه الطبقة تُستدعى
كسولاً للمرشحين المعروضين فقط (قبل المحاكاة) فتقرأ النص كاملاً وتستخرج
ملفاً منظّماً (الحي، التشطيب، الأثاث، الشروط، النية، متطلبات خاصة، نواقض الصفقة)،
ثم تقيّم التوافق الحقيقي بين الطلب والعرض بما يتجاوز الأرقام.

التخزين/الكاش: جدول sanad.masaed_comprehension (source, ext_id, text_hash, profile)
— لا نعيد استدعاء LLM إن لم يتغيّر النص (توفير موارد VPS ورصيد DeepSeek).
فشل LLM = إرجاع ملف منقوص (لا يكسر المحاكاة).
"""
import hashlib
import json
import re

from simulator import call_llm, _robust_json

MODEL = "deepseek"   # مزوّد واحد بقرار المستخدم

_SYS_EXTRACT = """أنت محلّل عقاري سعودي خبير. اقرأ نص إعلان/طلب إيجار كاملاً واستخرج فهماً منظّماً.
أعد JSON صالحاً فقط (بلا أي نص خارجه). اترك ما لا تجده null أو []. الحقول:
{
  "property_type": "شقة|فيلا|دور|استراحة|محل|...|null",
  "city": "المدينة أو null",
  "district": "الحي إن ذُكر أو null",
  "rooms": عدد الغرف رقماً أو null,
  "bathrooms": رقم أو null,
  "area_sqm": المساحة رقماً أو null,
  "furnished": true أو false أو null,
  "finishing": "وصف التشطيب إن ذُكر أو null",
  "price": السعر السنوي بالريال رقماً إن ذُكر صراحةً في النص فقط (إن كان شهرياً فاضربه ×12)؛ null إن لم يُذكر رقم — لا تخمّن ولا تقدّر من نوع العقار أو السوق,
  "conditions": ["شروط مذكورة: عوائل فقط، دفعة، تأمين، مدة..."],
  "intent": "نية المعلن بإيجاز أو null",
  "advertiser_name": "اسم المعلن/صاحب الترخيص/المكتب إن ذُكر أو null",
  "advertiser_type": "فرد|مؤسسة|null (مؤسسة إن ظهر شركة/مؤسسة/مكتب/عقارات/وساطة في الاسم، وإلا فرد)",
  "special": ["مزايا/متطلبات خاصة: قريب مسجد، مدخل خاص، موقف..."],
  "deal_breakers": ["نواقض محتملة: لا عزّاب، لا حيوانات..."],
  "summary": "سطر واحد يلخّص الجوهر"
}
لا تخترع ما ليس في النص — خاصةً السعر: null أصدق من رقم مُخمَّن. اجعل القيم موجزة جداً (≤ 10 كلمات للبند)."""

_SYS_ASSESS = """أنت وسيط عقاري سعودي خبير. أمامك فهمٌ منظّم لطلب باحث ولعرض مالك.
قيّم التوافق الحقيقي بما يتجاوز الأرقام (الحي، التشطيب، الشروط، نواقض الصفقة، النية).
أعد JSON صالحاً فقط:
{
  "verdict": "fit|partial|mismatch",
  "score": عدد 0-100,
  "reasons": ["أسباب توافق قوية"],
  "concerns": ["فجوات تحتاج تفاوضاً"],
  "deal_breakers": ["نواقض تمنع الصفقة فعلاً إن وُجدت"]
}
كن صريحاً: إن وُجد ناقض حقيقي (الطلب عوائل والعرض للعزّاب، مدينة مختلفة، نوع مختلف تماماً) فالحكم mismatch."""


def _hash(text):
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


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
                CREATE TABLE IF NOT EXISTS sanad.masaed_comprehension (
                    id         SERIAL PRIMARY KEY,
                    source     TEXT NOT NULL,
                    ext_id     TEXT NOT NULL,
                    text_hash  TEXT NOT NULL,
                    profile    JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (source, ext_id)
                )
            """)
            conn.commit()
    finally:
        if own:
            conn.close()


def extract_profile(text, role="listing", source=None, ext_id=None, conn=None):
    """يستخرج ملف فهم منظّم من النص الكامل، مع كاش عبر (source, ext_id, text_hash)."""
    text = (text or "").strip()
    if not text:
        return {}
    h = _hash(text)
    own = conn is None
    if own:
        conn = _get_conn()
    try:
        ensure_table(conn)
        # كاش: أعد المخزّن إن لم يتغيّر النص
        if source and ext_id is not None:
            with conn.cursor() as cur:
                cur.execute("""SELECT text_hash, profile FROM sanad.masaed_comprehension
                               WHERE source=%s AND ext_id=%s""", (source, str(ext_id)))
                row = cur.fetchone()
            if row and row[0] == h and row[1]:
                return row[1]
        # استدعاء LLM
        hint = "طلب باحث عن إيجار" if role == "seeker" else "إعلان عقار للإيجار"
        raw = call_llm(_SYS_EXTRACT, f"النوع: {hint}\nالنص:\n{text[:1500]}",
                       model=MODEL, max_tokens=600, timeout=40)
        profile = _robust_json(raw) if raw else None
        if not isinstance(profile, dict):
            profile = {"summary": text[:120], "_degraded": True}
        # 🛡️ منع هلوسة السعر (حتمي): اقبل السعر فقط إن ذُكر صراحةً في النص — رقمٌ
        # بجوار كلمة سعر/ايجار/ريال/شهري/سنوي. وإلا فهو مُخمَّن → null. (يتجاوز
        # أرقام الضجيج: هواتف/إيموجي/HTML.)
        if profile.get("price"):
            _has_price = re.search(
                r"(?:سعر|الايجار|الإيجار|إيجار|ايجار|بسعر|مطلوب|ريال|ر\.?\s?س|شهري|سنوي|الشهر|السن[ةه])"
                r"[^0-9]{0,12}[0-9][0-9,]{2,}"
                r"|[0-9][0-9,]{2,}[^0-9]{0,12}(?:ريال|ر\.?\s?س|شهري|سنوي|الف|ألف)", text)
            if not _has_price:
                profile["price"] = None
        # خزّن الكاش
        if source and ext_id is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO sanad.masaed_comprehension (source, ext_id, text_hash, profile)
                        VALUES (%s,%s,%s,%s)
                        ON CONFLICT (source, ext_id) DO UPDATE SET
                            text_hash=EXCLUDED.text_hash, profile=EXCLUDED.profile, created_at=NOW()
                    """, (source, str(ext_id), h, json.dumps(profile, ensure_ascii=False)))
                    conn.commit()
            except Exception as e:
                print(f"[COMPREHEND] فشل حفظ الكاش: {e}", flush=True)
        return profile
    finally:
        if own:
            conn.close()


def assess(seeker_profile, offer_profile):
    """حكم توافق حقيقي عبر LLM. يُرجع verdict/score/reasons/concerns/deal_breakers."""
    if not offer_profile:
        return {"verdict": "unknown", "score": None, "reasons": [],
                "concerns": ["لا يوجد فهم للعرض"], "deal_breakers": []}
    payload = (f"فهم الطلب:\n{json.dumps(seeker_profile or {}, ensure_ascii=False)}\n\n"
               f"فهم العرض:\n{json.dumps(offer_profile, ensure_ascii=False)}")
    raw = call_llm(_SYS_ASSESS, payload, model=MODEL, max_tokens=500, timeout=40)
    res = _robust_json(raw) if raw else None
    if not isinstance(res, dict):
        return {"verdict": "unknown", "score": None, "reasons": [],
                "concerns": [], "deal_breakers": [], "_degraded": True}
    return res


_SYS_CONV = """أنت «مساعد الحافظ» في منصّة عقارية. اقرأ محادثة تفاوض واستخرج الحقائق الجديدة
التي ظهرت في الحوار عن الطرف ({role_label}) — التي لم تكن في إعلانه أصلاً (مثل عدد أفراد الأسرة،
عزّاب/عائلة، جهة عمله، ما طلب معاينته، تفضيلاته، شروطه). أعد JSON صالحاً فقط:
{
  "household_type": "عائلة|عزّاب|null",
  "family_size": عدد أفراد الأسرة رقماً أو null,
  "occupation": "عمل/جهة عمل الطرف إن ذُكر أو null",
  "viewing_requested": true إن طلب معاينة/موعد وإلا false,
  "asked_about": ["ما سأل عنه فعلاً: صور، فيديو، موقع، موعد معاينة، مفتاح، عامل عمارة، مصعد، موقف، فواتير..."],
  "preferences": ["تفضيلات/اهتمامات ظهرت في الحوار"],
  "constraints": ["شروط/قيود ذكرها (مدة، دفعات، عوائل فقط...)"],
  "notes": "سطر يلخّص ما تعلّمناه عنه من المحادثة"
}
لا تخترع ما لم يُذكر. اترك ما لا تجده null أو []."""


def extract_conversation_facts(conversation, role="seeker"):
    """🧠 الحافظ: يحوّل المحادثة إلى حقائق منظّمة عن الطرف (ما تعلّمناه من سياق الحوار)."""
    if not conversation:
        return {}
    role_label = "المستأجر/الباحث" if role == "seeker" else "المالك"
    convo = "\n".join(f"{m.get('from','')}: {m.get('text','')}"
                      for m in conversation if m.get("text"))[:4000]
    raw = call_llm(_SYS_CONV.replace("{role_label}", role_label), convo,
                   model=MODEL, max_tokens=500, timeout=40)
    res = _robust_json(raw) if raw else None
    return res if isinstance(res, dict) else {"_degraded": True}


def enrich_specs(offer_profile, base="مواصفات عادية"):
    """يبني وصف مواصفات غنيّاً من الفهم لتغذية وكيل المالك في المحاكاة."""
    if not offer_profile:
        return base
    bits = []
    for k in ("property_type", "district", "finishing", "summary"):
        v = offer_profile.get(k)
        if v:
            bits.append(str(v))
    for k in ("conditions", "special"):
        vals = offer_profile.get(k) or []
        if isinstance(vals, list):
            bits.extend(str(x) for x in vals[:3])
    return " | ".join(dict.fromkeys(b for b in bits if b)) or base
