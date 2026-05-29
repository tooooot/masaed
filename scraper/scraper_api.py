#!/usr/bin/env python3
"""
مساعد — Scraper API
Flask API so n8n and the dashboard can trigger scraping and receive leads.
Port 5555
"""
import sys
import os, re, json, asyncio, time, requests
from urllib.parse import urlparse
from flask import Flask, jsonify, request
import psycopg2

# Ensure current directory is in path for local imports
sys.path.insert(0, os.path.dirname(__file__))

app = Flask(__name__)

DB_HOST = os.getenv("POSTGRES_HOST", "sanad-postgres")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "sanad")
DB_USER = os.getenv("POSTGRES_USER", "sanad")
DB_PASS = os.getenv("POSTGRES_PASSWORD", os.getenv("PG_SANAD_PWD", ""))

PHONE_RE = re.compile(r'(?:966|00966|\+966|0)?5\d{8}')


def get_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def normalize_phone(raw: str) -> str:
    """Normalize Saudi phone to 966XXXXXXXXX format."""
    clean = re.sub(r'[^0-9]', '', raw)
    if clean.startswith('00966'):
        clean = clean[5:]
        return '966' + clean
    if clean.startswith('966'):
        return clean
    if clean.startswith('0'):
        return '966' + clean[1:]
    if clean.startswith('5'):
        return '966' + clean
    return clean


def upsert_lead(conn, lead: dict) -> bool:
    """Insert lead; returns True if new."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_leads
                (source, external_id, url, title, body, city, phone, phone_hidden, listing_type, status)
            VALUES
                (%(source)s, %(external_id)s, %(url)s, %(title)s, %(body)s,
                 %(city)s, %(phone)s, %(phone_hidden)s, 'wanted', 'new')
            ON CONFLICT (source, external_id) DO NOTHING
            RETURNING id
        """, lead)
        result = cur.fetchone()
        conn.commit()
        return result is not None


# ── CORS ──────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/scrape", methods=["POST"])
def scrape():
    """Trigger a scraping run. Body: {cities: [...], queries: [...]}"""
    from haraj_scraper import run_scrape
    data = request.get_json() or {}
    cities = data.get("cities")
    queries = data.get("queries")
    try:
        new_count = run_async(run_scrape(cities=cities, queries=queries))
        return jsonify({"status": "ok", "new_leads": new_count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/leads")
def leads():
    """Return leads. Query params: status, city, limit"""
    status = request.args.get("status", "new")
    city = request.args.get("city")
    limit = int(request.args.get("limit", 50))

    where = []
    params = []

    if status != "all":
        where.append("status = %s")
        params.append(status)
    if city:
        where.append("city ILIKE %s")
        params.append(f"%{city}%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    sql = f"""
        SELECT id, source, external_id, url, title, city, phone, phone_hidden, status, scraped_at
        FROM sanad.masaed_leads
        {where_sql}
        ORDER BY scraped_at DESC
        LIMIT %s
    """
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()

    for row in rows:
        if row.get("scraped_at"):
            row["scraped_at"] = row["scraped_at"].isoformat()

    return jsonify({"leads": rows, "count": len(rows)})


@app.route("/leads/from-bookmarklet", methods=["POST"])
def from_bookmarklet():
    """Save a lead captured by the bookmarklet from any website."""
    data = request.get_json() or {}
    phone_raw = (data.get("phone") or "").strip()
    url = (data.get("url") or "")[:500]
    title = (data.get("title") or "")[:300]
    body = (data.get("body") or "")[:2000]
    source = re.sub(r'[^a-z0-9._-]', '', (data.get("source") or "bookmarklet").lower())[:50]

    if not phone_raw:
        return jsonify({"error": "phone required"}), 400

    phone = normalize_phone(phone_raw)

    parsed = urlparse(url)
    path_part = parsed.path.strip("/")
    external_id = (path_part[-60:] if path_part else url[-60:]) or phone

    lead = {
        "source": source or "bookmarklet",
        "external_id": external_id,
        "url": url,
        "title": title,
        "body": body,
        "city": "",
        "phone": phone,
        "phone_hidden": False,
    }

    conn = get_conn()
    is_new = upsert_lead(conn, lead)
    conn.close()

    return jsonify({"success": True, "new": is_new, "phone": phone})


@app.route("/extract-url", methods=["POST"])
def extract_url():
    """Open a URL with Playwright and try to extract a phone number."""
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400

    try:
        phone = run_async(_extract_phone_playwright(url))
        if phone:
            return jsonify({"phone": phone, "url": url})
        return jsonify({"phone": None, "message": "لم يتم العثور على رقم — جرب Bookmarklet"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/scrape-details", methods=["POST"])
def scrape_details():
    """
    اقرأ تفاصيل إعلان حراج واحد — يُستخدم من مساعد الطلبات.
    البوديات: {url: "https://haraj.com.sa/..."}
    العائد: التفاصيل الكاملة + الصور + الفيديو
    """
    from haraj_scraper import scrape_single_url_sync

    data = request.get_json() or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "url required"}), 400

    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    try:
        result = scrape_single_url_sync(url)
        if not result:
            print(f"[/scrape-details] None result for {url}", flush=True)
            return jsonify({"error": f"فشل في قراءة الإعلان من {url}"}), 400

        return jsonify({
            "success": True,
            "announcement": {
                "id": result.get("id"),
                "title": result.get("title"),
                "body": result.get("body"),
                "city": result.get("city"),
                "region": result.get("region"),
                "category": result.get("category"),
                "price": result.get("price"),
                "rooms": result.get("rooms"),
                "property_type": result.get("property_type"),
                "user_name": result.get("user_name"),
                "user_verified": result.get("user_verified"),
                "post_date": result.get("post_date"),
                "url": url,
            },
            "media": {
                "images": result.get("images", []),
                "video": result.get("video"),
            },
            "contact": {
                "phone": result.get("phone"),
            }
        })

    except Exception as e:
        import traceback
        print(f"[/scrape-details] {url}: {e}\n{traceback.format_exc()}", flush=True)
        return jsonify({"error": f"خطأ: {str(e)[:100]}"}), 500


async def _extract_phone_playwright(url: str):
    """Playwright-based phone extraction. Handles Haraj + generic pages."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        page = await browser.new_page()
        page.set_default_timeout(30000)

        try:
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_timeout(2000)

            # Haraj: try React Router context first (has more text data)
            if "haraj.com.sa" in url:
                body_text = await page.evaluate("""() => {
                    try {
                        const ctx = window.__reactRouterContext;
                        const ld = ctx?.state?.loaderData || {};
                        const key = Object.keys(ld).find(k =>
                            k.includes('post') || k.includes('detail') || k.includes('item'));
                        if (!key) return null;
                        const q = ld[key]?.dehydratedState?.queries || [];
                        for (const query of q) {
                            const d = query?.state?.data;
                            if (d?.bodyTEXT) return d.bodyTEXT;
                            if (d?.post?.bodyTEXT) return d.post.bodyTEXT;
                        }
                        return null;
                    } catch(e) { return null; }
                }""")
                if body_text:
                    phones = PHONE_RE.findall(body_text)
                    if phones:
                        await browser.close()
                        return normalize_phone(phones[0])

            # Try clicking "show phone" / "تواصل" buttons
            btns = await page.query_selector_all('button, a, span[role=button]')
            for btn in btns:
                try:
                    text = (await btn.inner_text()).strip()
                    if re.search(r'رقم|هاتف|جوال|تواصل|اتصال|phone|contact|اظهر', text, re.I):
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass

            # Search visible page text
            page_text = await page.inner_text('body')
            phones = PHONE_RE.findall(page_text)
            if phones:
                await browser.close()
                return normalize_phone(phones[0])

            await browser.close()
            return None

        except Exception:
            await browser.close()
            raise


@app.route("/leads/<int:lead_id>/phone", methods=["POST"])
def update_phone(lead_id):
    """Update phone for a lead."""
    data = request.get_json() or {}
    phone_raw = (data.get("phone") or "").strip()
    if not phone_raw:
        return jsonify({"error": "phone required"}), 400

    phone = normalize_phone(phone_raw)
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_leads
            SET phone = %s, phone_hidden = FALSE, status = 'phone_extracted'
            WHERE id = %s
            RETURNING id, title
        """, (phone, lead_id))
        result = cur.fetchone()
        conn.commit()
    conn.close()

    if result:
        return jsonify({"success": True, "id": result[0], "phone": phone})
    return jsonify({"error": "lead not found"}), 404


@app.route("/leads/<int:lead_id>/contact", methods=["POST"])
def mark_contacted(lead_id):
    """Mark lead as contacted."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sanad.masaed_leads SET status='contacted' WHERE id=%s RETURNING phone",
            (lead_id,)
        )
        result = cur.fetchone()
        conn.commit()
    conn.close()
    if result:
        return jsonify({"success": True, "phone": result[0]})
    return jsonify({"error": "not found"}), 404


# ══════════════════════════════════════════════════════════════════════════════
# LISTINGS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/listings/scrape", methods=["POST"])
def scrape_listings():
    from haraj_scraper import run_scrape_listings
    data = request.get_json() or {}
    cities = data.get("cities")
    queries = data.get("queries")
    try:
        new_count = run_async(run_scrape_listings(cities=cities, queries=queries))
        return jsonify({"status": "ok", "new_listings": new_count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/listings")
def get_listings():
    city   = request.args.get("city")
    limit  = int(request.args.get("limit", 100))
    status = request.args.get("status", "active")

    where, params = [], []
    if status != "all":
        where.append("status = %s"); params.append(status)
    if city:
        where.append("city ILIKE %s"); params.append(f"%{city}%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT id, source, external_id, url, title, city,
                   property_type, rooms, price, phone, phone_hidden, status, scraped_at
            FROM sanad.masaed_listings
            {where_sql}
            ORDER BY scraped_at DESC LIMIT %s
        """, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        if r.get("scraped_at"): r["scraped_at"] = r["scraped_at"].isoformat()
    return jsonify({"listings": rows, "count": len(rows)})


@app.route("/listings/stats")
def listings_stats():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE phone_hidden=FALSE) as with_phone,
                COUNT(*) FILTER (WHERE phone_hidden=TRUE)  as hidden_phone,
                COUNT(*) FILTER (WHERE status='contacted') as contacted,
                COUNT(DISTINCT city) as cities
            FROM sanad.masaed_listings
        """)
        cols = [d[0] for d in cur.description]
        row  = cur.fetchone()
    conn.close()
    return jsonify(dict(zip(cols, row)))


@app.route("/listings/<int:lst_id>/contacted", methods=["POST"])
def listing_contacted(lst_id):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sanad.masaed_listings SET status='contacted' WHERE id=%s RETURNING phone",
            (lst_id,)
        )
        result = cur.fetchone()
        conn.commit()
    conn.close()
    if result:
        return jsonify({"success": True, "phone": result[0]})
    return jsonify({"error": "not found"}), 404


# ══════════════════════════════════════════════════════════════════════════════
# MATCHING ENGINE — find top 5 listings for a given lead
# ══════════════════════════════════════════════════════════════════════════════

def _extract_rooms(text: str):
    from haraj_scraper import extract_rooms
    return extract_rooms(text)

def _extract_price(text: str):
    from haraj_scraper import extract_price
    return extract_price(text)

def _extract_type(text: str):
    from haraj_scraper import extract_type
    return extract_type(text)

def score_match(lead: dict, listing: dict) -> tuple:
    """Returns (score, reasons_str, missing_str)."""
    score   = 0
    reasons = []
    missing = []

    full_lead    = ((lead.get('title') or '') + ' ' + (lead.get('body') or ''))[:600]
    full_listing = ((listing.get('title') or '') + ' ' + (listing.get('body') or ''))[:600]

    # ── City (40 pts) ─────────────────────────────────────────────────────────
    lc = (lead.get('city') or '').strip()
    rc = (listing.get('city') or '').strip()
    if lc and rc:
        if lc == rc:
            score += 40; reasons.append(f"نفس المدينة ({rc})")
        elif lc in rc or rc in lc:
            score += 20; reasons.append(f"مدينة قريبة")
            missing.append(f"المدينة غير متطابقة تماماً (−20)")
        else:
            missing.append(f"مدينة مختلفة: الطلب {lc} / العرض {rc} (−40)")
    elif not rc:
        missing.append("المدينة غير محددة في العرض (−40)")

    # ── Rooms (25 pts) ────────────────────────────────────────────────────────
    lr = _extract_rooms(full_lead)
    rr = listing.get('rooms') or _extract_rooms(full_listing)
    if lr and rr:
        if lr == rr:
            score += 25; reasons.append(f"{rr} غرف متطابقة")
        elif abs(lr - rr) == 1:
            score += 10; reasons.append(f"غرف قريبة ({rr}±1)")
            missing.append(f"فارق غرفة واحدة: الطلب {lr} / العرض {rr} (−15)")
        else:
            missing.append(f"فارق كبير في الغرف: الطلب {lr} / العرض {rr} (−25)")
    elif not rr:
        missing.append("عدد الغرف غير محدد في العرض (−25)")

    # ── Property type (15 pts) ────────────────────────────────────────────────
    lt = _extract_type(full_lead)
    rt = listing.get('property_type') or _extract_type(full_listing)
    if lt and rt:
        if lt == rt:
            score += 15; reasons.append(f"نوع العقار: {rt}")
        else:
            missing.append(f"نوع مختلف: الطلب {lt} / العرض {rt} (−15)")
    elif lt and not rt:
        missing.append(f"نوع العقار غير محدد في العرض (−15)")

    # ── Budget (20 pts) ───────────────────────────────────────────────────────
    lb = _extract_price(full_lead)
    rp = listing.get('price') or _extract_price(full_listing)
    if lb and rp:
        if rp <= lb:
            score += 20; reasons.append(f"السعر مناسب ({rp:,} ≤ {lb:,})")
        elif rp <= lb * 1.15:
            score += 8;  reasons.append(f"السعر قريب ({rp:,})")
            missing.append(f"السعر أعلى قليلاً من الميزانية ({rp:,} > {lb:,}) (−12)")
        else:
            missing.append(f"السعر أعلى من الميزانية ({rp:,} > {lb:,}) (−20)")
    elif not rp:
        missing.append("السعر غير محدد في العرض (−20)")

    reason_str  = ' • '.join(reasons) if reasons else 'تطابق جغرافي'
    missing_str = ' • '.join(missing) if missing else ''
    return min(score, 100), reason_str, missing_str


@app.route("/match/<int:lead_id>")
def match_lead(lead_id):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, body, city, phone, phone_hidden, status
            FROM sanad.masaed_leads WHERE id = %s
        """, (lead_id,))
        cols = [d[0] for d in cur.description]
        row  = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "lead not found"}), 404
    lead = dict(zip(cols, row))

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, body, city, property_type, rooms, price,
                   phone, phone_hidden, url, status, scraped_at
            FROM sanad.masaed_listings
            WHERE status = 'active'
            ORDER BY CASE WHEN city = %s THEN 0 ELSE 1 END, scraped_at DESC
            LIMIT 200
        """, (lead.get('city') or '',))
        cols = [d[0] for d in cur.description]
        listings = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()

    scored = []
    for lst in listings:
        if lst.get("scraped_at"): lst["scraped_at"] = lst["scraped_at"].isoformat()
        sc, reason, missing = score_match(lead, lst)
        scored.append({**lst, "score": sc, "reason": reason, "missing": missing})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({"lead": lead, "matches": scored[:5], "total_searched": len(listings)})


@app.route("/negotiate/start", methods=["POST"])
def negotiate_start():
    """Start negotiation between a lead and a listing."""
    from negotiator import start_negotiation, ensure_table
    ensure_table()
    data       = request.get_json() or {}
    lead_id    = data.get("lead_id")
    listing_id = data.get("listing_id")
    if not lead_id or not listing_id:
        return jsonify({"error": "lead_id and listing_id required"}), 400

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id, phone, title, city FROM sanad.masaed_leads WHERE id=%s", (lead_id,))
        lead_row = cur.fetchone()
        cur.execute("SELECT id, phone, phone_hidden, title, city, price FROM sanad.masaed_listings WHERE id=%s", (listing_id,))
        lst_row  = cur.fetchone()
    conn.close()

    if not lead_row or not lst_row:
        return jsonify({"error": "lead or listing not found"}), 404

    lead_phone    = (lead_row[1] or "").replace("+","").replace(" ","")
    listing_phone = (lst_row[1] or "").replace("+","").replace(" ","")

    if not lead_phone or lst_row[2]:  # phone_hidden
        return jsonify({"error": "رقم أحد الطرفين غير متاح"}), 400

    result = start_negotiation(
        lead_id    = lead_id,
        listing_id = listing_id,
        lead_phone    = lead_phone,
        listing_phone = listing_phone,
        lead_name     = None,
        listing_title = lst_row[3],
        listing_city  = lst_row[4],
        listing_price = lst_row[5],
    )
    return jsonify(result)


@app.route("/negotiate/active")
def negotiate_active():
    """List all negotiations (any status)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, lead_phone, listing_phone, listing_title,
                       listing_city, listing_price, status, agreed_price,
                       needs_admin, admin_reason, lead_max_price, owner_min_price,
                       created_at
                FROM sanad.masaed_negotiations
                ORDER BY needs_admin DESC NULLS LAST, created_at DESC LIMIT 50
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            if r.get("created_at"): r["created_at"] = r["created_at"].isoformat()
        return jsonify({"negotiations": rows})
    except Exception as e:
        return jsonify({"negotiations": [], "error": str(e)})
    finally:
        conn.close()


@app.route("/negotiate/pending")
def negotiate_pending():
    """Count negotiations that need admin attention."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM sanad.masaed_negotiations
                WHERE needs_admin = true AND status = 'active'
            """)
            count = cur.fetchone()[0]
        return jsonify({"pending": count})
    except Exception as e:
        return jsonify({"pending": 0, "error": str(e)})
    finally:
        conn.close()


@app.route("/negotiate/<int:neg_id>/dismiss", methods=["POST"])
def negotiate_dismiss(neg_id):
    """Admin dismisses the needs_admin flag without closing."""
    from negotiator import _update_neg
    conn = get_conn()
    try:
        _update_neg(neg_id, conn, needs_admin=False, admin_reason=None)
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/negotiate/<int:neg_id>/agree", methods=["POST"])
def negotiate_agree(neg_id):
    """Admin manually closes a negotiation as agreed. Body: {agreed_price?}"""
    from negotiator import _close, _update_neg
    from bot import wa_send
    data = request.get_json() or {}
    agreed_price = data.get("agreed_price")
    if agreed_price is not None:
        try:
            agreed_price = int(agreed_price)
        except (ValueError, TypeError):
            agreed_price = None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, lead_phone, listing_phone, listing_title, listing_price, status
                FROM sanad.masaed_negotiations WHERE id = %s
            """, (neg_id,))
            row = cur.fetchone()

        if not row:
            return jsonify({"error": "negotiation not found"}), 404

        _, lead_phone, listing_phone, listing_title, listing_price, status = row

        if status in ('agreed', 'cancelled', 'failed'):
            return jsonify({"error": f"التفاوض مُغلق بالفعل ({status})"}), 400

        price = agreed_price if agreed_price is not None else listing_price
        _close(neg_id, "agreed", conn, agreed_price=price)
        _update_neg(neg_id, conn, needs_admin=False, admin_reason=None)
    finally:
        conn.close()

    import time as _time
    p_str     = f"{price:,} ر/سنة" if price else "متفق عليه"
    title_str = listing_title or "العقار"
    msg = (
        f"🎉 تم الاتفاق!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏠 {title_str}\n"
        f"💰 السعر: {p_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"الخطوات التالية:\n"
        f"١. تواصل مباشرة مع الطرف الآخر لترتيب المعاينة\n"
        f"٢. توقيع العقد وسداد الدفعة الأولى\n"
        f"٣. استلام المفاتيح\n\n"
        f"شكراً لاستخدامكم مساعد العقاري 🏠"
    )
    wa_send(lead_phone,    msg)
    _time.sleep(0.5)
    wa_send(listing_phone, msg)

    print(f"[AGREE] Admin closed neg #{neg_id} as agreed, price={price}", flush=True)
    return jsonify({"ok": True, "neg_id": neg_id, "agreed_price": price})


@app.route("/negotiate/<int:neg_id>/log")
def negotiate_log(neg_id):
    """Return full chat log for a negotiation."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, listing_title, listing_city, listing_price,
                   lead_phone, listing_phone, status, agreed_price,
                   lead_max_price, owner_min_price, proposed_price,
                   lead_accepted, owner_accepted, needs_admin, admin_reason,
                   chat_log, created_at
            FROM sanad.masaed_negotiations WHERE id = %s
        """, (neg_id,))
        row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    cols = ['id','listing_title','listing_city','listing_price',
            'lead_phone','listing_phone','status','agreed_price',
            'lead_max_price','owner_min_price','proposed_price',
            'lead_accepted','owner_accepted','needs_admin','admin_reason',
            'chat_log','created_at']
    d = dict(zip(cols, row))
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    d['chat_log'] = d['chat_log'] or []
    return jsonify(d)


@app.route("/negotiate/<int:neg_id>/propose", methods=["POST"])
def negotiate_propose(neg_id):
    """Admin proposes a middle price to both parties. Body: {proposed_price}"""
    from negotiator import _update_neg, _append_log
    from bot import wa_send

    data = request.get_json() or {}
    proposed_price = data.get("proposed_price")
    if not proposed_price:
        return jsonify({"error": "proposed_price required"}), 400
    try:
        proposed_price = int(proposed_price)
    except (ValueError, TypeError):
        return jsonify({"error": "invalid price"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lead_phone, listing_phone, listing_title, status
                FROM sanad.masaed_negotiations WHERE id = %s
            """, (neg_id,))
            row = cur.fetchone()

        if not row:
            return jsonify({"error": "not found"}), 404
        lead_phone, listing_phone, listing_title, status = row

        if status not in ('active', 'pending'):
            return jsonify({"error": f"التفاوض مُغلق ({status})"}), 400

        _update_neg(neg_id, conn,
                    proposed_price=proposed_price,
                    lead_accepted=False,
                    owner_accepted=False,
                    needs_admin=False,
                    admin_reason=None)

        p_str = f"{proposed_price:,}"
        msg = (
            f"💡 اقتراح من المسؤول\n"
            f"━━━━━━━━━━━━\n"
            f"السعر المقترح: {p_str} ر/سنة\n\n"
            f"هل توافق على هذا السعر؟\n"
            f"رد بـ نعم للموافقة أو لا للرفض."
        )
        _append_log(neg_id, "bot→مستأجر", msg, conn)
        wa_send(lead_phone, msg)
        time.sleep(0.5)
        _append_log(neg_id, "bot→مالك", msg, conn)
        wa_send(listing_phone, msg)
    finally:
        conn.close()

    print(f"[PROPOSE] #{neg_id} proposed {proposed_price} to both parties", flush=True)
    return jsonify({"ok": True, "proposed_price": proposed_price})


# ══════════════════════════════════════════════════════════════════════════════
# MATCHES — masaed_matches table + auto-matching + approve/reject
# ══════════════════════════════════════════════════════════════════════════════

def ensure_matches_table():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sanad.masaed_matches (
                id          SERIAL PRIMARY KEY,
                lead_id     INT NOT NULL,
                listing_id  INT NOT NULL,
                score       INT,
                reason      TEXT,
                missing     TEXT,
                status      TEXT DEFAULT 'pending',
                neg_id      INT,
                req_city    TEXT,
                req_budget  INT,
                req_phone   TEXT,
                lst_city    TEXT,
                lst_price   INT,
                lst_phone   TEXT,
                matched_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(lead_id, listing_id)
            )
        """)
        conn.commit()
    conn.close()


@app.route("/matches")
def list_matches():
    ensure_matches_table()
    status = request.args.get('status', 'pending')
    limit  = min(int(request.args.get('limit', 100)), 200)
    conn   = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, lead_id, listing_id, score, reason, missing,
                   status, neg_id, req_city, req_budget, req_phone,
                   lst_city, lst_price, lst_phone, matched_at
            FROM sanad.masaed_matches
            WHERE status = %s
            ORDER BY score DESC, matched_at DESC
            LIMIT %s
        """, (status, limit))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        if r.get('matched_at'):
            r['matched_at'] = r['matched_at'].isoformat()
    return jsonify({"ok": True, "success": True, "data": rows})


@app.route("/matches/auto", methods=["POST"])
def auto_match():
    """
    تشغيل المطابقة التلقائية.
    الأولوية: المسجّلون في v1 (masaed_registrations) ثم مصادر حراج (masaed_leads/listings).
    يعطي علامة is_registered للتوفيقات من v1.
    """
    ensure_matches_table()
    conn = get_conn()
    with conn.cursor() as cur:
        # ── طلبات المستأجرين: v1 أولاً، ثم حراج ──────────────────────────────
        cur.execute("""
            SELECT
                r.id            AS id,
                r.phone         AS phone,
                FALSE           AS phone_hidden,
                COALESCE(r.city, c.city) AS city,
                COALESCE(r.special_notes, '') || ' ' ||
                    COALESCE(r.preferred_districts::text,'') AS body,
                COALESCE(r.property_type,'') AS title,
                r.budget_annual AS budget,
                TRUE            AS is_registered
            FROM sanad.masaed_registrations r
            LEFT JOIN sanad.masaed_contacts c ON c.phone = r.phone
            WHERE r.type = 'wanted'
              AND r.status IN ('collecting','complete')
            ORDER BY r.created_at DESC LIMIT 30
        """)
        reg_lead_cols = [d[0] for d in cur.description]
        reg_leads = [dict(zip(reg_lead_cols, r)) for r in cur.fetchall()]

        cur.execute("""
            SELECT id, title, body, city, phone, phone_hidden,
                   FALSE AS is_registered
            FROM sanad.masaed_leads
            WHERE listing_type = 'wanted' AND status IN ('new', 'active')
            ORDER BY id DESC LIMIT 30
        """)
        lead_cols = [d[0] for d in cur.description]
        scraped_leads = [dict(zip(lead_cols, r)) for r in cur.fetchall()]

        # ── إعلانات الملاك: v1 أولاً، ثم حراج ───────────────────────────────
        cur.execute("""
            SELECT
                r.id            AS id,
                r.phone         AS phone,
                FALSE           AS phone_hidden,
                COALESCE(r.city, c.city) AS city,
                r.property_type AS property_type,
                r.rooms         AS rooms,
                r.price_annual  AS price,
                COALESCE(r.location_desc,'') AS body,
                r.property_type AS title,
                TRUE            AS is_registered
            FROM sanad.masaed_registrations r
            LEFT JOIN sanad.masaed_contacts c ON c.phone = r.phone
            WHERE r.type = 'listing'
              AND r.status IN ('collecting','complete')
            ORDER BY r.created_at DESC LIMIT 200
        """)
        reg_lst_cols = [d[0] for d in cur.description]
        reg_listings = [dict(zip(reg_lst_cols, r)) for r in cur.fetchall()]

        cur.execute("""
            SELECT id, title, body, city, property_type, rooms, price,
                   phone, phone_hidden, FALSE AS is_registered
            FROM sanad.masaed_listings
            WHERE status = 'active'
            ORDER BY scraped_at DESC LIMIT 300
        """)
        lst_cols = [d[0] for d in cur.description]
        scraped_listings = [dict(zip(lst_cols, r)) for r in cur.fetchall()]

    conn.close()

    # v1 مسجّلون أولاً، ثم حراج
    leads    = reg_leads    + scraped_leads
    listings = reg_listings + scraped_listings

    new_matches = 0
    for lead in leads:
        scored = []
        for lst in listings:
            sc, reason, missing = score_match(lead, lst)
            if sc >= 40:
                scored.append((sc, reason, missing, lst))
        scored.sort(key=lambda x: x[0], reverse=True)

        for sc, reason, missing, lst in scored[:3]:
            # رفع النسبة 5 نقاط إذا كلا الطرفين مسجّلان في v1
            both_registered = lead.get('is_registered') and lst.get('is_registered')
            final_sc = min(sc + (5 if both_registered else 0), 100)
            try:
                conn2 = get_conn()
                with conn2.cursor() as cur:
                    cur.execute("""
                        INSERT INTO sanad.masaed_matches
                            (lead_id, listing_id, score, reason, missing,
                             req_city, req_phone, lst_city, lst_price, lst_phone)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (lead_id, listing_id) DO NOTHING
                        RETURNING id
                    """, (
                        lead['id'], lst['id'], final_sc, reason, missing,
                        lead.get('city'), lead.get('phone'),
                        lst.get('city'), lst.get('price'), lst.get('phone'),
                    ))
                    if cur.fetchone():
                        new_matches += 1
                conn2.commit()
                conn2.close()
            except Exception as e:
                print(f"[MATCH AUTO] {e}", flush=True)
                try: conn2.close()
                except: pass

    src_note = f"v1:{len(reg_leads)}+haraj:{len(scraped_leads)} leads"
    return jsonify({"ok": True, "new_matches": new_matches,
                    "leads_processed": len(leads), "sources": src_note})


@app.route("/matches/<int:match_id>/approve", methods=["POST"])
def approve_match(match_id):
    """
    الإدارة توافق على توفيق.
    - إذا كلا الطرفين مسجّلان → ابدأ التفاوض مباشرة.
    - إذا أحدهم غير مسجّل → أرسل رسالة تعريف وطلب تسجيل، ضع الحالة awaiting_registration.
    """
    ensure_matches_table()
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, lead_id, listing_id, req_phone, lst_phone,
                   lst_city, lst_price, req_city
            FROM sanad.masaed_matches WHERE id = %s
        """, (match_id,))
        cols = [d[0] for d in cur.description]
        row  = cur.fetchone()

    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "match not found"}), 404
    m = dict(zip(cols, row))

    with conn.cursor() as cur:
        cur.execute("SELECT title FROM sanad.masaed_listings WHERE id=%s", (m['listing_id'],))
        r = cur.fetchone()
        listing_title = r[0] if r else None

        # فحص التسجيل
        cur.execute("""
            SELECT phone FROM sanad.masaed_registrations
            WHERE phone IN (%s, %s) AND status != 'abandoned'
        """, (m['req_phone'], m['lst_phone']))
        registered = {r2[0] for r2 in cur.fetchall()}
    conn.close()

    unregistered = []
    if m['req_phone'] and m['req_phone'] not in registered:
        unregistered.append(('tenant', m['req_phone']))
    if m['lst_phone'] and m['lst_phone'] not in registered:
        unregistered.append(('owner', m['lst_phone']))

    if unregistered:
        # أرسل رسالة تعريف لكل غير مسجّل
        from negotiator import wa_send as neg_wa
        city_str = m.get('lst_city') or m.get('req_city') or ''

        for role, phone in unregistered:
            if role == 'owner':
                msg = (
                    f"مرحباً 👋، وجدنا إعلانك في حراج"
                    + (f" عن {listing_title}" if listing_title else "") + ".\n"
                    f"نحن مساعد العقاري — خدمة مجانية تربطك بالمستأجر المناسب.\n"
                    f"لدينا طالب مهتم بعقارك الآن 🏠\n"
                    f"تحدّث معي وسنساعدك في إتمام الإيجار."
                )
            else:
                msg = (
                    f"مرحباً 👋، وجدنا طلبك في حراج"
                    + (f" عن إيجار في {city_str}" if city_str else "") + ".\n"
                    f"نحن مساعد العقاري — خدمة مجانية تساعدك في إيجاد العقار المناسب.\n"
                    f"لدينا عقار يناسب طلبك الآن 🔍\n"
                    f"تحدّث معي وسنساعدك في إيجاد ما تبحث عنه."
                )
            neg_wa(phone, msg)

        conn2 = get_conn()
        with conn2.cursor() as cur:
            cur.execute("""
                UPDATE sanad.masaed_matches
                SET status='awaiting_registration', updated_at=NOW()
                WHERE id=%s
            """, (match_id,))
            conn2.commit()
        conn2.close()

        roles = " و".join("المالك" if r == 'owner' else "المستأجر" for r, _ in unregistered)
        return jsonify({
            "ok": True,
            "success": True,
            "status": "awaiting_registration",
            "message": f"رسالة تعريف أُرسلت لـ{roles} — سيُبلَّغ عند اكتمال التسجيل",
            "unregistered": [p for _, p in unregistered],
        })

    # كلاهما مسجّل → ابدأ التفاوض
    from negotiator import start_negotiation, ensure_table as _ensure_neg
    _ensure_neg()
    result = start_negotiation(
        lead_id       = m['lead_id'],
        listing_id    = m['listing_id'],
        lead_phone    = m['req_phone'],
        listing_phone = m['lst_phone'],
        listing_title = listing_title,
        listing_city  = m.get('lst_city'),
        listing_price = m.get('lst_price'),
    )

    neg_id = result.get('neg_id')
    if not result.get('ok') and not neg_id:
        return jsonify({"ok": False, "error": result.get('error', 'فشل بدء التفاوض')})

    conn2 = get_conn()
    with conn2.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_matches
            SET status='session_created', neg_id=%s, updated_at=NOW()
            WHERE id=%s
        """, (neg_id, match_id))
        conn2.commit()
    conn2.close()
    return jsonify({"ok": True, "success": True, "neg_id": neg_id, "match_id": match_id})


@app.route("/matches/<int:match_id>/reject", methods=["POST"])
def reject_match(match_id):
    ensure_matches_table()
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_matches
            SET status='rejected', updated_at=NOW()
            WHERE id=%s
        """, (match_id,))
        conn.commit()
    conn.close()
    return jsonify({"ok": True, "success": True})


# ══════════════════════════════════════════════════════════════════════════════
# ACTIVITY LOG
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/logs")
def get_logs():
    limit = min(int(request.args.get('limit', 50)), 200)
    conn  = get_conn()
    logs  = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT updated_at,
                   CASE status
                     WHEN 'agreed'    THEN 'success'
                     WHEN 'cancelled' THEN 'warn'
                     WHEN 'failed'    THEN 'error'
                     ELSE 'info'
                   END,
                   '🤝 ' || COALESCE(listing_title,'عقار') || ' — ' || status
            FROM sanad.masaed_negotiations
            ORDER BY updated_at DESC LIMIT 30
        """)
        for ts, level, msg in cur.fetchall():
            logs.append({'time': ts.isoformat() if ts else None, 'level': level, 'message': msg})

        cur.execute("""
            SELECT created_at,
                   CASE WHEN status='completed' THEN 'success' ELSE 'info' END,
                   '📝 ' || COALESCE(type,'') || ': ' || COALESCE(name, phone,'?') || ' (' || status || ')'
            FROM sanad.masaed_registrations
            ORDER BY created_at DESC LIMIT 30
        """)
        for ts, level, msg in cur.fetchall():
            logs.append({'time': ts.isoformat() if ts else None, 'level': level, 'message': msg})

        cur.execute("""
            SELECT matched_at,
                   CASE WHEN score >= 70 THEN 'success' WHEN score >= 50 THEN 'warn' ELSE 'info' END,
                   '🎯 توفيق ' || COALESCE(req_city,'') || ' — نسبة ' || score || '٪ (' || status || ')'
            FROM sanad.masaed_matches
            ORDER BY matched_at DESC LIMIT 20
        """)
        for ts, level, msg in cur.fetchall():
            logs.append({'time': ts.isoformat() if ts else None, 'level': level, 'message': msg})
    conn.close()

    logs.sort(key=lambda x: x.get('time') or '', reverse=True)
    return jsonify({"success": True, "data": logs[:limit]})


@app.route("/stats")
def stats():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status='new') as new,
                COUNT(*) FILTER (WHERE phone_hidden=FALSE) as with_phone,
                COUNT(*) FILTER (WHERE phone_hidden=TRUE) as hidden_phone,
                COUNT(*) FILTER (WHERE status='contacted') as contacted,
                COUNT(*) as total,
                COUNT(DISTINCT city) as cities
            FROM sanad.masaed_leads
        """)
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
    conn.close()
    return jsonify(dict(zip(cols, row)))


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER — جهاز التوجيه المركزي
# ══════════════════════════════════════════════════════════════════════════════
#
#  كل رسالة واتساب واردة تمر من هنا بالترتيب:
#
#  1. مساعد الحافظ ← دائماً (يُحدّث last_seen ويُحضّر الذاكرة)
#
#  2. مساعد المفاوض ← إذا يوجد تفاوض نشط لهذا الرقم في masaed_negotiations
#     → يعالج الرسالة ويرد ← يتوقف هنا
#
#  3. مساعد المسجّل ← في كل الحالات الأخرى:
#     - شخص جديد لم يسبق تواصله
#     - شخص في منتصف تسجيل (collecting)
#     - شخص مسجّل يريد إضافة عقار جديد
#     - شخص انتهى تفاوضه ويريد التحدث مجدداً
#
# ══════════════════════════════════════════════════════════════════════════════

def _route_message(phone: str, text: str, media_url: str = None):
    from bot import get_contact, handle_message, wa_send as bot_wa
    from negotiator import handle_negotiation_message
    from editor import handle_edit_message, is_edit_request, get_editing_reg

    # ── الحافظ: دائماً ────────────────────────────────────────────────────────
    get_contact(phone)                         # upsert + last_seen

    # ── المفاوض: إذا تفاوض نشط (يقبل نصاً و/أو وسائط) ─────────────────────────
    if (text or media_url) and handle_negotiation_message(phone, text, media_url):
        return                                 # المفاوض تولّى

    # ── المعدّل: إذا جلسة تعديل جارية أو طلب تعديل صريح ────────────────────
    from bot import get_active_reg
    in_edit_session = get_editing_reg(phone) is not None
    in_reg_session  = get_active_reg(phone) is not None

    if text and (in_edit_session or (not in_reg_session and is_edit_request(text))):
        reply = handle_edit_message(phone, text)
        if reply:
            bot_wa(phone, reply)
            return                             # المعدّل تولّى

    # ── المسجّل: كل ما تبقى ───────────────────────────────────────────────────
    reply = handle_message(phone, text, media_url)
    if reply:
        bot_wa(phone, reply)


# ══════════════════════════════════════════════════════════════════════════════
# BOT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

ALLOWED_INSTANCE = os.getenv("MASAED_GREEN_INSTANCE", "")

@app.route("/bot/webhook", methods=["POST"])
def bot_webhook():
    from bot import parse_webhook, handle_message, wa_send
    data = request.get_json(silent=True) or {}

    # Validate request is from our Green API instance
    # Green API puts idInstance in instanceData.idInstance
    instance_id = (
        str(data.get("instanceData", {}).get("idInstance", "")) or
        str(data.get("instanceId", "")) or
        str(data.get("body", {}).get("instanceId", ""))
    )
    if ALLOWED_INSTANCE and instance_id and str(instance_id) != str(ALLOWED_INSTANCE):
        print(f"[WEBHOOK] Rejected unknown instance: {instance_id}")
        return jsonify({"ok": True})

    phone, text, media_url = parse_webhook(data)
    if not phone:
        return jsonify({"ok": True})
    try:
        _route_message(phone, text, media_url)
    except Exception as e:
        print(f"[BOT ERROR] {e}", flush=True)
    return jsonify({"ok": True})


@app.route("/bot/test", methods=["POST"])
def bot_test():
    """
    Dry-run test — NO real WhatsApp messages sent.
    Body: {phone, text, media_url?}
    Returns: {reply, wa_sent: [{to, text}, ...]}
    """
    from bot import _wa_test_local
    data      = request.get_json() or {}
    phone     = data.get("phone", "966500000000")
    text      = data.get("text", "") or ""
    media_url = data.get("media_url") or None
    if not text and not media_url:
        text = "مرحبا"

    # Dry-run: captures all wa_send calls, nothing reaches real phones
    _wa_test_local.active = True
    _wa_test_local.log    = []
    try:
        _route_message(phone, text or None, media_url)
        sent = list(_wa_test_local.log)
        # أول رسالة نصية (نتجاهل عناصر الوسائط التي لا تحوي text)
        first_text = next((m["text"] for m in sent if m.get("text")), None)
        return jsonify({
            "reply":   first_text,
            "wa_sent": sent,
            "dry_run": True,
        })
    finally:
        _wa_test_local.active = False
        _wa_test_local.log    = []


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATOR & CRITIC ENDPOINTS (مساعد المختبر المتقدم)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/lab/simulate", methods=["POST"])
def lab_simulate():
    """
    محاكاة تفاوض كاملة مع تقييم ذكي

    Body: {
      "reg_id": 1,
      "seeker_data": {...},
      "owner_data": {...}
    }

    Returns: {ok, job_id} — المحاكاة تعمل في الخلفية (async)
    استطلع /lab/simulate-status?job_id=... حتى status=done
    """
    from sim_engine import start_job, RateLimited

    data = request.get_json() or {}
    reg_id = data.get("reg_id")
    seeker_data = data.get("seeker_data", {})
    owner_data = data.get("owner_data", {})

    if not reg_id:
        return jsonify({"ok": False, "error": "reg_id مطلوب"}), 400

    try:
        job_id = start_job(reg_id, seeker_data, owner_data)
        print(f"[API] بدأت محاكاة async للطلب #{reg_id} (job={job_id})", flush=True)
        return jsonify({"ok": True, "job_id": job_id, "status": "running"}), 202
    except RateLimited as e:
        return jsonify({"ok": False, "error": str(e)}), 429
    except Exception as e:
        print(f"[API] خطأ في بدء المحاكاة: {e}", flush=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/lab/simulate-status", methods=["GET"])
def lab_simulate_status():
    """حالة محاكاة async عبر job_id."""
    from sim_engine import get_status

    job_id = request.args.get("job_id", "")
    if not job_id:
        return jsonify({"ok": False, "error": "job_id مطلوب"}), 400

    job = get_status(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job غير موجود (ربما انتهت صلاحيته)"}), 404

    resp = {"ok": True, "status": job.get("status"), "stage": job.get("stage")}
    if job.get("status") == "done":
        resp["result"] = job.get("result")
    elif job.get("status") == "error":
        resp["error"] = job.get("error")
        resp["result"] = job.get("result")
    return jsonify(resp)


@app.route("/bot/reset", methods=["POST"])
def bot_reset():
    """Reset a phone's active conversation for testing."""
    data = request.get_json() or {}
    phone = data.get("phone", "")
    if not phone:
        return jsonify({"error": "phone required"}), 400
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE sanad.masaed_registrations
            SET status = 'abandoned'
            WHERE phone = %s AND status = 'collecting'
        """, (phone,))
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/profile/<int:reg_id>")
def get_profile(reg_id):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, name, type, slug, city, district,
                   property_type, rooms, bathrooms, floor_num,
                   furnished, price_annual, price_monthly, for_family,
                   location_desc, photos, features,
                   budget_annual, preferred_districts, move_date, special_notes,
                   status, created_at
            FROM sanad.masaed_registrations WHERE id = %s
        """, (reg_id,))
        cols = [d[0] for d in cur.description]
        row  = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    reg = dict(zip(cols, row))
    if reg.get("created_at"):
        reg["created_at"] = reg["created_at"].isoformat()
    reg["wa_phone"] = os.getenv("MASAED_WA_PHONE", os.getenv("MASAED_GREEN_INSTANCE", ""))
    return jsonify(reg)


@app.route("/registrations")
def list_registrations():
    limit  = int(request.args.get("limit", 50))
    status = request.args.get("status", "all")
    conn = get_conn()
    with conn.cursor() as cur:
        if status != "all":
            cur.execute("""
                SELECT id, name, type, city, property_type, rooms,
                       price_annual, budget_annual, status, created_at
                FROM sanad.masaed_registrations
                WHERE status = %s ORDER BY created_at DESC LIMIT %s
            """, (status, limit))
        else:
            cur.execute("""
                SELECT id, name, type, city, property_type, rooms,
                       price_annual, budget_annual, status, created_at
                FROM sanad.masaed_registrations
                ORDER BY created_at DESC LIMIT %s
            """, (limit,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        if r.get("created_at"): r["created_at"] = r["created_at"].isoformat()
    return jsonify({"registrations": rows, "count": len(rows)})


@app.route("/photos/<path:filename>")
def serve_photo(filename):
    from flask import send_from_directory
    photos_dir = os.path.join(os.path.dirname(__file__), "photos")
    return send_from_directory(photos_dir, filename)


@app.route("/tg/callback", methods=["POST"])
def tg_callback():
    """Handle Telegram inline button presses from admin."""
    from negotiator import _close, _update_neg
    from bot import wa_send
    import tg_notify

    data = request.get_json(silent=True) or {}
    cb   = data.get("callback_query")
    if not cb:
        return jsonify({"ok": True})

    cb_id   = cb["id"]
    cb_data = cb.get("data", "")
    parts   = cb_data.split(":")
    action  = parts[0] if parts else ""
    neg_id  = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

    # أجب على Telegram فوراً لإزالة مؤشر التحميل
    tg_token = os.getenv("TELEGRAM_MASAED_BOT_TOKEN",
                         os.getenv("TELEGRAM_CROSSPOST_BOT_TOKEN", ""))
    if tg_token:
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg_token}/answerCallbackQuery",
                json={"callback_query_id": cb_id}, timeout=5
            )
        except Exception:
            pass

    if not neg_id:
        return jsonify({"ok": True})

    if action == "agree":
        from negotiator import _close as neg_close
        price = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() and parts[2] != "0" else None
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT lead_phone, listing_phone, lead_max_price, owner_min_price,
                           listing_price, status
                    FROM sanad.masaed_negotiations WHERE id = %s
                """, (neg_id,))
                row = cur.fetchone()

            if not row:
                return jsonify({"ok": True})
            lead_phone, listing_phone, lead_max, owner_min, listing_price, status = row

            if status in ("agreed", "cancelled", "failed"):
                return jsonify({"ok": True})

            if not price:
                if lead_max and owner_min:
                    price = round((lead_max + owner_min) / 2 / 500) * 500
                else:
                    price = listing_price

            neg_close(neg_id, "agreed", conn, agreed_price=price)
        finally:
            conn.close()
        p_str = f"{price:,} ر/سنة" if price else "متفق عليه"
        wa_send(lead_phone,   f"🎉 تم الاتفاق! السعر: {p_str}\nسيتواصل معك الطرف الآخر لإتمام الإجراءات.")
        time.sleep(0.5)
        wa_send(listing_phone, f"🎉 تم الاتفاق! السعر: {p_str}\nسيتواصل معك الطرف الآخر لإتمام الإجراءات.")
        print(f"[TG] Deal #{neg_id} agreed at {price}", flush=True)

    elif action == "view":
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT chat_log, listing_title FROM sanad.masaed_negotiations WHERE id=%s", (neg_id,))
            row = cur.fetchone()
        conn.close()
        if row:
            log, title = row
            log = log or []
            lines = [f"📋 <b>محادثة #{neg_id} — {title or 'العقار'}</b>\n"]
            for e in log[-12:]:
                r = e.get("role", "")
                t = (e.get("text") or "")[:80]
                if "bot" not in r and r:
                    lines.append(f"<b>{r}:</b> {t}")
            tg_notify._send("\n".join(lines))

    elif action == "manual":
        base = os.getenv("MASAED_BASE_URL", "https://masaed.wardyat.net")
        tg_notify._send(f"⚙️ تفاوض #{neg_id}\n{base}/sessions")

    # ignore → لا شيء

    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# CRON — مهام دورية يستدعيها cron أو n8n
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/cron/auto-match", methods=["POST"])
def cron_auto_match():
    """تشغيل المطابقة التلقائية + تنظيف التفاوضات المنتهية."""
    # 1. auto-match
    match_result = {"new_matches": 0, "leads_processed": 0}
    try:
        with app.test_request_context('/matches/auto', method='POST'):
            r = auto_match()
            d = r.get_json() if hasattr(r, 'get_json') else {}
            match_result = d
    except Exception as e:
        match_result["error"] = str(e)

    # 2. تنظيف التفاوضات المنتهية (expires_at < NOW)
    expired = 0
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE sanad.masaed_negotiations
                SET status = 'failed'
                WHERE status = 'active'
                  AND expires_at IS NOT NULL
                  AND expires_at < NOW()
                RETURNING id, lead_phone, listing_phone
            """)
            rows = cur.fetchall()
            expired = len(rows)
            conn.commit()
            # أبلغ الطرفين
            for neg_id, lead_ph, lst_ph in rows:
                try:
                    from negotiator import wa_send as neg_wa
                    msg = "⏰ انتهت مهلة التفاوض. يمكنك التواصل مجدداً لاحقاً."
                    neg_wa(lead_ph, msg)
                    neg_wa(lst_ph, msg)
                except Exception:
                    pass
        conn.close()
    except Exception as e:
        print(f"[CRON] expire cleanup: {e}", flush=True)

    return jsonify({"ok": True, "matches": match_result, "expired_closed": expired})


# ══════════════════════════════════════════════════════════════════════════════
# LAB — مساعد المختبر
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/lab/requests")
def lab_requests():
    """قائمة طلبات الباحثين المسجّلين — لاختيار سيناريو اختبار."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, name, phone, type, city, district,
                   property_type, rooms, budget_annual,
                   preferred_districts, special_notes, status, created_at
            FROM sanad.masaed_registrations
            WHERE type = 'wanted'
              AND status IN ('collecting', 'complete', 'completed')
            ORDER BY created_at DESC
            LIMIT 50
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
    return jsonify({"requests": rows, "count": len(rows)})


@app.route("/lab/scenario", methods=["POST"])
def lab_scenario():
    """
    معاينة ما سيحدث لو بدأنا التفاوض بهذا الطلب مع أرقام الاختبار.
    Body: {reg_id, seeker_phone, owner_phone, listing_price?}
    """
    data = request.get_json() or {}
    reg_id = data.get("reg_id")
    seeker_phone = normalize_phone(str(data.get("seeker_phone", "0548060060")))
    owner_phone  = normalize_phone(str(data.get("owner_phone", "")))
    listing_price = data.get("listing_price")

    if not reg_id or not owner_phone:
        return jsonify({"error": "reg_id و owner_phone مطلوبان"}), 400

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT name, city, district, property_type, rooms,
                   budget_annual, preferred_districts, special_notes
            FROM sanad.masaed_registrations WHERE id = %s
        """, (reg_id,))
        row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "الطلب غير موجود"}), 404

    name, city, district, prop_type, rooms, budget_annual, pref_dist, notes = row
    price = listing_price or budget_annual or 0
    price_str  = f"{price:,} ر/سنة" if price else "قابل للتفاوض"
    title_str  = f"{prop_type or 'شقة'} للإيجار"
    city_str   = city or ""
    name_str   = name or ""

    seeker_msg = (
        f"مرحباً{' ' + name_str if name_str else ''} 👋، أنا مساعد العقاري — وسيط إلكتروني.\n"
        f"وجدت طلبك المُسجَّل وربطتك بعرض يناسبه:\n"
        f"📍 {title_str}" + (f" — {city_str}" if city_str else "") + "\n"
        f"💰 {price_str}\n\n"
        f"تحدّث معي مباشرة — أنا الوسيط بينك وبين المالك."
    )

    owner_msg = (
        f"مرحباً 👋، أنا مساعد العقاري — وسيط إلكتروني.\n"
        f"ربطناك بمستأجر مهتم بعقارك"
        + (f" في {city_str}" if city_str else "") + ".\n"
        f"💰 {price_str}\n\n"
        f"تحدّث معي مباشرة — أنا الوسيط بينك وبين المستأجر."
    )

    return jsonify({
        "ok": True,
        "scenario": {
            "reg": {"name": name_str, "city": city_str, "district": district,
                    "rooms": rooms, "budget_annual": budget_annual},
            "seeker_phone": seeker_phone,
            "owner_phone": owner_phone,
            "listing_title": title_str,
            "listing_price": price,
            "seeker_msg": seeker_msg,
            "owner_msg": owner_msg,
        }
    })


@app.route("/lab/start", methods=["POST"])
def lab_start():
    """
    ابدأ تفاوضاً حقيقياً بأرقام الاختبار بدلاً من الأرقام الحقيقية.
    يسجّل كلا الطرفين تلقائياً ثم يستدعي start_negotiation.
    Body: {reg_id, seeker_phone, owner_phone, listing_price?}
    """
    from negotiator import start_negotiation, ensure_table as _ensure_neg
    _ensure_neg()

    data = request.get_json() or {}
    reg_id       = data.get("reg_id")
    seeker_phone = normalize_phone(str(data.get("seeker_phone", "0548060060")))
    owner_phone  = normalize_phone(str(data.get("owner_phone", "")))
    listing_price_override = data.get("listing_price")

    if not reg_id or not owner_phone:
        return jsonify({"error": "reg_id و owner_phone مطلوبان"}), 400
    if seeker_phone == owner_phone:
        return jsonify({"error": "رقم الباحث ورقم المالك يجب أن يكونا مختلفين"}), 400

    # 1. حمّل بيانات الطلب الأصلي
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT name, city, district, property_type, rooms,
                   budget_annual, special_notes
            FROM sanad.masaed_registrations WHERE id = %s
        """, (reg_id,))
        row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "الطلب غير موجود"}), 404

    name, city, district, prop_type, rooms, budget_annual, notes = row
    listing_price = listing_price_override or budget_annual or 0
    listing_title = f"{prop_type or 'شقة'} للإيجار" + (f" في {city}" if city else "")

    # 2. سجّل رقم الباحث التجريبي (UPSERT — لا يمس أرقام حقيقية)
    conn = get_conn()
    seeker_slug = f"lab-seeker-{seeker_phone}"
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_registrations
                (phone, name, type, slug, city, district, property_type, rooms,
                 budget_annual, special_notes, status)
            VALUES (%s, %s, 'wanted', %s, %s, %s, %s, %s, %s, %s, 'complete')
            ON CONFLICT (slug) DO UPDATE SET
                city=EXCLUDED.city, district=EXCLUDED.district,
                rooms=EXCLUDED.rooms, budget_annual=EXCLUDED.budget_annual,
                status='complete', updated_at=NOW()
            RETURNING id
        """, (seeker_phone, name or "باحث اختبار", seeker_slug,
              city, district, prop_type, rooms, budget_annual, notes))
        seeker_reg_id = cur.fetchone()[0]
        conn.commit()

    # 3. سجّل رقم المالك التجريبي (UPSERT)
    owner_slug = f"lab-owner-{owner_phone}"
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_registrations
                (phone, name, type, slug, city, district, property_type, rooms,
                 price_annual, status)
            VALUES (%s, %s, 'listing', %s, %s, %s, %s, %s, %s, 'complete')
            ON CONFLICT (slug) DO UPDATE SET
                city=EXCLUDED.city, property_type=EXCLUDED.property_type,
                rooms=EXCLUDED.rooms, price_annual=EXCLUDED.price_annual,
                status='complete', updated_at=NOW()
            RETURNING id
        """, (owner_phone, "مالك اختبار", owner_slug,
              city, district, prop_type, rooms, listing_price))
        owner_reg_id = cur.fetchone()[0]
        conn.commit()
    conn.close()

    # 4. ابدأ التفاوض — lead_id=seeker_reg_id, listing_id=owner_reg_id
    result = start_negotiation(
        lead_id       = seeker_reg_id,
        listing_id    = owner_reg_id,
        lead_phone    = seeker_phone,
        listing_phone = owner_phone,
        lead_name     = name or "باحث اختبار",
        listing_title = listing_title,
        listing_city  = city,
        listing_price = listing_price,
    )

    if result.get("ok"):
        return jsonify({
            "ok": True,
            "neg_id": result["neg_id"],
            "seeker_phone": seeker_phone,
            "owner_phone": owner_phone,
            "message": f"بدأ التفاوض #{result['neg_id']} — ستصل رسائل واتساب للرقمين الآن",
        })
    else:
        return jsonify({"ok": False, "error": result.get("error", "فشل بدء التفاوض")}), 500


if __name__ == "__main__":
    import tg_notify
    tg_notify.setup_webhook()
    port = int(os.getenv("SCRAPER_PORT", 5555))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
