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
import re
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

def _load_env():
    """Load .env file and set environment variables"""
    env_file = "/root/.env"
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                if line.strip() and not line.startswith('#'):
                    match = re.match(r'(\w+)=(.+)', line.strip())
                    if match:
                        key = match.group(1)
                        val = match.group(2).strip('"\'')
                        os.environ[key] = val

_load_env()

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Seeker Simulator — يتقمص دور الباحث ──────────────────────────────────────

from prompts import SYS_SEEKER as _SYS_SEEKER, SYS_OWNER as _SYS_OWNER, SYS_CRITIC as _SYS_CRITIC


# ── LLM Calls ────────────────────────────────────────────────────────────────

LLM_TIMEOUT = 20.0   # ثانية — يمنع تعليق طلب المحاكاة وتجاوز مهلة nginx
LLM_RETRIES = 2      # محاولة + إعادة واحدة عند الفشل العابر

def call_llm(system: str, user_msg: str, model: str = "deepseek",
             max_tokens: int = 500, timeout: float = None) -> str | None:
    """استدعي LLM (DeepSeek أو Anthropic) مع مهلة وإعادة محاولة"""

    tmo = timeout or LLM_TIMEOUT

    for attempt in range(1, LLM_RETRIES + 1):
        if model == "anthropic" and ANTHROPIC_KEY:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=tmo)
                resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=max_tokens,
                    temperature=0.7,
                    system=system,
                    messages=[{"role": "user", "content": user_msg}]
                )
                return resp.content[0].text.strip()
            except Exception as e:
                print(f"[LLM] Anthropic error (محاولة {attempt}/{LLM_RETRIES}): {e}", flush=True)

        if DEEPSEEK_KEY:
            try:
                import openai
                client = openai.OpenAI(
                    api_key=DEEPSEEK_KEY,
                    base_url="https://api.deepseek.com/v1",
                    timeout=tmo
                )
                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_msg}
                    ],
                    max_tokens=max_tokens,
                    temperature=0.7
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                print(f"[LLM] DeepSeek error (محاولة {attempt}/{LLM_RETRIES}): {e}", flush=True)

        if attempt < LLM_RETRIES:
            time.sleep(1)  # backoff بسيط قبل الإعادة

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
        self.max_rounds = 4

    def get_seeker_message(self) -> str:
        """اطلب رسالة من الباحث"""
        furnished_text = "مفروشة" if self.reg.get("furnished", False) else "بدون أثاث"
        system = _SYS_SEEKER.format(
            city=self.reg.get("city", "جدة"),
            district=self.reg.get("district", "—"),
            rooms=self.reg.get("rooms", 3),
            budget=self.reg.get("budget", 2200),
            furnished=furnished_text,
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
        furnished_text = "مفروشة بالكامل" if self.listing.get("furnished", False) else "بدون أثاث"
        system = _SYS_OWNER.format(
            title=self.listing.get("title", "عقار"),
            city=self.listing.get("city", "جدة"),
            price=self.listing.get("price", 2800),
            furnished=furnished_text,
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

    # كلمات تدل على بلوغ اتفاق نهائي → نوقف المحاكاة مبكراً
    _AGREE_SIGNALS = [
        "اتفقنا", "نلتقي", "أوقع", "اوقع", "خلاص نتفق", "تم الاتفاق",
        "نتفق على", "العقد جاهز", "العقد حاضر", "تعال نشوف", "نلتقي بكرة",
    ]

    def _reached_agreement(self) -> bool:
        """افحص آخر رسالتين بحثاً عن إشارة اتفاق نهائي"""
        recent = " ".join(m["text"] for m in self.messages[-2:])
        return any(sig in recent for sig in self._AGREE_SIGNALS)

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

            # توقّف مبكر عند بلوغ اتفاق (بعد جولتين على الأقل) — يمنع التكرار والهدر
            if self.round >= 2 and self._reached_agreement():
                print(f"[SIM] ✅ تم بلوغ اتفاق في الجولة {self.round} — إيقاف مبكر", flush=True)
                break

            # تأخير بسيط بين الجولات
            time.sleep(0.3)

        # كشف التشغيلات المُنحطّة: لا نُرجع "نجاحاً" بمحادثة فارغة
        if len(self.messages) < 2:
            return {
                "ok": False,
                "error": "تعذّر توليد المحادثة — لا استجابة من نموذج اللغة (تحقّق من مفاتيح API أو حاول مجدداً)",
                "messages": self.messages,
                "rounds": self.round,
            }

        return {
            "ok": True,
            "messages": self.messages,
            "rounds": self.round
        }


# ── محلّل JSON مرن للناقد (لا يفشل التقييم) ──────────────────────────────────────

def _try_json(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None


def _balance_close(s: str) -> str:
    """يردّ JSON مقطوعاً: يغلق أي سلسلة/قوس مفتوح ويحذف فاصلة زائدة."""
    out, stack, in_str, esc = [], [], False, False
    for ch in s:
        out.append(ch)
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    if in_str:                                  # سلسلة غير منتهية → أغلقها
        out.append('"')
    res = re.sub(r",\s*$", "", "".join(out).rstrip())
    closer = {"{": "}", "[": "]"}
    res += "".join(closer[c] for c in reversed(stack))
    return res


def _robust_json(raw: str):
    """استخراج JSON بثلاث طبقات: مباشر → إصلاح فواصل → موازنة أقواس مقطوعة."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s).strip()
    a = s.find("{")
    if a == -1:
        return None
    s = s[a:]
    b = s.rfind("}")
    if b != -1:
        d = _try_json(s[: b + 1])
        if d is not None:
            return d
    cand = re.sub(r",(\s*[}\]])", r"\1", s)      # احذف الفواصل الزائدة
    b = cand.rfind("}")
    if b != -1:
        d = _try_json(cand[: b + 1])
        if d is not None:
            return d
    return _try_json(_balance_close(cand))       # ردّ مقطوع → وازن وأغلق


def _salvage_findings(raw: str):
    """آخر ملاذ: التقاط النقاط والمشاكل بـregex حتى لو JSON معطوب كلياً."""
    out = {}
    for key in ("conversation_quality", "mediator_performance",
                "code_errors", "linguistic_quality"):
        seg = re.search(key + r'"\s*:\s*\{(.*?)(?:\}\s*,\s*"|\}\s*\})', raw, re.DOTALL)
        chunk = seg.group(1) if seg else ""
        sc = re.search(r'"score"\s*:\s*(\d+)', chunk)
        blk = re.search(r'"(?:issues|critical)"\s*:\s*\[(.*?)\]', chunk, re.DOTALL)
        issues = re.findall(r'"([^"]{3,})"', blk.group(1)) if blk else []
        if sc or issues:
            out[key] = {"score": int(sc.group(1)) if sc else None, "issues": issues}
    return out or None


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

        # max_tokens أوسع + تعليمة صرامة JSON لتقليل القطع/التشويه
        prompt += ("\n\nمهم: أعد JSON صالحاً فقط بلا أي نص خارجه، واجعل كل بند في "
                   "issues جملة قصيرة جداً (≤ 12 كلمة) كي لا يُقطع الرد.")
        # DeepSeek فقط (لا Anthropic) — بقرار المستخدم: مزوّد واحد
        result = call_llm(_SYS_CRITIC, prompt, model="deepseek", max_tokens=3000, timeout=80)

        if not result:
            print("[CRITIC] لا يوجد رد من Anthropic", flush=True)
            return {"error": "فشل التقييم", "_reason": "no_response"}

        # طبقة 1+2+3: محلّل مرن (مباشر/إصلاح فواصل/موازنة أقواس مقطوعة)
        parsed = _robust_json(result)
        if parsed is not None:
            self.findings = parsed
            return self.findings

        # طبقة 4: إنقاذ بـregex (تقييم منقوص لكنه مفيد ولا يفشل)
        salvaged = _salvage_findings(result)
        if salvaged:
            salvaged["_degraded"] = "تم إنقاذ التقييم جزئياً (JSON معطوب)"
            print(f"[CRITIC] JSON معطوب — أُنقِذ جزئياً ({len(salvaged)} أقسام)", flush=True)
            self.findings = salvaged
            return self.findings

        print(f"[CRITIC] تعذّر التحليل والإنقاذ | الرد: {result[:200]}", flush=True)
        return {"error": "فشل التقييم", "_raw": result[:1000]}

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
        return {
            "ok": False,
            "error": sim_result.get("error", "فشلت المحاكاة"),
            "simulation": {
                "messages": sim_result.get("messages", []),
                "rounds": sim_result.get("rounds", 0),
            },
        }

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
