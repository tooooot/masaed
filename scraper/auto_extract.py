#!/usr/bin/env python3
"""
مساعد — Auto Phone Extractor
Processes all leads with hidden phones and extracts numbers via Playwright.
"""
import asyncio, re, sys
from playwright.async_api import async_playwright
import psycopg2, os

DB_HOST = os.getenv("POSTGRES_HOST", "sanad-postgres")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "sanad")
DB_USER = os.getenv("POSTGRES_USER", "sanad")
DB_PASS = os.getenv("POSTGRES_PASSWORD", os.getenv("PG_SANAD_PWD", ""))

PHONE_RE = re.compile(r'(?:966|00966|\+966|0)?5\d{8}')


def get_conn():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def normalize_phone(raw):
    clean = re.sub(r'[^0-9]', '', raw)
    if clean.startswith('00966'): return '966' + clean[5:]
    if clean.startswith('966'): return clean
    if clean.startswith('0'): return '966' + clean[1:]
    if clean.startswith('5'): return '966' + clean
    return clean


async def extract_phone(page, url):
    """Try every method to get the phone from a listing page."""
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        # 1. Haraj React Router context (post detail)
        body_text = await page.evaluate("""() => {
            try {
                const ctx = window.__reactRouterContext;
                const ld = ctx?.state?.loaderData || {};
                for (const key of Object.keys(ld)) {
                    const q = ld[key]?.dehydratedState?.queries || [];
                    for (const query of q) {
                        const d = query?.state?.data;
                        const t = d?.bodyTEXT || d?.post?.bodyTEXT || d?.item?.bodyTEXT || '';
                        if (t) return t;
                    }
                }
                return '';
            } catch(e) { return ''; }
        }""")

        if body_text:
            phones = PHONE_RE.findall(body_text)
            if phones:
                return normalize_phone(phones[0]), "react-context"

        # 2. Click reveal / contact buttons
        btns = await page.query_selector_all('button, a, span[role=button], div[role=button]')
        for btn in btns:
            try:
                text = (await btn.inner_text()).strip()
                if re.search(r'رقم|هاتف|جوال|تواصل|اتصال|اظهر|contact|phone', text, re.I):
                    await btn.click()
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                pass

        # 3. Full page text scan
        page_text = await page.inner_text('body')
        phones = PHONE_RE.findall(page_text)
        if phones:
            return normalize_phone(phones[0]), "page-text"

        return None, "not-found"

    except Exception as e:
        return None, f"error: {e}"


async def main():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, url, title FROM sanad.masaed_leads
            WHERE phone_hidden = TRUE AND url IS NOT NULL
            ORDER BY scraped_at DESC
        """)
        leads = cur.fetchall()

    print(f"🔍 {len(leads)} طلب بأرقام مخفية — بدء الاستخراج التلقائي\n")

    extracted = 0
    failed = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        page = await browser.new_page()
        page.set_default_timeout(35000)

        for lead_id, url, title in leads:
            short_title = (title or '')[:55]
            print(f"  [{lead_id}] {short_title}", end=" ... ", flush=True)

            phone, method = await extract_phone(page, url)

            if phone:
                conn2 = get_conn()
                with conn2.cursor() as cur:
                    cur.execute("""
                        UPDATE sanad.masaed_leads
                        SET phone = %s, phone_hidden = FALSE, status = 'phone_extracted'
                        WHERE id = %s
                    """, (phone, lead_id))
                    conn2.commit()
                conn2.close()
                print(f"✅ {phone} [{method}]")
                extracted += 1
            else:
                print(f"❌ {method}")
                failed.append((lead_id, url, title))

        await browser.close()

    conn.close()

    print(f"\n{'='*60}")
    print(f"✅ تم استخراج: {extracted}/{len(leads)}")
    if failed:
        print(f"❌ لم يُستخرج ({len(failed)}):")
        for fid, furl, ftitle in failed:
            print(f"   [{fid}] {(ftitle or '')[:50]} → {furl}")

    return failed


if __name__ == "__main__":
    failed = asyncio.run(main())
    sys.exit(1 if failed else 0)
