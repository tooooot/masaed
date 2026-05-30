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

from datetime import datetime, timezone
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


def _score(seeker: dict, lst: dict) -> int:
    """مطابقة مبنية على الحقول المنظَّمة (مستقلّة، لا تحتاج bs4): مدينة40 غرف25 نوع15 سعر20."""
    score = 0
    sc = (seeker.get("city") or "").strip()
    lc = (lst.get("city") or "").strip()
    if sc and lc:
        if sc == lc:                 score += 40
        elif sc in lc or lc in sc:   score += 20
    sr, lr = seeker.get("rooms"), lst.get("rooms")
    if sr and lr:
        if sr == lr:                 score += 25
        elif abs(sr - lr) == 1:      score += 10
    st, lt = seeker.get("property_type"), lst.get("property_type")
    if st and lt and st == lt:       score += 15
    sb = seeker.get("budget_annual") or seeker.get("price_annual")
    lp = lst.get("price")
    if sb and lp:
        if lp <= sb:                 score += 20
        elif lp <= sb * 1.15:        score += 8
    return min(score, 100)


def _match_listings(seeker: dict, conn) -> list:
    """طابق العروض النشطة ضد طلب الباحث وأرجعها مرتّبة بالدرجة."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, body, city, property_type, rooms, price, phone, status, url
            FROM sanad.masaed_listings
            WHERE status='active' AND phone IS NOT NULL
            ORDER BY CASE WHEN city=%s THEN 0 ELSE 1 END, scraped_at DESC LIMIT 200
        """, (seeker.get("city") or "",))
        cols = [d[0] for d in cur.description]
        listings = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = [{**lst, "score": _score(seeker, lst)} for lst in listings]
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def run_outbound_for_seeker(seeker: dict, do_scrape: bool = True,
                            max_contacts: int = 3, conn=None) -> dict:
    """
    المحرّك الصادر لطلب باحث واحد.
    seeker: dict فيه phone, city, rooms, budget_annual, property_type, for_family, district
    """
    from bot import wa_send, get_config       # حارس sandbox مدمج + إعدادات الاختبار
    own = conn is None
    if own:
        conn = get_conn()
    test_mode  = get_config("test_mode", "off") == "on"
    test_owner = (get_config("test_owner", "") or "").replace("+", "").replace(" ", "") if test_mode else ""
    summary = {"matched": 0, "contacted": 0, "scraped": False, "test_mode": test_mode, "details": []}
    try:
        hint = _seeker_hint(seeker)
        matches = _match_listings(seeker, conn)
        good = [m for m in matches if m["score"] >= MATCH_MIN]

        # سحب حراج عند قلّة النتائج (المدفوع بالطلب: نبحث في النت)
        if do_scrape and len(good) < MIN_RESULTS and seeker.get("city"):
            try:
                import asyncio
                from haraj_scraper import run_scrape_listings
                print(f"[OUTBOUND] نتائج قليلة ({len(good)}) — أسحب حراج لـ{seeker['city']}", flush=True)
                asyncio.run(run_scrape_listings(cities=[seeker["city"]]))
                summary["scraped"] = True
                matches = _match_listings(seeker, conn)
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
                if test_mode and test_owner:
                    # وضع الاختبار: أنشئ عرضاً برقم الاختبار (ليعمل لوب cold_reply) وأرسل له
                    cur.execute("""
                        INSERT INTO sanad.masaed_listings
                            (source, external_id, title, city, price, rooms, property_type, phone, status, outreach_to)
                        VALUES ('test', %s, %s, %s, %s, %s, %s, %s, 'contacted', %s)
                        ON CONFLICT (source, external_id) DO UPDATE
                            SET status='contacted', outreach_to=EXCLUDED.outreach_to,
                                phone=EXCLUDED.phone, title=EXCLUDED.title, price=EXCLUDED.price
                    """, (f"TESTREDIR-{seeker_phone}", m.get("title"), m.get("city"),
                          m.get("price"), m.get("rooms"), m.get("property_type"),
                          test_owner, seeker_phone))
                    conn.commit()
                    sent = wa_send(test_owner, msg)
                    summary["contacted"] += 1
                    summary["details"].append({"listing": "TEST", "owner": test_owner,
                                                "orig_owner": owner, "score": m["score"], "sent": bool(sent)})
                    print(f"[OUTBOUND-TEST] بادرت رقم الاختبار {test_owner} بدل {owner} (درجة {m['score']})", flush=True)
                    break    # عرض واحد يكفي في الاختبار
                # وضع الإنتاج: المالك الحقيقي (محكوم بحارس wa_send)
                sent = wa_send(owner, msg)
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


def run_periodic_rematch(do_scrape: bool = False, max_per_seeker: int = 2,
                         followup_hours: int = 12) -> dict:
    """
    المتابعة الدورية (نفَس المكتب الطويل): لكل باحث نشِط بلا تفاوض جارٍ،
    أعِد المطابقة ضد العروض الحالية وبادر أصحاب العروض الجديدة، وطمئن الباحث
    (مرة كل followup_hours) أنك تتابع له. تُشغّل دورياً (cron/جدولة).
    """
    from bot import wa_send
    conn = get_conn()
    out = {"ok": True, "seekers": 0, "contacted_total": 0, "followups": 0, "details": []}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.phone, r.city, r.district, r.rooms, r.budget_annual,
                       r.property_type, r.for_family, r.last_followup
                FROM sanad.masaed_registrations r
                WHERE r.type='wanted' AND r.status='complete'
                  AND NOT EXISTS (
                    SELECT 1 FROM sanad.masaed_negotiations n
                    WHERE (n.lead_phone=r.phone OR n.listing_phone=r.phone)
                      AND n.status='active')
                ORDER BY r.created_at DESC LIMIT 100
            """)
            cols = [d[0] for d in cur.description]
            seekers = [dict(zip(cols, row)) for row in cur.fetchall()]

        for s in seekers:
            res = run_outbound_for_seeker(s, do_scrape=do_scrape,
                                          max_contacts=max_per_seeker, conn=conn)
            out["seekers"] += 1
            out["contacted_total"] += res.get("contacted", 0)

            # متابعة لطيفة للباحث عند وجود عروض جديدة (محكومة بمعدّل)
            if res.get("contacted", 0) > 0:
                last = s.get("last_followup")
                due = last is None or \
                    (datetime.now(timezone.utc) - last).total_seconds() > followup_hours * 3600
                if due:
                    wa_send(s["phone"],
                        "أبشّرك 👋 لقيت عروضاً جديدة قد تناسب طلبك وأتواصل مع أصحابها — "
                        "أوافيك أول ما يردّون 🤝")
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE sanad.masaed_registrations SET last_followup=NOW()
                                       WHERE phone=%s AND type='wanted'""", (s["phone"],))
                        conn.commit()
                    out["followups"] += 1
            out["details"].append({"seeker": s["phone"], "contacted": res.get("contacted", 0)})
        print(f"[REMATCH] باحثون={out['seekers']} مبادرات={out['contacted_total']} متابعات={out['followups']}", flush=True)
        return out
    finally:
        conn.close()


def handle_cold_reply(phone: str, text: str, conn=None) -> bool:
    """
    إكمال محادثة المبادرة الباردة: المالك المُبادَر ردّ.
    - رفض → إغلاق مهذّب ووسم declined.
    - موافقة → سجّل عقاره من بيانات الإعلان + افتح تفاوضاً مع الباحث المربوط.
    - غير ذلك (سؤال/تردّد) → عرّف بمساعد، اذكر الباحث، واطلب الإذن.
    يُرجع True إذا تولّى الرسالة.
    """
    from bot import wa_send
    from intent_parser import parse_intent
    from negotiator import start_negotiation

    own = conn is None
    if own:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, city, price, rooms, property_type, outreach_to
                FROM sanad.masaed_listings
                WHERE phone=%s AND status='contacted'
                ORDER BY id DESC LIMIT 1
            """, (phone,))
            row = cur.fetchone()
        if not row:
            return False
        lid, title, city, price, rooms, ptype, seeker_phone = row

        # بيانات الباحث المربوط (للذكر والربط)
        seeker_id, hint = None, ""
        if seeker_phone:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, rooms, budget_annual, for_family FROM sanad.masaed_registrations
                    WHERE phone=%s AND type='wanted' AND status<>'abandoned'
                    ORDER BY created_at DESC LIMIT 1
                """, (seeker_phone,))
                s = cur.fetchone()
            if s:
                seeker_id = s[0]
                hint = _seeker_hint({"rooms": s[1], "budget_annual": s[2], "for_family": s[3]})

        intent = parse_intent(text)
        itype  = intent.get("intent")
        senti  = intent.get("sentiment")

        # ── رفض → إغلاق مهذّب ──────────────────────────────────────────────
        if itype in ("reject", "cancel") and senti != "positive":
            with conn.cursor() as cur:
                cur.execute("UPDATE sanad.masaed_listings SET status='declined' WHERE id=%s", (lid,))
                conn.commit()
            wa_send(phone, "تمام، شكراً لوقتك 🙏 لو احتجت تأجير عقارك مستقبلاً أنا موجود.")
            return True

        # ── ليست موافقة واضحة → عرّف بمساعد + اذكر الباحث + اطلب الإذن ──────
        if itype != "accept" and senti != "positive":
            extra = f" يطابق عقارك ({hint})" if hint else " يطابق عقارك"
            wa_send(phone,
                "أنا «مساعد» — وكيل عقاري يعمل بالذكاء الاصطناعي 🤝 "
                f"عندي مستأجر جاد{extra}، وأتولّى التنسيق والتفاوض نيابةً عنك.\n"
                "تحب أربطك فيه ونبدأ؟")
            return True

        # ── موافقة → سجّل المالك من الإعلان + افتح التفاوض ──────────────────
        with conn.cursor() as cur:
            cur.execute("""UPDATE sanad.masaed_registrations SET status='abandoned'
                           WHERE phone=%s AND status<>'abandoned'""", (phone,))
            cur.execute("""
                INSERT INTO sanad.masaed_registrations
                    (phone, type, status, city, rooms, property_type, price_annual)
                VALUES (%s, 'listing', 'complete', %s, %s, %s, %s) RETURNING id
            """, (phone, city, rooms, ptype, price))
            owner_reg = cur.fetchone()[0]
            cur.execute("UPDATE sanad.masaed_listings SET status='registered' WHERE id=%s", (lid,))
            conn.commit()

        if not seeker_id:   # الباحث لم يعد متاحاً
            wa_send(phone, "ممتاز! 🎉 سجّلت عقارك، وبنتواصل معك بتفاصيل المستأجر قريباً.")
            return True

        start_negotiation(
            lead_id=seeker_id, listing_id=None,
            lead_phone=seeker_phone, listing_phone=phone,
            listing_title=title or "عقارك", listing_city=city, listing_price=price,
        )
        print(f"[COLD] المالك {phone} وافق → سجّلته (#{owner_reg}) وفتحت تفاوضاً مع الباحث {seeker_phone}", flush=True)
        return True
    finally:
        if own:
            conn.close()


# وصف مختصر لكل هدف (للسجل/الواجهة)
GOAL_LABELS = {
    "negotiate":             "تفاوض نشط",
    "complete_registration": "إكمال تسجيل",
    "cold_reply":            "ردّ على مبادرة باردة",
    "returning":             "عميل عائد",
    "new_inbound":           "تواصل جديد",
}
