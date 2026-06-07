#!/usr/bin/env python3
"""
🟣 هوية «مساعد» العقاري — المصدر الواحد (Single Source of Truth)، قابلة للتعديل من اللوحة.

- القيم الافتراضية في DEFAULTS.
- أي تعديل من صفحة الهوية يُخزَّن في DB (config: identity_overrides) ويُحمَّل حيّاً هنا.
- يقرأ منها: المحاكي (sim_engine)، المختبرون (deal/wa-test)، الإطلاق (negotiator)،
  وطبقة الـLLM (prompts عبر persona()).

المتغيرات بين {} في القوالب تُملأ برمجياً: {greet} {url} {fields} {missing} {page}
{alts} {title}. احذف أو حرّك أيّاً منها كما تريد عند التعديل.
"""
import json

BOT_NAME = "مساعد"

STAGES = ["outreach", "consent", "registration", "viewing", "negotiation", "alternatives"]

# ── القيم الافتراضية (تُعدَّل من اللوحة) ─────────────────────────────────────
DEFAULTS = {
    "persona": (
        "أنت «مساعد» — وسيط عقاري سعودي محترف وودود. صادق وشفّاف: لا تبالغ، لا تخفي، "
        "ولا تضغط على أحد. تحترم وقت العميل، تتكلّم بعامية سعودية طبيعية ومختصرة، "
        "وتخدم الطرفين بإنصاف للوصول لاتفاق عادل."
    ),
    "principles": [
        "المبادرة = مبرر + إثبات (رابط إعلان الطرف نفسه) + معلومة المطابقة + سؤال الرغبة.",
        "لا سعر في رسالة المبادرة الأولى، ولا ربط بالطرف الآخر قبل التسجيل.",
        "التسجيل قبل التفاوض: تأكيد المستخرَج + سؤال النواقص فقط + إنشاء صفحة احترافية.",
        "الصور/الموقع/المعاينة قبل التفاوض؛ إن غابت الصور تُطلب من المالك وتُضاف لصفحته.",
        "عند عدم الرغبة أو طلب البدائل: تُرشَّح أفضل العروض الأخرى المطابقة.",
        "صدق وشفافية: لا وعود لا تُنفَّذ، ولا مبالغة في وصف العقار.",
        "🛑 الخط الأحمر: لا رسالة واتساب حقيقية بلا إذن صريح (اختبار بأرقام المستخدم فقط).",
    ],
    # ١) المبادرة
    "outreach_seeker": ("{greet} معك «مساعد» — وسيطك العقاري الإلكتروني.\n"
                        "لاحظنا طلبك المنشور على حراج{url}\n"
                        "وعندنا عرض قد يناسبه — حبّينا نتأكّد منك أول. لا زلت تدوّر على سكن؟"),
    "outreach_owner": ("{greet} معك «مساعد» — وسيطك العقاري الإلكتروني.\n"
                       "لاحظنا إعلانك المنشور على حراج{url}\n"
                       "وعندنا باحث قد يناسب عقارك — حبّينا نتأكّد. العقار لا زال متاح؟"),
    # ٣) التسجيل
    "reg_confirm_seeker": ("تمام 🙌 خلّني أكمّل تسجيلك بسرعة. من طلبك سجّلت لك مبدئياً:\n"
                           "{fields}\nصحّ كذا؟{missing}"),
    "reg_confirm_owner": ("تمام 🙌 خلّني أكمّل تسجيلك بسرعة. من إعلانك سجّلت لك مبدئياً:\n"
                          "{fields}\nصحّ كذا؟{missing}"),
    "reg_done": "✅ تمّ تسجيلك عندنا، الله يسهّل.{page}",
    "waiting_other": "بس ننتظر تأكيد الطرف الثاني، وبعدها أبدأ التفاوض نيابةً عنك 👌",
    # ٤) المعاينة
    "ask_owner_photos": ("إعلانك ما فيه صور واضحة — ترسل لي صور وفيديو للعقار من الداخل؟ "
                         "أضيفها لصفحتك وأعرضها للمستأجرين 📸🎥"),
    "photos_received": "✅ وصلتني الصور وأضفتها لصفحة عقارك.",
    "viewing_seeker_url": ("وقبل التفاوض، تفضّل صور العقار وموقعه على الخريطة من إعلان المالك 📸📍{url}\n"
                           "تحب أنسّق لك موعد معاينة على الطبيعة؟"),
    "viewing_seeker_from_owner": ("وقبل التفاوض، وصلتني صور العقار من المالك وحطّيتها بصفحته، "
                                  "وأرسلها لك الحين 📸 مع موقعه على الخريطة 📍.\n"
                                  "تحب أنسّق لك موعد معاينة على الطبيعة؟"),
    "viewing_owner": "المستأجر شاف الصور والموقع ويبي يعاين العقار. متى يناسبك الموعد؟",
    "viewing_confirmed_seeker": "تمام 🗓️ نسّقت لك موعد المعاينة مبدئياً، وبعدها نكمّل التفاوض على السعر والشروط.",
    "viewing_confirmed_owner": "ممتاز، حجزت الموعد. وبعد المعاينة نبدأ التفاوض على السعر النهائي.",
    # ٥) بدء التفاوض
    "negotiation_start_seeker": "✅ اكتمل تسجيل الطرفين، نبدأ بإذن الله. وش السعر السنوي اللي يناسبك؟",
    "negotiation_start_owner": "✅ اكتمل تسجيل الطرفين، عندي مستأجر جادّ. وش أفضل سعر سنوي تقبل فيه؟",
    # ٦) البدائل
    "alternatives_offer": ("تمام، عندي عروض ثانية تناسب طلبك:\n{alts}\n"
                           "أيّها يهمّك أجهّز لك تفاصيله ومعاينته؟ (أرسل رقمه)"),
    "alternatives_none": "ما لقيت عروض ثانية مطابقة حالياً، بدوّر لك وأوافيك أول ما يتوفّر 👌",
    "alternative_chosen": "ممتاز ✅ اخترت: {title}. جهّزته لك وبرتّب تفاصيله ومعاينته قريب 👌",
    # الإلغاء
    "cancel_to_party": "تمام، شكراً لك 🙏 وإذا احتجت شي لاحقاً أنا بالخدمة.",
    "cancel_to_other": "أعتذر، الطرف الثاني ما كمّل حالياً. بوافيك أول ما يتوفّر مناسب ثاني.",
}

# ترتيب وأوصاف الحقول لعرضها في اللوحة
FIELD_LABELS = {
    "persona": "الشخصية (مَن هو مساعد)",
    "principles": "المبادئ/القواعد",
    "outreach_seeker": "١) المبادرة — للباحث", "outreach_owner": "١) المبادرة — للمالك",
    "reg_confirm_seeker": "٣) التسجيل — تأكيد (باحث/طلبك)",
    "reg_confirm_owner": "٣) التسجيل — تأكيد (مالك/إعلانك)",
    "reg_done": "٣) اكتمال التسجيل + الصفحة",
    "waiting_other": "٣) بانتظار الطرف الآخر",
    "ask_owner_photos": "٤) طلب الصور", "photos_received": "٤) استلام الصور",
    "viewing_seeker_url": "٤) المعاينة — للباحث (برابط)",
    "viewing_seeker_from_owner": "٤) المعاينة — للباحث (صور المالك)",
    "viewing_owner": "٤) المعاينة — للمالك",
    "viewing_confirmed_seeker": "٤) تأكيد الموعد — للباحث",
    "viewing_confirmed_owner": "٤) تأكيد الموعد — للمالك",
    "negotiation_start_seeker": "٥) بدء التفاوض — للباحث",
    "negotiation_start_owner": "٥) بدء التفاوض — للمالك",
    "alternatives_offer": "٦) عرض البدائل", "alternatives_none": "٦) لا بدائل",
    "alternative_chosen": "٦) اختار بديلاً",
    "cancel_to_party": "الإلغاء — للطرف", "cancel_to_other": "الإلغاء — للطرف الآخر",
}


# ── تحميل/حفظ التعديلات (DB) ─────────────────────────────────────────────────
def _overrides():
    try:
        from bot import get_config
        raw = (get_config("identity_overrides", "") or "").strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def cfg(key):
    """قيمة الحقل: التعديل من اللوحة إن وُجد، وإلا الافتراضي."""
    return _overrides().get(key, DEFAULTS.get(key, ""))


def save_overrides(overrides: dict):
    """يحفظ التعديلات (فقط ما يختلف عن الافتراضي)."""
    from bot import set_config
    clean = {k: v for k, v in (overrides or {}).items()
             if k in DEFAULTS and v not in (None, "") and v != DEFAULTS.get(k)}
    set_config("identity_overrides", json.dumps(clean, ensure_ascii=False))
    return clean


def reset_overrides():
    from bot import set_config
    set_config("identity_overrides", "")


def snapshot():
    """الهوية الحالية كاملةً (للوحة): الحقول + القيم الحالية + الافتراضية + التسميات."""
    ov = _overrides()
    out = {}
    for k, dv in DEFAULTS.items():
        out[k] = {"label": FIELD_LABELS.get(k, k), "value": ov.get(k, dv),
                  "default": dv, "edited": k in ov}
    return out


# ── الوصول الموحّد ───────────────────────────────────────────────────────────
def persona():
    return cfg("persona")


# PERSONA كقيمة افتراضية لمن يحتاج لقطة ثابتة (طبقة الـLLM تستخدم persona() للحيّ)
PERSONA = DEFAULTS["persona"]


def principles():
    return _overrides().get("principles", DEFAULTS["principles"])


def _greet(name=None):
    return f"السلام عليكم {name} 👋" if name else "السلام عليكم 👋"


# ── ١) المبادرة ─────────────────────────────────────────────────────────────
def outreach(role, url=None, name=None):
    tmpl = cfg("outreach_seeker" if role == "seeker" else "outreach_owner")
    return tmpl.format(greet=_greet(name), url=(f":\n{url}" if url else ""))


# ── حقول التسجيل (منطق ثابت، تعريف موحّد) ────────────────────────────────────
def reg_fields(profile, role):
    profile = profile or {}
    if role == "seeker":
        fields = [("المدينة", "city"), ("الحي", "district"), ("عدد الغرف", "rooms"),
                  ("الميزانية السنوية", "price"), ("التأثيث", "furnished")]
    else:
        fields = [("نوع العقار", "property_type"), ("المدينة", "city"), ("الحي", "district"),
                  ("عدد الغرف", "rooms"), ("السعر السنوي", "price"), ("التشطيب", "finishing"),
                  ("التأثيث", "furnished")]
    known, missing = [], []
    for label, key in fields:
        v = profile.get(key)
        if key == "furnished":
            v = "مفروشة" if v is True else ("بدون أثاث" if v is False else None)
        if isinstance(v, list):
            v = "، ".join(str(x) for x in v) if v else None
        if v not in (None, "", "null", "غير محدد"):
            known.append(f"{label}: {v}")
        else:
            missing.append(label)
    return known, missing


# ── ٣) التسجيل ──────────────────────────────────────────────────────────────
def registration_confirm(role, known, missing):
    src = "طلبك" if role == "seeker" else "إعلانك"
    fields = "- " + "\n- ".join(known or [f"(ما لقيت تفاصيل كافية في {src})"])
    miss = (" وينقصني بس: " + "، ".join(missing) + ".") if missing else ""
    key = "reg_confirm_seeker" if role == "seeker" else "reg_confirm_owner"
    return cfg(key).format(fields=fields, missing=miss)


def registration_done(role, page_url=None):
    page = (f"\n📄 جهّزت لك صفحة احترافية بكل بياناتك: {page_url}") if page_url else ""
    return cfg("reg_done").format(page=page)


def waiting_other():
    return cfg("waiting_other")


# ── ٤) المعاينة ─────────────────────────────────────────────────────────────
def ask_owner_photos():
    return cfg("ask_owner_photos")


def photos_received():
    return cfg("photos_received")


def viewing_to_seeker(url=None, photos_from_owner=False):
    if photos_from_owner:
        return cfg("viewing_seeker_from_owner")
    return cfg("viewing_seeker_url").format(url=(f":\n{url}" if url else ""))


def viewing_to_owner():
    return cfg("viewing_owner")


def viewing_confirmed(role):
    return cfg("viewing_confirmed_seeker" if role == "seeker" else "viewing_confirmed_owner")


# ── ٥) بدء التفاوض ──────────────────────────────────────────────────────────
def negotiation_start(role):
    return cfg("negotiation_start_seeker" if role == "seeker" else "negotiation_start_owner")


# ── ٦) البدائل ──────────────────────────────────────────────────────────────
def alternatives_offer(alt_lines):
    return cfg("alternatives_offer").format(alts=alt_lines)


def alternatives_none():
    return cfg("alternatives_none")


def alternative_chosen(title):
    return cfg("alternative_chosen").format(title=title or "العرض")


# ── الإلغاء ─────────────────────────────────────────────────────────────────
def cancel_to_party():
    return cfg("cancel_to_party")


def cancel_to_other():
    return cfg("cancel_to_other")
