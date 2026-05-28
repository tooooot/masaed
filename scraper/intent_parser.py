#!/usr/bin/env python3
"""
Intent Parser — يستخرج النية المنظّمة من رسالة واتساب.
LLM يُجيب سؤالاً واحداً: ماذا يريد هذا الشخص؟
"""
import os, re, json

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEEPSEEK_KEY  = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))

_SYSTEM = """\
أنت محلل نصوص عقارية متخصص في العامية السعودية.
استخرج النية من رسالة المستخدم وأعد JSON فقط — لا نص خارجه.

الحقول:
- intent: "price_offer" | "accept" | "reject" | "question" | "cancel" | "other"
- amount: رقم صحيح للسعر المذكور، أو null
- sentiment: "positive" | "negative" | "neutral"
- is_firm: true إذا كان الموقف نهائياً حازماً ("آخر كلام"، "ما أقدر أنزل")

أمثلة:
"10000 وبس" → {"intent":"price_offer","amount":10000,"sentiment":"neutral","is_firm":true}
"ممكن تنزل لـ11500؟" → {"intent":"price_offer","amount":11500,"sentiment":"neutral","is_firm":false}
"موافق" → {"intent":"accept","amount":null,"sentiment":"positive","is_firm":true}
"السعر غالي ما يناسبني" → {"intent":"reject","amount":null,"sentiment":"negative","is_firm":false}
"في مصعد؟ كم الدور؟" → {"intent":"question","amount":null,"sentiment":"neutral","is_firm":false}
"لا يهمني، إلغاء" → {"intent":"cancel","amount":null,"sentiment":"negative","is_firm":true}
"حسناً سأفكر" → {"intent":"other","amount":null,"sentiment":"neutral","is_firm":false}
"""

_DEFAULT = {"intent": "other", "amount": None, "sentiment": "neutral", "is_firm": False}


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
    """Extract structured intent from a single Arabic message."""
    prompt = f"الرسالة: {text}"

    if ANTHROPIC_KEY:
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
