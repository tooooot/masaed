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
# توفير التكلفة: DeepSeek فقط افتراضياً (Claude غالٍ). فعّل Claude بـ MASAED_USE_ANTHROPIC=true
USE_ANTHROPIC  = os.getenv("MASAED_USE_ANTHROPIC", "false").lower() == "true"

# ── Sandbox/Test Phone Whitelist ───────────────────────────────────────────────
# CRITICAL: Only these phone numbers can receive test messages.
# Real production phones MUST NOT be in this list.
# Format: 966XXXXXXXXX (Saudi numbers) or test numbers starting with 0050
SANDBOX_PHONES = set(os.getenv("MASAED_SANDBOX_PHONES", "966500000000,966500000001,0500000000,0500000001").split(","))
print(f"[INIT] Sandbox phones for testing: {SANDBOX_PHONES}", flush=True)

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
    # تحقق من توفيقات معلّقة تنتظر تسجيل هذا الرقم
    _check_pending_matches_after_reg(phone)


def _check_pending_matches_after_reg(phone: str):
    """بعد التسجيل: إذا يوجد توفيق awaiting_registration يضم هذا الرقم وكلاهما مسجّل الآن → أبلغ الإدارة."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, req_phone, lst_phone
                FROM sanad.masaed_matches
                WHERE status = 'awaiting_registration'
                  AND (req_phone = %s OR lst_phone = %s)
            """, (phone, phone))
            pending = cur.fetchall()

        for match_id, req_phone, lst_phone in pending:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(DISTINCT phone)
                    FROM sanad.masaed_registrations
                    WHERE phone IN (%s, %s) AND status != 'abandoned'
                """, (req_phone, lst_phone))
                count = cur.fetchone()[0]

            if count >= 2:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE sanad.masaed_matches
                        SET status = 'pending', updated_at = NOW()
                        WHERE id = %s
                    """, (match_id,))
                conn.commit()

                admin = os.getenv("MASAED_WA_PHONE", "")
                base  = os.getenv("MASAED_BASE_URL", "https://masaed.wardyat.net")
                if admin:
                    wa_send(admin,
                        f"✅ مساعد — توفيق #{match_id} جاهز\n"
                        f"كلا الطرفين اكتمل تسجيلهم\n"
                        f"👉 {base} ← تبويب التوفيقات"
                    )
                print(f"[REG] match #{match_id} → pending (both registered)", flush=True)

        conn.close()
    except Exception as e:
        print(f"[REG] _check_pending_matches: {e}", flush=True)

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
    """
    سياق الحافظ — يُمرَّر للـLLM في كل رسالة.
    يعمل دائماً، حتى لو لم يُعرف الاسم بعد.
    """
    lines = ["📱 ذاكرة العميل:"]
    has_data = False

    if contact.get("name"):
        lines.append(f"• الاسم: {contact['name']}")
        has_data = True
    else:
        lines.append("• الاسم: غير معروف بعد — اسأل عنه")

    if contact.get("city"):
        lines.append(f"• المدينة: {contact['city']}")
        has_data = True

    if contact.get("is_family") is not None:
        lines.append(f"• النوع: {'عائلي' if contact['is_family'] else 'عزاب'}")
        has_data = True

    if contact.get("notes"):
        lines.append(f"• ملاحظات: {contact['notes']}")
        has_data = True

    if regs:
        lines.append(f"\n📂 تسجيلاته السابقة ({len(regs)}):")
        for r in regs:
            reg_type = "عرض عقار" if r["type"] == "listing" else "طلب إيجار"
            status   = "✅ مكتمل" if r["status"] == "complete" else "🔄 جارٍ"
            desc_parts = []
            if r.get("property_type"): desc_parts.append(r["property_type"])
            if r.get("rooms"):         desc_parts.append(f"{r['rooms']} غرف")
            if r.get("district"):      desc_parts.append(r["district"])
            if r.get("city"):          desc_parts.append(r["city"])
            if r.get("price_annual"):  desc_parts.append(f"{r['price_annual']:,} ريال/سنة")
            desc = " | ".join(desc_parts) if desc_parts else "بدون تفاصيل"
            lines.append(f"  — #{r['id']} {reg_type} {status}: {desc}")
        has_data = True

    if not has_data:
        # أول تواصل — لا تاريخ
        lines.append("• أول تواصل — لا تاريخ سابق")
        lines.append("⚠️ ابدأ بسؤاله عن اسمه الكريم قبل أي شيء آخر.")
        return "\n".join(lines)

    if contact.get("name"):
        lines.append(f"\nرحّب بـ{contact['name']} بحرارة واذكر آخر تعامل.")
    return "\n".join(lines)


def build_party_profile(phone: str, conn=None) -> str:
    """
    🧠 جدول الحافظ الذكي — ملف مضغوط للعميل (حقائق فقط، بلا استدعاء LLM).
    يُحقن بدل السياق العام ليعرف البوت الشخص كاملاً ويوفّر الرصيد.
    يجمع: الاسم/الدور/الصفقات/التفضيلات/الحالة/الملاحظات من قاعدة البيانات.
    """
    own = conn is None
    if own:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            # الاسم + الملاحظات اليدوية (الحافظ)
            cur.execute("SELECT name, notes FROM sanad.masaed_contacts WHERE phone=%s", (phone,))
            c = cur.fetchone() or (None, None)
            name, notes = c[0], c[1]
            # ملخّص التسجيلات: الأدوار + أحدث تفضيلات
            cur.execute("""
                SELECT type, city, rooms, COALESCE(price_annual, budget_annual), for_family, created_at
                FROM sanad.masaed_registrations
                WHERE phone=%s AND type IS NOT NULL AND status<>'abandoned'
                ORDER BY created_at DESC
            """, (phone,))
            regs = cur.fetchall()
            # عدّاد التفاوضات + الناجحة
            cur.execute("""
                SELECT count(*), count(*) FILTER (WHERE status='agreed')
                FROM sanad.masaed_negotiations
                WHERE lead_phone=%s OR listing_phone=%s
            """, (phone, phone))
            neg = cur.fetchone() or (0, 0)
    finally:
        if own:
            conn.close()

    roles = set()
    for r in regs:
        roles.add("مالك" if r[0] == "listing" else "باحث")
    role_str = " + ".join(roles) if roles else "غير محدّد"

    lines = [f"🧠 ملف العميل{(' — ' + name) if name else ''}:"]
    lines.append(f"• الدور: {role_str} | تسجيلاته: {len(regs)} | تفاوضاته: {neg[0]} (ناجحة: {neg[1]})")
    if regs:
        r = regs[0]  # أحدث تفضيلات
        pref = []
        if r[1]: pref.append(r[1])
        if r[2]: pref.append(f"{r[2]} غرف")
        if r[3]: pref.append(f"~{r[3]:,} ر/سنة")
        if r[4]: pref.append("عائلي" if r[4] == "family" else "عزّاب")
        if pref:
            lines.append(f"• تفضيلاته الأحدث: {' · '.join(pref)}")
    if notes:
        lines.append(f"• ملاحظات: {notes}")
    if len(regs) == 0 and neg[0] == 0:
        lines.append("• عميل جديد — لا تاريخ سابق")
    return "\n".join(lines)


def get_active_reg(phone: str) -> dict | None:
    """Return active registration. Prefers most complete one if multiple exist."""
    conn = get_conn()
    with conn.cursor() as cur:
        # أخذ الأكثر بيانات (data_collected لها أكبر عدد مفاتيح)
        cur.execute("""
            SELECT id, type, status, data_collected, name
            FROM sanad.masaed_registrations
            WHERE phone = %s AND status IN ('collecting') AND type IS NOT NULL
            ORDER BY jsonb_array_length(COALESCE(
                (SELECT jsonb_agg(k) FROM jsonb_object_keys(data_collected) k), '[]'::jsonb
            )) DESC, created_at DESC
            LIMIT 1
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
    """Get pending messages before registration is created. Capped at 12."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT profile->'pending_chat' FROM sanad.masaed_contacts WHERE phone = %s", (phone,))
        row = cur.fetchone()
    conn.close()
    pending = row[0] if row and row[0] else []
    return list(pending)[-12:]

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

def _to_bool(val):
    """Convert furnished value to bool — handles Arabic strings and booleans."""
    if val is None: return None
    if isinstance(val, bool): return val
    s = str(val).strip().lower()
    if s in ('true', 'yes', 'نعم', 'مفروش', 'مفروشة', '1'):  return True
    if s in ('false', 'no', 'لا', 'فارغ', 'غير مفروش', '0'): return False
    return None


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

def _coalesce_or_new(new_val, col_name: str) -> str:
    """
    إذا القيمة الجديدة موجودة → استخدمها (تُلغي القديمة عند التصحيح).
    إذا null → احتفظ بالقديمة (لا تمسح بيانات صحيحة).
    """
    return f"CASE WHEN %s IS NOT NULL THEN %s ELSE {col_name} END"


def update_reg(reg_id: int, data: dict):
    """
    Update registration.
    القيم الجديدة غير الـnull تُكتب فوق القديمة (للسماح بالتصحيح).
    القيم الـnull تُبقي القديمة.
    """
    d = data.get("data_collected", {})

    name     = d.get("name")     or data.get("name", "") or ""
    reg_type = d.get("type")     or data.get("type", "") or ""
    city     = d.get("city", "") or ""
    slug_base = f"{name}-{reg_type}-{city}".lower()
    slug_base = re.sub(r'[^a-z0-9؀-ۿ]', '-', slug_base)
    slug_base = re.sub(r'-+', '-', slug_base).strip('-')
    slug = slug_base[:40] + "-" + str(reg_id)

    # helper: إذا قيمة جديدة → اكتب، وإلا احتفظ بالقديمة
    def ow(new):
        return new if new is not None else None

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_registrations SET
                name          = CASE WHEN %s IS NOT NULL THEN %s ELSE name          END,
                type          = CASE WHEN %s IS NOT NULL THEN %s ELSE type          END,
                slug          = %s,
                city          = CASE WHEN %s IS NOT NULL THEN %s ELSE city          END,
                district      = CASE WHEN %s IS NOT NULL THEN %s ELSE district      END,
                property_type = CASE WHEN %s IS NOT NULL THEN %s ELSE property_type END,
                rooms         = CASE WHEN %s IS NOT NULL THEN %s ELSE rooms         END,
                bathrooms     = CASE WHEN %s IS NOT NULL THEN %s ELSE bathrooms     END,
                floor_num     = CASE WHEN %s IS NOT NULL THEN %s ELSE floor_num     END,
                furnished     = CASE WHEN %s IS NOT NULL THEN %s ELSE furnished     END,
                price_annual  = CASE WHEN %s IS NOT NULL THEN %s ELSE price_annual  END,
                price_monthly = CASE WHEN %s IS NOT NULL THEN %s ELSE price_monthly END,
                for_family    = CASE WHEN %s IS NOT NULL THEN %s ELSE for_family    END,
                location_desc = CASE WHEN %s IS NOT NULL THEN %s ELSE location_desc END,
                features      = CASE WHEN %s::jsonb IS NOT NULL THEN %s::jsonb ELSE features END,
                budget_annual = CASE WHEN %s IS NOT NULL THEN %s ELSE budget_annual END,
                preferred_districts = CASE WHEN %s::jsonb IS NOT NULL THEN %s::jsonb ELSE preferred_districts END,
                move_date     = CASE WHEN %s IS NOT NULL THEN %s ELSE move_date     END,
                special_notes = CASE WHEN %s IS NOT NULL THEN %s ELSE special_notes END,
                status        = COALESCE(%s, status),
                data_collected = %s::jsonb,
                updated_at    = NOW()
            WHERE id = %s
        """, (
            # كل حقل يحتاج القيمة مرتين: مرة للـCASE WHEN ومرة للـTHEN
            *(v := name or None,          v),
            *(v := d.get("type") or data.get("type") or None, v),
            slug,
            *(v := d.get("city") or None,          v),
            *(v := d.get("district") or None,       v),
            *(v := d.get("property_type") or None,  v),
            *(v := _to_int(d.get("rooms")),         v),
            *(v := _to_int(d.get("bathrooms")),     v),
            *(v := _to_int(d.get("floor") or d.get("floor_num")), v),
            *(v := _to_bool(d.get("furnished")),    v),
            *(v := _to_int(d.get("price_annual")),  v),
            *(v := _to_int(d.get("price_monthly")), v),
            *(v := d.get("for_family") or None,     v),
            *(v := d.get("location_desc") or None,  v),
            *(v := json.dumps(d["features"]) if d.get("features") else None, v),
            *(v := _to_int(d.get("budget_annual")), v),
            *(v := json.dumps(d["preferred_districts"]) if d.get("preferred_districts") else None, v),
            *(v := str(d["move_date"]) if d.get("move_date") else None, v),
            *(v := d.get("special_notes") or None,  v),
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
    """
    Send WhatsApp message.

    CRITICAL SAFEGUARD:
    - Sandbox mode (via /bot/test): only logs, never sends
    - Real mode: only sends to SANDBOX_PHONES or if MASAED_ALLOW_REAL_SEND=true
    - If real send is blocked, logs error and halts
    """
    # Normalize phone number
    phone_clean = str(phone).replace("+", "").replace(" ", "")

    # Dry-run mode: capture message without sending
    if getattr(_wa_test_local, 'active', False):
        if not hasattr(_wa_test_local, 'log'):
            _wa_test_local.log = []
        _wa_test_local.log.append({"to": phone_clean, "text": text})
        print(f"[WA DRY-RUN] → {phone_clean}: {text[:80]}", flush=True)
        return

    if not GREEN_INSTANCE or not GREEN_TOKEN:
        print(f"[WA MOCK] → {phone_clean}: {text[:80]}")
        return

    # ── SAFEGUARD: Check if phone is in sandbox list ───────────────────────────
    allow_real = os.getenv("MASAED_ALLOW_REAL_SEND", "false").lower() == "true"
    is_sandbox = phone_clean in SANDBOX_PHONES

    if not is_sandbox and not allow_real:
        print(f"[WA BLOCKED] ❌ CRITICAL: Attempted to send to {phone_clean} (not in sandbox)", flush=True)
        print(f"[WA BLOCKED] Set MASAED_ALLOW_REAL_SEND=true to send to real phones", flush=True)
        raise ValueError(f"Real send to {phone_clean} blocked by safety check. Use sandbox phones only.")

    if not is_sandbox:
        print(f"[WA REAL] ⚠️ Sending to real phone {phone_clean}", flush=True)

    url = f"https://api.green-api.com/waInstance{GREEN_INSTANCE}/sendMessage/{GREEN_TOKEN}"
    try:
        resp = requests.post(url, json={"chatId": f"{phone_clean}@c.us", "message": text}, timeout=10)
        print(f"[WA SENT] → {phone_clean}: OK", flush=True)
    except Exception as e:
        print(f"[WA ERROR] {phone_clean}: {e}")


def wa_send_media(phone: str, file_url: str, caption: str = "") -> bool:
    """إرسال ملف/صورة عبر Green API (sendFileByUrl) — بنفس حارس الأمان."""
    phone_clean = str(phone).replace("+", "").replace(" ", "")
    # Dry-run: التقاط بدل الإرسال
    if getattr(_wa_test_local, 'active', False):
        if not hasattr(_wa_test_local, 'log'):
            _wa_test_local.log = []
        _wa_test_local.log.append({"to": phone_clean, "media": file_url, "caption": caption})
        print(f"[WA DRY-RUN MEDIA] → {phone_clean}: {file_url}", flush=True)
        return True
    if not GREEN_INSTANCE or not GREEN_TOKEN:
        print(f"[WA MOCK MEDIA] → {phone_clean}: {file_url}")
        return False
    # حارس: فقط أرقام sandbox أو إذا فُعِّل الإرسال الحقيقي
    allow_real = os.getenv("MASAED_ALLOW_REAL_SEND", "false").lower() == "true"
    if phone_clean not in SANDBOX_PHONES and not allow_real:
        print(f"[WA BLOCKED MEDIA] ❌ {phone_clean} ليس في sandbox", flush=True)
        return False
    fname = file_url.rstrip("/").split("/")[-1] or "file"
    api = f"https://api.green-api.com/waInstance{GREEN_INSTANCE}/sendFileByUrl/{GREEN_TOKEN}"
    try:
        requests.post(api, json={"chatId": f"{phone_clean}@c.us", "urlFile": file_url,
                                 "fileName": fname, "caption": caption}, timeout=20)
        print(f"[WA SENT MEDIA] → {phone_clean}: {fname}", flush=True)
        return True
    except Exception as e:
        print(f"[WA ERROR MEDIA] {phone_clean}: {e}")
        return False


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
إذا وردت "معلومات العميل المحفوظة":
- رحّب بالعميل باسمه فوراً مع ترحيب دافئ
- لا تسأل عن معلومات موجودة مسبقاً
- اسأل: هل لديه عقار جديد أم طلب جديد؟ مع الإشارة لآخر تعامل

إذا لم توجد معلومات محفوظة (عميل جديد):
"مرحباً! 🏠 أنا مساعد العقاري. هل أنت:
1️⃣ صاحب عقار تريد إدراجه للإيجار
2️⃣ تبحث عن عقار للإيجار"

━━━━━━━━━━━━━━━━━━━━
أنواع العقارات السكنية: شقة، فيلا، غرفة، استوديو، دور، شاليه
أنواع العقارات غير السكنية: أرض، مستودع، محل، مكتب، معرض، مزرعة، عمارة، مصنع، مبنى

━━━━━━━━━━━━━━━━━━━━
مبدأ الاستخراج أولاً (مهم جداً):
عندما يرسل المستخدم رسالته — سواء كانت جملة واحدة أو إعلاناً كاملاً من حراج أو أي منصة —
استخرج منها كل المعلومات المتاحة فوراً وضعها في extracted.
ثم اسأل فقط عن الحقول الإلزامية المفقودة.
لا تسأل عن معلومة موجودة في نص الرسالة ولو ذُكرت عرضاً.

مثال:
المستخدم: "شقة 3 غرف مفروشة حي الروضة جدة دور ثاني 28000 للعائلات"
الاستخراج الصحيح: {property_type:شقة, rooms:3, furnished:true, district:الروضة, city:جدة, floor:2, price_annual:28000, for_family:family}
الحقول الإلزامية الناقصة: name, location_desc
السؤال الصحيح: "ممتاز 👍 استلمت التفاصيل. ما اسمك الكريم؟"

━━━━━━━━━━━━━━━━━━━━
حقول عرض العقار (listing):
إلزامية: الاسم، المدينة، نوع العقار، السعر السنوي، الموقع
اختيارية: الحي، الغرف، الحمامات، الطابق، مفروش، عوائل/عزاب، مميزات، صور

للعقار غير السكني (أرض/محل/مستودع): تخطّ الغرف والطابق والمفروش وعوائل/عزاب،
واسأل بدلاً منها: الغرض المسموح (تجاري/صناعي/زراعي/حر).

━━━━━━━━━━━━━━━━━━━━
حقول طلب الإيجار (wanted):
إلزامية: الاسم، المدينة، الميزانية السنوية
اختيارية: الأحياء المفضلة، الغرف، مفروش، عوائل/عزاب، موعد الانتقال، ملاحظات

━━━━━━━━━━━━━━━━━━━━
القواعد الذهبية:
- سؤال واحد فقط في كل رسالة
- أكّد فهمك بكلمة لطيفة قبل الانتقال
- لا تسأل عن معلومة ذُكرت سابقاً ولم يُصحَّح عليها
- إذا أرسل صورة بدون نص: قل "تم الاستلام، هل هناك المزيد؟"
- الاسم إلزامي — لا تضع complete:true إذا لم يُذكر الاسم

━━━━━━━━━━━━━━━━━━━━
التصحيح والمراجعة (مهم جداً):
- إذا قال المستخدم "هذا خطأ" أو "غيّر" أو "لا يوجد X" أو "اعتذر":
  → افهم أي معلومة خاطئة
  → ضع القيمة الصحيحة الجديدة في extracted (لا تضع null)
  → لا تواصل على المعلومة الخاطئة أبداً
- مثال: قال "شقة" ثم قال "لا يوجد لدي شقة، لدي أرض"
  → extracted: {"property_type": "أرض"} ← تُلغي "شقة" السابقة
  → لا تقل "لنركز على الشقة أولاً"
- البيانات المجموعة حتى الآن قابلة للتغيير دائماً

━━━━━━━━━━━━━━━━━━━━
قواعد ذكية لتحديد النوع:
- صورة/فيديو كأول رسالة → listing مباشرة
- "عندي شقة/عقار/أرض" → listing
- "أبحث/أريد/محتاج إيجار" → wanted
- لا تسأل "صاحب أم باحث؟" إلا إذا لم يكن هناك أي مؤشر

━━━━━━━━━━━━━━━━━━━━
تحديث عقار قائم:
إذا طلب إضافة صور لعقار سابق أو ذكر "إعلاني القديم":
- اشكره وأخبره أن الوسائط ستُضاف
- ضع رقم التسجيل في update_reg_id

━━━━━━━━━━━━━━━━━━━━
أعد ردك دائماً بهذا الـJSON:
{
  "reply": "الرسالة للمستخدم",
  "extracted": {
    "name": null,
    "type": "listing|wanted|null",
    "city": null,
    "district": null,
    "property_type": null,
    "rooms": null,
    "bathrooms": null,
    "floor": null,
    "furnished": null,
    "price_annual": null,
    "price_monthly": null,
    "for_family": "family|bachelor|both|commercial|industrial|agricultural|null",
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

complete:true فقط إذا: الاسم موجود + المدينة موجودة + نوع العقار موجود + السعر (listing) أو الميزانية (wanted) موجودة + الموقع موجود.
"""

# أنواع العقارات غير السكنية — لا يُسأل عنها عوائل/عزاب
_NON_RESIDENTIAL = {
    'أرض', 'مستودع', 'محل', 'مكتب', 'معرض', 'مزرعة',
    'عمارة', 'مصنع', 'مبنى', 'land', 'commercial',
}

# الحقول الإلزامية لكل نوع
_REQUIRED_LISTING = {'name', 'city', 'property_type', 'price_annual'}
_REQUIRED_WANTED  = {'name', 'city', 'budget_annual'}


def _validate_complete(reg_type: str, data: dict) -> tuple:
    """Python checklist — LLM complete is only a suggestion."""
    required = _REQUIRED_LISTING if reg_type == 'listing' else _REQUIRED_WANTED
    missing  = [f for f in required if not data.get(f)]
    return len(missing) == 0, missing

def ai_respond(history: list, current_data: dict, memory_ctx: str = "") -> dict:
    """Call AI and get reply + extracted data."""
    data_summary = json.dumps(current_data, ensure_ascii=False)

    # DeepSeek أولاً (توفير التكلفة)؛ Claude فقط إن فُعِّل صراحةً
    if ANTHROPIC_KEY and USE_ANTHROPIC:
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
def _format_collected_data(reg_type: str, data: dict) -> str:
    """اعرض البيانات المجمعة بصيغة مقروءة للتأكيد."""
    lines = []

    if reg_type == "listing":
        lines.append("📋 **عرض العقار**")
        if data.get("name"):
            lines.append(f"👤 الاسم: {data['name']}")
        if data.get("property_type"):
            lines.append(f"🏠 نوع العقار: {data['property_type']}")
        if data.get("city"):
            lines.append(f"📍 المدينة: {data['city']}")
        if data.get("district"):
            lines.append(f"🏘️ الحي: {data['district']}")
        if data.get("rooms"):
            lines.append(f"🛏️ الغرف: {data['rooms']}")
        if data.get("bathrooms"):
            lines.append(f"🚿 الحمامات: {data['bathrooms']}")
        if data.get("floor"):
            lines.append(f"⬆️ الطابق: {data['floor']}")
        if data.get("furnished") is not None:
            furnished_text = "مفروش ✨" if data["furnished"] else "غير مفروش"
            lines.append(f"🛋️ الحالة: {furnished_text}")
        if data.get("price_annual"):
            lines.append(f"💰 السعر السنوي: {data['price_annual']:,} ريال")
        if data.get("price_monthly"):
            lines.append(f"📅 السعر الشهري: {data['price_monthly']:,} ريال")
        if data.get("for_family") is not None:
            family_text = "للعوائل 👨‍👩‍👧‍👦" if data["for_family"] else "للعزاب 👨"
            lines.append(f"👥 الفئة: {family_text}")
        if data.get("location_desc"):
            lines.append(f"📝 الموقع/التفاصيل: {data['location_desc'][:100]}")
        if data.get("features"):
            lines.append(f"⭐ المميزات: {data['features'][:100]}")

    elif reg_type == "wanted":
        lines.append("🔍 **طلب الإيجار**")
        if data.get("name"):
            lines.append(f"👤 الاسم: {data['name']}")
        if data.get("property_type"):
            lines.append(f"🏠 نوع العقار: {data['property_type']}")
        if data.get("city"):
            lines.append(f"📍 المدينة: {data['city']}")
        if data.get("preferred_districts"):
            lines.append(f"🏘️ الأحياء المفضلة: {data['preferred_districts']}")
        if data.get("rooms"):
            lines.append(f"🛏️ عدد الغرف المطلوبة: {data['rooms']}")
        if data.get("budget_annual"):
            lines.append(f"💰 الميزانية السنوية: {data['budget_annual']:,} ريال")
        if data.get("for_family") is not None:
            family_text = "للعوائل 👨‍👩‍👧‍👦" if data["for_family"] else "للعزاب 👨"
            lines.append(f"👥 الفئة: {family_text}")
        if data.get("furnished") is not None:
            furnished_text = "مفروش ✨" if data["furnished"] else "غير مفروش"
            lines.append(f"🛋️ المطلوب: {furnished_text}")
        if data.get("move_date"):
            lines.append(f"📅 موعد الانتقال: {data['move_date']}")
        if data.get("special_notes"):
            lines.append(f"📝 ملاحظات: {data['special_notes'][:100]}")

    return "\n".join(lines)


def get_profile_url(reg_id: int) -> str:
    return f"{BASE_URL}/p/{reg_id}"

# ── Fix 2: Auto-generate notes ────────────────────────────────────────────────
def _generate_and_save_notes(phone: str, reg_id: int):
    """Launch notes generation in background — non-blocking."""
    import threading
    threading.Thread(target=_do_generate_notes, args=(phone, reg_id), daemon=True).start()


def _do_generate_notes(phone: str, reg_id: int):
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

        # نرمّل النوع: "request" → "wanted"
        reg_type = extracted.get("type")
        if reg_type == "request":
            reg_type = "wanted"
            extracted["type"] = "wanted"

        if reg_type and reg_type not in (None, "null"):
            merged = {**current_data}
            for k, v in extracted.items():
                if v is not None and v != [] and v != "null":
                    merged[k] = v

            reg_id = create_reg(phone, reg_type,
                                name=merged.get("name"),
                                city=merged.get("city"))
            flush_pending_chat(phone, reg_id)

            # Python validation بدل الاعتماد الكلي على LLM
            py_complete, missing = _validate_complete(reg_type, merged)
            if complete and not py_complete:
                print(f"[REG] LLM said complete but missing {missing} — overriding", flush=True)
                complete = False

            update_reg(reg_id, {
                "data_collected": merged,
                "type": reg_type,
                "name": merged.get("name"),
                "complete": complete,
            })

            # ── عرض البيانات المجمعة للتأكيد ──────────────────────────────────
            if merged.get("name"):
                data_summary = _format_collected_data(reg_type, merged)
                reply += f"\n\n{data_summary}"
                reply += "\n\n✅ هل هذه البيانات صحيحة؟ (رد: نعم/لا)"

                # إذا كانت البيانات كاملة، عرّض الخيار مباشرة
                if complete:
                    reply = reply.replace("هل هذه البيانات صحيحة؟", "تم جمع البيانات الكاملة! تأكيد؟")

            # إذا كانت البيانات كاملة جداً، اكمل التسجيل
            if complete and merged.get("name"):
                sync_contact_after_reg(phone, reg_id, reg_type,
                                       merged.get("name"), merged.get("city"))
                _generate_and_save_notes(phone, reg_id)
                profile_url = get_profile_url(reg_id)
                name_str = merged.get("name") or ""
                reply = (f"🎉 تم التسجيل{' يا ' + name_str if name_str else ''}!\n"
                         f"صفحتك جاهزة:\n{profile_url}\n\nشاركها مع من تريد 🏠")

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

    # ── تأكيد المستخدم للبيانات ────────────────────────────────────────────
    if text and text.lower() in ("نعم", "ايوه", "اي", "موافق", "تمام", "صح"):
        # المستخدم وافق على البيانات
        print(f"[REG #{reg_id}] User confirmed data", flush=True)

        # وضع complete = true وتسجيل
        update_reg(reg_id, {
            "complete": True,
        })

        sync_contact_after_reg(phone, reg_id, reg["type"],
                               current_data.get("name"), current_data.get("city"))
        _generate_and_save_notes(phone, reg_id)
        profile_url = get_profile_url(reg_id)
        name_str = current_data.get("name") or ""

        save_chat(phone, reg_id, "assistant", json.dumps({
            "reply": f"✅ تم التسجيل بنجاح!",
            "complete": True
        }, ensure_ascii=False))

        return (f"🎉 تم التسجيل{' يا ' + name_str if name_str else ''}!\n"
                f"صفحتك جاهزة:\n{profile_url}\n\nشاركها مع من تريد 🏠")

    # ── رفض المستخدم أو تصحيح البيانات ──────────────────────────────────────
    if text and text.lower() in ("لا", "لا يا", "خطأ", "اعدل"):
        save_chat(phone, reg_id, "assistant", json.dumps({
            "reply": "حسناً، ما الذي تريد تعديله؟",
            "complete": False
        }, ensure_ascii=False))
        return "حسناً، ما الذي تريد تعديله؟"

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

    # نرمّل النوع
    if merged.get("type") == "request":
        merged["type"] = "wanted"

    # Python validation
    final_type = merged.get("type") or reg["type"]
    py_complete, missing = _validate_complete(final_type, merged)
    if complete and not py_complete:
        print(f"[REG #{reg_id}] LLM complete but missing {missing} — overriding", flush=True)
        complete = False

    update_reg(reg_id, {
        "data_collected": merged,
        "type": final_type,
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

    # ── عرض البيانات المجمعة للتأكيد ──────────────────────────────────────
    if merged.get("name") and not complete:
        data_summary = _format_collected_data(final_type, merged)
        reply += f"\n\n{data_summary}"
        reply += "\n\n✅ هل هذه البيانات صحيحة؟ (رد: نعم/لا)"

    if complete:
        sync_contact_after_reg(phone, reg_id, final_type,
                               merged.get("name"), merged.get("city"))
        _generate_and_save_notes(phone, reg_id)
        profile_url = get_profile_url(reg_id)
        name_str = merged.get("name") or ""
        reply = (f"🎉 تم التسجيل{' يا ' + name_str if name_str else ''}!\n"
                 f"صفحتك جاهزة:\n{profile_url}\n\nشاركها مع من تريد 🏠")

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
