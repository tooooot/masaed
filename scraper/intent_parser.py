#!/usr/bin/env python3
"""
Intent Parser — يستخرج النية من رسالة واتساب.
Fast-path: regex محلي (لا شبكة) → LLM فقط للغامض.
"""
import os, re, json

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
USE_ANTHROPIC = os.getenv("MASAED_USE_ANTHROPIC", "false").lower() == "true"
DEEPSEEK_KEY  = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))

from prompts import INTENT_SYSTEM as _SYSTEM

_DEFAULT = {"intent": "other", "amount": None, "sentiment": "neutral", "is_firm": False}

# ── Fast-path sets ─────────────────────────────────────────────────────────────

_ACCEPT_EXACT = {
    'موافق','اوافق','أوافق','تمام','ماشي','اوكي','ok','okay',
    'نعم','ايوه','اي','خلاص','تم','ينفع','قبلت','قبلنا',
    'موافقين','اوك','تمام تمام','نعم موافق','اه','آه',
    'حسنا','حسناً','قبول','تمام تمام',
}
_ACCEPT_CONTAINS = {'موافق على','قبلت العرض','تم الاتفاق'}

_CANCEL_WORDS = {
    'لا شكرا','مو مهتم','لا يهمني','إلغاء','الغاء',
    'انهاء','إنهاء','مو رايه','مش مهتم','مو متاح',
    'ما أرغب','ما ارغب',
}

# طلب عروض أخرى / بدائل (عبارات متعدّدة الكلمات لتفادي الالتباس مع النفي)
_ALT_PHRASES = (
    'عروض ثاني', 'عروض ثانيه', 'عروض ثانية', 'عروض اخرى', 'عروض أخرى', 'عروض ثانيه',
    'عرض ثاني', 'عرض اخر', 'عرض آخر', 'عندك غير', 'عندك غيره', 'فيه غيره', 'في غيره',
    'عندك بديل', 'بدائل', 'ابي ثاني', 'ابغى ثاني', 'ابغى غيره', 'ابي غيره', 'ودي غيره',
    'خيارات ثاني', 'شي ثاني', 'عندك عروض', 'وريني غيره', 'ماعجبني ابي غيره',
)

_IDENTITY_TRIGGERS = {
    'من انت','من أنت','كيف جبت','كيف حصلت',
    'رقمي من وين','وين جبت','من اين','من أين',
    'ما غرضك','ما الغرض','ليش اتصلت',
}

_FIRM_WORDS = re.compile(r'وبس|فقط|نهائي|آخر كلام|ما أقدر|ما اقدر|لا أقدر|لا اقدر|أقل من كذا ما|ما أتعدا|ما اتعدا|ما أزيد|ما ازيد|سقف|أقصى|اقصى')
_PRICE_RE   = re.compile(r'(?<!\d)(\d{4,6})(?!\d)')
_THOUSANDS_RE = re.compile(r'(?<!\d)(\d{1,3})\s*(?:ألف|الف|آلاف|الاف)')
_QUESTION_RE = re.compile(r'[؟?]|هل |في |كم |وين |فيه |يوجد |هناك |متاح ')


def _amount_from(text: str):
    """مبلغ إيجار معقول من النص: رقم 4-6 خانات أو «<n> ألف» (40 ألف → 40000).
    عند تعدّد المبالغ (مثل «٤٠ قريب لكن المطلوب ٤٢، نتفق على ٤١») نأخذ الأخير
    لأن المتحدّث يذكر موقفه عادةً في نهاية الجملة — تجنّباً لالتقاط رقم الطرف الآخر."""
    cands = []
    for m in _PRICE_RE.finditer(text):
        a = int(m.group(1))
        if 3_000 <= a <= 999_000:
            cands.append((m.start(), a))
    for m in _THOUSANDS_RE.finditer(text):
        a = int(m.group(1)) * 1000
        if 3_000 <= a <= 999_000:
            cands.append((m.start(), a))
    if not cands:
        return None
    cands.sort()
    return cands[-1][1]


def amounts_in(text: str) -> list:
    """كل المبالغ المعقولة المميّزة في النص — لكشف العروض المشروطة متعدّدة الأرقام."""
    seen = []
    for m in _PRICE_RE.finditer(text):
        a = int(m.group(1))
        if 3_000 <= a <= 999_000 and a not in seen:
            seen.append(a)
    for m in _THOUSANDS_RE.finditer(text):
        a = int(m.group(1)) * 1000
        if 3_000 <= a <= 999_000 and a not in seen:
            seen.append(a)
    return seen


def _fast_parse(text: str) -> dict | None:
    """
    كشف سريع بدون LLM.
    يُعيد None للحالات الغامضة → تذهب للـLLM.
    """
    t = text.strip()
    t_low = t.lower()

    # هوية / من أنت — يُعامَل كـother ليصل لـ_SYS_INTRO
    for trig in _IDENTITY_TRIGGERS:
        if trig in t_low:
            return {"intent": "other", "amount": None,
                    "sentiment": "neutral", "is_firm": False,
                    "_identity": True}

    # طلب بدائل/عروض أخرى — قبل الإلغاء (أكثر تحديداً وقابلية للتنفيذ)
    if any(p in t_low for p in _ALT_PHRASES):
        return {"intent": "want_alternatives", "amount": None,
                "sentiment": "neutral", "is_firm": False}

    # إلغاء — فحص العبارات أولاً ثم الكلمات المفردة
    words = set(re.split(r'[\s،,؟?!.،؛؟\n]+', t_low))
    if any(phrase in t_low for phrase in _CANCEL_WORDS):
        return {"intent": "cancel", "amount": None,
                "sentiment": "negative", "is_firm": True}
    if words & _CANCEL_WORDS:
        return {"intent": "cancel", "amount": None,
                "sentiment": "negative", "is_firm": True}

    # قبول — رسالة قصيرة أو كلمة واضحة (مع التقاط أي سعر مذكور إن وُجد)
    if t_low in _ACCEPT_EXACT:
        return {"intent": "accept", "amount": _amount_from(t),
                "sentiment": "positive", "is_firm": True}
    if len(words) <= 4 and words & _ACCEPT_EXACT:
        return {"intent": "accept", "amount": _amount_from(t),
                "sentiment": "positive", "is_firm": True}
    for phrase in _ACCEPT_CONTAINS:
        if phrase in t_low:
            return {"intent": "accept", "amount": _amount_from(t),
                    "sentiment": "positive", "is_firm": True}

    # عرض سعر — رقم 4-6 أرقام أو «<n> ألف» في نطاق معقول
    amount = _amount_from(t)
    if amount is not None:
        firm = bool(_FIRM_WORDS.search(t))
        return {"intent": "price_offer", "amount": amount,
                "sentiment": "neutral", "is_firm": firm}

    # سؤال — علامة استفهام أو كلمة استفهام
    if _QUESTION_RE.search(t):
        return {"intent": "question", "amount": None,
                "sentiment": "neutral", "is_firm": False}

    return None  # غامض → LLM


# 🧠 الفهم العميق: LLM يفسّر نيّة كل رسالة (لا regex). يُعطّل بـMASAED_DEEP_INTENT=false
DEEP_INTENT = os.getenv("MASAED_DEEP_INTENT", "true").lower() == "true"


def _norm_amount(a):
    """في سياق الإيجار السنوي: الرقم الصغير (<1000) يعني بالآلاف.
    «١٥»→15000، «٣٠»→30000، «٣٠الف»→30000. (الإيجار السنوي نادراً <1000 ر.)"""
    try:
        a = int(float(a))
    except (TypeError, ValueError):
        return None
    if a <= 0:
        return None
    return a * 1000 if a < 1000 else a


def _parse_raw(raw: str) -> dict | None:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group())
        intent = d.get("intent", "other")
        is_identity = bool(d.get("is_identity")) or intent == "identity"
        # «identity/greeting» تُعامَل كـother مع علامة _identity (يستهلكها المفاوض)
        if intent in ("identity", "greeting"):
            intent = "other"
        res = {
            "intent":    intent,
            "amount":    _norm_amount(d.get("amount")) if d.get("amount") else None,
            "sentiment": d.get("sentiment", "neutral"),
            "is_firm":   bool(d.get("is_firm", False)),
            "mood":      d.get("mood", "neutral"),
        }
        if is_identity:
            res["_identity"] = True
        return res
    except Exception:
        return None


def _llm_parse(text: str) -> dict | None:
    """يفسّر النيّة عبر LLM (Anthropic إن فُعّل، وإلا DeepSeek)."""
    prompt = f"الرسالة: {text}"
    if ANTHROPIC_KEY and USE_ANTHROPIC:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=160,
                system=_SYSTEM, messages=[{"role": "user", "content": prompt}])
            r = _parse_raw(resp.content[0].text.strip())
            if r:
                return r
        except Exception as e:
            print(f"[INTENT] Anthropic: {e}", flush=True)
    if DEEPSEEK_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": _SYSTEM},
                          {"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, max_tokens=160)
            r = _parse_raw(resp.choices[0].message.content)
            if r:
                return r
        except Exception as e:
            print(f"[INTENT] DeepSeek: {e}", flush=True)
    return None


def parse_intent(text: str) -> dict:
    """فهم نيّة الرسالة. الوضع العميق: LLM لكل رسالة (مع احتياط regex عند الفشل)."""
    text = (text or "").strip()
    if not text:
        return _DEFAULT.copy()

    if DEEP_INTENT:
        # 🧠 LLM أولاً لكل رسالة — فهم عميق بالسياق لا بالكلمات
        r = _llm_parse(text)
        if r:
            return r
        # فشل LLM → احتياط سريع
        return _fast_parse(text) or _DEFAULT.copy()

    # الوضع السريع القديم: regex أولاً، LLM للغامض
    fast = _fast_parse(text)
    if fast is not None:
        return fast
    return _llm_parse(text) or _DEFAULT.copy()
