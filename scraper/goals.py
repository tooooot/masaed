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


# وصف مختصر لكل هدف (للسجل/الواجهة)
GOAL_LABELS = {
    "negotiate":             "تفاوض نشط",
    "complete_registration": "إكمال تسجيل",
    "cold_reply":            "ردّ على مبادرة باردة",
    "returning":             "عميل عائد",
    "new_inbound":           "تواصل جديد",
}
