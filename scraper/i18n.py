#!/usr/bin/env python3
"""
🌍 طبقة اللغة — يجعل «مساعد» يخدم كل اللغات.

- detect_lang(text): يكشف لغة الطرف من رسالته (عربي بكشف محلي رخيص؛ غيره عبر LLM).
- localize(text_ar, lang): يترجم رسالة المرحلة العربية إلى لغة الطرف (مع كاش).
  عربي → بلا تغيير ولا استدعاء. الروابط/الأرقام/الإيموجي تبقى كما هي.

الفلسفة: مساعد يكلّم كل طرف بلغته، دون إعادة بدء التدفّق — نفس المراحل، لغة مختلفة.
ردود الـLLM الحرّة تُولَّد مباشرةً بلغة الطرف (لا تمرّ هنا)؛ هذه الطبقة لرسائل
المراحل الثابتة فقط.
"""
import re

_AR = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[A-Za-z]")

# تسميات تُعتبر «عربية» فلا تُترجم
_ARABIC_LABELS = {"العربية", "عربي", "عربية", "arabic", "ar"}

_cache = {}


def is_arabic(lang) -> bool:
    return (not lang) or str(lang).strip().lower() in {l.lower() for l in _ARABIC_LABELS}


def detect_lang(text) -> str:
    """يُرجع تسمية لغة: «العربية» للعربي، وإلا اسم اللغة (English/اردو/Tagalog…)."""
    t = (text or "").strip()
    if not t:
        return "العربية"
    ar = len(_AR.findall(t))
    lat = len(_LATIN.findall(t))
    if ar >= max(lat, 1):
        return "العربية"
    if ar == 0 and lat == 0:        # أرقام/رموز فقط → لا نغيّر
        return "العربية"
    # غير عربي واضح → اسأل النموذج عن اسم اللغة (مرّة واحدة لكل طرف عملياً)
    try:
        from simulator import call_llm
        r = call_llm(
            "Identify the language of the message. Reply with ONLY the language "
            "name (e.g. English, اردو, हिन्दी, Tagalog, Bahasa). One or two words.",
            t[:200], max_tokens=10, timeout=15)
        r = (r or "English").strip().splitlines()[0][:24]
        return r or "English"
    except Exception:
        return "English"


def localize(text_ar, lang) -> str:
    """يترجم رسالة مرحلة عربية إلى لغة الطرف. عربي → بلا تغيير."""
    if is_arabic(lang) or not text_ar:
        return text_ar
    key = (text_ar, str(lang))
    if key in _cache:
        return _cache[key]
    try:
        from simulator import call_llm
        out = call_llm(
            f"You are translating a friendly Saudi real-estate broker's WhatsApp message "
            f"into {lang}. Keep the same meaning, warm and concise tone. "
            f"Keep URLs, phone numbers, prices and emojis EXACTLY as they are. "
            f"Return ONLY the translation, no notes.",
            text_ar, max_tokens=420, timeout=25)
        out = (out or text_ar).strip() or text_ar
        _cache[key] = out
        return out
    except Exception:
        return text_ar
