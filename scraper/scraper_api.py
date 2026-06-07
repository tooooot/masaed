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


@app.route("/deal/candidates/<int:lead_id>")
def deal_candidates(lead_id):
    """🧠 الصفقة الكاملة: طلب الباحث (رابط+نص+نية) + أفضل 5 عروض، كلٌّ مفهوم بعمق
    ومُقيَّم توافقه الحقيقي عبر LLM. ?deep=0 يرجع regex فقط فوراً (بلا LLM)."""
    deep = request.args.get("deep", "1") == "1"
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""SELECT id, url, title, body, city, phone, phone_hidden, listing_type, status
                       FROM sanad.masaed_leads WHERE id=%s""", (lead_id,))
        cols = [d[0] for d in cur.description]; row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"ok": False, "error": "الطلب غير موجود"}), 404
        lead = dict(zip(cols, row))

        cur.execute("""SELECT id, title, body, city, property_type, rooms, price,
                              phone, phone_hidden, url, status
                       FROM sanad.masaed_listings WHERE status='active'
                       ORDER BY CASE WHEN city=%s THEN 0 ELSE 1 END, scraped_at DESC LIMIT 200""",
                    (lead.get('city') or '',))
        lcols = [d[0] for d in cur.description]
        listings = [dict(zip(lcols, r)) for r in cur.fetchall()]
    conn.close()

    scored = []
    for lst in listings:
        sc, reason, missing = score_match(lead, lst)
        scored.append({**lst, "score": sc, "reason": reason, "missing": missing})
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:5]

    request_understanding = None
    if deep and top:
        import comprehension
        from concurrent.futures import ThreadPoolExecutor
        lead_text = " ".join(str(lead.get(k) or "") for k in ("title", "body")).strip()
        try:
            request_understanding = comprehension.extract_profile(
                lead_text, role="seeker", source="lead", ext_id=lead_id)
        except Exception as e:
            print(f"[DEAL] فهم الطلب فشل: {e}", flush=True)

        def _work(lst):
            try:
                otext = " ".join(str(lst.get(k) or "") for k in ("title", "body")).strip()
                prof = comprehension.extract_profile(otext, role="listing",
                                                     source="listing", ext_id=lst["id"])
                asmt = comprehension.assess(request_understanding or {}, prof)
                return lst["id"], prof, asmt
            except Exception as e:
                print(f"[DEAL] فهم عرض {lst.get('id')} فشل: {e}", flush=True)
                return lst["id"], None, None

        with ThreadPoolExecutor(max_workers=4) as ex:
            for lid, prof, asmt in ex.map(_work, top):
                for t in top:
                    if t["id"] == lid:
                        t["understanding"] = prof
                        t["assessment"] = asmt
        # رتّب نهائياً بحكم الفهم العميق إن توفّر (وإلا regex)
        def _rank(t):
            a = t.get("assessment") or {}
            return a.get("score") if isinstance(a.get("score"), (int, float)) else t.get("score", 0)
        top.sort(key=_rank, reverse=True)

    return jsonify({"ok": True, "lead": {**lead, "understanding": request_understanding},
                    "candidates": top, "deep": deep})


def _deal_stage(gate_status, neg_status):
    """يحسب مرحلة الصفقة في خط الأنابيب من حالة البوّابة + التفاوض."""
    if neg_status == "agreed":
        return "agreed", "✅ اتفاق"
    if neg_status in ("cancelled", "failed"):
        return "failed", "✋ متعثّرة"
    if neg_status == "active":
        return "negotiating", "💬 تفاوض جارٍ"
    if gate_status == "rejected":
        return "rejected", "❌ مرفوضة"
    if gate_status in ("approved", "consumed"):
        return "approved", "✅ معتمدة (لم يبدأ التنفيذ)"
    if gate_status == "pending_review":
        return "review", "🧪 محاكاة بانتظار المراجعة"
    return "candidate", "🔎 مرشّحة"


_TEST_PREFIXES = ("966588", "966577000", "966577123", "9665000000")
_TEST_PHONES = {"966536882728", "966548060060", "966500000000", "966500000001",
                "966550688470", "966558197191", "966511110001",
                "966599990001", "966599990002"}
_TEST_URL_MARK = "masaed.wardyat.net/test"   # رابط بيانات الاختبار (placeholder)


def _is_test_phone(p):
    p = (p or "").strip()
    return p in _TEST_PHONES or any(p.startswith(x) for x in _TEST_PREFIXES)


@app.route("/deals/list")
def deals_list():
    """قائمة الصفقات (كل زوج باحث↔مالك مرّ بالبوّابة أو التفاوض) + مرحلتها الحالية.
    تُخفى الصفقات التجريبية المزروعة افتراضياً (?include_test=1 لإظهارها)."""
    include_test = request.args.get("include_test", "0") == "1"
    deals = {}
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""SELECT seeker_phone, owner_phone, listing_id, gate_status,
                              sim_score, sim_status, created_at, decided_at, id
                       FROM sanad.masaed_deal_gate ORDER BY created_at DESC LIMIT 300""")
        for r in cur.fetchall():
            deals[(r[0], r[1])] = {
                "seeker_phone": r[0], "owner_phone": r[1], "listing_id": r[2],
                "gate_status": r[3], "sim_score": r[4], "sim_status": r[5],
                "ts": (r[7] or r[6]), "neg_status": None, "neg_id": None,
                "title": None, "agreed_price": None, "gate_id": r[8],
            }
        cur.execute("""SELECT lead_phone, listing_phone, listing_id, status, agreed_price,
                              listing_title, id, created_at, updated_at
                       FROM sanad.masaed_negotiations ORDER BY created_at DESC LIMIT 300""")
        for r in cur.fetchall():
            key = (r[0], r[1])
            d = deals.setdefault(key, {
                "seeker_phone": r[0], "owner_phone": r[1], "listing_id": r[2],
                "gate_status": None, "sim_score": None, "sim_status": None, "ts": None,
            })
            d["neg_status"] = r[3]; d["agreed_price"] = r[4]
            d["title"] = d.get("title") or r[5]; d["neg_id"] = r[6]
            d["ts"] = d.get("ts") or r[8] or r[7]
    conn.close()

    out = []
    test_hidden = 0
    for d in deals.values():
        is_test = _is_test_phone(d.get("seeker_phone")) or _is_test_phone(d.get("owner_phone"))
        if is_test and not include_test:
            test_hidden += 1
            continue
        stage, label = _deal_stage(d.get("gate_status"), d.get("neg_status"))
        ts = d.get("ts")
        deal_no = d.get("gate_id") or d.get("neg_id")
        out.append({**d, "stage": stage, "stage_label": label, "test": is_test,
                    "deal_no": deal_no, "ts": ts.isoformat() if ts else None})
    out.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return jsonify({"ok": True, "deals": out, "test_hidden": test_hidden})


@app.route("/deal/timeline")
def deal_timeline():
    """🔭 السلسلة الكاملة لصفقة: طلب → عرض → فهم → محاكاة → قرار → تنفيذ."""
    import deal_gate, comprehension  # noqa
    s = deal_gate.norm(request.args.get("seeker_phone", ""))
    o = deal_gate.norm(request.args.get("owner_phone", ""))
    if not s or not o:
        return jsonify({"ok": False, "error": "seeker_phone و owner_phone مطلوبان"}), 400

    conn = get_conn()
    _ensure_listing_photos_col(conn)
    tl = {"seeker_phone": s, "owner_phone": o}

    def _comp(cur, source, ext_id):
        if ext_id is None:
            return None
        cur.execute("SELECT profile FROM sanad.masaed_comprehension WHERE source=%s AND ext_id=%s",
                    (source, str(ext_id)))
        r = cur.fetchone()
        return r[0] if r else None

    def _party_db(cur, phone, ptype):
        """اسم العميل من الحافظ + تسجيله المؤكّد في قاعدتنا (إن وُجد)."""
        cur.execute("SELECT name, profile, notes FROM sanad.masaed_contacts WHERE phone=%s", (phone,))
        c = cur.fetchone()
        name = c[0] if c else None
        keeper = {"profile": (c[1] if c else None), "notes": (c[2] if c else None)}
        pricec = "budget_annual" if ptype == "wanted" else "price_annual"
        cur.execute(f"""SELECT id,name,city,district,property_type,rooms,{pricec},
                               special_notes,status,slug,created_at
                        FROM sanad.masaed_registrations
                        WHERE phone=%s AND type=%s AND status<>'abandoned'
                        ORDER BY created_at DESC LIMIT 1""", (phone, ptype))
        g = cur.fetchone()
        reg = None
        if g:
            reg = {"id": g[0], "name": g[1], "city": g[2], "district": g[3],
                   "property_type": g[4], "rooms": g[5], "price": g[6],
                   "special_notes": g[7], "status": g[8], "slug": g[9],
                   "created_at": g[10].isoformat() if g[10] else None}
        return name, reg, keeper

    with conn.cursor() as cur:
        # 4) القرار (البوّابة) — أول ما نجلبه لأنه يحمل listing_id
        cur.execute("""SELECT gate_status,sim_score,sim_status,fact_errors,decided_at,decided_by,created_at,listing_id
                       FROM sanad.masaed_deal_gate WHERE seeker_phone=%s AND owner_phone=%s""", (s, o))
        r = cur.fetchone()
        lid = None
        if r:
            lid = r[7]
            tl["decision"] = {"gate_status": r[0], "sim_score": r[1], "sim_status": r[2],
                              "fact_errors": r[3], "decided_at": r[4].isoformat() if r[4] else None,
                              "decided_by": r[5], "created_at": r[6].isoformat() if r[6] else None}
        else:
            tl["decision"] = None

        # 5) التنفيذ (التفاوض) — مصدر إضافي لـ listing_id
        cur.execute("""SELECT id,status,agreed_price,listing_title,chat_log,created_at,updated_at,listing_id
                       FROM sanad.masaed_negotiations WHERE lead_phone=%s AND listing_phone=%s
                       ORDER BY created_at DESC LIMIT 1""", (s, o))
        r = cur.fetchone()
        if r:
            lid = lid or r[7]
            chat = r[4] if isinstance(r[4], list) else []
            tl["execution"] = {"neg_id": r[0], "status": r[1], "agreed_price": r[2],
                               "title": r[3], "messages": chat[-12:] if chat else [],
                               "created_at": r[5].isoformat() if r[5] else None,
                               "updated_at": r[6].isoformat() if r[6] else None}
        else:
            tl["execution"] = None

        # 3) المحاكاة (آخر تشغيل محفوظ) — بالزوج أو بـ listing_id (أوثق عند اختلاف الأرقام)
        cur.execute("""SELECT result, created_at, listing_id FROM sanad.masaed_sim_runs
                       WHERE seeker_phone=%s AND (owner_phone=%s OR (listing_id IS NOT NULL AND listing_id=%s))
                       ORDER BY created_at DESC LIMIT 1""", (s, o, lid))
        r = cur.fetchone()
        if r:
            lid = lid or r[2]
            tl["simulation"] = {"result": r[0], "at": r[1].isoformat() if r[1] else None}
        else:
            tl["simulation"] = None

        # 1) الطلب
        cur.execute("""SELECT id,url,title,body,city,phone FROM sanad.masaed_leads
                       WHERE phone=%s AND listing_type='wanted' ORDER BY scraped_at DESC LIMIT 1""", (s,))
        r = cur.fetchone()
        if r:
            tl["request"] = {"lead_id": r[0], "url": r[1], "title": r[2], "body": r[3],
                             "city": r[4], "phone": r[5], "understanding": _comp(cur, "lead", r[0])}
        else:
            tl["request"] = {"phone": s}
        _nm, _reg, _kp = _party_db(cur, s, "wanted")
        tl["request"]["name"] = _nm
        tl["request"]["registration"] = _reg
        tl["request"]["keeper"] = _kp

        # 2) العرض — بـ listing_id أولاً (أوثق)، وإلا برقم المالك
        if lid:
            cur.execute("""SELECT id,url,title,body,city,price,phone,property_type,rooms,photos,location,advertiser
                           FROM sanad.masaed_listings WHERE id=%s""", (lid,))
        else:
            cur.execute("""SELECT id,url,title,body,city,price,phone,property_type,rooms,photos,location,advertiser
                           FROM sanad.masaed_listings WHERE phone=%s ORDER BY id DESC LIMIT 1""", (o,))
        r = cur.fetchone()
        if r:
            tl["offer"] = {"listing_id": r[0], "url": r[1], "title": r[2], "body": r[3],
                           "city": r[4], "price": r[5], "phone": r[6], "property_type": r[7],
                           "photos": r[9] or [], "location": r[10], "advertiser": r[11],
                           "rooms": r[8], "understanding": _comp(cur, "listing", r[0])}
        else:
            tl["offer"] = {"phone": o}
        _nm, _reg, _kp = _party_db(cur, o, "listing")
        tl["offer"]["name"] = _nm
        tl["offer"]["registration"] = _reg
        tl["offer"]["keeper"] = _kp
    conn.close()

    stage, label = _deal_stage((tl.get("decision") or {}).get("gate_status"),
                               (tl.get("execution") or {}).get("status"))
    tl["stage"] = stage; tl["stage_label"] = label
    return jsonify({"ok": True, "timeline": tl})


def _ensure_listing_photos_col(conn):
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE sanad.masaed_listings ADD COLUMN IF NOT EXISTS photos JSONB DEFAULT '[]'")
        cur.execute("ALTER TABLE sanad.masaed_listings ADD COLUMN IF NOT EXISTS location JSONB")
        cur.execute("ALTER TABLE sanad.masaed_listings ADD COLUMN IF NOT EXISTS advertiser TEXT")
        conn.commit()


@app.route("/listings/<int:lst_id>/photos", methods=["GET", "POST"])
def listing_photos(lst_id):
    """🖼️📍 صور وموقع العقار: تُسحب من الإعلان (Playwright) وتُخزَّن في masaed_listings.
    GET يرجع المخزّن (ويسحب إن كان فارغاً)؛ POST يُجبر إعادة السحب."""
    conn = get_conn()
    _ensure_listing_photos_col(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT url, photos, location, advertiser FROM sanad.masaed_listings WHERE id=%s", (lst_id,))
        row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "العرض غير موجود"}), 404
    url, photos, location, advertiser = row[0], (row[1] or []), row[2], row[3]
    force = request.method == "POST"
    if url and (force or not photos):
        try:
            from haraj_scraper import scrape_single_url_sync
            data = scrape_single_url_sync(url) or {}
            imgs = [i.get("url") for i in (data.get("images") or []) if isinstance(i, dict) and i.get("url")]
            loc = data.get("location")
            adv = data.get("advertiser")
            if imgs or loc or adv:
                photos = imgs or photos
                location = loc or location
                advertiser = adv or advertiser
                with conn.cursor() as cur:
                    cur.execute("UPDATE sanad.masaed_listings SET photos=%s, location=%s, advertiser=%s WHERE id=%s",
                                (json.dumps(photos, ensure_ascii=False),
                                 json.dumps(location, ensure_ascii=False) if location else None,
                                 advertiser, lst_id))
                    conn.commit()
        except Exception as e:
            print(f"[PHOTOS] تعذّر سحب صور/موقع العرض {lst_id}: {e}", flush=True)
    conn.close()
    return jsonify({"ok": True, "photos": photos, "count": len(photos),
                    "location": location, "advertiser": advertiser, "source": url})


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


def _phone_variants(phones):
    """وسّع أرقام الساندبوكس لتشمل الصيغتين السعوديتين: 966XXXXXXXXX و0XXXXXXXXX."""
    out = set()
    for p in phones:
        p = p.strip()
        if not p:
            continue
        out.add(p)
        core = p[3:] if p.startswith("966") else (p[1:] if p.startswith("0") else p)
        if core.isdigit() and len(core) == 9:       # رقم سعودي صالح
            out.add("966" + core)
            out.add("0" + core)
    return list(out)


def _build_deal_sim(seeker_phone, listing_id, listing_phone):
    """يبني مدخلات محاكاة الصفقة (مشترك بين /lab/simulate-deal و /cron/auto-simulate).
    يُرجع dict فيه seeker_data/owner_data/extras/offer/seeker، أو None إن لا عرض."""
    from deal_preparer import _assemble
    conn = get_conn()
    try:
        deal = _assemble(seeker_phone, listing_id, listing_phone, conn)
    finally:
        conn.close()
    offer = deal.get("offer") or {}
    seeker = deal.get("seeker") or {}
    if not offer:
        return None
    owner_data = {
        "phone": offer.get("phone"), "title": offer.get("title"),
        "city": offer.get("city"), "rooms": offer.get("rooms"),
        "price": offer.get("price"), "property_type": offer.get("property_type"),
        "specs": offer.get("body") or "مواصفات عادية", "url": offer.get("url"),
    }
    seeker_data = {
        "name": seeker.get("name"), "type": "wanted",
        "city": seeker.get("city"), "rooms": seeker.get("rooms"),
        "budget_annual": seeker.get("budget_annual"),
        "for_family": seeker.get("for_family"),
        "special_notes": f"يبحث في {seeker.get('city') or '—'}",
        "url": seeker.get("url"),
    }
    offer_text = " ".join(str(offer.get(k) or "") for k in ("title", "body")).strip()
    if offer.get("advertiser"):
        offer_text += f"\nاسم المعلن/صاحب الترخيص: {offer.get('advertiser')}"
    extras = {
        "comprehend": bool(offer_text),
        "seeker_phone": seeker_phone,
        "owner_phone": offer.get("phone") or listing_phone,
        "listing_id": listing_id,
        "offer_text": offer_text, "offer_source": "listing", "offer_id": offer.get("id"),
        "seeker_profile": {
            "city": seeker.get("city"), "rooms": seeker.get("rooms"),
            "budget": seeker.get("budget_annual"), "for_family": seeker.get("for_family"),
            "property_type": seeker.get("property_type"),
        },
        "seeker_text": (" ".join(str(seeker.get(k) or "") for k in ("title", "body")).strip()
                        or (seeker.get("special_notes") or "").strip() or None),
        "seeker_source": "lead" if seeker.get("lead_id") else "registration",
        "seeker_id": seeker.get("lead_id") or seeker.get("phone"),
    }
    try:
        conn_p = get_conn()
        _ensure_listing_photos_col(conn_p)
        with conn_p.cursor() as cur:
            cur.execute("SELECT photos FROM sanad.masaed_listings WHERE id=%s", (listing_id,))
            rr = cur.fetchone()
            # بدائل: أفضل عروض أخرى مطابقة لنفس الباحث (إن لم يرغب بهذا العرض)
            cur.execute("""SELECT l.title, l.price, l.city FROM sanad.masaed_matches m
                           JOIN sanad.masaed_listings l ON l.id=m.listing_id
                           WHERE m.req_phone=%s AND m.listing_id<>%s AND m.status='pending'
                           ORDER BY m.score DESC NULLS LAST LIMIT 2""",
                        (seeker_phone, listing_id))
            alts = [{"title": a[0], "price": a[1], "city": a[2]} for a in cur.fetchall()]
        conn_p.close()
        extras["owner_has_photos"] = bool(rr and rr[0])
        extras["alternatives"] = alts
    except Exception:
        extras["owner_has_photos"] = False
        extras["alternatives"] = []
    return {"reg_id": 41, "seeker_data": seeker_data, "owner_data": owner_data,
            "extras": extras, "offer": offer, "seeker": seeker}


@app.route("/lab/simulate-deal", methods=["POST"])
def lab_simulate_deal():
    """محاكاة صفقة قبل بدئها: يحمّل حقائق الإعلان+الطلب، يشغّل الوكلاء الثلاثة
    (مالك يعرف الإعلان / مستأجر / وسيط)، ويكشف أخطاء الحقائق. async عبر job_id."""
    from sim_engine import start_job, RateLimited
    data = request.get_json() or {}
    seeker_phone = (data.get("seeker_phone") or "").strip()
    listing_id = data.get("listing_id")
    listing_phone = (data.get("listing_phone") or "").strip() or None
    if not seeker_phone:
        return jsonify({"ok": False, "error": "seeker_phone مطلوب"}), 400

    inp = _build_deal_sim(seeker_phone, listing_id, listing_phone)
    if not inp:
        return jsonify({"ok": False, "error": "لا يوجد عرض مرتبط بهذه الصفقة"}), 404
    seeker_data = inp["seeker_data"]; owner_data = inp["owner_data"]
    extras = inp["extras"]; offer = inp["offer"]; seeker = inp["seeker"]
    reg_id = data.get("reg_id") or inp["reg_id"]
    if (data.get("mode") or request.args.get("mode")) == "hard":
        extras["mode"] = "hard"   # 🔥 وضع أسوأ حالة (شخصيات عدائية)

    try:
        job_id = start_job(reg_id, seeker_data, owner_data, extras=extras)
        print(f"[DEAL-SIM] محاكاة صفقة {seeker_phone}↔{offer.get('phone')} (job={job_id})", flush=True)
        return jsonify({"ok": True, "job_id": job_id, "status": "running",
                        "deal": {"offer": offer, "seeker": seeker}}), 202
    except RateLimited as e:
        return jsonify({"ok": False, "error": str(e)}), 429
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/route/trace")
def route_trace_feed():
    """آخر الرسائل مُفكّكة إلى طبقات (الاتجاه/السياق/النية/الموظف)."""
    import route_trace
    return jsonify({"trace": route_trace.recent(60)})


@app.route("/negotiate/cleanup-tests", methods=["POST"])
def negotiate_cleanup_tests():
    """احذف التفاوضات التجريبية: عنوان فيه 🧪 أو «اختبار»، أو paused_test،
    أو أرقام SIM، أو أرقام الساندبوكس بالصيغتين (966 و05xx)."""
    sandbox = _phone_variants(
        os.getenv("MASAED_SANDBOX_PHONES", "").split(","))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM sanad.masaed_negotiations
                WHERE listing_title LIKE '%%🧪%%'
                   OR listing_title LIKE '%%اختبار%%'
                   OR status = 'paused_test'
                   OR lead_phone LIKE 'SIM%%' OR listing_phone LIKE 'SIM%%'
                   OR lead_phone = ANY(%s) OR listing_phone = ANY(%s)
            """, (sandbox, sandbox))
            deleted = cur.rowcount
            conn.commit()
        print(f"[CLEANUP] حُذِف {deleted} تفاوضاً تجريبياً", flush=True)
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


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

    # 🚦 البوّابة الإلزامية: لا موافقة (ولا حتى رسالة تعريف) قبل محاكاة معتمدة لهذا الزوج.
    import deal_gate
    if not deal_gate.check(m.get("req_phone"), m.get("lst_phone"), conn):
        conn.close()
        return jsonify({
            "ok": False,
            "gate": "blocked",
            "error": ("⛔ لم تُحاكَ هذه الصفقة وتُعتمَد بعد. شغّل «🧪 محاكاة الصفقة»، "
                      "راجِع النتيجة، ثم اعتمدها قبل بدء التواصل."),
            "seeker_phone": m.get("req_phone"),
            "owner_phone": m.get("lst_phone"),
            "listing_id": m.get("listing_id"),
        }), 403

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
        # مبادرة المنتج: مبرر + إثبات (رابط إعلان الطرف نفسه) + طلب الرغبة، ثم التسجيل.
        # لا نربطه بالطرف الآخر ولا نتفاوض قبل التسجيل (خط المنتج الصحيح).
        from negotiator import wa_send as neg_wa
        conn_u = get_conn()
        with conn_u.cursor() as cur:
            cur.execute("SELECT url FROM sanad.masaed_listings WHERE id=%s", (m['listing_id'],))
            r = cur.fetchone(); owner_url = r[0] if r else None
            cur.execute("""SELECT url FROM sanad.masaed_leads
                           WHERE phone=%s AND listing_type='wanted'
                           ORDER BY scraped_at DESC LIMIT 1""", (m['req_phone'],))
            r = cur.fetchone(); seeker_url = r[0] if r else None
        conn_u.close()

        for role, phone in unregistered:
            if role == 'owner':
                msg = (
                    "السلام عليكم 👋 أنا «مساعد» العقاري. "
                    "لاحظنا إعلانك المنشور على حراج"
                    + (f":\n{owner_url}" if owner_url else "") + "\n"
                    "وقد يطابق طلب باحث لدينا في قاعدتنا — نودّ التأكد معك. "
                    "هل العقار ما زال متاحاً؟"
                )
            else:
                msg = (
                    "السلام عليكم 👋 أنا «مساعد» العقاري. "
                    "لاحظنا طلبك المنشور على حراج"
                    + (f":\n{seeker_url}" if seeker_url else "") + "\n"
                    "وقد يطابق عرضاً متاحاً لدينا في قاعدتنا — نودّ التأكد معك. "
                    "هل ما زلت تبحث؟"
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
    """المنسّق (Orchestrator): ينادي الموظفين ككتل متكاملة، لا يمدّ يده في تفاصيلهم."""
    from memory import Memory               # 🧠 الحافظ
    from registrar import Registrar         # 📝 المسجّل والمعدّل
    from negotiator import handle_negotiation_message  # 💬 المفاوض
    from goals import session_goal, GOAL_LABELS, handle_cold_reply  # 🎯 الموجّه + المبادرة
    from bot import wa_send as bot_wa

    # 🧠 الحافظ: سجّل الحضور دائماً
    Memory.touch(phone)

    # 🎯 موجّه الأهداف: ما مهمة هذه الجلسة؟
    goal = session_goal(phone)
    print(f"[GOAL] {phone} → {goal} ({GOAL_LABELS.get(goal, goal)})", flush=True)

    # سطر تتبّع دائم + سلسلة استدلال «الظلّ»: ماذا فهمنا وكيف توجّهنا ولماذا؟
    _EXPECT = {"cold_reply": "📞 المبادرة", "negotiate": "💬 المفاوض",
               "new_inbound": "📝 المسجّل", "complete_registration": "📝 المسجّل",
               "returning": "📝 المسجّل"}
    def _route(emp):
        print(f"[ROUTE] {phone} → goal={goal} → {emp}", flush=True)
        try:
            import route_trace
            from intent_parser import parse_intent
            from strategy import detect_mood, strategy_for
            parsed = parse_intent(text) if text else {"intent": "-"}
            it = parsed.get("intent", "-")
            mood = detect_mood(text, parsed) if text else "neutral"
            st = strategy_for(mood)
            known = "نعرفه (سياق سابق)" if goal != "new_inbound" else "جديد لا نعرفه"
            exp = _EXPECT.get(goal, "")
            align = ("✅ الإجراء ضمن أهدافنا" if (exp and exp in emp) or not exp
                     else f"⚠️ خرجنا عن المتوقّع (المنتظر: {exp})")
            analysis = [
                f"📩 الرسالة قالت: {(text or '')[:70]}",
                f"🧠 السياق: {GOAL_LABELS.get(goal, goal)} — {known}",
                f"🎯 فهمتُ نيّته: {it} · مزاجه: {mood}",
                f"🧭 استراتيجيتي: {st['tone']} — " + ("أتقدّم نحو السعر" if st['push_price'] else "احتواء بلا إلحاح على السعر"),
                f"👤 وجّهتُها إلى: {emp}",
                align,
            ]
            route_trace.add("وارد", phone, goal, it, emp, text, mood, analysis)
        except Exception as _e:
            print(f"[TRACE] تعذّر: {_e}", flush=True)

    # 💬 المفاوض: تفاوض نشط (نص و/أو وسائط)
    if (text or media_url) and handle_negotiation_message(phone, text, media_url):
        _route("💬 المفاوض")
        return

    # 📞 ردّ على مبادرة باردة (مالك معلِن في حراج)
    if goal == "cold_reply" and text and handle_cold_reply(phone, text):
        _route("📞 المبادرة cold_reply")
        return

    # 📝 المسجّل والمعدّل: جلسة تعديل جارية أو طلب تعديل صريح
    if text and (Registrar.in_edit(phone) or
                 (not Registrar.in_registration(phone) and Registrar.wants_edit(text))):
        reply = Registrar.edit(phone, text)
        if reply:
            _route("📝 المسجّل/المعدّل (تعديل)")
            bot_wa(phone, reply)
            return

    # 📝 المسجّل: كل ما تبقى (جمع البيانات)
    reply = Registrar.handle(phone, text, media_url)
    if reply:
        _route("📝 المسجّل (جمع/تسجيل)")
        bot_wa(phone, reply)
    else:
        _route("🧠 الحافظ فقط (بلا ردّ)")


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


@app.route("/contacts")
def list_contacts():
    """🧠 الحافظ — كل العملاء وملفاتهم (للوحة الحافظ)."""
    from bot import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT phone, name, last_reg_type, total_regs, notes, last_seen
                FROM sanad.masaed_contacts
                ORDER BY last_seen DESC NULLS LAST LIMIT 300
            """)
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                d = dict(zip(cols, r)); d["last_seen"] = str(d["last_seen"] or "")
                rows.append(d)
        return jsonify({"count": len(rows), "contacts": rows})
    finally:
        conn.close()


@app.route("/deals")
def list_deals():
    """📋 الصفقات الجاهزة (مخرجات مُعِدّ الصفقة) — للوحة التحكم."""
    from bot import get_conn
    status = request.args.get("status")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            q = """SELECT id, seeker_phone, listing_id, listing_phone, status,
                          deal_file, created_at, updated_at
                   FROM sanad.masaed_deals"""
            if status:
                q += " WHERE status=%s"; args = (status,)
            else:
                args = ()
            q += " ORDER BY updated_at DESC LIMIT 100"
            cur.execute(q, args)
            cols = [d[0] for d in cur.description]
            deals = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["created_at"] = str(d["created_at"]); d["updated_at"] = str(d["updated_at"])
                deals.append(d)
        return jsonify({"count": len(deals), "deals": deals})
    finally:
        conn.close()


@app.route("/lab/test-negotiation", methods=["POST"])
def lab_test_negotiation():
    """يبدأ تفاوضاً تجريبياً على رقمي الاختبار مزروعاً بطلب حقيقي (زر اختبار المحاكاة)."""
    from goals import start_test_negotiation
    data = request.get_json() or {}
    lead_id = data.get("lead_id")
    if not lead_id:
        return jsonify({"ok": False, "error": "lead_id مطلوب"}), 400
    try:
        res = start_test_negotiation(int(lead_id))
        return jsonify(res), (200 if res.get("ok") else 400)
    except Exception as e:
        print(f"[API] خطأ في اختبار المحاكاة: {e}", flush=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/config/test", methods=["GET", "POST"])
def config_test():
    """قراءة/تعيين إعدادات وضع الاختبار (تبديل أرقام الملاك لرقم اختبار)."""
    from bot import get_config, set_config
    if request.method == "POST":
        data = request.get_json() or {}
        if "test_mode" in data:
            set_config("test_mode", "on" if data["test_mode"] in (True, "on", "true", 1) else "off")
        if "test_owner" in data:
            set_config("test_owner", str(data["test_owner"]).replace("+", "").replace(" ", ""))
        if "test_seeker" in data:
            set_config("test_seeker", str(data["test_seeker"]).replace("+", "").replace(" ", ""))
    return jsonify({
        "test_mode":   get_config("test_mode", "off"),
        "test_owner":  get_config("test_owner", ""),
        "test_seeker": get_config("test_seeker", ""),
    })


@app.route("/outbound/rematch", methods=["POST"])
def outbound_rematch():
    """
    المتابعة الدورية: أعِد مطابقة كل الباحثين النشطين ضد العروض الحالية،
    بادر أصحاب العروض الجديدة، وطمئن الباحثين. (تُشغّل دورياً عبر cron/جدولة).
    Body (اختياري): {scrape?: bool, max?: int, followup_hours?: int}
    """
    from goals import run_periodic_rematch
    data = request.get_json(silent=True) or {}
    try:
        res = run_periodic_rematch(
            do_scrape=bool(data.get("scrape", False)),
            max_per_seeker=int(data.get("max", 2)),
            followup_hours=int(data.get("followup_hours", 12)),
        )
        return jsonify(res)
    except Exception as e:
        print(f"[API] خطأ في إعادة المطابقة: {e}", flush=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/outbound/run", methods=["POST"])
def outbound_run():
    """
    المحرّك الصادر المدفوع بالطلب: طلب باحث → مطابقة → سحب حراج عند الحاجة →
    مبادرة الملاك المطابقين (مبادرة باردة).
    Body: {phone: "<رقم الباحث المسجّل>", scrape?: bool, max?: int}
    """
    from goals import run_outbound_for_phone
    data = request.get_json() or {}
    phone = data.get("phone")
    if not phone:
        return jsonify({"ok": False, "error": "phone مطلوب (رقم باحث مسجّل)"}), 400
    do_scrape = bool(data.get("scrape", True))
    max_c     = int(data.get("max", 3))
    try:
        res = run_outbound_for_phone(phone, do_scrape=do_scrape, max_contacts=max_c)
        return jsonify(res), (200 if res.get("ok") else 404)
    except Exception as e:
        print(f"[API] خطأ في المحرّك الصادر: {e}", flush=True)
        return jsonify({"ok": False, "error": str(e)}), 500


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


@app.route("/gate/status", methods=["GET"])
def gate_status():
    """حالة بوّابة الصفقة لزوج (seeker, owner) — للواجهة لتعرف هل تُظهر «ابدأ التفاوض»."""
    import deal_gate
    seeker = request.args.get("seeker_phone", "")
    owner  = request.args.get("owner_phone", "")
    if not seeker or not owner:
        return jsonify({"ok": False, "error": "seeker_phone و owner_phone مطلوبان"}), 400
    return jsonify({"ok": True, "gate": deal_gate.status(seeker, owner)})


@app.route("/gate/decision", methods=["POST"])
def gate_decision():
    """
    قرار المستخدم على محاكاة صفقة: approve يفتح التواصل الحقيقي، reject يغلقه.
    الاعتماد لا يُقبل إلا بوجود محاكاة مكتملة ناجحة (job_id منتهٍ بنجاح).
    Body: {seeker_phone, owner_phone, decision: approve|reject, job_id?, listing_id?}
    """
    import deal_gate
    from sim_engine import get_status
    data = request.get_json() or {}
    seeker   = (data.get("seeker_phone") or "").strip()
    owner    = (data.get("owner_phone") or "").strip()
    decision = (data.get("decision") or "").strip()
    job_id   = data.get("job_id")
    listing_id = data.get("listing_id")

    if not seeker or not owner or decision not in ("approve", "reject"):
        return jsonify({"ok": False,
                        "error": "seeker_phone و owner_phone و decision(approve|reject) مطلوبة"}), 400

    # استخرج ملخّص المحاكاة من الـjob (إن وُجد ومكتمل)
    summary = {}
    if job_id:
        job = get_status(job_id)
        if job and job.get("status") == "done":
            res = job.get("result") or {}
            ev  = res.get("evaluation") or {}
            sim = res.get("simulation") or {}
            summary = {
                "score": ev.get("overall_score") or ev.get("score"),
                "final_status": sim.get("final_status"),
                "agreed_price": sim.get("agreed_price"),
                "fact_errors": len(res.get("fact_errors") or []),
                "rounds": sim.get("rounds"),
            }

    if decision == "approve":
        # لا اعتماد دون محاكاة مكتملة فعلية
        if not job_id or not summary:
            return jsonify({"ok": False,
                            "error": "لا يمكن الاعتماد دون محاكاة مكتملة — شغّل المحاكاة أولاً"}), 400
        deal_gate.record(seeker, owner, listing_id, job_id, summary,
                         gate_status="approved", by="dashboard")
        print(f"[GATE] ✅ اعتُمدت صفقة {deal_gate.norm(seeker)}↔{deal_gate.norm(owner)} "
              f"(score={summary.get('score')})", flush=True)
        return jsonify({"ok": True, "gate_status": "approved", "summary": summary})

    deal_gate.record(seeker, owner, listing_id, job_id, summary,
                     gate_status="rejected", by="dashboard")
    return jsonify({"ok": True, "gate_status": "rejected"})


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


# حالة آخر تشغيل لمحاكاة تلقائية (للوحة التحكم)
_autosim_state = {"running": False, "done": 0, "skipped": 0, "total": 0, "started": None, "finished": None}


def _run_auto_simulate(batch, mode="normal"):
    """يحاكي أفضل التوفيقات المعلّقة (بلا إرسال) ويسجّلها pending_review."""
    import deal_gate
    from sim_engine import start_job, get_status, RateLimited
    import time as _t
    _autosim_state.update({"running": True, "done": 0, "skipped": 0, "total": batch,
                           "started": _t.strftime("%H:%M"), "finished": None})
    try:
        ensure_matches_table()
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, req_phone, lst_phone, listing_id, score FROM (
                  SELECT DISTINCT ON (m.req_phone)
                         m.id, m.req_phone, m.lst_phone, m.listing_id, m.score
                  FROM sanad.masaed_matches m
                  WHERE m.status='pending'
                    AND m.req_phone IS NOT NULL AND m.lst_phone IS NOT NULL
                    AND m.listing_id IN (SELECT id FROM sanad.masaed_listings)
                    AND NOT EXISTS (SELECT 1 FROM sanad.masaed_deal_gate g
                                    WHERE g.seeker_phone=m.req_phone AND g.owner_phone=m.lst_phone)
                  ORDER BY m.req_phone, m.score DESC NULLS LAST
                ) t
                ORDER BY t.score DESC NULLS LAST
                LIMIT 80
            """)
            rows = cur.fetchall()
        conn.close()
        results = []
        for mid, req_phone, lst_phone, listing_id, score in rows:
            if _autosim_state["done"] >= batch:
                break
            if _is_test_phone(req_phone) or _is_test_phone(lst_phone) or req_phone == lst_phone:
                _autosim_state["skipped"] += 1; continue
            try:
                inp = _build_deal_sim(req_phone, listing_id, lst_phone)
                if not inp or not (inp["offer"] or {}).get("phone"):
                    _autosim_state["skipped"] += 1; continue
                if mode == "hard":
                    inp["extras"]["mode"] = "hard"
                job = start_job(inp["reg_id"], inp["seeker_data"], inp["owner_data"], extras=inp["extras"])
                st = None
                for _ in range(50):
                    _t.sleep(3)
                    st = get_status(job)
                    if st and st.get("status") in ("done", "error"):
                        break
                res = (st or {}).get("result") or {}
                if not res.get("ok"):
                    _autosim_state["skipped"] += 1; continue
                ev = res.get("evaluation") or {}; sm = res.get("simulation") or {}
                deal_gate.record(req_phone, lst_phone, listing_id, job,
                                 {"score": ev.get("overall_score"), "final_status": sm.get("final_status"),
                                  "fact_errors": len(res.get("fact_errors") or [])},
                                 gate_status="pending_review", by="auto-sim")
                _autosim_state["done"] += 1
                results.append({"match": mid, "score": ev.get("overall_score")})
            except RateLimited:
                break
            except Exception as e:
                print(f"[AUTO-SIM] {e}", flush=True); _autosim_state["skipped"] += 1
        return {"ok": True, "simulated": _autosim_state["done"],
                "skipped": _autosim_state["skipped"], "results": results}
    finally:
        import time as _t2
        _autosim_state["running"] = False
        _autosim_state["finished"] = _t2.strftime("%H:%M")


@app.route("/cron/auto-simulate", methods=["POST"])
def cron_auto_simulate():
    """🤖 يحاكي أفضل التوفيقات المعلّقة (بلا أي إرسال) ويسجّلها pending_review لتظهر
    في «الشفافية». يدوي من اللوحة. Query: batch (افتراضي 3، أقصى 6)، async=1 للخلفية."""
    import threading
    body = request.get_json(silent=True) or {}
    batch = min(int(request.args.get("batch", body.get("batch", 3))), 6)
    mode = "hard" if (request.args.get("mode") or body.get("mode")) == "hard" else "normal"
    if _autosim_state.get("running"):
        return jsonify({"ok": False, "error": "محاكاة تلقائية قيد التشغيل بالفعل",
                        "state": _autosim_state}), 409
    if request.args.get("async") == "1" or body.get("async"):
        threading.Thread(target=_run_auto_simulate, args=(batch, mode), daemon=True).start()
        return jsonify({"ok": True, "started": True, "batch": batch, "mode": mode,
                        "message": "بدأت المحاكاة في الخلفية — حدّث بعد دقائق"}), 202
    return jsonify(_run_auto_simulate(batch, mode))


@app.route("/cron/auto-simulate-status", methods=["GET"])
def cron_auto_simulate_status():
    """حالة آخر/جاري محاكاة تلقائية (للوحة التحكم)."""
    return jsonify({"ok": True, "state": _autosim_state})


# ── 🟣 الهوية: قراءة/تعديل من اللوحة (المصدر الواحد identity.py) ──────────────
@app.route("/identity", methods=["GET"])
def identity_get():
    import identity
    return jsonify({"ok": True, "bot_name": identity.BOT_NAME,
                    "principles": identity.principles(),
                    "fields": identity.snapshot()})


@app.route("/identity", methods=["POST"])
def identity_save():
    import identity
    data = request.get_json() or {}
    overrides = data.get("overrides") if isinstance(data.get("overrides"), dict) else data
    try:
        saved = identity.save_overrides(overrides)
        return jsonify({"ok": True, "saved_count": len(saved), "saved": list(saved.keys())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/identity/reset", methods=["POST"])
def identity_reset():
    import identity
    identity.reset_overrides()
    return jsonify({"ok": True, "message": "أُعيدت الهوية للقيم الافتراضية"})


# ── 🔧 نظام التطوير: اقتراحات تحسين ما بعد المحادثة ──────────────────────────
@app.route("/improvements", methods=["GET"])
def improvements_list():
    import reviewer
    return jsonify({"ok": True, "items": reviewer.list_open(), "stats": reviewer.stats()})


@app.route("/improvements/<int:imp_id>/apply", methods=["POST"])
def improvements_apply(imp_id):
    import reviewer
    r = reviewer.apply_improvement(imp_id)
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/improvements/<int:imp_id>/dismiss", methods=["POST"])
def improvements_dismiss(imp_id):
    import reviewer
    return jsonify(reviewer.dismiss_improvement(imp_id))


@app.route("/improvements/review", methods=["POST"])
def improvements_review():
    """تشغيل المراجعة يدوياً على محاكاة محفوظة لصفقة (seeker_phone+owner_phone)."""
    import reviewer
    data = request.get_json() or {}
    sp = normalize_phone(str(data.get("seeker_phone", "")))
    op = normalize_phone(str(data.get("owner_phone", "")))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT result FROM sanad.masaed_sim_runs
                           WHERE seeker_phone=%s AND owner_phone=%s
                           ORDER BY created_at DESC LIMIT 1""", (sp, op))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"ok": False, "error": "لا توجد محاكاة محفوظة لهذه الصفقة"}), 404
    msgs = (row[0].get("simulation") or {}).get("messages") or []
    r = reviewer.review_conversation(msgs, {"seeker_phone": sp, "owner_phone": op}, "sim", None)
    return jsonify({"ok": bool(r), "result": r})


@app.route("/deal/wa-test", methods=["POST"])
def deal_wa_test():
    """🧪 اختبار واتساب حقيقي بأرقام المستخدم نفسه (محاكاة الدرجة الثانية): يبدأ
    تفاوضاً فعلياً بين رقمَيك (أنت + رقم اختبار تضيفه) ببيانات صفقة حقيقية، فتلعب
    الطرفين بردود صعبة وتختبر مساعد على واتساب الحقيقي.
    حارس أمان: يرفض إن طابق أحد الرقمين رقم الإعلان الحقيقي (منعاً للتواصل بالخطأ).
    Body: {my_phone, test_phone, deal_seeker_phone, listing_id, listing_phone?, mode?}"""
    from negotiator import start_negotiation, ensure_table as _ensure_neg
    _ensure_neg()
    data = request.get_json() or {}
    my_phone   = normalize_phone(str(data.get("my_phone", "")))     # الباحث (أنت)
    test_phone = normalize_phone(str(data.get("test_phone", "")))   # المالك (رقمك الآخر)
    deal_seeker  = (data.get("deal_seeker_phone") or "").strip()
    listing_id   = data.get("listing_id")
    listing_phone = (data.get("listing_phone") or "").strip() or None
    if not my_phone or not test_phone:
        return jsonify({"ok": False, "error": "رقمك ورقم الاختبار مطلوبان"}), 400
    if my_phone == test_phone:
        return jsonify({"ok": False, "error": "الرقمان يجب أن يكونا مختلفين"}), 400

    inp = _build_deal_sim(deal_seeker or my_phone, listing_id, listing_phone)
    if not inp:
        return jsonify({"ok": False, "error": "لا توجد بيانات لهذه الصفقة"}), 404
    offer = inp["offer"]; seeker = inp["seeker"]

    # 🛑 حارس الخط الأحمر: امنع أرقام الإعلانات الحقيقية — إلا إن كانت ضمن أرقام
    # الاختبار/الـsandbox المُصرّح بها (أرقام المستخدم نفسه).
    from bot import SANDBOX_PHONES, get_config
    _allowed = set(SANDBOX_PHONES) | {n for n in (get_config("test_owner", ""),
                                                  get_config("test_seeker", "")) if n}
    real_nums = {normalize_phone(str(x)) for x in
                 (deal_seeker, listing_phone, offer.get("phone"), seeker.get("phone")) if x}
    for p in (my_phone, test_phone):
        if p in real_nums and p not in _allowed:
            return jsonify({"ok": False,
                            "error": "⛔ استخدم أرقامك أنت فقط للاختبار — لا أرقام الإعلانات الحقيقية."}), 400

    city = offer.get("city") or seeker.get("city")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO sanad.masaed_registrations
            (phone,name,type,slug,city,property_type,rooms,budget_annual,status)
            VALUES (%s,%s,'wanted',%s,%s,%s,%s,%s,'complete')
            ON CONFLICT (slug) DO UPDATE SET city=EXCLUDED.city,status='complete',updated_at=NOW()
            RETURNING id""", (my_phone, "باحث اختبار", f"watest-seeker-{my_phone}", city,
                              offer.get("property_type"), seeker.get("rooms") or offer.get("rooms"),
                              seeker.get("budget_annual") or offer.get("price")))
        seeker_reg_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO sanad.masaed_registrations
            (phone,name,type,slug,city,property_type,rooms,price_annual,status)
            VALUES (%s,%s,'listing',%s,%s,%s,%s,%s,'complete')
            ON CONFLICT (slug) DO UPDATE SET city=EXCLUDED.city,price_annual=EXCLUDED.price_annual,
                status='complete',updated_at=NOW()
            RETURNING id""", (test_phone, "مالك اختبار", f"watest-owner-{test_phone}", city,
                              offer.get("property_type"), offer.get("rooms"), offer.get("price")))
        owner_reg_id = cur.fetchone()[0]
        # أغلق أي تفاوض اختبار سابق لرقمَي المستخدم → يسمح بإعادة الاختبار بلا حظر
        cur.execute("""UPDATE sanad.masaed_negotiations SET status='cancelled'
                       WHERE (lead_phone IN (%s,%s) OR listing_phone IN (%s,%s))
                         AND status='active'""",
                    (my_phone, test_phone, my_phone, test_phone))
        conn.commit()
    conn.close()

    result = start_negotiation(
        lead_id=seeker_reg_id, listing_id=owner_reg_id,
        lead_phone=my_phone, listing_phone=test_phone, lead_name="باحث اختبار",
        listing_title=offer.get("title") or "عقار للإيجار",
        listing_city=city, listing_price=offer.get("price"),
        lead_url=seeker.get("url"), listing_url=offer.get("url"),  # روابط الصفقة الحقيقية
        require_gate=False)   # اختبار صريح بأرقام المستخدم → خارج البوّابة
    if result.get("ok"):
        return jsonify({"ok": True, "neg_id": result["neg_id"],
                        "message": f"بدأ اختبار التفاوض #{result['neg_id']} — وصلت رسائل واتساب لرقميك. العبهما وتابع في «جلسات»."})
    return jsonify({"ok": False, "error": result.get("error", "فشل بدء الاختبار")}), 500


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
