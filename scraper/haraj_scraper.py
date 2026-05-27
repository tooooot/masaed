#!/usr/bin/env python3
"""
مساعد — Haraj Scraper
Scrapes rental requests (طلبات إيجار) from haraj.com.sa
using Playwright to execute JavaScript and extract React Router data.
"""
import asyncio, json, re, os, sys
from datetime import datetime
import psycopg2
from playwright.async_api import async_playwright

# ── Config ────────────────────────────────────────────────────────────────────
DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = os.getenv("POSTGRES_PORT", "5433")
DB_NAME = os.getenv("POSTGRES_DB", "sanad")
DB_USER = os.getenv("POSTGRES_USER", "sanad")
DB_PASS = os.getenv("POSTGRES_PASSWORD", os.getenv("PG_SANAD_PWD", ""))

CITIES = ["جدة", "الرياض", "مكة", "المدينة", "الدمام"]

# Queries phrased as requests — not "apartment for rent" but "looking for apartment"
SEARCH_QUERIES = [
    "ابحث عن شقة للايجار",
    "محتاج شقة ايجار",
    "مطلوب شقة للايجار",
    "ابغى شقة ايجار",
    "احتاج شقة للإيجار",
    "طالب ايجار شقة",
]

# Must have at least one request keyword in title or first 300 chars of body
REQUEST_KW = [
    'طالب', 'أبحث', 'ابحث', 'مطلوب', 'نبحث', 'نريد', 'محتاج',
    'احتاج', 'ابغى', 'ابغي', 'أبغى', 'أبغي', 'بدور', 'عايز',
    'نبغى', 'نبغي', 'نحتاج', 'أريد شقة', 'اريد شقة',
]

# Exclude listings that are clearly offers (owner-side posts)
OFFER_KW = [
    'للإيجار', 'للايجار', 'لإيجار', 'للأيجار',
    'نؤجر', 'يتوفر', 'لدينا', 'عندنا', 'نوفر',
    'عرض خاص', 'تواصل للحجز', 'ايجار يومي', 'ايجار شهري',
    'ايجار سنوي', 'شقق عزاب', 'شقه عزاب', 'شقة عزاب',
    'للتمليك', 'فندقية', 'مفروشة يومي',
]

PHONE_PATTERN = re.compile(r'(?:966|0)?5\d{8}')

# ── DB ────────────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )

def upsert_lead(conn, lead: dict) -> bool:
    """Returns True if new, False if already existed."""
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

# ── Scraper ───────────────────────────────────────────────────────────────────
async def scrape_haraj(query: str, city: str = "") -> list[dict]:
    """Scrape Haraj search results and return list of leads."""
    search_term = f"{query} {city}".strip()
    encoded = search_term.replace(" ", "+")
    url = f"https://haraj.com.sa/search/{encoded}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        page = await browser.new_page()
        page.set_default_timeout(30000)

        try:
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_timeout(3000)

            # Extract items from React Router context
            items = await page.evaluate("""() => {
                try {
                    const ctx = window.__reactRouterContext;
                    if (!ctx) return [];
                    const ld = ctx.state?.loaderData || {};
                    const key = Object.keys(ld).find(k => k.includes('search'));
                    if (!key) return [];
                    const queries = ld[key]?.dehydratedState?.queries || [];
                    const pages = queries[0]?.state?.data?.pages || [];
                    const items = pages.flatMap(p => p?.search?.items || []);
                    return items.map(item => ({
                        id: String(item.id),
                        title: item.title || '',
                        body: item.bodyTEXT || '',
                        city: item.city || '',
                        url: item.URL || '',
                        author: item.authorUsername || '',
                        date: item.postDate || 0
                    }));
                } catch(e) {
                    return [{error: e.message}];
                }
            }""")

        except Exception as e:
            print(f"[ERROR] {url}: {e}", file=sys.stderr)
            items = []
        finally:
            await browser.close()

    leads = []
    for item in items:
        if 'error' in item:
            print(f"[WARN] JS error: {item['error']}", file=sys.stderr)
            continue

        body = item.get('body', '')

        # Extract phone if visible in body
        phones = PHONE_PATTERN.findall(body)
        phone = phones[0] if phones else None
        phone_hidden = phone is None

        title = item.get('title', '')
        title_lower = title.lower()

        # Request = keyword in TITLE only (body is unreliable — offers mention request words too)
        is_request = any(kw in title_lower for kw in REQUEST_KW)
        is_offer   = any(kw in title_lower for kw in OFFER_KW)

        # Skip: not a request in title, OR clearly an offer in title
        if not is_request or is_offer:
            continue

        lead = {
            'source': 'haraj',
            'external_id': item['id'],
            'url': f"https://haraj.com.sa/{item['url']}" if item['url'] else None,
            'title': title[:300],
            'body': body[:2000],
            'city': item.get('city', city) or city,
            'phone': phone,
            'phone_hidden': phone_hidden,
        }

        leads.append(lead)

    return leads


async def run_scrape(cities=None, queries=None, dry_run=False):
    """Main scraping loop."""
    if cities is None:
        cities = CITIES
    if queries is None:
        queries = SEARCH_QUERIES[:2]  # default: first 2 queries

    conn = None if dry_run else get_conn()
    total_new = 0
    total_seen = 0

    for city in cities:
        for query in queries:
            print(f"[SCRAPE] {query} | {city}")
            leads = await scrape_haraj(query, city)
            print(f"  → found {len(leads)} potential leads")

            for lead in leads:
                total_seen += 1
                if dry_run:
                    print(f"  [DRY] {lead['external_id']} | {lead['title'][:60]} | phone={'visible' if not lead['phone_hidden'] else 'hidden'}")
                else:
                    is_new = upsert_lead(conn, lead)
                    if is_new:
                        total_new += 1
                        status = "📱 " + lead['phone'] if lead['phone'] else "🔒 hidden"
                        print(f"  [NEW] {lead['external_id']} | {lead['title'][:60]} | {status}")

    if conn:
        conn.close()

    print(f"\n✅ Done. {total_new} new leads stored (seen {total_seen} total)")
    return total_new


# ══════════════════════════════════════════════════════════════════════════════
# LISTINGS SCRAPER — scrapes "للإيجار" offers (owner-side ads)
# ══════════════════════════════════════════════════════════════════════════════

LISTING_QUERIES = [
    "شقة للإيجار",
    "شقق للإيجار",
    "فيلا للإيجار",
    "غرفة للإيجار",
    "استوديو للإيجار",
    "شقة مفروشة للإيجار",
]

# Must have offer keyword in title
LISTING_MUST_KW = ['للإيجار', 'للايجار', 'لإيجار', 'للأيجار', 'نؤجر', 'يتوفر للإيجار']

# Extract rooms count from Arabic text
def extract_rooms(text: str):
    t = text.lower()
    pairs = [
        (r'(\d+)\s*غرف', lambda m: int(m.group(1))),
        (r'غرفتين|غرفتان', lambda m: 2),
        (r'ثلاث\s*غرف|ثلاثة\s*غرف', lambda m: 3),
        (r'أربع\s*غرف|اربع\s*غرف', lambda m: 4),
        (r'خمس\s*غرف', lambda m: 5),
        (r'غرفة\s*واحدة|غرفه\s*واحده|غرفه\s+وصاله|غرفة\s+وصالة', lambda m: 1),
    ]
    for pattern, fn in pairs:
        m = re.search(pattern, t)
        if m:
            try: return fn(m)
            except: pass
    return None

# Extract annual price in SAR
def extract_price(text: str):
    t = re.sub(r'[,٬]', '', text)
    m = re.search(r'(\d{3,6})\s*(?:ريال|ر\.?س\.?|SAR)', t, re.IGNORECASE)
    if m: return int(m.group(1))
    m = re.search(r'(?:السعر|إيجار|بـ?)\s*(\d{3,6})', t, re.IGNORECASE)
    if m: return int(m.group(1))
    return None

# Extract property type
def extract_type(text: str):
    t = text.lower()
    if 'فيلا' in t: return 'فيلا'
    if 'استوديو' in t: return 'استوديو'
    if 'غرفة' in t and 'شقة' not in t: return 'غرفة'
    if 'شقة' in t or 'شقه' in t or 'شقق' in t: return 'شقة'
    if 'دور' in t: return 'دور'
    if 'بيت' in t or 'منزل' in t: return 'منزل'
    return None

def upsert_listing(conn, listing: dict) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sanad.masaed_listings
                (source, external_id, url, title, body, city, property_type,
                 rooms, price, phone, phone_hidden, status)
            VALUES
                (%(source)s, %(external_id)s, %(url)s, %(title)s, %(body)s,
                 %(city)s, %(property_type)s, %(rooms)s, %(price)s,
                 %(phone)s, %(phone_hidden)s, 'active')
            ON CONFLICT (source, external_id) DO NOTHING
            RETURNING id
        """, listing)
        result = cur.fetchone()
        conn.commit()
        return result is not None

async def scrape_haraj_listings(query: str, city: str = "") -> list[dict]:
    search_term = f"{query} {city}".strip()
    encoded = search_term.replace(" ", "+")
    url = f"https://haraj.com.sa/search/{encoded}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        page = await browser.new_page()
        page.set_default_timeout(30000)
        try:
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_timeout(3000)
            items = await page.evaluate("""() => {
                try {
                    const ctx = window.__reactRouterContext;
                    if (!ctx) return [];
                    const ld = ctx.state?.loaderData || {};
                    const key = Object.keys(ld).find(k => k.includes('search'));
                    if (!key) return [];
                    const queries = ld[key]?.dehydratedState?.queries || [];
                    const pages = queries[0]?.state?.data?.pages || [];
                    const items = pages.flatMap(p => p?.search?.items || []);
                    return items.map(item => ({
                        id: String(item.id),
                        title: item.title || '',
                        body: item.bodyTEXT || '',
                        city: item.city || '',
                        url: item.URL || '',
                        date: item.postDate || 0
                    }));
                } catch(e) { return [{error: e.message}]; }
            }""")
        except Exception as e:
            print(f"[LISTINGS ERROR] {url}: {e}", file=sys.stderr)
            items = []
        finally:
            await browser.close()

    listings = []
    for item in items:
        if 'error' in item: continue
        title = item.get('title', '')
        body  = item.get('body', '')
        title_lower = title.lower()

        is_listing = any(kw in title_lower for kw in LISTING_MUST_KW)
        is_request = any(kw in title_lower for kw in REQUEST_KW)
        if not is_listing or is_request:
            continue

        phones = PHONE_PATTERN.findall(body + ' ' + title)
        phone = phones[0] if phones else None

        full_text = title + ' ' + body
        listing = {
            'source': 'haraj',
            'external_id': item['id'],
            'url': f"https://haraj.com.sa/{item['url']}" if item['url'] else None,
            'title': title[:300],
            'body': body[:2000],
            'city': item.get('city', city) or city,
            'property_type': extract_type(full_text),
            'rooms': extract_rooms(full_text),
            'price': extract_price(full_text),
            'phone': phone,
            'phone_hidden': phone is None,
        }
        listings.append(listing)

    return listings

async def run_scrape_listings(cities=None, queries=None):
    if cities is None: cities = CITIES
    if queries is None: queries = LISTING_QUERIES[:3]

    conn = get_conn()
    total_new = 0
    for city in cities:
        for query in queries:
            print(f"[LISTINGS] {query} | {city}")
            items = await scrape_haraj_listings(query, city)
            print(f"  → found {len(items)} listings")
            for item in items:
                if upsert_listing(conn, item):
                    total_new += 1
                    print(f"  [NEW] {item['external_id']} | {item['title'][:60]} | rooms={item['rooms']} price={item['price']}")
    conn.close()
    print(f"\n✅ Listings done. {total_new} new stored")
    return total_new


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    if "--listings" in sys.argv:
        asyncio.run(run_scrape_listings())
    else:
        asyncio.run(run_scrape(dry_run=dry))
