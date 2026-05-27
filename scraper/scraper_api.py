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
    score = 0
    reasons = []

    # ── City match (40 pts) ──────────────────────────────────────────────────
    lc = (lead.get('city') or '').strip()
    rc = (listing.get('city') or '').strip()
    if lc and rc:
        if lc == rc:
            score += 40; reasons.append(f"نفس المدينة ({rc})")
        elif lc in rc or rc in lc:
            score += 20; reasons.append(f"مدينة قريبة")

    full_lead    = (lead.get('title','') + ' ' + (lead.get('body') or ''))[:600]
    full_listing = (listing.get('title','') + ' ' + (listing.get('body') or ''))[:600]

    # ── Rooms match (25 pts) ─────────────────────────────────────────────────
    lr = _extract_rooms(full_lead)
    rr = listing.get('rooms') or _extract_rooms(full_listing)
    if lr and rr:
        if lr == rr:
            score += 25; reasons.append(f"{rr} غرف متطابقة")
        elif abs(lr - rr) == 1:
            score += 10; reasons.append(f"غرف قريبة ({rr}±1)")

    # ── Property type (15 pts) ───────────────────────────────────────────────
    lt = _extract_type(full_lead)
    rt = listing.get('property_type') or _extract_type(full_listing)
    if lt and rt and lt == rt:
        score += 15; reasons.append(f"نوع العقار: {rt}")

    # ── Budget match (20 pts) ────────────────────────────────────────────────
    lb = _extract_price(full_lead)
    rp = listing.get('price') or _extract_price(full_listing)
    if lb and rp:
        if rp <= lb:
            score += 20; reasons.append(f"السعر مناسب ({rp:,} ≤ {lb:,})")
        elif rp <= lb * 1.15:
            score += 8; reasons.append(f"السعر قريب ({rp:,})")

    reason_str = ' • '.join(reasons) if reasons else 'تطابق جغرافي'
    return min(score, 100), reason_str


@app.route("/match/<int:lead_id>")
def match_lead(lead_id):
    conn = get_conn()

    # Load lead
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, body, city, phone, status
            FROM sanad.masaed_leads WHERE id = %s
        """, (lead_id,))
        cols = [d[0] for d in cur.description]
        row  = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "lead not found"}), 404

    lead = dict(zip(cols, row))

    # Load candidate listings (same city preferred, all active)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, body, city, property_type, rooms, price,
                   phone, phone_hidden, url, status, scraped_at
            FROM sanad.masaed_listings
            WHERE status = 'active'
            ORDER BY
                CASE WHEN city = %s THEN 0 ELSE 1 END,
                scraped_at DESC
            LIMIT 200
        """, (lead.get('city') or '',))
        cols = [d[0] for d in cur.description]
        listings = [dict(zip(cols, r)) for r in cur.fetchall()]

    conn.close()

    # Score and rank
    scored = []
    for lst in listings:
        if lst.get("scraped_at"): lst["scraped_at"] = lst["scraped_at"].isoformat()
        sc, reason = score_match(lead, lst)
        scored.append({**lst, "score": sc, "reason": reason})

    scored.sort(key=lambda x: x["score"], reverse=True)
    top5 = scored[:5]

    return jsonify({
        "lead": lead,
        "matches": top5,
        "total_searched": len(listings)
    })


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


if __name__ == "__main__":
    port = int(os.getenv("SCRAPER_PORT", 5555))
    app.run(host="0.0.0.0", port=port, debug=False)
