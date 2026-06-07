#!/usr/bin/env python3
"""
🔧 نظام التطوير — مراجِع ما بعد المحادثة (Self-Improvement Loop).

بعد كل محادثة (محاكاة أو حقيقية) يحلّلها مقابل:
- هوية «مساعد» الحالية (شخصية + قواعد + قوالب الرسائل).
- حقائق الصفقة (روابط/سعر/مدينة).

ويُخرج اقتراحات إصلاح محدّدة:
- type=identity → إصلاح صياغة/نبرة/رسالة، قابل للتطبيق بضغطة (يعدّل حقل الهوية).
- type=code     → خطأ منطقي/تقني (رابط خاطئ، تكرار، خلط أدوار) → يُبلَّغ للمطوّر.
- type=data     → بيانات ناقصة/خاطئة.

لا يطبّق شيئاً تلقائياً — يقترح فقط؛ المستخدم يعتمد. (تجنّباً لإدخال أخطاء جديدة.)
"""
import os
import json
from bot import get_conn

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))


def ensure_table():
    with get_conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS sanad.masaed_improvements(
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            source TEXT, neg_id INT,
            severity TEXT, stage TEXT, issue TEXT,
            type TEXT, field TEXT, suggestion TEXT, evidence TEXT,
            status TEXT DEFAULT 'open'
        )""")
        c.commit()


_SYS = """أنت مدقّق جودة صارم لبوت وساطة عقارية سعودي اسمه «مساعد».
ستحصل على: (أ) هوية البوت الحالية، (ب) حقائق الصفقة، (ج) محادثة كاملة بين الوسيط والطرفين.

مهمتك: اكتشف المشاكل الحقيقية فقط في أداء «الوسيط» (لا الطرفين). لا تخترع مشاكل ولا تتحذلق.
صنّف كل مشكلة:
- type=identity: خطأ في صياغة/نبرة/رسالة ثابتة → حدّد field من قائمة الحقول، واقترح النص البديل الكامل في suggestion.
- type=code: خطأ منطقي/تقني (رابط خاطئ أو مفقود، تكرار حرفي، تجاهل سؤال، خلط أدوار المالك/المستأجر، كشف رقم هاتف) → صِف الإصلاح في suggestion.
- type=data: بيانات ناقصة/خاطئة (سعر/موقع/غرف).

قواعد الهوية التي يجب احترامها: لا سعر في المبادرة، لا ربط مبكر، رابط الإعلان الذاتي موجود، تسجيل قبل تفاوض، صدق بلا مبالغة، لا ضغط، لا كشف أرقام.

لكل مشكلة: {stage, severity (high|medium|low), issue, type, field (إن identity وإلا اتركه فارغاً), suggestion, evidence (اقتباس قصير من المحادثة)}.

أعد JSON فقط:
{"score": <1-10 جودة أداء الوسيط>, "summary": "<سطر>", "findings": [...]}
إن كان الأداء سليماً فأعد findings=[]. الحقول المتاحة للهوية: {fields}"""


def _convo_text(transcript):
    lines = []
    for m in (transcript or []):
        frm = m.get("from", "?")
        to = m.get("to")
        tag = f"{frm}→{to}" if to else frm
        lines.append(f"[{tag}] {m.get('text', '')}")
    return "\n".join(lines)


def _call_llm(sys, user):
    if not DEEPSEEK_KEY:
        print("[REVIEWER] لا مفتاح DeepSeek", flush=True)
        return None
    try:
        import openai
        client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            response_format={"type": "json_object"}, max_tokens=2200, temperature=0.2)
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[REVIEWER] LLM: {e}", flush=True)
        return None


def review_conversation(transcript, deal_data=None, source="sim", neg_id=None):
    """يحلّل محادثة ويخزّن الاقتراحات. يُرجع ملخّصاً أو None."""
    if not transcript or len(transcript) < 3:
        return None
    ensure_table()
    import identity
    msg_keys = [k for k in identity.DEFAULTS if k not in ("persona", "principles")]
    ident = {
        "persona": identity.persona(),
        "principles": identity.principles(),
        "messages": {k: identity.cfg(k) for k in msg_keys},
    }
    fields = "، ".join(msg_keys + ["persona"])
    user = (f"الهوية:\n{json.dumps(ident, ensure_ascii=False)}\n\n"
            f"حقائق الصفقة:\n{json.dumps(deal_data or {}, ensure_ascii=False)}\n\n"
            f"المحادثة:\n{_convo_text(transcript)}")
    out = _call_llm(_SYS.replace("{fields}", fields), user)
    if not out:
        return None
    findings = out.get("findings", []) or []
    saved = 0
    valid = set(identity.DEFAULTS.keys())
    with get_conn() as c, c.cursor() as cur:
        for f in findings:
            fld = f.get("field") or None
            if fld and fld not in valid:
                fld = None
            cur.execute("""INSERT INTO sanad.masaed_improvements
                (source,neg_id,severity,stage,issue,type,field,suggestion,evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (source, neg_id, f.get("severity"), f.get("stage"), f.get("issue"),
                 f.get("type"), fld, f.get("suggestion"), f.get("evidence")))
            saved += 1
        c.commit()
    return {"score": out.get("score"), "summary": out.get("summary"), "count": saved}


def review_negotiation(neg_id):
    """يراجع تفاوضاً حقيقياً من chat_log (المختبرون/الإطلاق)."""
    from bot import get_conn
    with get_conn() as c, c.cursor() as cur:
        cur.execute("""SELECT chat_log, listing_title, listing_city, listing_price,
                              lead_phone, listing_phone
                       FROM sanad.masaed_negotiations WHERE id=%s""", (neg_id,))
        row = cur.fetchone()
    if not row:
        return None
    chat, title, city, price, lp, sp = row
    transcript = []
    for e in (chat or []):
        role = e.get("role", "") or ""
        text = e.get("text", "")
        if role.startswith("bot→"):
            transcript.append({"from": "الوسيط", "to": role.split("→", 1)[1], "text": text})
        else:
            transcript.append({"from": role, "text": text})
    facts = {"title": title, "city": city, "price": price}
    return review_conversation(transcript, facts, "real", neg_id)


def review_negotiation_async(neg_id):
    import threading
    threading.Thread(target=lambda: _safe_neg_review(neg_id), daemon=True).start()


def _safe_neg_review(neg_id):
    try:
        r = review_negotiation(neg_id)
        if r:
            print(f"[REVIEWER] تفاوض #{neg_id} → {r['count']} اقتراح (score {r.get('score')})", flush=True)
    except Exception as e:
        print(f"[REVIEWER] تفاوض #{neg_id} فشل: {e}", flush=True)


def review_async(transcript, deal_data=None, source="sim", neg_id=None):
    """يشغّل المراجعة في خيط منفصل (لا يؤخّر عرض نتيجة المحاكاة)."""
    import threading
    threading.Thread(target=lambda: _safe_review(transcript, deal_data, source, neg_id),
                     daemon=True).start()


def _safe_review(transcript, deal_data, source, neg_id):
    try:
        r = review_conversation(transcript, deal_data, source, neg_id)
        if r:
            print(f"[REVIEWER] {source} → {r['count']} اقتراح (score {r.get('score')})", flush=True)
    except Exception as e:
        print(f"[REVIEWER] فشل: {e}", flush=True)


def list_open(limit=100):
    ensure_table()
    with get_conn() as c, c.cursor() as cur:
        cur.execute("""SELECT id, created_at, source, neg_id, severity, stage, issue,
                              type, field, suggestion, evidence, status
                       FROM sanad.masaed_improvements
                       WHERE status='open'
                       ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                                created_at DESC LIMIT %s""", (limit,))
        cols = ["id", "created_at", "source", "neg_id", "severity", "stage", "issue",
                "type", "field", "suggestion", "evidence", "status"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
    return rows


def stats():
    ensure_table()
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM sanad.masaed_improvements GROUP BY status")
        return {s: n for s, n in cur.fetchall()}


def apply_improvement(imp_id):
    """يطبّق اقتراح هوية (نصّي) على الحقل المناسب. غير الهوية لا يُطبَّق تلقائياً."""
    ensure_table()
    import identity
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT type, field, suggestion FROM sanad.masaed_improvements WHERE id=%s", (imp_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "غير موجود"}
        typ, field, sugg = row
        if typ != "identity" or not field or field == "principles":
            return {"ok": False, "error": "هذا الاقتراح (كود/بيانات) يحتاج مراجعة يدوية — لا يُطبَّق تلقائياً."}
        if field not in identity.DEFAULTS or not sugg:
            return {"ok": False, "error": f"حقل غير صالح: {field}"}
        ov = identity._overrides()
        ov[field] = sugg
        identity.save_overrides(ov)
        cur.execute("UPDATE sanad.masaed_improvements SET status='applied' WHERE id=%s", (imp_id,))
        c.commit()
    return {"ok": True, "field": field}


def dismiss_improvement(imp_id):
    ensure_table()
    with get_conn() as c, c.cursor() as cur:
        cur.execute("UPDATE sanad.masaed_improvements SET status='dismissed' WHERE id=%s", (imp_id,))
        c.commit()
    return {"ok": True}
