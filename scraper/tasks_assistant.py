#!/usr/bin/env python3
"""
مساعد الطلبات (v2) — جمع البيانات من روابط حراج والـleads.
يعرض الروابط والبيانات بدون تسجيل — التسجيل دور مساعد المسجل (v1).
"""
import re

# ── Detect Haraj URL ──────────────────────────────────────────────────────────

def detect_haraj_url(text: str) -> str | None:
    """كشف رابط حراج في الرسالة."""
    if not text:
        return None
    match = re.search(r'https?://(?:www\.)?haraj\.com\.sa/\S+', text, re.IGNORECASE)
    if match:
        return match.group(0)
    # جرّب بدون https
    match = re.search(r'haraj\.com\.sa/([A-Za-z0-9]+)', text, re.IGNORECASE)
    if match:
        return f"https://haraj.com.sa/{match.group(1)}"
    return None


def fetch_haraj_details(url: str) -> dict | None:
    """احصل على تفاصيل إعلان حراج من /scrape-details endpoint."""
    try:
        import requests
        resp = requests.post(
            "http://localhost:5555/scrape-details",
            json={"url": url},
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                return data.get("announcement")
    except Exception as e:
        print(f"[TASKS] fetch failed for {url}: {e}", flush=True)
    return None


def format_announcement(data: dict) -> str:
    """اعرض الإعلان بصيغة مقروءة."""
    if not data:
        return "❌ فشل في قراءة الإعلان"

    title = data.get("title", "")
    price = data.get("price")
    rooms = data.get("rooms")
    city = data.get("city", "")
    body = data.get("body", "")

    lines = [f"📌 {title}"]

    if price:
        lines.append(f"💰 السعر: {price:,} ريال")
    if rooms:
        lines.append(f"🛏️ الغرف: {rooms}")
    if city:
        lines.append(f"📍 المدينة: {city}")
    if body:
        lines.append(f"📝 التفاصيل: {body[:200]}...")

    lines.append(f"\n🔗 الرابط: {data.get('url')}")

    return "\n".join(lines)


def handle_task_message(phone: str, text: str) -> str | None:
    """
    معالجة رسالة عميل يبحث عن إعلان.
    يجمع البيانات بدون تسجيل — التسجيل من دور المسجل.
    """
    if not text:
        return None

    # ── كشف رابط حراج ────────────────────────────────────────────────────────
    url = detect_haraj_url(text)
    if url:
        print(f"[TASKS #{phone}] Detected Haraj URL: {url}", flush=True)

        # استخرج التفاصيل
        details = fetch_haraj_details(url)
        if details:
            reply = format_announcement(details)
            print(f"[TASKS #{phone}] Extracted announcement: {details.get('title')[:50]}", flush=True)
            reply += "\n\n✅ هل هذا الإعلان يناسبك؟ رد بـ'نعم' لتسجيل البيانات"
            return reply
        else:
            return f"❌ فشلت قراءة الإعلان من الرابط: {url}\nجرّب إعلان آخر"

    # ── لا توجد روابط، اسأل عن التفاصيل ─────────────────────────────────────
    return None


# ── For testing ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # اختبر كشف رابط
    test_text = "السلام عليكم، شفت إعلان حلو: https://haraj.com.sa/ABC123"
    url = detect_haraj_url(test_text)
    print(f"URL detected: {url}")

    # اختبر معالجة الرسالة
    reply = handle_task_message("9665012345", test_text)
    if reply:
        print(f"\nReply:\n{reply}")
