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
SEARCH_QUERIES = [
    "طالب ايجار شقة",
    "مطلوب شقة ايجار",
    "أبحث عن شقة للايجار",
    "طلب شقة",
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

        # Only keep posts that look like rental REQUESTS
        title_lower = (item.get('title', '') + ' ' + body).lower()
        is_request = any(kw in title_lower for kw in [
            'طالب', 'أبحث', 'ابحث', 'مطلوب', 'نبحث', 'نريد',
            'wanted', 'looking', 'seek'
        ])

        lead = {
            'source': 'haraj',
            'external_id': item['id'],
            'url': f"https://haraj.com.sa/{item['url']}" if item['url'] else None,
            'title': item['title'][:300],
            'body': body[:2000],
            'city': item.get('city', city) or city,
            'phone': phone,
            'phone_hidden': phone_hidden,
        }

        if is_request or not phone_hidden:
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


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    asyncio.run(run_scrape(dry_run=dry))
