#!/usr/bin/env python3
"""📋 مساعد مُعِدّ الصفقة — جسر بين الطلبات والمختبر/المفاوض.

عند مطابقة طلب بعرض، يجمع «ملف صفقة» كامل (الباحث + العقار + الصور + السعر +
الرابط) ويحفظه في masaed_deals (الصفقات الجاهزة)، ويتتبّع حالته في دورة حياته:
prepared → contacted → negotiating → agreed/failed.
المختبر يستدعيه للمحاكاة، والمفاوض يستدعيه للإطلاق."""
import json
from bot import get_conn


def _assemble(seeker_phone, listing_id, listing_phone, conn):
    """يجمع بيانات الباحث + العرض في ملف موحّد.

    الباحث قد يكون: (أ) مسجّلاً يدوياً في masaed_registrations (حقول منظّمة)،
    أو (ب) طلب حراج مسحوب في masaed_leads (رابط + نص كامل). نجمع الاثنين:
    حقول التسجيل إن وُجدت + رابط/نص/عنوان الطلب من حراج إن وُجد (لفهم النية كاملةً)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT phone, name, city, district, rooms, budget_annual,
                              property_type, for_family
                       FROM sanad.masaed_registrations
                       WHERE phone=%s AND type='wanted' AND status<>'abandoned'
                       ORDER BY created_at DESC LIMIT 1""", (seeker_phone,))
        sc = [d[0] for d in cur.description]; sr = cur.fetchone()
        seeker = dict(zip(sc, sr)) if sr else {"phone": seeker_phone}

        # طلب حراج المسحوب (الرابط + النص الكامل) — مصدر فهم النية
        cur.execute("""SELECT id, url, title, body, city
                       FROM sanad.masaed_leads
                       WHERE phone=%s AND listing_type='wanted'
                       ORDER BY scraped_at DESC LIMIT 1""", (seeker_phone,))
        lr = cur.fetchone()
        if lr:
            seeker.setdefault("phone", seeker_phone)
            seeker["lead_id"] = lr[0]
            seeker["url"]   = lr[1]
            seeker["title"] = lr[2]
            seeker["body"]  = lr[3]
            seeker.setdefault("city", lr[4])

        offer = None
        if listing_id or listing_phone:
            where, val = ("id=%s", listing_id) if listing_id else \
                         ("phone=%s AND status<>'declined'", listing_phone)
            cur.execute(f"""SELECT id, title, body, city, rooms, property_type,
                                   price, phone, url,
                                   COALESCE(photos,'[]'::jsonb) AS photos, location, advertiser
                            FROM sanad.masaed_listings WHERE {where}
                            ORDER BY id DESC LIMIT 1""", (val,))
            oc = [d[0] for d in cur.description]; orow = cur.fetchone()
            offer = dict(zip(oc, orow)) if orow else None
    if offer and not offer.get("photos"):
        offer["photos"] = []
    return {"seeker": seeker, "offer": offer,
            "ready": bool(offer and seeker.get("phone"))}


def prepare(seeker_phone, listing_id=None, listing_phone=None,
            status="prepared", conn=None) -> dict:
    """يجمع ملف الصفقة ويحفظه/يحدّثه في masaed_deals، ويُرجعه."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        deal = _assemble(seeker_phone, listing_id, listing_phone, conn)
        offer = deal.get("offer") or {}
        lphone = listing_phone or offer.get("phone")
        lid = listing_id or offer.get("id")
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sanad.masaed_deals
                        (seeker_phone, listing_id, listing_phone, status, deal_file)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (seeker_phone, listing_phone) DO UPDATE
                        SET status=EXCLUDED.status, deal_file=EXCLUDED.deal_file,
                            listing_id=EXCLUDED.listing_id, updated_at=NOW()
                    RETURNING id
                """, (seeker_phone, lid, lphone, status,
                      json.dumps(deal, ensure_ascii=False, default=str)))
                deal["id"] = cur.fetchone()[0]
                conn.commit()
        except Exception as e:
            print(f"[DEAL] فشل حفظ ملف الصفقة: {e}", flush=True)
        return deal
    finally:
        if own:
            conn.close()


def mark(seeker_phone, listing_phone, status, conn=None):
    """حدّث حالة صفقة (negotiating/agreed/failed)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE sanad.masaed_deals SET status=%s, updated_at=NOW()
                           WHERE seeker_phone=%s AND listing_phone=%s""",
                        (status, seeker_phone, listing_phone))
            conn.commit()
    except Exception as e:
        print(f"[DEAL] فشل تحديث الحالة: {e}", flush=True)
    finally:
        if own:
            conn.close()


class DealPreparer:
    prepare = staticmethod(prepare)
    mark    = staticmethod(mark)
