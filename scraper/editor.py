#!/usr/bin/env python3
"""
مساعد المعدّل — يُعدّل بيانات تسجيل قائم بناءً على طلب العميل.

الحالات:
- مالك يريد تغيير السعر / إضافة تفاصيل / تغيير الحي
- باحث يريد تعديل ميزانيته أو متطلباته

التدفق:
1. يأتي من handle_message بعد فشل get_active_reg (لا تسجيل جارٍ)
2. يُكتشف أن المستخدم يريد التعديل
3. يُحمَّل آخر تسجيل مكتمل ويُوضع في حالة editing
4. LLM يرى البيانات الحالية ويعدّل ما طُلب فقط
5. يحفظ التعديلات ويُعيد الحالة إلى complete
"""
import os, json, re
from bot import (get_conn, wa_send, get_contact, get_contact_registrations,
                 ai_respond, update_reg, save_chat, get_chat_history,
                 get_profile_url, ANTHROPIC_KEY, USE_ANTHROPIC, BASE_URL)

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))

# ── كلمات تدل على رغبة التعديل ─────────────────────────────────────────────

_EDIT_TRIGGERS = re.compile(
    r'عدّل|عدل|غيّر|غير|بدّل|بدل|حدّث|حدث|تعديل|تغيير|تحديث'
    r'|أريد أغير|ابي اغير|أبغى أغير|ابغى اغير'
    r'|السعر تغيّر|السعر تغير|الميزانية تغيرت'
    r'|اعدل|اغير|ابدل',
    re.IGNORECASE
)

# ── System Prompt للمعدّل ────────────────────────────────────────────────────

_SYS_EDIT = """\
أنت "مساعد المعدّل" — تساعد العميل على تعديل بيانات تسجيله الموجود.

البيانات الحالية للتسجيل:
{current_data}

قواعد التعديل:
- اسأل تحديداً ما الذي يريد تغييره
- لا تُعيد سؤاله عن المعلومات التي لا يريد تغييرها
- استخرج القيمة الجديدة فقط وضعها في extracted
- الحقول التي لم يذكرها → ضعها null في extracted (تبقى كما هي)
- عندما تنتهي من التعديلات → complete: true

أعد ردك بهذا الـJSON:
{{
  "reply": "رد للعميل",
  "extracted": {{
    "name": null,
    "city": null,
    "district": null,
    "property_type": null,
    "rooms": null,
    "bathrooms": null,
    "floor": null,
    "furnished": null,
    "price_annual": null,
    "price_monthly": null,
    "for_family": null,
    "location_desc": null,
    "features": null,
    "budget_annual": null,
    "preferred_districts": null,
    "move_date": null,
    "special_notes": null
  }},
  "complete": false
}}

complete: true عندما يقول "شكراً" أو "تمام" أو "خلاص" أو يؤكد انتهاء التعديل.
"""


# ── DB helpers ───────────────────────────────────────────────────────────────

def get_editing_reg(phone: str) -> dict | None:
    """تسجيل في وضع التعديل."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, type, status, data_collected, name
            FROM sanad.masaed_registrations
            WHERE phone = %s AND status = 'editing'
            ORDER BY updated_at DESC LIMIT 1
        """, (phone,))
        row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "type": row[1], "status": row[2],
            "data": row[3] or {}, "name": row[4]}


def get_latest_complete_reg(phone: str) -> dict | None:
    """آخر تسجيل مكتمل."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, type, status, data_collected, name, city,
                   property_type, price_annual, budget_annual
            FROM sanad.masaed_registrations
            WHERE phone = %s AND status = 'complete'
            ORDER BY updated_at DESC LIMIT 1
        """, (phone,))
        row = cur.fetchone()
    conn.close()
    if not row:
        return None
    cols = ['id','type','status','data_collected','name','city',
            'property_type','price_annual','budget_annual']
    return dict(zip(cols, row))


def start_editing(reg_id: int) -> None:
    """ضع التسجيل في وضع التعديل."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_registrations
            SET status = 'editing', updated_at = NOW()
            WHERE id = %s
        """, (reg_id,))
        conn.commit()
    conn.close()


def finish_editing(reg_id: int) -> None:
    """أعد التسجيل لـcomplete بعد التعديل."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_registrations
            SET status = 'complete', updated_at = NOW()
            WHERE id = %s
        """, (reg_id,))
        conn.commit()
    conn.close()


def _format_current_data(reg: dict) -> str:
    """اعرض البيانات الحالية بشكل مقروء للـLLM."""
    d = reg.get("data_collected") or {}
    lines = [
        f"النوع: {'عرض عقار (listing)' if reg['type'] == 'listing' else 'طلب إيجار (wanted)'}",
    ]
    field_labels = {
        "name": "الاسم", "city": "المدينة", "district": "الحي",
        "property_type": "نوع العقار", "rooms": "الغرف", "bathrooms": "الحمامات",
        "floor": "الطابق", "furnished": "مفروش", "price_annual": "السعر السنوي",
        "price_monthly": "السعر الشهري", "for_family": "عوائل/عزاب",
        "location_desc": "الموقع", "features": "المميزات",
        "budget_annual": "الميزانية السنوية", "preferred_districts": "الأحياء المفضلة",
        "move_date": "موعد الانتقال", "special_notes": "ملاحظات",
    }
    for key, label in field_labels.items():
        val = d.get(key)
        if val is not None and val != [] and val != "":
            if isinstance(val, bool):
                val = "مفروش" if val else "غير مفروش"
            lines.append(f"• {label}: {val}")
    return "\n".join(lines)


# ── Is this an edit request? ─────────────────────────────────────────────────

def is_edit_request(text: str) -> bool:
    """هل الرسالة تطلب تعديل تسجيل قائم؟"""
    if not text:
        return False
    return bool(_EDIT_TRIGGERS.search(text))


# ── Main handler ──────────────────────────────────────────────────────────────

def handle_edit_message(phone: str, text: str) -> str | None:
    """
    معالجة رسائل المعدّل.
    يُستدعى من _handle_message_inner.
    """
    # ── هل في جلسة تعديل نشطة؟ ──────────────────────────────────────────────
    reg = get_editing_reg(phone)

    if reg is None:
        # ابحث عن آخر تسجيل مكتمل
        complete_reg = get_latest_complete_reg(phone)
        if not complete_reg:
            return None  # لا شيء للتعديل → المسجّل يتولى

        # ابدأ جلسة تعديل
        start_editing(complete_reg["id"])
        reg = {
            "id":   complete_reg["id"],
            "type": complete_reg["type"],
            "status": "editing",
            "data": complete_reg.get("data_collected") or {},
            "name": complete_reg.get("name"),
        }

        prop = complete_reg.get("property_type") or ""
        city = complete_reg.get("city") or ""
        desc = f"{prop} في {city}" if prop and city else (prop or city or "العقار")

        intro = (
            f"بالطبع 😊 سأساعدك في تعديل تسجيلك ({desc}).\n"
            f"ما الذي تريد تغييره؟"
        )
        save_chat(phone, reg["id"], "assistant",
                  json.dumps({"reply": intro, "extracted": {}, "complete": False},
                              ensure_ascii=False))
        return intro

    # ── جلسة تعديل جارية ─────────────────────────────────────────────────────
    reg_id       = reg["id"]
    current_data = reg["data"] or {}

    save_chat(phone, reg_id, "user", text)
    history  = get_chat_history(phone, reg_id)
    data_str = _format_current_data(reg)

    system   = _SYS_EDIT.format(current_data=data_str)

    # استخدم نفس ai_respond لكن بـsystem prompt مختلف
    ai_result = _ai_edit(history, system)
    reply     = ai_result.get("reply", "")
    extracted = ai_result.get("extracted") or {}
    complete  = ai_result.get("complete", False)

    # ادمج التعديلات (فقط القيم غير الـnull)
    merged = {**current_data}
    for k, v in extracted.items():
        if v is not None and v != [] and v != "null":
            merged[k] = v

    update_reg(reg_id, {"data_collected": merged, "complete": False})

    save_chat(phone, reg_id, "assistant",
              json.dumps({"reply": reply, "extracted": extracted, "complete": complete},
                          ensure_ascii=False))

    if complete:
        finish_editing(reg_id)
        profile_url = get_profile_url(reg_id)
        name_str = merged.get("name") or current_data.get("name") or ""
        reply += (
            f"\n\n✅ تم حفظ التعديلات{' يا ' + name_str if name_str else ''}!"
            f"\nصفحتك المحدّثة:\n{profile_url}"
        )

    print(f"[EDIT #{reg_id}] {phone} | complete={complete}", flush=True)
    return reply


def _ai_edit(history: list, system: str) -> dict:
    """استدعاء LLM بـsystem prompt المعدّل."""
    from bot import _parse_ai_response

    msgs = []
    for m in history[-12:]:
        role = "user" if m["role"] == "user" else "assistant"
        content = m["content"]
        if isinstance(content, str) and content.startswith("{"):
            try:
                content = json.loads(content).get("reply", content)
            except Exception:
                pass
        msgs.append({"role": role, "content": content})

    if not msgs or msgs[-1]["role"] == "assistant":
        msgs.append({"role": "user", "content": "(انتظر)"})

    if ANTHROPIC_KEY and USE_ANTHROPIC:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=system,
                messages=msgs,
            )
            return _parse_ai_response(resp.content[0].text.strip())
        except Exception as e:
            print(f"[EDIT] Anthropic: {e}", flush=True)

    if DEEPSEEK_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": system}] + msgs,
                response_format={"type": "json_object"},
                max_tokens=300,
            )
            return _parse_ai_response(resp.choices[0].message.content)
        except Exception as e:
            print(f"[EDIT] DeepSeek: {e}", flush=True)

    return {"reply": "وصلت رسالتك، سأتابع معك.", "extracted": {}, "complete": False}
