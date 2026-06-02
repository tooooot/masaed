"""كاشف أخطاء الحقائق في محاكاة الصفقة — يقارن مجرى الحوار بحقائق الإعلان/الطلب.
يرصد: تضارب الموقع (جدة/الرياض)، الغرف، انحراف السعر، تأليف سمات، وخروج عن السرب.
حتمي (بلا LLM) لرصد سريع رخيص، يُكمّله ناقد المحاكاة."""
import re

# مدن سعودية شائعة (لرصد تضارب الموقع)
_CITIES = ["الرياض", "جدة", "جده", "مكة", "مكه", "المدينة", "المدينه", "الدمام",
           "الخبر", "الطائف", "الطايف", "تبوك", "بريدة", "بريده", "خميس مشيط",
           "حائل", "حايل", "نجران", "جازان", "أبها", "ابها", "الجبيل", "ينبع",
           "القصيم", "الأحساء", "الاحساء", "عرعر", "سكاكا", "الباحة", "الباحه"]

# سمات عقار قد تُختلَق إن لم تكن في حقائق الإعلان
_ATTRS = ["مصعد", "موقف", "مسبح", "حديقة", "حديقه", "مفروش", "تكييف", "مكيف",
          "مستودع", "خادمة", "خادمه", "مطبخ راكب", "اطلالة", "إطلالة"]


def _norm(c):
    return c.replace("ة", "ه").replace("أ", "ا").replace("إ", "ا")


def analyze(messages, offer, seeker, sim=None):
    """يُرجع قائمة أخطاء: [{type, sev, detail}] (sev: high/med/low)."""
    issues = []
    offer = offer or {}
    seeker = seeker or {}
    text = " ".join((m.get("text") or "") for m in (messages or []))
    facts_blob = " ".join(str(offer.get(k) or "") for k in
                          ("title", "body", "city", "property_type"))

    # 1) تضارب الموقع — أخطر خطأ
    ocity = _norm(offer.get("city") or "")
    if ocity:
        mentioned = {_norm(c) for c in _CITIES if c in text}
        wrong = [c for c in mentioned if c and c != ocity and c not in ocity and ocity not in c]
        if wrong:
            issues.append({"type": "موقع", "sev": "high",
                           "detail": f"الحوار ذكر «{wrong[0]}» بينما العقار في «{offer.get('city')}»."})

    # 2) تضارب الغرف (تطبيع الأرقام العربية ٠-٩ إلى int)
    oro = offer.get("rooms")
    mr = re.search(r"(\d+)\s*غرف", text)
    if oro and mr:
        try:
            if int(mr.group(1)) != int(oro):
                issues.append({"type": "غرف", "sev": "med",
                               "detail": f"ذُكر {mr.group(1)} غرف بينما العرض {oro} غرف."})
        except (ValueError, TypeError):
            pass

    # 3) انحراف السعر النهائي/المقترح
    price = offer.get("price")
    agreed = (sim or {}).get("agreed_price") or (sim or {}).get("proposed_price")
    if price and agreed:
        try:
            price = int(price); agreed = int(agreed)
            if agreed > price:
                issues.append({"type": "سعر", "sev": "med",
                               "detail": f"السعر المتّفق ({agreed:,}) أعلى من سعر الإعلان ({price:,})."})
            elif price and (price - agreed) / price > 0.30:
                issues.append({"type": "سعر", "sev": "low",
                               "detail": f"خصم كبير ({(price-agreed)/price*100:.0f}%): الإعلان {price:,} والمتّفق {agreed:,}."})
        except Exception:
            pass

    # 4) تأليف سمة من الوسيط غير موجودة في حقائق الإعلان
    for m in (messages or []):
        if str(m.get("from")) != "الوسيط":
            continue
        t = m.get("text") or ""
        # تجاهل رسائل الترحيل/الأسئلة (لا تؤكّد سمة)
        if "يطلب" in t or "أستوضح" in t or "بخصوص استفسارك" in t:
            continue
        for a in _ATTRS:
            # الوسيط يؤكّد وجود سمة (موجود/فيه/شغال) وليست في الحقائق
            if a in t and a not in facts_blob and re.search(a + r"[^؟?]{0,12}(موجود|فيه|شغال|متوفر|نعم)", t):
                issues.append({"type": "تأليف", "sev": "high",
                               "detail": f"الوسيط أكّد «{a}» وهي غير مذكورة في حقائق الإعلان."})
                break

    return issues


def summary(issues):
    if not issues:
        return "لا أخطاء حقائق مرصودة ✅"
    high = sum(1 for i in issues if i["sev"] == "high")
    return f"{len(issues)} ملاحظة ({high} حرجة)"
