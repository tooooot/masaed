#!/usr/bin/env python3
"""
Rules Engine — محرك القرار الحتمي.
لا LLM. Python خالص.
يأخذ حالة التفاوض + نية جديدة → يقرر ماذا نفعل.
"""

# ── Thresholds ─────────────────────────────────────────────────────────────────
GAP_SAR   = 1500   # أبلغ الإدارة إذا الفجوة ≤ 1500 ريال
GAP_PCT   = 0.12   # أو ≤ 12% من سعر الإعلان
MAX_ROUNDS = 8     # بعد 8 رسائل من الطرفين → تذكير الإدارة


def _middle(a: int, b: int) -> int:
    """Round to nearest 500."""
    return round((a + b) / 2 / 500) * 500


def _near(lead_max: int, owner_min: int, listing_price: int) -> bool:
    if lead_max is None or owner_min is None:
        return False
    if lead_max >= owner_min:
        return True  # يتقاطعان
    gap = owner_min - lead_max
    if gap <= GAP_SAR:
        return True
    ref = listing_price or owner_min
    if ref and (gap / ref) <= GAP_PCT:
        return True
    return False


def evaluate(neg: dict, sender_role: str, intent: dict) -> dict:
    """
    Returns:
    {
      "action":          "auto_reply" | "notify_admin" | "cancel",
      "reason":          "routine" | "near_agreement" | "ready_to_close" |
                         "party_leaving" | "many_rounds",
      "suggested_price": int | None,
      "gap":             int | None,
      "update_price":    {"field": str, "value": int} | None,
    }
    """
    base = {
        "action": "auto_reply",
        "reason": "routine",
        "suggested_price": None,
        "gap": None,
        "update_price": None,
    }

    # ── إلغاء ─────────────────────────────────────────────────────────────────
    if intent["intent"] == "cancel":
        return {**base, "action": "cancel", "reason": "party_cancelled"}

    # ── استخراج السعر وحفظه ────────────────────────────────────────────────────
    price_update = None
    if intent["intent"] == "price_offer" and intent["amount"]:
        field = "lead_max_price" if sender_role == "مستأجر" else "owner_min_price"
        price_update = {"field": field, "value": intent["amount"]}
        base["update_price"] = price_update

    # ── قبول صريح → أبلغ الإدارة ──────────────────────────────────────────────
    if intent["intent"] == "accept":
        return {**base, "action": "notify_admin", "reason": "ready_to_close"}

    # ── حساب الفجوة مع السعر الجديد ───────────────────────────────────────────
    lead_max  = neg.get("lead_max_price")
    owner_min = neg.get("owner_min_price")
    listing   = neg.get("listing_price")

    if price_update:
        if price_update["field"] == "lead_max_price":
            lead_max = price_update["value"]
        else:
            owner_min = price_update["value"]

    if lead_max and owner_min:
        if lead_max >= owner_min:
            # يتقاطعان — أبلغ الإدارة فوراً
            suggested = _middle(lead_max, owner_min)
            return {**base,
                    "action": "notify_admin",
                    "reason": "near_agreement",
                    "gap": 0,
                    "suggested_price": suggested,
                    "update_price": price_update}

        gap = owner_min - lead_max
        if _near(lead_max, owner_min, listing):
            return {**base,
                    "action": "notify_admin",
                    "reason": "near_agreement",
                    "gap": gap,
                    "suggested_price": _middle(lead_max, owner_min),
                    "update_price": price_update}

    # ── رفض حازم → قد ينسحب ──────────────────────────────────────────────────
    if intent["intent"] == "reject" and intent["is_firm"] and intent["sentiment"] == "negative":
        return {**base, "action": "notify_admin", "reason": "party_leaving",
                "update_price": price_update}

    # ── كثرة الرسائل → تذكير الإدارة كل 3 رسائل بعد الحد ────────────────────
    rounds = sum(1 for e in neg.get("chat_log", [])
                 if e.get("role") in ("مستأجر", "مالك"))
    if rounds >= MAX_ROUNDS and rounds % 3 == 0:
        return {**base, "action": "notify_admin", "reason": "many_rounds",
                "update_price": price_update}

    return {**base, "update_price": price_update}
