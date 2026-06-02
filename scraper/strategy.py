"""طبقة المشاعر والاستراتيجية (المرحلة ١): تقرأ مزاج الطرف وتختار النبرة قبل الردّ.
حتمية ورخيصة (بلا LLM) — تُوجّه النبرة وتمنع البرود/الإلحاح على السعر في غير محلّه.
الهدف: لو غضب الطرف → اعتذار حار واحتواء (لا ردّ بارد ولا دفع للسعر)؛
       لو متردّد → طمأنة بلا ضغط؛ لو ودود → متابعة مهنية نحو الاتفاق."""

_ANGER = ("زعلان", "غاضب", "مستاء", "معصب", "متضايق", "قهر", "سيّئ", "سيء",
          "تعبتو", "تعبتوني", "ضايقت", "مزعج", "احتيال", "نصب", "نصابين", "كذب",
          "تكذب", "خداع", "أبلغ عنك", "ابلغ عنك", "بلّغ", "شكوى", "ما تحترم",
          "قليل أدب", "قليل ادب", "وقاحة", "فاشل", "افشل", "زباله", "زبالة",
          "حرام عليك", "استغلال", "كفووو", "كفو", "بلوك", "احظرك", "أحظرك")
_BLOCK_RISK = ("بلغ", "أبلغ", "ابلغ", "شكوى", "حظر", "احظر", "سبام", "spam",
               "report", "إيقاف", "ايقاف", "وزارة", "هيئة")
_FRUSTRATION = ("مليت", "ملّيت", "طفشت", "كثرة الرسائل", "كثر الرسائل", "بطيء",
                "ما فهمت", "مو فاهم", "معقدة", "لخبطة", "وش دخلك", "ليش تكلمني",
                "ما أعرفك", "ما اعرفك", "مزعجين", "بطلوا", "خلصوني")
_HESITATION = ("ما أدري", "ما ادري", "لست متأكد", "مو متأكد", "خلني أفكر",
               "خلني افكر", "ابي أفكر", "يمكن", "لاحقاً", "لاحقا", "بعدين",
               "مو الحين", "انتظر", "أتردد", "مو متفرّغ")


def detect_mood(text: str, intent: dict | None = None) -> str:
    """angry | frustrated | hesitant | positive | neutral."""
    t = text or ""
    if any(w in t for w in _ANGER) or any(w in t for w in _BLOCK_RISK):
        return "angry"
    if any(w in t for w in _FRUSTRATION):
        return "frustrated"
    senti = (intent or {}).get("sentiment")
    if senti == "negative" and (intent or {}).get("intent") != "price_offer":
        return "frustrated"
    if any(w in t for w in _HESITATION):
        return "hesitant"
    if senti == "positive":
        return "positive"
    return "neutral"


# لكل مزاج: نبرة + توجيه للنموذج + هل نلِحّ على السعر + هل نحتوي بلطف
_STRATEGY = {
    "angry":      {"tone": "اعتذار حار واحتواء",
                   "guide": "اعتذر بصدق ودفء، لا تُدافع ولا تُبرّر، لا تذكر السعر إطلاقاً، "
                            "اعترف بانزعاجه، واعرض الإنهاء بلطف أو المساعدة كما يريد.",
                   "push_price": False, "contain": True},
    "frustrated": {"tone": "تهدئة لطيفة",
                   "guide": "اعتذر باختصار وطمئنه، خفّف، لا تُلِحّ على السعر في هذا الرد.",
                   "push_price": False, "contain": True},
    "hesitant":   {"tone": "طمأنة بلا ضغط",
                   "guide": "طمئنه وامنحه راحة، قدّم قيمة، لا تستعجله على السعر.",
                   "push_price": False, "contain": False},
    "positive":   {"tone": "ودّي متعاون",
                   "guide": "حافظ على الحماس وتابع بسلاسة نحو إتمام الاتفاق.",
                   "push_price": True, "contain": False},
    "neutral":    {"tone": "مهني ودّي",
                   "guide": "كن ودوداً ومهنياً، وتقدّم نحو الاتفاق دون إلحاح.",
                   "push_price": True, "contain": False},
}


def strategy_for(mood: str) -> dict:
    return _STRATEGY.get(mood, _STRATEGY["neutral"])


def mood_guidance(text: str, intent: dict | None = None) -> str:
    """سطر توجيه نبرة يُحقَن في prompt النموذج."""
    mood = detect_mood(text, intent)
    s = strategy_for(mood)
    return f"🎭 مزاج الطرف: {mood} — النبرة المطلوبة: {s['tone']}. {s['guide']}"


# ردود احتواء دافئة جاهزة (بلا LLM) عند الغضب/الإحباط — تُدوّر لتجنّب التكرار
_CONTAIN_ANGRY = (
    "أعتذر منك بصدق إن كنت سبّبت لك إزعاجاً 🙏 ما كان قصدي أبداً. "
    "راحتك أهم من أي شيء — تحب أتوقّف، أو فيه شي أقدر أساعدك فيه؟",
    "آسف جداً على الإزعاج، وأتفهّم تماماً 🙏 لك كامل الحق. "
    "أنا تحت أمرك: أوقف التواصل فوراً، أو أكمل بالطريقة اللي تريحك.",
)
_CONTAIN_FRUSTRATED = (
    "عذراً على الإطالة 🙏 خلّني أختصر لك وأكون عند راحتك تماماً. "
    "وش الأنسب لك الآن؟",
    "آسف إن كثرت عليك 🙏 راحتك أولاً — قل لي كيف تحب نكمّل وأنا معك.",
)


def warm_reply(mood: str, n: int = 0) -> str:
    pool = _CONTAIN_ANGRY if mood == "angry" else _CONTAIN_FRUSTRATED
    return pool[n % len(pool)]
