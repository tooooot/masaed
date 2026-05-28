#!/usr/bin/env python3
"""
Rules Engine — محرك القرار الحتمي.
لا LLM. Python خالص.
يأخذ حالة التفاوض + نية جديدة → يقرر ماذا نفعل.

ملاحظة: notify_admin ومنطق relay انتقلا إلى negotiator.py مباشرة.
هذا الملف يبقى للتحقق من حالة الفجوة فقط عند الحاجة.
"""

GAP_SAR  = 1500   # فجوة ≤ 1500 ريال → قريبون
GAP_PCT  = 0.12   # أو ≤ 12% من سعر الإعلان
MAX_ROUNDS = 8


def _middle(a: int, b: int) -> int:
    return round((a + b) / 2 / 500) * 500


def is_near(lead_max: int, owner_min: int, listing_price: int) -> bool:
    """هل السعران متقاربان بما يكفي للاقتراح؟"""
    if lead_max is None or owner_min is None:
        return False
    if lead_max >= owner_min:
        return True
    gap = owner_min - lead_max
    if gap <= GAP_SAR:
        return True
    ref = listing_price or owner_min
    return bool(ref and (gap / ref) <= GAP_PCT)


def evaluate(neg: dict, sender_role: str, intent: dict) -> dict:
    """
    واجهة متوافقة مع الكود القديم.
    negotiator.py الآن يتحكم في المنطق مباشرة، لكن هذه الدالة
    محفوظة للاستدعاءات الخارجية إن وُجدت.
    """
    base = {
        "action": "auto_reply",
        "reason": "routine",
        "suggested_price": None,
        "gap": None,
        "update_price": None,
    }

    if intent["intent"] == "cancel":
        return {**base, "action": "cancel", "reason": "party_cancelled"}

    price_update = None
    if intent["intent"] == "price_offer" and intent.get("amount"):
        field = "lead_max_price" if sender_role == "مستأجر" else "owner_min_price"
        price_update = {"field": field, "value": intent["amount"]}
        base["update_price"] = price_update

    if intent["intent"] == "accept":
        return {**base, "action": "notify_admin",
                "reason": "ready_to_close", "update_price": price_update}

    lead_max  = neg.get("lead_max_price")
    owner_min = neg.get("owner_min_price")
    listing   = neg.get("listing_price")

    if price_update:
        if price_update["field"] == "lead_max_price":
            lead_max = price_update["value"]
        else:
            owner_min = price_update["value"]

    if lead_max and owner_min and is_near(lead_max, owner_min, listing):
        return {**base,
                "action": "notify_admin",
                "reason": "near_agreement",
                "gap": max(0, owner_min - lead_max),
                "suggested_price": _middle(lead_max, owner_min),
                "update_price": price_update}

    if (intent["intent"] == "reject"
            and intent.get("is_firm")
            and intent.get("sentiment") == "negative"):
        return {**base, "action": "notify_admin",
                "reason": "party_leaving", "update_price": price_update}

    rounds = sum(1 for e in neg.get("chat_log", [])
                 if e.get("role") in ("مستأجر", "مالك"))
    if rounds >= MAX_ROUNDS and rounds % 3 == 0:
        return {**base, "action": "notify_admin",
                "reason": "many_rounds", "update_price": price_update}

    return {**base, "update_price": price_update}
