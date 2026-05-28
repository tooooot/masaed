#!/usr/bin/env python3
"""
محاكي الأطراف — يحاكي محادثة تفاوضية كاملة
+ مساعد الانتقاد يكتشف الأخطاء ويحسّن الكود والتعليمات

المكونات:
1. Seeker Simulator — يتقمص دور الباحث (DeepSeek)
2. Owner Simulator — يتقمص دور صاحب العرض (DeepSeek)
3. Critic Assistant — يقيّم المحادثة والكود
4. Code Improver — يحسّن الكود والـ prompts بناءً على الملاحظات
"""

import os
import json
import time
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Seeker Simulator — يتقمص دور الباحث ──────────────────────────────────────

_SYS_SEEKER = """\
أنت باحث حقيقي عن سكن في السوق السعودي.

المعطيات:
- المدينة: {city}
- الحي المفضل: {district}
- عدد الغرف: {rooms}
- الميزانية: {budget} ريال/سنة
- الأثاث: {'مفروشة' if furnished else 'بدون أثاث'}
- الاحتياجات الخاصة: {special_needs}

السلوك:
- ابدأ بتحية ودية
- أخبر عن احتياجاتك بشكل طبيعي
- كن واقعياً في عروضك (لا تنزل أقل من ميزانيتك بـ 10%)
- اسأل عن التفاصيل المهمة (مصعد، موقف، فواتير)
- كن متحفظاً في البداية، تدريجياً أكثر مرونة
- استخدم العربية السعودية الطبيعية

الهدف: الوصول لاتفاق معقول بدون تضييع الوقت
"""

_SYS_OWNER = """\
أنت مالك عقار تريد تأجيره بسعر جيد.

المعطيات:
- العقار: {title}
- المدينة: {city}
- السعر المطلوب: {price} ريال/سنة
- الأثاث: {'مفروشة بالكامل' if furnished else 'بدون أثاث'}
- المواصفات: {specs}
- شروط التأجير: {terms}

السلوك:
- أرحب بالمستأجر بحماس
- اشرح مميزات العقار
- اطلب سعراً عادلاً (لا تنزل أكثر من 15%)
- اسأل عن خلفية المستأجر (وظيفة، عائلة)
- كن حازماً في البداية، لكن منفتحاً للتفاوض
- استخدم العربية السعودية الطبيعية

الهدف: إيجاد مستأجر موثوق بسعر جيد
"""

# ── Critic Assistant — يقيّم المحادثة والكود ────────────────────────────────

_SYS_CRITIC = """\
أنت "مساعد الانتقاد والتطوير" — محلل متخصص في التفاوضات العقارية.

مهمتك: تقييم محادثة التفاوض والكود الذي أنتجها

معايير التقييم:

1️⃣ جودة المحادثة:
   - هل كانت طبيعية وواقعية؟
   - هل التزم كل طرف بشخصيته؟
   - هل كان التفاوض منطقياً؟
   - هل وصلا لاتفاق معقول؟

2️⃣ أداء الوسيط (مساعد):
   - هل فهم النوايا صحيح؟
   - هل التكتيكات مناسبة؟
   - هل اقترح أسعاراً معقولة؟
   - هل حافظ على الحوار؟

3️⃣ أخطاء الكود:
   - هل هناك logic errors؟
   - هل الرسائل واضحة؟
   - هل التعامل مع الحالات الحدية؟
   - هل هناك missing cases؟

4️⃣ مشاكل التعليمات (prompts):
   - هل الـ prompt واضح وكافي؟
   - هل يشمل كل الحالات؟
   - هل هناك تناقضات؟
   - هل يحتاج توضيح أكثر؟

الإخراج:
أعد JSON بهذه الصيغة:
{{
  "conversation_quality": {
    "score": 1-10,
    "issues": ["مشكلة 1", "مشكلة 2", ...]
  },
  "mediator_performance": {
    "score": 1-10,
    "issues": ["مشكلة 1", ...]
  },
  "code_errors": {
    "critical": ["خطأ حرج 1", ...],
    "medium": ["خطأ متوسط", ...],
    "minor": ["خطأ بسيط", ...]
  },
  "prompt_issues": {
    "gaps": ["نقص 1", ...],
    "contradictions": ["تناقض 1", ...],
    "suggestions": ["اقتراح 1", ...]
  },
  "overall_score": 1-10,
  "recommendations": ["توصية 1", ...]
}}
"""

# ── LLM Calls ────────────────────────────────────────────────────────────────

def call_llm(system: str, user_msg: str, model: str = "deepseek") -> str | None:
    """استدعي LLM (DeepSeek أو Anthropic)"""

    if model == "anthropic" and ANTHROPIC_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                temperature=0.7,
                system=system,
                messages=[{"role": "user", "content": user_msg}]
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"[LLM] Anthropic error: {e}", flush=True)

    if DEEPSEEK_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg}
                ],
                max_tokens=500,
                temperature=0.7
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"[LLM] DeepSeek error: {e}", flush=True)

    return None


# ── Simulation Engine ─────────────────────────────────────────────────────────

class NegotiationSimulator:
    """محاكي التفاوض الكامل"""

    def __init__(self, reg_data: dict, listing_data: dict):
        """
        reg_data: بيانات الطلب (باحث)
        listing_data: بيانات العرض (مالك)
        """
        self.reg = reg_data
        self.listing = listing_data
        self.messages = []
        self.round = 0
        self.max_rounds = 6

    def get_seeker_message(self) -> str:
        """اطلب رسالة من الباحث"""
        system = _SYS_SEEKER.format(
            city=self.reg.get("city", "جدة"),
            district=self.reg.get("district", "—"),
            rooms=self.reg.get("rooms", 3),
            budget=self.reg.get("budget", 2200),
            furnished=self.reg.get("furnished", False),
            special_needs=self.reg.get("notes", "—")
        )

        context = "آخر الرسائل:\n"
        if self.messages:
            for msg in self.messages[-4:]:
                context += f"{msg['from']}: {msg['text']}\n"
        else:
            context = "هذه رسالتك الأولى"

        prompt = f"{context}\n\nالآن أرسل رسالتك (جملة أو جملتان):"

        return call_llm(system, prompt)

    def get_owner_message(self) -> str:
        """اطلب رسالة من صاحب العرض"""
        system = _SYS_OWNER.format(
            title=self.listing.get("title", "عقار"),
            city=self.listing.get("city", "جدة"),
            price=self.listing.get("price", 2800),
            furnished=self.listing.get("furnished", False),
            specs=self.listing.get("specs", "مواصفات عادية"),
            terms=self.listing.get("terms", "عام واحد")
        )

        context = "آخر الرسائل:\n"
        if self.messages:
            for msg in self.messages[-4:]:
                context += f"{msg['from']}: {msg['text']}\n"
        else:
            context = "هذه رسالتك الأولى"

        prompt = f"{context}\n\nالآن أرسل رسالتك (جملة أو جملتان):"

        return call_llm(system, prompt)

    def run(self) -> dict:
        """شغّل المحاكاة"""
        print("[SIM] بدء محاكاة التفاوض...", flush=True)

        # الرسالة الأولى من الباحث
        seeker_msg = self.get_seeker_message()
        if seeker_msg:
            self.messages.append({
                "round": 0,
                "from": "باحث",
                "text": seeker_msg,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            print(f"[SIM] 👤 الباحث: {seeker_msg[:80]}...", flush=True)

        # جولات التفاوض
        while self.round < self.max_rounds:
            self.round += 1

            # رد صاحب العرض
            owner_msg = self.get_owner_message()
            if owner_msg:
                self.messages.append({
                    "round": self.round,
                    "from": "مالك",
                    "text": owner_msg,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                print(f"[SIM] 🏠 المالك: {owner_msg[:80]}...", flush=True)

            # رد الباحث
            seeker_msg = self.get_seeker_message()
            if seeker_msg:
                self.messages.append({
                    "round": self.round,
                    "from": "باحث",
                    "text": seeker_msg,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                print(f"[SIM] 👤 الباحث: {seeker_msg[:80]}...", flush=True)

            # تأخير بين الرسائل
            time.sleep(0.5)

        return {
            "ok": True,
            "messages": self.messages,
            "rounds": self.round
        }


# ── Critic Engine ────────────────────────────────────────────────────────────

class CriticAssistant:
    """مساعد الانتقاد والتطوير"""

    def __init__(self):
        self.findings = None

    def evaluate(self, conversation: list, reg_data: dict, listing_data: dict) -> dict:
        """قيّم المحادثة والكود"""

        conv_text = "\n".join([f"{msg['from']}: {msg['text']}" for msg in conversation])

        prompt = f"""
المحادثة:
{conv_text}

بيانات الباحث: {json.dumps(reg_data, ensure_ascii=False)}
بيانات العرض: {json.dumps(listing_data, ensure_ascii=False)}

قيّم هذه المحادثة وأعد JSON بالمعايير المطلوبة.
"""

        result = call_llm(_SYS_CRITIC, prompt, model="anthropic")

        if result:
            try:
                # استخرج JSON من النص
                start = result.find('{')
                end = result.rfind('}') + 1
                if start != -1 and end > start:
                    self.findings = json.loads(result[start:end])
                    return self.findings
            except:
                pass

        return {"error": "فشل التقييم"}

    def get_recommendations(self) -> list:
        """احصل على توصيات التحسين"""
        if not self.findings:
            return []

        recs = []

        # من مشاكل المحادثة
        if self.findings.get("conversation_quality", {}).get("issues"):
            recs.extend(self.findings["conversation_quality"]["issues"])

        # من مشاكل الكود
        if self.findings.get("code_errors", {}).get("critical"):
            recs.extend([f"🔴 {e}" for e in self.findings["code_errors"]["critical"]])
        if self.findings.get("code_errors", {}).get("medium"):
            recs.extend([f"🟠 {e}" for e in self.findings["code_errors"]["medium"]])

        # من مشاكل التعليمات
        if self.findings.get("prompt_issues", {}).get("gaps"):
            recs.extend([f"📝 {e}" for e in self.findings["prompt_issues"]["gaps"]])

        return recs


# ── Main Export ──────────────────────────────────────────────────────────────

def simulate_negotiation(reg_id: int, reg_data: dict, listing_data: dict) -> dict:
    """
    شغّل محاكاة التفاوض الكاملة مع التقييم
    """
    print(f"[SIMULATOR] بدء محاكاة للطلب #{reg_id}", flush=True)

    # 1. شغّل المحاكاة
    simulator = NegotiationSimulator(reg_data, listing_data)
    sim_result = simulator.run()

    if not sim_result.get("ok"):
        return {"ok": False, "error": "فشلت المحاكاة"}

    # 2. قيّم المحادثة
    critic = CriticAssistant()
    evaluation = critic.evaluate(
        sim_result["messages"],
        reg_data,
        listing_data
    )

    # 3. النتيجة النهائية
    return {
        "ok": True,
        "reg_id": reg_id,
        "simulation": {
            "messages": sim_result["messages"],
            "rounds": sim_result["rounds"]
        },
        "evaluation": evaluation,
        "recommendations": critic.get_recommendations(),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
