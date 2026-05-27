#!/usr/bin/env python3
"""
مساعد — Scraper API
Flask API so n8n and the dashboard can trigger scraping and receive leads.
Port 5555
"""
import os, re, json, asyncio
from urllib.parse import urlparse
from flask import Flask, jsonify, request
import psycopg2

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


@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return '', 204


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

    full_lead    = (lead.get('title','') + ' ' + (lead.get('body') or ''))[:600]
    full_listing = (listing.get('title','') + ' ' + (listing.get('body') or ''))[:600]

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
    """List active negotiations."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, lead_phone, listing_phone, listing_title,
                       listing_city, listing_price, status, created_at
                FROM sanad.masaed_negotiations
                ORDER BY created_at DESC LIMIT 50
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
# BOT ENDPOINTS — مساعد المسجّل
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
        # Route: negotiation takes priority over registration bot
        from negotiator import handle_negotiation_message
        if text and handle_negotiation_message(phone, text):
            pass  # handled by negotiator
        else:
            reply = handle_message(phone, text, media_url)
            if reply:
                wa_send(phone, reply)
    except Exception as e:
        print(f"[BOT ERROR] {e}", flush=True)
    return jsonify({"ok": True})


@app.route("/bot/test", methods=["POST"])
def bot_test():
    """Test bot without WhatsApp — body: {phone, text, media_url?}"""
    from bot import handle_message, wa_send
    from negotiator import handle_negotiation_message
    data      = request.get_json() or {}
    phone     = data.get("phone", "966500000000")
    text      = data.get("text", "") or ""
    media_url = data.get("media_url") or None
    if not text and not media_url:
        text = "مرحبا"
    # Same routing as webhook
    if text and handle_negotiation_message(phone, text):
        return jsonify({"reply": f"[negotiator handled — WA sent to {phone}]"})
    reply = handle_message(phone, text or None, media_url)
    return jsonify({"reply": reply})


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


if __name__ == "__main__":
    port = int(os.getenv("SCRAPER_PORT", 5555))
    app.run(host="0.0.0.0", port=port, debug=False)
