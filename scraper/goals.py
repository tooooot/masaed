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

            # 3) ردّ على مبادرة باردة: عرضٌ راسلناه (contacted) ينتظر ردّه — أولوية على
            #    'عائد' حتى لو كان مسجّلاً (قد يكون سُجّل في تجربة سابقة)
            if _scalar(cur, """
                SELECT 1 FROM sanad.masaed_listings
                WHERE phone=%s AND status='contacted' LIMIT 1
            """, (phone,)):
                return "cold_reply"

            # 4) عميل مسجّل عائد
            if _scalar(cur, """
                SELECT 1 FROM sanad.masaed_registrations
                WHERE phone=%s AND status <> 'abandoned' AND type IS NOT NULL LIMIT 1
            """, (phone,)):
                return "returning"

            # 5) جديد
            return "new_inbound"
    finally:
        if own:
            conn.close()


from prompts import build_cold_outbound_intro


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


def _tried_phones(seeker_phone: str, conn) -> set:
    """أرقام عروض جُرِّبت سابقاً لهذا الباحث (تفاوضات أُلغيت/رُفضت) — تُستبعد من الترشيح الجديد."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT listing_phone FROM sanad.masaed_negotiations
            WHERE lead_phone=%s AND status IN ('cancelled','declined','expired')
        """, (seeker_phone,))
        return {r[0] for r in cur.fetchall() if r[0]}


def _match_listings(seeker: dict, conn, exclude_phones: set = None) -> list:
    """طابق العروض النشطة ضد طلب الباحث وأرجعها مرتّبة بالدرجة (مستبعِداً المُجرَّبة)."""
    exclude_phones = exclude_phones or set()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, body, city, property_type, rooms, price, phone, status, url
            FROM sanad.masaed_listings
            WHERE status='active' AND phone IS NOT NULL
            ORDER BY CASE WHEN city=%s THEN 0 ELSE 1 END, scraped_at DESC LIMIT 200
        """, (seeker.get("city") or "",))
        cols = [d[0] for d in cur.description]
        listings = [dict(zip(cols, r)) for r in cur.fetchall()]
    self_phone = seeker.get("phone")
    out = [{**lst, "score": _score(seeker, lst)} for lst in listings
           if lst.get("phone") not in exclude_phones and lst.get("phone") != self_phone]
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def recommend_offer(seeker: dict, conn, do_scrape: bool = True) -> dict | None:
    """مساعد الطلبات: يرشّح أفضل عرض مطابق غير مُجرَّب (يسحب حراج عند الحاجة)."""
    seeker_phone = seeker.get("phone") or ""
    tried = _tried_phones(seeker_phone, conn)
    matches = [m for m in _match_listings(seeker, conn, tried) if m["score"] >= MATCH_MIN]
    if not matches and do_scrape and seeker.get("city"):
        try:
            import asyncio
            from haraj_scraper import run_scrape_listings
            print(f"[SOURCING] لا عرض غير مُجرَّب — أسحب حراج لـ{seeker['city']}", flush=True)
            asyncio.run(run_scrape_listings(cities=[seeker["city"]]))
            matches = [m for m in _match_listings(seeker, conn, tried) if m["score"] >= MATCH_MIN]
        except Exception as e:
            print(f"[SOURCING] فشل السحب: {e}", flush=True)
    return matches[0] if matches else None


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
        seeker_phone = seeker.get("phone") or ""
        tried = _tried_phones(seeker_phone, conn)   # استبعاد العروض المُجرَّبة (الحلقة)
        matches = _match_listings(seeker, conn, tried)
        good = [m for m in matches if m["score"] >= MATCH_MIN]

        # سحب حراج عند قلّة النتائج غير المُجرَّبة (المدفوع بالطلب: نبحث في النت)
        if do_scrape and len(good) < MIN_RESULTS and seeker.get("city"):
            try:
                import asyncio
                from haraj_scraper import run_scrape_listings
                print(f"[OUTBOUND] لا عرض جديد كافٍ — أسحب حراج لـ{seeker['city']}", flush=True)
                asyncio.run(run_scrape_listings(cities=[seeker["city"]]))
                summary["scraped"] = True
                matches = _match_listings(seeker, conn, tried)
                good = [m for m in matches if m["score"] >= MATCH_MIN]
            except Exception as e:
                print(f"[OUTBOUND] فشل السحب: {e}", flush=True)

        summary["matched"] = len(good)
        if tried:
            print(f"[OUTBOUND] استبعدت {len(tried)} عرضاً مُجرَّباً للباحث {seeker_phone}", flush=True)
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
                    print(f"[ROUTE] {test_owner} → goal=outbound → 📤 المبادرة الباردة (الصادر، اختبار)", flush=True)
                    break    # عرض واحد يكفي في الاختبار
                # وضع الإنتاج: المالك الحقيقي (محكوم بحارس wa_send)
                sent = wa_send(owner, msg)
                cur.execute("""UPDATE sanad.masaed_listings
                               SET status='contacted', outreach_to=%s WHERE id=%s""",
                            (seeker_phone, m["id"]))
                conn.commit()
                # 📋 مُعِدّ الصفقة: سجّل ملف صفقة (الصفقات الجاهزة)
                try:
                    from deal_preparer import prepare as _prep_deal
                    _prep_deal(seeker_phone, listing_id=m["id"], listing_phone=owner,
                               status="contacted", conn=conn)
                except Exception as _e:
                    print(f"[DEAL] تعذّر إعداد الصفقة: {_e}", flush=True)
                summary["contacted"] += 1
                summary["details"].append({"listing_id": m["id"], "owner": owner,
                                            "score": m["score"], "sent": bool(sent)})
                print(f"[OUTBOUND] بادرت المالك {owner} (عرض #{m['id']}, درجة {m['score']})", flush=True)
                print(f"[ROUTE] {owner} → goal=outbound → 📤 المبادرة الباردة (الصادر)", flush=True)
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


def start_test_negotiation(lead_id: int) -> dict:
    """
    يبدأ تفاوضاً تجريبياً على رقمي الاختبار (باحث/مالك) مزروعاً ببيانات طلب حقيقي،
    فيتقمّص المالكُ الرقمين الطرفين ويرى الحوار حياً. يُستدعى من زر «اختبار محاكاة».
    """
    from bot import get_config
    from negotiator import start_negotiation, ensure_table
    seeker = (get_config("test_seeker", "") or "").replace("+", "").replace(" ", "")
    owner  = (get_config("test_owner", "") or "").replace("+", "").replace(" ", "")
    if not seeker or not owner:
        return {"ok": False, "error": "عيّن رقمي الاختبار (الباحث والمالك) في إعدادات وضع الاختبار أولاً"}
    if seeker == owner:
        return {"ok": False, "error": "رقم الباحث والمالك يجب أن يختلفا"}

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT title, city FROM sanad.masaed_leads WHERE id=%s", (lead_id,))
            row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "الطلب غير موجود"}
        title, city = (row[0] or "عقار مطلوب"), (row[1] or "")

        ensure_table()
        # idempotent: لا تُعِد الإرسال إن وُجد تفاوض نشط حديث على نفس الرقمين (تجنّب الإزعاج)
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM sanad.masaed_negotiations
                           WHERE status='active' AND lead_phone=%s AND listing_phone=%s
                             AND created_at > NOW() - INTERVAL '5 minutes' LIMIT 1""",
                        (seeker, owner))
            recent = cur.fetchone()
        if recent:
            return {"ok": True, "neg_id": recent[0],
                    "note": "التفاوض جارٍ بالفعل — لم تُرسَل رسائل جديدة (تجنّباً للإزعاج). أرسل رسالة من جوّالك لتكمل."}
        with conn.cursor() as cur:
            # ألغِ أي تفاوض نشط على رقمي الاختبار (لإعادة التجربة بنظافة)
            cur.execute("""UPDATE sanad.masaed_negotiations SET status='cancelled'
                           WHERE status='active' AND (lead_phone IN (%s,%s) OR listing_phone IN (%s,%s))""",
                        (seeker, owner, seeker, owner))
            # سجّل الرقمين (مطلوب لبدء التفاوض): الباحث wanted، المالك listing
            seeker_reg = None
            for ph, typ in ((seeker, "wanted"), (owner, "listing")):
                cur.execute("UPDATE sanad.masaed_registrations SET status='abandoned' WHERE phone=%s AND status<>'abandoned'", (ph,))
                cur.execute("""INSERT INTO sanad.masaed_registrations (phone, type, status, city)
                               VALUES (%s, %s, 'complete', %s) RETURNING id""", (ph, typ, city))
                rid = cur.fetchone()[0]
                if typ == "wanted":
                    seeker_reg = rid
            conn.commit()

        # نبدأ التفاوض بلا افتتاح آلي، ثم نرسل افتتاحاً واضحاً للمحاكاة
        res = start_negotiation(
            lead_id=seeker_reg, listing_id=None,
            lead_phone=seeker, listing_phone=owner,
            listing_title=title[:80], listing_city=city, listing_price=None,
            send_intro=False,
        )
        if res.get("ok"):
            from bot import wa_send
            loc = f" في {city}" if city else ""
            # الباحث (أنت): طلب حقيقي
            wa_send(seeker,
                f"🧪 [محاكاة اختبار]\nمرحباً 👋 معك مساعد العقاري. بخصوص بحثك «{title[:60]}» — "
                f"لقيت لك عرضاً مناسباً، تحدّث معي وأنا الوسيط بينك وبين المالك.")
            # المالك (أنت): بلا ادّعاء إعلان — عرض مستأجر مطابق
            wa_send(owner,
                f"🧪 [محاكاة اختبار]\nمرحباً 👋 معك مساعد العقاري. عندي مستأجر جاد يبحث عن "
                f"عقار{loc} يطابق ما لديك، تحدّث معي وأنا الوسيط بينك وبين المستأجر.")
            res["note"] = f"بدأ التفاوض على رقميك — تقمّص الباحث ({seeker}) والمالك ({owner})"
        return res
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

        # شكر المالك (الجمع تمّ عبر الإعلان + سيُكمل الناقص أثناء التفاوض عبر الـrelay)
        wa_send(phone, "ممتاز! 🎉 سجّلت عقارك. بأعرضه على المستأجر الآن وأبدأ التنسيق بينكما.")

        # ── عرض: قدّم العقار للباحث قبل التفاوض ──────────────────────────────
        loc = f" في {city}" if city else ""
        pr  = f" بسعر {int(price):,} ريال/سنة" if price else ""
        wa_send(seeker_phone,
            f"بشّرك 👋 لقيت لك عرضاً يطابق طلبك: «{(title or 'عقار')[:60]}»{loc}{pr}.\n"
            f"المالك جاهز للتفاوض — أبدأ التنسيق بينكما الآن؟")

        # ── تفاوض ────────────────────────────────────────────────────────────
        start_negotiation(
            lead_id=seeker_id, listing_id=None,
            lead_phone=seeker_phone, listing_phone=phone,
            listing_title=title or "عقارك", listing_city=city, listing_price=price,
            send_intro=False,   # عرضنا للباحث يدوياً أعلاه؛ نتجنّب ازدواج الافتتاح
        )
        # 📋 مُعِدّ الصفقة: الصفقة دخلت التفاوض
        try:
            from deal_preparer import prepare as _prep_deal
            _prep_deal(seeker_phone, listing_phone=phone, status="negotiating", conn=conn)
        except Exception:
            pass
        print(f"[COLD] المالك {phone} وافق → سجّلته (#{owner_reg}) → عرضت للباحث {seeker_phone} → بدأ التفاوض", flush=True)
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
