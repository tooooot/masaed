#!/usr/bin/env python3
"""
مساعد المسجّل — WhatsApp registration bot
Collects property listing or rental request data via conversation,
saves to DB, and generates a profile deep link.
"""
import os, re, json, time, hashlib, requests, threading
import psycopg2

# ── Phone-level mutex (Fix: race condition) ───────────────────────────────────
_phone_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

def _phone_lock(phone: str) -> threading.Lock:
    with _locks_guard:
        if phone not in _phone_locks:
            _phone_locks[phone] = threading.Lock()
        return _phone_locks[phone]

# ── Config ────────────────────────────────────────────────────────────────────
GREEN_INSTANCE = os.getenv("MASAED_GREEN_INSTANCE", "")
GREEN_TOKEN    = os.getenv("MASAED_GREEN_TOKEN", "")
DEEPSEEK_KEY   = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))
BASE_URL       = os.getenv("MASAED_BASE_URL", "https://masaed.wardyat.net")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

DB_HOST = os.getenv("POSTGRES_HOST", "sanad-postgres")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "sanad")
DB_USER = os.getenv("POSTGRES_USER", "sanad")
DB_PASS = os.getenv("POSTGRES_PASSWORD", os.getenv("PG_SANAD_PWD", ""))

# ── DB ────────────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT,
                            dbname=DB_NAME, user=DB_USER, password=DB_PASS)

def get_contact(phone: str) -> dict:
    """Load permanent contact profile. Creates if new."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_contacts (phone)
            VALUES (%s)
            ON CONFLICT (phone) DO UPDATE SET last_seen = NOW()
            RETURNING phone, name, city, is_family, total_regs,
                      last_reg_type, last_reg_id, notes, profile, first_seen
        """, (phone,))
        row = cur.fetchone()
        conn.commit()
    conn.close()
    if not row:
        return {"phone": phone, "name": None, "city": None, "total_regs": 0,
                "last_reg_type": None, "last_reg_id": None, "notes": None, "profile": {}}
    return {
        "phone": row[0], "name": row[1], "city": row[2], "is_family": row[3],
        "total_regs": row[4] or 0, "last_reg_type": row[5], "last_reg_id": row[6],
        "notes": row[7], "profile": row[8] or {}, "first_seen": str(row[9] or "")
    }

def update_contact(phone: str, data: dict):
    """Update permanent contact with new info extracted from conversation."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_contacts SET
                name      = COALESCE(%s, name),
                city      = COALESCE(%s, city),
                is_family = COALESCE(%s, is_family),
                notes     = COALESCE(%s, notes),
                profile   = profile || %s::jsonb,
                last_seen = NOW()
            WHERE phone = %s
        """, (
            data.get("name") or None,
            data.get("city") or None,
            data.get("is_family"),
            data.get("notes") or None,
            json.dumps({k: v for k, v in data.items()
                        if k not in ("name","city","is_family","notes") and v is not None}),
            phone
        ))
        conn.commit()
    conn.close()

def sync_contact_after_reg(phone: str, reg_id: int, reg_type: str, name: str = None, city: str = None):
    """Update contact stats after a registration is saved."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_contacts SET
                total_regs   = total_regs + 1,
                last_reg_type = %s,
                last_reg_id   = %s,
                name          = COALESCE(%s, name),
                city          = COALESCE(%s, city),
                last_seen     = NOW()
            WHERE phone = %s
        """, (reg_type, reg_id, name or None, city or None, phone))
        conn.commit()
    conn.close()

def get_contact_registrations(phone: str) -> list:
    """Load all meaningful registrations for a phone (with data)."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, type, city, district, property_type, rooms,
                   price_annual, price_monthly, for_family, furnished, status
            FROM sanad.masaed_registrations
            WHERE phone = %s
              AND (name IS NOT NULL OR data_collected != '{}')
              AND type IS NOT NULL
            ORDER BY created_at DESC LIMIT 5
        """, (phone,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows

def build_memory_context(contact: dict, regs: list = None) -> str:
    """Build a rich memory summary to inject into AI system prompt."""
    if not contact.get("name"):
        return ""

    lines = ["📱 معلومات العميل المحفوظة (لا تسأل عنها مجدداً):"]
    lines.append(f"• الاسم: {contact['name']}")
    if contact.get("city"):
        lines.append(f"• المدينة: {contact['city']}")
    if contact.get("is_family") is not None:
        lines.append(f"• النوع: {'عائلي' if contact['is_family'] else 'عزاب'}")

    if regs:
        lines.append(f"\n📂 تسجيلاته السابقة ({len(regs)}):")
        for r in regs:
            reg_type = "عرض عقار" if r["type"] == "listing" else "طلب إيجار"
            status   = "✅ مكتمل" if r["status"] == "complete" else "🔄 جارٍ"
            profile  = f"{BASE_URL}/p/{r['id']}"
            desc_parts = []
            if r.get("property_type"): desc_parts.append(r["property_type"])
            if r.get("rooms"):         desc_parts.append(f"{r['rooms']} غرف")
            if r.get("district"):      desc_parts.append(r["district"])
            if r.get("city"):          desc_parts.append(r["city"])
            if r.get("price_annual"):  desc_parts.append(f"{r['price_annual']:,} ريال/سنة")
            desc = " | ".join(desc_parts) if desc_parts else "بدون تفاصيل"
            lines.append(f"  — #{r['id']} {reg_type} {status}: {desc}")
            lines.append(f"    رابط الصفحة: {profile}")

    lines.append("\nعامل هذا العميل كصديق قديم — رحّب به بحرارة وأشر لآخر تعامل معه.")
    return "\n".join(lines)

def get_active_reg(phone: str) -> dict | None:
    """Return active registration only if it has real data (type known)."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, type, status, data_collected, name
            FROM sanad.masaed_registrations
            WHERE phone = %s AND status = 'collecting' AND type IS NOT NULL
            ORDER BY created_at DESC LIMIT 1
        """, (phone,))
        row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "type": row[1], "status": row[2],
            "data": row[3] or {}, "name": row[4]}

def create_reg(phone: str, reg_type: str, name: str = None, city: str = None) -> int:
    """Create a registration row only after type is confirmed."""
    conn = get_conn()
    with conn.cursor() as cur:
        # Archive any leftover empty shells
        cur.execute("""
            UPDATE sanad.masaed_registrations
            SET status = 'abandoned'
            WHERE phone = %s AND status = 'collecting'
              AND type IS NULL AND data_collected = '{}'
        """, (phone,))
        cur.execute("""
            INSERT INTO sanad.masaed_registrations (phone, type, name, city, status)
            VALUES (%s, %s, %s, %s, 'collecting') RETURNING id
        """, (phone, reg_type, name or None, city or None))
        reg_id = cur.fetchone()[0]
        conn.commit()
    conn.close()
    return reg_id

def save_pending_chat(phone: str, role: str, content: str):
    """Save pre-registration messages to contact profile until type is known."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_contacts
            SET profile = jsonb_set(
                profile,
                '{pending_chat}',
                COALESCE(profile->'pending_chat', '[]'::jsonb) || %s::jsonb
            )
            WHERE phone = %s
        """, (json.dumps([{"role": role, "content": content}]), phone))
        conn.commit()
    conn.close()

def save_pending_media(phone: str, local_url: str, media_type: str):
    """Store downloaded media URL in contact profile until reg is created."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_contacts
            SET profile = jsonb_set(
                profile,
                '{pending_media}',
                COALESCE(profile->'pending_media', '[]'::jsonb) || %s::jsonb
            )
            WHERE phone = %s
        """, (json.dumps([{"url": local_url, "type": media_type}]), phone))
        conn.commit()
    conn.close()

def flush_pending_chat(phone: str, reg_id: int):
    """Migrate pending messages + media to a real registration and clear them."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT profile->'pending_chat', profile->'pending_media'
            FROM sanad.masaed_contacts WHERE phone = %s
        """, (phone,))
        row = cur.fetchone()
        pending_chat  = row[0] if row and row[0] else []
        pending_media = row[1] if row and row[1] else []

        for msg in pending_chat:
            cur.execute("""
                INSERT INTO sanad.masaed_chats (phone, reg_id, role, content)
                VALUES (%s, %s, %s, %s)
            """, (phone, reg_id, msg["role"], msg["content"]))

        # Attach pre-reg media to the new registration
        for m in pending_media:
            cur.execute("""
                UPDATE sanad.masaed_registrations
                SET photos = photos || %s::jsonb, updated_at = NOW()
                WHERE id = %s
            """, (json.dumps([m]), reg_id))

        cur.execute("""
            UPDATE sanad.masaed_contacts
            SET profile = profile - 'pending_chat' - 'pending_media'
            WHERE phone = %s
        """, (phone,))
        conn.commit()
    conn.close()

def get_pending_history(phone: str) -> list:
    """Get pending messages before registration is created."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT profile->'pending_chat' FROM sanad.masaed_contacts WHERE phone = %s", (phone,))
        row = cur.fetchone()
    conn.close()
    pending = row[0] if row and row[0] else []
    return list(pending)[-20:]  # last 20

def get_chat_history(phone: str, reg_id: int, limit: int = 20) -> list:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT role, content FROM sanad.masaed_chats
            WHERE phone = %s AND reg_id = %s
            ORDER BY created_at DESC LIMIT %s
        """, (phone, reg_id, limit))
        rows = cur.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def save_chat(phone: str, reg_id: int, role: str, content: str):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_chats (phone, reg_id, role, content)
            VALUES (%s, %s, %s, %s)
        """, (phone, reg_id, role, content))
        conn.commit()
    conn.close()

FLOOR_MAP = {
    'أول': 1, 'الأول': 1, 'اول': 1,
    'ثاني': 2, 'الثاني': 2, 'ثانى': 2,
    'ثالث': 3, 'الثالث': 3,
    'رابع': 4, 'الرابع': 4,
    'خامس': 5, 'الخامس': 5,
    'ارضي': 0, 'أرضي': 0, 'الأرضي': 0, 'ground': 0,
    'قبو': -1,
}

def _to_int(val):
    """Convert value to int safely; handles Arabic floor words."""
    if val is None: return None
    if isinstance(val, int): return val
    s = str(val).strip()
    # Arabic floor words
    for word, num in FLOOR_MAP.items():
        if word in s: return num
    # Extract first number
    m = re.search(r'\d+', s)
    if m:
        try: return int(m.group())
        except: pass
    return None

def update_reg(reg_id: int, data: dict):
    """Update registration with extracted data."""
    d = data.get("data_collected", {})

    # Build slug from name + type
    name = d.get("name") or data.get("name", "")
    reg_type = d.get("type") or data.get("type", "")
    city = d.get("city", "")
    slug_base = f"{name}-{reg_type}-{city}".lower()
    slug_base = re.sub(r'[^a-z0-9؀-ۿ]', '-', slug_base)
    slug_base = re.sub(r'-+', '-', slug_base).strip('-')
    slug = slug_base[:40] + "-" + str(reg_id)

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_registrations SET
                name          = COALESCE(%s, name),
                type          = COALESCE(%s, type),
                slug          = %s,
                city          = COALESCE(%s, city),
                district      = COALESCE(%s, district),
                property_type = COALESCE(%s, property_type),
                rooms         = COALESCE(%s, rooms),
                bathrooms     = COALESCE(%s, bathrooms),
                floor_num     = COALESCE(%s, floor_num),
                furnished     = COALESCE(%s, furnished),
                price_annual  = COALESCE(%s, price_annual),
                price_monthly = COALESCE(%s, price_monthly),
                for_family    = COALESCE(%s, for_family),
                location_desc = COALESCE(%s, location_desc),
                features      = COALESCE(%s::jsonb, features),
                budget_annual = COALESCE(%s, budget_annual),
                preferred_districts = COALESCE(%s::jsonb, preferred_districts),
                move_date     = COALESCE(%s, move_date),
                special_notes = COALESCE(%s, special_notes),
                status        = COALESCE(%s, status),
                data_collected = %s::jsonb,
                updated_at    = NOW()
            WHERE id = %s
        """, (
            name or None,
            d.get("type") or data.get("type") or None,
            slug,
            d.get("city") or None,
            d.get("district") or None,
            d.get("property_type") or None,
            _to_int(d.get("rooms")),
            _to_int(d.get("bathrooms")),
            _to_int(d.get("floor") or d.get("floor_num")),
            d.get("furnished") if d.get("furnished") is not None else None,
            _to_int(d.get("price_annual")),
            _to_int(d.get("price_monthly")),
            d.get("for_family") or None,
            d.get("location_desc") or None,
            json.dumps(d.get("features") or []) if d.get("features") else None,
            _to_int(d.get("budget_annual")),
            json.dumps(d.get("preferred_districts") or []) if d.get("preferred_districts") else None,
            str(d.get("move_date")) if d.get("move_date") else None,
            d.get("special_notes") or None,
            "complete" if data.get("complete") else None,
            json.dumps(d),
            reg_id
        ))
        conn.commit()
    conn.close()
    return slug

def save_media(reg_id: int, url: str, media_type: str = "image"):
    """Save photo or video URL to registration. Stores as {url, type} objects."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_registrations
            SET photos = photos || %s::jsonb, updated_at = NOW()
            WHERE id = %s
        """, (json.dumps([{"url": url, "type": media_type}]), reg_id))
        conn.commit()
    conn.close()

def save_photo(reg_id: int, url: str):
    save_media(reg_id, url, "image")

# ── Green API ─────────────────────────────────────────────────────────────────

# Thread-local dry-run interceptor — set by /bot/test to prevent real WA sends
_wa_test_local = threading.local()

def wa_send(phone: str, text: str):
    # Dry-run mode: capture message without sending
    if getattr(_wa_test_local, 'active', False):
        if not hasattr(_wa_test_local, 'log'):
            _wa_test_local.log = []
        _wa_test_local.log.append({"to": phone, "text": text})
        print(f"[WA DRY-RUN] → {phone}: {text[:80]}", flush=True)
        return

    if not GREEN_INSTANCE or not GREEN_TOKEN:
        print(f"[WA MOCK] → {phone}: {text[:80]}")
        return
    url = f"https://api.green-api.com/waInstance{GREEN_INSTANCE}/sendMessage/{GREEN_TOKEN}"
    try:
        requests.post(url, json={"chatId": f"{phone}@c.us", "message": text}, timeout=10)
    except Exception as e:
        print(f"[WA ERROR] {e}")

def wa_get_file(url_file: str) -> str | None:
    """Download a WhatsApp media file and return a local URL."""
    if not url_file:
        return None
    try:
        resp = requests.get(url_file, timeout=60)
        if resp.ok:
            content_type = resp.headers.get("Content-Type", "")
            if "video" in content_type:
                ext = "mp4"
            elif "jpeg" in content_type or "jpg" in content_type:
                ext = "jpg"
            elif "png" in content_type:
                ext = "png"
            else:
                ext = url_file.split(".")[-1].split("?")[0][:4] or "jpg"
            fname = hashlib.md5(url_file.encode()).hexdigest() + "." + ext
            path = f"/app/photos/{fname}"
            os.makedirs("/app/photos", exist_ok=True)
            with open(path, "wb") as f:
                f.write(resp.content)
            return f"{BASE_URL}/photos/{fname}"
    except Exception as e:
        print(f"[MEDIA ERROR] {e}")
    return None

# ── AI Conversation ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = """أنت "مساعد" — وكيل عقاري ذكي تعمل عبر واتساب في المملكة العربية السعودية.
لديك ذاكرة دائمة لكل عميل — لا تنسى أي رقم مهما مرّ من الوقت.

━━━━━━━━━━━━━━━━━━━━
إذا وردت "معلومات العميل المحفوظة" في هذا الـprompt:
- رحّب بالعميل باسمه فوراً مع ترحيب دافئ يُشعره بأنك تتذكره
- لا تسأل عن معلومات موجودة مسبقاً (الاسم / المدينة / النوع)
- اسأل: هل لديه عقار جديد أم طلب جديد؟ مع الإشارة لآخر تعامل

إذا لم توجد معلومات محفوظة (عميل جديد):
رحّب وابدأ بسؤال واحد:
"مرحباً! 🏠 أنا مساعد العقاري. هل أنت:
1️⃣ صاحب عقار تريد إدراجه للإيجار
2️⃣ تبحث عن عقار للإيجار"

━━━━━━━━━━━━━━━━━━━━
إذا كان صاحب عقار (listing)، اجمع بالترتيب:
1. اسمه (تخطّ إذا معروف)
2. نوع العقار (شقة / فيلا / غرفة / استوديو / دور)
3. المدينة والحي (تخطّ المدينة إذا معروفة)
4. عدد الغرف وعدد الحمامات
5. الطابق
6. مفروش أم فارغ
7. السعر السنوي (وهل يقبل شهري)
8. عوائل / عزاب / الكل
9. وصف الموقع أو رابط خرائط جوجل
10. مميزات إضافية (موقف / مصعد / مسبح / حديقة)
11. اطلب منه إرسال صور العقار

━━━━━━━━━━━━━━━━━━━━
إذا كان باحثاً عن عقار (request)، اجمع بالترتيب:
1. اسمه (تخطّ إذا معروف)
2. عوائل أم عزاب
3. المدينة والأحياء المفضلة
4. عدد الغرف المطلوبة
5. مفروش أم فارغ
6. الميزانية السنوية
7. تاريخ الانتقال المطلوب
8. أي متطلبات أو ملاحظات خاصة

━━━━━━━━━━━━━━━━━━━━
القواعد الذهبية:
- سؤال واحد فقط في كل رسالة
- أكّد فهمك لكل إجابة بكلمة لطيفة قبل الانتقال
- لا تسأل عن معلومة ذكرها العميل من قبل في أي جلسة
- إذا أرسل صورة أو فيديو بدون نص: النظام تعامل معه تلقائياً، لا تذكره
- كن ودّياً كصديق قديم مع العملاء المعروفين

━━━━━━━━━━━━━━━━━━━━
قواعد ذكية لتحديد النوع:
- إذا أرسل المستخدم صورة أو فيديو كأول رسالة → افترض أنه صاحب عقار (listing) فوراً، لا تسأل
- إذا قال "عندي شقة/فيلا/غرفة/عقار" → listing مباشرة
- إذا قال "أبحث/أريد أيجار/محتاج" → request مباشرة
- لا تسأل "صاحب عقار أم تبحث؟" إلا إذا لم يكن هناك أي مؤشر

━━━━━━━━━━━━━━━━━━━━
تحديث عقار قائم:
إذا طلب العميل إضافة صور/فيديو لعقار سابق أو ذكر "إعلاني القديم" أو "عقاري رقم X":
- اشكره وأخبره أن الوسائط ستُضاف لإعلانه
- ضع رقم التسجيل في update_reg_id (من قائمة تسجيلاته أعلاه)
- إذا لم تعرف الرقم بالضبط، ضع رقم آخر تسجيل له

━━━━━━━━━━━━━━━━━━━━
أعد ردك دائماً بهذا الـJSON:
{
  "reply": "الرسالة التي ستُرسل للمستخدم",
  "extracted": {
    "name": null,
    "type": "listing|request|null",
    "city": null,
    "district": null,
    "property_type": null,
    "rooms": null,
    "bathrooms": null,
    "floor": null,
    "furnished": null,
    "price_annual": null,
    "price_monthly": null,
    "for_family": "family|bachelor|both|null",
    "location_desc": null,
    "features": [],
    "budget_annual": null,
    "preferred_districts": [],
    "move_date": null,
    "special_notes": null,
    "update_reg_id": null
  },
  "complete": false
}

ضع null للحقول غير المعروفة. complete:true فقط عند اكتمال البيانات الأساسية.
"""

def ai_respond(history: list, current_data: dict, memory_ctx: str = "") -> dict:
    """Call AI and get reply + extracted data."""
    data_summary = json.dumps(current_data, ensure_ascii=False)

    # Try Anthropic first, then DeepSeek as fallback
    if ANTHROPIC_KEY:
        try:
            return _ai_anthropic(history, data_summary, memory_ctx)
        except Exception as e:
            print(f"[AI] Anthropic failed ({e}), falling back to DeepSeek", flush=True)

    if DEEPSEEK_KEY:
        try:
            return _ai_deepseek(history, data_summary, memory_ctx)
        except Exception as e:
            print(f"[AI] DeepSeek also failed: {e}", flush=True)

    return {"reply": "عذراً، خدمة الذكاء الاصطناعي غير متاحة حالياً.", "extracted": {}, "complete": False}

def _context_prefix(data_summary: str, memory_ctx: str) -> str:
    """Dynamic context injected as first user message — keeps system prompt static for caching."""
    parts = []
    if memory_ctx:
        parts.append(memory_ctx)
    parts.append(f"البيانات المجموعة حتى الآن:\n{data_summary}")
    return "\n\n".join(parts)

def _ai_anthropic(history: list, data_summary: str, memory_ctx: str = "") -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prefix = _context_prefix(data_summary, memory_ctx)
    msgs = []
    # Inject context as synthetic first exchange so system prompt stays static
    msgs.append({"role": "user",      "content": prefix})
    msgs.append({"role": "assistant", "content": "فهمت. سأتابع المحادثة بناءً على هذه البيانات."})

    for m in history[-16:]:
        role = "user" if m["role"] == "user" else "assistant"
        msgs.append({"role": role, "content": m["content"]})
    if not msgs or msgs[-1]["role"] == "assistant":
        msgs.append({"role": "user", "content": "(انتظر الرد)"})

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=SYSTEM_PROMPT,   # ثابت → يُكاش
        messages=msgs
    )
    text = resp.content[0].text.strip()
    return _parse_ai_response(text)

def _ai_deepseek(history: list, data_summary: str, memory_ctx: str = "") -> dict:
    import openai
    client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")

    prefix = _context_prefix(data_summary, memory_ctx)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]  # ثابت → يُكاش
    # Inject dynamic context as synthetic first exchange
    msgs.append({"role": "user",      "content": prefix})
    msgs.append({"role": "assistant", "content": "فهمت. سأتابع المحادثة بناءً على هذه البيانات."})

    for m in history[-16:]:
        msgs.append({"role": m["role"], "content": m["content"]})

    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=msgs,
        response_format={"type": "json_object"},
        max_tokens=400
    )
    text = resp.choices[0].message.content.strip()
    return _parse_ai_response(text)

def _parse_ai_response(text: str) -> dict:
    try:
        # Extract JSON if wrapped in markdown
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            return {
                "reply": data.get("reply", "أهلاً! كيف أستطيع مساعدتك؟"),
                "extracted": data.get("extracted", {}),
                "complete": bool(data.get("complete", False))
            }
    except Exception as e:
        print(f"[AI PARSE ERROR] {e}: {text[:200]}")
    return {"reply": text[:500] if text else "أهلاً!", "extracted": {}, "complete": False}

# ── Profile URL ───────────────────────────────────────────────────────────────
def get_profile_url(reg_id: int) -> str:
    return f"{BASE_URL}/p/{reg_id}"

# ── Fix 2: Auto-generate notes ────────────────────────────────────────────────
def _generate_and_save_notes(phone: str, reg_id: int):
    """After registration completes, generate a 2-sentence AI summary for the contact."""
    try:
        contact = get_contact(phone)
        regs    = get_contact_registrations(phone)
        prompt  = (
            f"اكتب ملاحظة مختصرة جملتان بالعربية عن العميل {contact.get('name','هذا العميل')}. "
            f"تصف: طبيعته كعميل، وأهم عقاراته أو طلباته. "
            f"البيانات: {json.dumps(regs, ensure_ascii=False, default=str)}. "
            "اكتب الملاحظة فقط دون مقدمة."
        )
        history    = [{"role": "user", "content": prompt}]
        ai_result  = ai_respond(history, {}, "")
        notes_text = ai_result.get("reply", "").strip()
        # Strip JSON wrapper if AI ignored the instruction
        try:
            parsed = json.loads(notes_text)
            if isinstance(parsed, dict):
                notes_text = parsed.get("reply", notes_text)
        except Exception:
            pass
        if notes_text:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sanad.masaed_contacts SET notes = %s WHERE phone = %s",
                    (notes_text, phone)
                )
                conn.commit()
            conn.close()
            print(f"[NOTES] Saved for {phone}: {notes_text[:80]}", flush=True)
    except Exception as e:
        print(f"[NOTES ERROR] {e}", flush=True)

# ── Main handler ──────────────────────────────────────────────────────────────
def handle_message(phone: str, text: str, media_url: str = None) -> str | None:
    """Process incoming WhatsApp message. Returns reply text."""
    with _phone_lock(phone):   # Fix: one message at a time per phone
        return _handle_message_inner(phone, text, media_url)

def _is_video_url(url: str) -> bool:
    return (any(url.lower().endswith(e) for e in ('.mp4', '.mov', '.avi', '.3gp'))
            or 'video' in url.lower())

def _handle_message_inner(phone: str, text: str, media_url: str = None) -> str | None:
    contact    = get_contact(phone)
    regs       = get_contact_registrations(phone)
    memory_ctx = build_memory_context(contact, regs)
    reg        = get_active_reg(phone)

    # ── PRE-REGISTRATION: type not yet confirmed ──────────────────────────────
    if reg is None:
        # Download media immediately before URL expires
        media_local_url = None
        if media_url:
            is_video = _is_video_url(media_url)
            label    = "فيديو" if is_video else "صورة"
            media_local_url = wa_get_file(media_url)
            if media_local_url:
                save_pending_media(phone, media_local_url, "video" if is_video else "image")
            save_pending_chat(phone, "user", f"[{label}] {media_local_url or media_url}")

            # Short-circuit: media with no caption
            if not text:
                return "تم الاستلام 📸 هل هناك المزيد؟"
            text = f"[أرسل المستخدم {label}]"

        if text:
            save_pending_chat(phone, "user", text)

        history = get_pending_history(phone)

        # Seed with known contact data so AI skips asking
        current_data = {}
        if contact.get("name"):
            current_data["name"] = contact["name"]
        if contact.get("city"):
            current_data["city"] = contact["city"]

        ai_result = ai_respond(history, current_data, memory_ctx)
        reply     = ai_result.get("reply", "")
        extracted = ai_result.get("extracted") or {}
        complete  = ai_result.get("complete", False)

        assistant_record = json.dumps(
            {"reply": reply, "extracted": extracted, "complete": complete},
            ensure_ascii=False
        )
        save_pending_chat(phone, "assistant", assistant_record)

        # Persist any name/city we just learned
        update_contact(phone, {
            "name": extracted.get("name"),
            "city": extracted.get("city"),
            "is_family": ((extracted.get("for_family") == "family")
                          if extracted.get("for_family") else None),
        })

        # Fix: user wants to add media to an existing registration
        update_target = _to_int(extracted.get("update_reg_id"))
        if update_target and media_local_url:
            save_media(update_target, media_local_url,
                       "video" if (media_url and _is_video_url(media_url)) else "image")
            # clear pending media so it's not double-attached later
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("UPDATE sanad.masaed_contacts SET profile = profile - 'pending_media' WHERE phone = %s", (phone,))
                conn.commit()
            conn.close()
            return reply

        # If type is now known — upgrade to a real registration
        reg_type = extracted.get("type")
        if reg_type and reg_type not in (None, "null"):
            merged = {**current_data}
            for k, v in extracted.items():
                if v is not None and v != [] and v != "null":
                    merged[k] = v

            reg_id = create_reg(phone, reg_type,
                                name=merged.get("name"),
                                city=merged.get("city"))
            flush_pending_chat(phone, reg_id)   # migrate all pending + media → real reg

            update_reg(reg_id, {
                "data_collected": merged,
                "type": reg_type,
                "name": merged.get("name"),
                "complete": complete,
            })

            if complete:
                sync_contact_after_reg(phone, reg_id, reg_type,
                                       merged.get("name"), merged.get("city"))
                _generate_and_save_notes(phone, reg_id)
                profile_url = get_profile_url(reg_id)
                reply += (f"\n\n🎉 تم التسجيل يا {merged.get('name','صديقي')}!"
                          f"\nصفحتك جاهزة:\n{profile_url}\n\nشاركها مع من تريد 🏠")

        return reply

    # ── ACTIVE REGISTRATION: type already confirmed ───────────────────────────
    reg_id       = reg["id"]
    current_data = reg["data"] or {}

    if not current_data.get("name") and contact.get("name"):
        current_data["name"] = contact["name"]
    if not current_data.get("city") and contact.get("city"):
        current_data["city"] = contact["city"]

    # Handle media
    media_local_url = None
    if media_url:
        is_video = _is_video_url(media_url)
        media_local_url = wa_get_file(media_url)
        if media_local_url:
            save_media(reg_id, media_local_url, "video" if is_video else "image")
        label = "فيديو" if is_video else "صورة"
        save_chat(phone, reg_id, "user", f"[{label}] {media_local_url or media_url}")

        # Short-circuit: media with no caption → no AI needed
        if not text:
            return "تم الاستلام 📸 هل هناك المزيد؟"

        text = f"[أرسل المستخدم {label} للعقار]"

    save_chat(phone, reg_id, "user", text or "")

    history   = get_chat_history(phone, reg_id)
    ai_result = ai_respond(history, current_data, memory_ctx)
    reply     = ai_result.get("reply", "")
    extracted = ai_result.get("extracted") or {}
    complete  = ai_result.get("complete", False)

    # Fix: user wants to add media to a different existing registration
    update_target = _to_int(extracted.get("update_reg_id"))
    if update_target and update_target != reg_id and media_local_url:
        save_media(update_target, media_local_url,
                   "video" if (media_url and _is_video_url(media_url)) else "image")
        save_chat(phone, reg_id, "assistant", json.dumps(
            {"reply": reply, "extracted": extracted, "complete": complete}, ensure_ascii=False))
        return reply

    merged = {**current_data}
    for k, v in extracted.items():
        if v is not None and v != [] and v != "null":
            merged[k] = v

    update_reg(reg_id, {
        "data_collected": merged,
        "type": merged.get("type"),
        "name": merged.get("name"),
        "complete": complete,
    })

    update_contact(phone, {
        "name": merged.get("name"),
        "city": merged.get("city"),
        "is_family": ((merged.get("for_family") == "family")
                      if merged.get("for_family") else None),
    })

    assistant_record = json.dumps(
        {"reply": reply, "extracted": extracted, "complete": complete},
        ensure_ascii=False
    )
    save_chat(phone, reg_id, "assistant", assistant_record)

    if complete:
        sync_contact_after_reg(phone, reg_id, merged.get("type", ""),
                               merged.get("name"), merged.get("city"))
        _generate_and_save_notes(phone, reg_id)
        profile_url = get_profile_url(reg_id)
        reply += (f"\n\n🎉 تم التسجيل يا {merged.get('name','صديقي')}!"
                  f"\nصفحتك جاهزة:\n{profile_url}\n\nشاركها مع من تريد 🏠")

    return reply

# ── Parse Green API webhook ───────────────────────────────────────────────────
def parse_webhook(data: dict) -> tuple[str, str, str | None]:
    """Returns (phone, text, media_url) from Green API webhook.
    Handles both direct format (real Green API) and body-wrapped format (legacy test).
    """
    try:
        # Real Green API sends typeWebhook at top level
        # Legacy test simulation wraps in {"body": {...}}
        body = data if data.get("typeWebhook") else data.get("body", {})
        msg_type = body.get("typeWebhook", "")

        print(f"[WEBHOOK] type={msg_type}", flush=True)

        if msg_type not in ("incomingMessageReceived",):
            return None, None, None

        sender = body.get("senderData", {}).get("sender", "")
        phone = sender.replace("@c.us", "").replace("+", "")

        msg = body.get("messageData", {})
        mtype = msg.get("typeMessage", "")
        print(f"[WEBHOOK] phone={phone} msgtype={mtype}", flush=True)

        if mtype == "textMessage":
            return phone, msg.get("textMessageData", {}).get("textMessage", ""), None

        if mtype in ("imageMessage", "videoMessage", "documentMessage"):
            media = msg.get("fileMessageData", {})
            url = media.get("downloadUrl") or media.get("jpegThumbnail")
            caption = media.get("caption", "")
            label = "[فيديو]" if mtype == "videoMessage" else "[صورة]"
            return phone, caption or label, url

        return phone, msg.get("extendedTextMessageData", {}).get("text", ""), None

    except Exception as e:
        print(f"[PARSE ERROR] {e}")
        return None, None, None
