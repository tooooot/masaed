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


def _parse_raw(raw: str) -> dict | None:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group())
        return {
            "intent":    d.get("intent", "other"),
            "amount":    int(d["amount"]) if d.get("amount") else None,
            "sentiment": d.get("sentiment", "neutral"),
            "is_firm":   bool(d.get("is_firm", False)),
        }
    except Exception:
        return None


def parse_intent(text: str) -> dict:
    """Extract structured intent. Fast-path first, LLM as fallback."""

    # ── Fast path (بدون شبكة، < 1ms) ────────────────────────────────────────
    fast = _fast_parse(text)
    if fast is not None:
        return fast

    prompt = f"الرسالة: {text}"

    # ── Anthropic (معطّل افتراضياً لتوفير التكلفة) ─────────────────────────────
    if ANTHROPIC_KEY and USE_ANTHROPIC:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=120,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}]
            )
            result = _parse_raw(resp.content[0].text.strip())
            if result:
                return result
        except Exception as e:
            print(f"[INTENT] Anthropic: {e}", flush=True)

    # ── DeepSeek fallback ──────────────────────────────────────────────────────
    if DEEPSEEK_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": prompt}
                ],
                response_format={"type": "json_object"},
                max_tokens=120
            )
            result = _parse_raw(resp.choices[0].message.content)
            if result:
                return result
        except Exception as e:
            print(f"[INTENT] DeepSeek: {e}", flush=True)

    return _DEFAULT.copy()
