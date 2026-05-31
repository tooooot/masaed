#!/usr/bin/env python3
"""📋 مساعد مُعِدّ الصفقة — جسر بين الطلبات والمختبر/المفاوض.
عند مطابقة طلب بعرض، يجمع «ملف صفقة» كامل (الباحث + العقار + الصور + السعر)
ويسلّمه للمختبر (محاكاة) أو المفاوض (إطلاق)."""
from bot import get_conn


def prepare(seeker_phone: str, listing_id: int = None, listing_phone: str = None,
            conn=None) -> dict:
    """يجمع ملف صفقة متكامل جاهز للاختبار/الإطلاق."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            # الباحث (طلب مسجّل)
            cur.execute("""SELECT phone, name, city, district, rooms, budget_annual,
                                  property_type, for_family
                           FROM sanad.masaed_registrations
                           WHERE phone=%s AND type='wanted' AND status<>'abandoned'
                           ORDER BY created_at DESC LIMIT 1""", (seeker_phone,))
            sc = [d[0] for d in cur.description]; sr = cur.fetchone()
            seeker = dict(zip(sc, sr)) if sr else {"phone": seeker_phone}

            # العرض (إعلان)
            offer = None
            if listing_id or listing_phone:
                where = "id=%s" if listing_id else "phone=%s AND status<>'declined'"
                cur.execute(f"""SELECT id, title, body, city, rooms, property_type,
                                       price, phone, url, photos
                                FROM sanad.masaed_listings WHERE {where}
                                ORDER BY id DESC LIMIT 1""",
                            (listing_id or listing_phone,))
                oc = [d[0] for d in cur.description]; orow = cur.fetchone()
                offer = dict(zip(oc, orow)) if orow else None
        if offer and offer.get("photos") is None:
            offer["photos"] = []
        return {
            "ok": bool(offer),
            "seeker": seeker,
            "offer": offer,
            "ready": bool(offer and seeker.get("phone")),
        }
    finally:
        if own:
            conn.close()


class DealPreparer:
    prepare = staticmethod(prepare)
