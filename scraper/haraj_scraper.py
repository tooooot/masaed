#!/usr/bin/env python3
"""
مساعد — Haraj Scraper

⚠️ LEGAL WARNING ⚠️
This scraper collects data from haraj.com.sa WITHOUT PERMISSION.
This violates Haraj's Terms of Service and may infringe on:
- Copyright (listing text, photos)
- Personal data (phone numbers, location data)
- PDPA (Saudi data protection law)

BEFORE RUNNING:
1. Obtain written permission from Haraj.com.sa management
2. OR disable this scraper and use manual registration only:
   - Users opt-in via /registrar.html form
   - No automated data collection
   - See: LEGAL.md for compliance details

Scrapes rental requests (طلبات إيجار) from haraj.com.sa
using Playwright to execute JavaScript and extract React Router data.
"""
import asyncio, json, re, os, sys
from datetime import datetime
import psycopg2
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

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

# ── Read single URL ─────────────────────────────────────────────────────────────
async def scrape_single_url(url: str) -> dict:
    """
    قراءة إعلان حراج واحد — تستخرج التفاصيل الكاملة والوسائط.
    يُستدعى من مساعد الطلبات.
    """
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

            # احصل على الـHTML الكامل
            html = await page.content()
            soup = BeautifulSoup(html, 'html.parser')

            # استخرج الـtitle
            title_elem = soup.select_one('h1, [class*="title"]')
            title = title_elem.get_text(strip=True) if title_elem else ""

            # استخرج الـbody/description
            desc_elem = soup.select_one('[class*="description"], [class*="body"], article')
            body = desc_elem.get_text(strip=True) if desc_elem else ""

            # استخرج الـprice من الـscript tags (React data)
            price = None
            scripts = soup.find_all('script')
            for script in scripts:
                if script.string and 'price' in script.string.lower():
                    try:
                        # حاول استخراج الرقم من الـscript
                        price_match = re.search(r'["\']?price["\']?\s*:\s*(\d{4,6})', script.string, re.IGNORECASE)
                        if price_match:
                            price = int(price_match.group(1))
                            break
                    except:
                        pass

            # إذا لم نجد سعراً من الـscript، ابحث في الـtext
            if not price and body:
                price = extract_price(body)

            # الصور من الـimg tags (حراج يخدم صوره من CDN، فنوسّع الفلتر مع تجاهل الأيقونات)
            images = []
            for img in soup.select('img[src], img[data-src]'):
                img_url = img.get('src') or img.get('data-src')
                if not img_url or img_url.startswith('data:'):
                    continue
                low = img_url.lower()
                base = low.split('?')[0]
                is_icon = (base.endswith('.svg') or '/flags/' in low
                           or any(x in low for x in ('logo', 'icon', 'avatar', 'sprite',
                                                     'placeholder', 'profile', 'flag', 'emoji')))
                # صور العقار الحقيقية على postcdn/userfiles؛ وإلا أي صورة من CDN حراج
                is_photo = ('userfiles' in low or 'postcdn' in low
                            or (('haraj' in low or 'cdn' in low)
                                and base.endswith(('.jpg', '.jpeg', '.png', '.webp'))))
                if is_photo and not is_icon:
                    images.append({"url": img_url, "alt": img.get('alt', '')})

            # الـvideo
            video = None
            video_elem = soup.select_one('video, [class*="video"]')
            if video_elem:
                video_src = video_elem.get('src')
                if video_src:
                    video = {"url": video_src, "thumbnail": None}

            # الموقع: إحداثيات من رابط خرائط جوجل المضمّن في الصفحة
            location = None
            geo = re.search(r'maps\.google\.com/\?q=(-?\d+\.\d+),(-?\d+\.\d+)', html)
            if geo:
                lat, lng = geo.group(1), geo.group(2)
                location = {"lat": float(lat), "lng": float(lng),
                            "map": f"https://maps.google.com/?q={lat},{lng}"}
                nb = re.search(r'geoNeighborhood\\?"\s*:\s*\\?"([^"\\]{1,40})', html)
                ct = re.search(r'geoCity\\?"\s*:\s*\\?"([^"\\]{1,40})', html)
                if nb: location["neighborhood"] = nb.group(1)
                if ct: location["city"] = ct.group(1)

            # الـphone من الـtext
            full_text = title + ' ' + body
            phone = None
            phone_match = PHONE_PATTERN.search(full_text)
            if phone_match:
                phone = phone_match.group(0)

            # الـcity من الـtext أو meta
            city = ""
            city_elem = soup.select_one('[class*="city"], [class*="location"]')
            if city_elem:
                city = city_elem.get_text(strip=True)

            if not city and body:
                # حاول استخراج من قائمة المدن المعروفة
                for c in ["جدة", "الرياض", "مكة", "المدينة", "الدمام", "الخبر"]:
                    if c in body:
                        city = c
                        break

            return {
                'id': url.split('/')[-1][:20],  # استخدم آخر جزء من الـURL كـID
                'title': title[:300],
                'body': body[:2000],
                'city': city,
                'region': "",
                'category': "",
                'price': price,
                'rooms': extract_rooms(full_text),
                'property_type': extract_type(full_text),
                'images': images[:10],  # أول 10 صور
                'video': video,
                'location': location,
                'user_name': "",
                'user_verified': False,
                'post_date': None,
                'url': url,
                'phone': phone,
            }

        except Exception as e:
            import traceback
            print(f"[SCRAPE_SINGLE] {url}: {e}\n{traceback.format_exc()}", file=sys.stderr)
            return None
        finally:
            await browser.close()


def scrape_single_url_sync(url: str) -> dict:
    """Sync wrapper لـ scrape_single_url."""
    return asyncio.run(scrape_single_url(url))

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
