#!/usr/bin/env python3
"""
موجّه الأهداف (Session-Goal Router)
يعلو فوق آلة حالات التفاوض: يحدّد «مهمة» مساعد مع كل شخص قبل أي رد.

المبدأ الحاكم: مساعد يتبع صاحب المال (المستأجر/المشتري) ويسعى لتلبية طلبه —
بالبحث في قاعدتنا، ثم النت (حراج)، ثم مبادرة الملاك المطابقين.

الهدف = دالّة على حالة الشخص المخزّنة (لا تخمين):
  negotiate              تفاوض نشط جارٍ
  complete_registration  تسجيل بدأ ولم يكتمل
  cold_reply             ردّ على مبادرة باردة منّا (معلِن حراج غير مسجّل)
  returning              عميل مسجّل عائد
  new_inbound            جديد كلياً
"""

from bot import get_conn


def _scalar(cur, sql, args):
    cur.execute(sql, args)
    return cur.fetchone()


def session_goal(phone: str, conn=None) -> str:
    """اشتقّ مهمة الجلسة الحالية من حالة الشخص (أول شرط يتحقق)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 1) تفاوض نشط
            if _scalar(cur, """
                SELECT 1 FROM sanad.masaed_negotiations
                WHERE (lead_phone=%s OR listing_phone=%s) AND status='active'
                  AND (expires_at IS NULL OR expires_at > NOW()) LIMIT 1
            """, (phone, phone)):
                return "negotiate"

            # 2) تسجيل جارٍ (collecting)
            if _scalar(cur, """
                SELECT 1 FROM sanad.masaed_registrations
                WHERE phone=%s AND status='collecting' AND type IS NOT NULL LIMIT 1
            """, (phone,)):
                return "complete_registration"

            registered = _scalar(cur, """
                SELECT 1 FROM sanad.masaed_registrations
                WHERE phone=%s AND status <> 'abandoned' AND type IS NOT NULL LIMIT 1
            """, (phone,)) is not None

            # 3) ردّ على مبادرة باردة: عرض في حراج راسلناه (contacted) وغير مسجّل
            if not registered and _scalar(cur, """
                SELECT 1 FROM sanad.masaed_listings
                WHERE phone=%s AND status='contacted' LIMIT 1
            """, (phone,)):
                return "cold_reply"

            # 4) عميل مسجّل عائد
            if registered:
                return "returning"

            # 5) جديد
            return "new_inbound"
    finally:
        if own:
            conn.close()


def build_cold_outbound_intro(listing: dict, seeker_hint: str = "") -> str:
    """
    رسالة المبادرة الباردة لمالك معلِن في حراج (غير مسجّل).
    تُقدّم مبرّر الاتصال (إعلانه) + تعرّف بمساعد + تطرح القيمة + سؤال موافقة.
    listing: {title, city, price, url}
    """
    title = (listing.get("title") or "عقارك المعروض").strip()
    city  = listing.get("city")
    price = listing.get("price")
    loc   = f" في {city}" if city else ""
    pr    = f" بسعر {int(price):,} ريال" if price else ""
    hint  = f" ({seeker_hint})" if seeker_hint else ""
    return (
        "السلام عليكم ورحمة الله 👋\n"
        f"شفت إعلانك في حراج عن «{title}»{loc}{pr}، وأتواصل معك بخصوصه.\n\n"
        "أنا «مساعد» — وكيل عقاري يعمل بالذكاء الاصطناعي. مهمتي أجيب لك "
        f"مستأجرين جادّين وأتولّى التنسيق والتفاوض نيابةً عنك بدون عناء.\n"
        f"وعندي حالياً باحث جاد يطابق مواصفات عقارك{hint}.\n\n"
        "تحب أعرض عليك التفاصيل ونبدأ؟"
    )


# ══════════════════════════════════════════════════════════════════════════════
# المحرّك الصادر المدفوع بالطلب (Outbound Engine)
# طلب باحث → مطابقة عروض القاعدة → سحب حراج عند الحاجة → مبادرة الملاك المطابقين
# ══════════════════════════════════════════════════════════════════════════════

MATCH_MIN   = 50    # أدنى درجة مطابقة للمبادرة
MIN_RESULTS = 2     # إن قلّت المطابقات الجيدة عن هذا → اسحب حراج


def _seeker_to_lead(s: dict) -> dict:
    """حوّل طلب باحث مسجّل إلى dict يفهمه score_match (مدينة + title/body)."""
    rooms = s.get("rooms")
    budget = s.get("budget_annual") or s.get("price_annual")
    ptype = s.get("property_type") or "سكن"
    fam = "عائلي" if s.get("for_family") == "family" else ""
    body = " ".join(str(x) for x in [
        s.get("city"), s.get("district"), f"{rooms} غرف" if rooms else "",
        ptype, f"ميزانية {budget}" if budget else "", fam] if x)
    return {"city": s.get("city"), "title": f"باحث عن {ptype}", "body": body}


def _seeker_hint(s: dict) -> str:
    parts = []
    if s.get("for_family") == "family": parts.append("عائلة")
    if s.get("rooms"): parts.append(f"{s['rooms']} غرف")
    b = s.get("budget_annual") or s.get("price_annual")
    if b: parts.append(f"ميزانية {int(b):,}")
    return "، ".join(parts)


def _is_registered(phone: str, cur) -> bool:
    cur.execute("""SELECT 1 FROM sanad.masaed_registrations
                   WHERE phone=%s AND status<>'abandoned' AND type IS NOT NULL LIMIT 1""", (phone,))
    return cur.fetchone() is not None


def _match_listings(lead: dict, conn) -> list:
    """طابق العروض النشطة ضد الطلب وأرجعها مرتّبة بالدرجة."""
    from scraper_api import score_match     # lazy: تجنّب الاستيراد الدائري
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, body, city, property_type, rooms, price, phone, status, url
            FROM sanad.masaed_listings
            WHERE status='active' AND phone IS NOT NULL
            ORDER BY CASE WHEN city=%s THEN 0 ELSE 1 END, scraped_at DESC LIMIT 200
        """, (lead.get("city") or "",))
        cols = [d[0] for d in cur.description]
        listings = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = []
    for lst in listings:
        sc, reason, _ = score_match(lead, lst)
        out.append({**lst, "score": sc, "reason": reason})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def run_outbound_for_seeker(seeker: dict, do_scrape: bool = True,
                            max_contacts: int = 3, conn=None) -> dict:
    """
    المحرّك الصادر لطلب باحث واحد.
    seeker: dict فيه phone, city, rooms, budget_annual, property_type, for_family, district
    """
    from bot import wa_send       # حارس sandbox مدمج
    own = conn is None
    if own:
        conn = get_conn()
    summary = {"matched": 0, "contacted": 0, "scraped": False, "details": []}
    try:
        lead = _seeker_to_lead(seeker)
        hint = _seeker_hint(seeker)
        matches = _match_listings(lead, conn)
        good = [m for m in matches if m["score"] >= MATCH_MIN]

        # سحب حراج عند قلّة النتائج (المدفوع بالطلب: نبحث في النت)
        if do_scrape and len(good) < MIN_RESULTS and lead.get("city"):
            try:
                import asyncio
                from haraj_scraper import run_scrape_listings
                print(f"[OUTBOUND] نتائج قليلة ({len(good)}) — أسحب حراج لـ{lead['city']}", flush=True)
                asyncio.run(run_scrape_listings(cities=[lead["city"]]))
                summary["scraped"] = True
                matches = _match_listings(lead, conn)
                good = [m for m in matches if m["score"] >= MATCH_MIN]
            except Exception as e:
                print(f"[OUTBOUND] فشل السحب: {e}", flush=True)

        summary["matched"] = len(good)

        # بادر الملاك المطابقين غير المسجّلين (مبادرة باردة)
        seeker_phone = seeker.get("phone") or ""
        with conn.cursor() as cur:
            for m in good[:max_contacts]:
                owner = m.get("phone")
                if not owner or _is_registered(owner, cur):
                    continue
                msg = build_cold_outbound_intro(m, hint)
                sent = wa_send(owner, msg)   # يُحجب تلقائياً إن لم يكن sandbox
                cur.execute("""UPDATE sanad.masaed_listings
                               SET status='contacted', outreach_to=%s WHERE id=%s""",
                            (seeker_phone, m["id"]))
                conn.commit()
                summary["contacted"] += 1
                summary["details"].append({"listing_id": m["id"], "owner": owner,
                                            "score": m["score"], "sent": bool(sent)})
                print(f"[OUTBOUND] بادرت المالك {owner} (عرض #{m['id']}, درجة {m['score']})", flush=True)
        return summary
    finally:
        if own:
            conn.close()


def run_outbound_for_phone(seeker_phone: str, do_scrape: bool = True, max_contacts: int = 3) -> dict:
    """حمّل طلب باحث مسجّل بالهاتف ثم شغّل المحرّك."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT phone, city, district, rooms, budget_annual, property_type, for_family
                FROM sanad.masaed_registrations
                WHERE phone=%s AND type='wanted' AND status<>'abandoned'
                ORDER BY created_at DESC LIMIT 1
            """, (seeker_phone,))
            row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "لا يوجد طلب باحث مسجّل لهذا الرقم"}
        cols = ["phone", "city", "district", "rooms", "budget_annual", "property_type", "for_family"]
        seeker = dict(zip(cols, row))
        res = run_outbound_for_seeker(seeker, do_scrape=do_scrape, max_contacts=max_contacts, conn=conn)
        res["ok"] = True
        return res
    finally:
        conn.close()


# وصف مختصر لكل هدف (للسجل/الواجهة)
GOAL_LABELS = {
    "negotiate":             "تفاوض نشط",
    "complete_registration": "إكمال تسجيل",
    "cold_reply":            "ردّ على مبادرة باردة",
    "returning":             "عميل عائد",
    "new_inbound":           "تواصل جديد",
}
