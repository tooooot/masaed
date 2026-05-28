#!/usr/bin/env python3
"""Create test listing, lead, and match for verification."""
import os, psycopg2

conn = psycopg2.connect(
    host='sanad-postgres', port=5432,
    dbname='sanad', user='sanad',
    password=os.getenv('PG_SANAD_PWD', '')
)
cur = conn.cursor()

# 1. Listing — owner 966548060060
cur.execute("""
    INSERT INTO sanad.masaed_listings
        (source, external_id, url, title, body, city,
         property_type, rooms, price, phone, phone_hidden, status)
    VALUES
        ('test','verify-owner-01','https://masaed.wardyat.net/test',
         'شقة للإيجار في حي الجود — جدة',
         'شقة 3 غرف مفروشة بالكامل، حي الجود جدة، الدور الثاني، موقف سيارة',
         'جدة','شقة',3,25000,'966548060060',false,'active')
    ON CONFLICT (source,external_id)
        DO UPDATE SET status='active', phone='966548060060'
    RETURNING id
""")
lst_id = cur.fetchone()[0]

# 2. Lead — tenant 966550688470
cur.execute("""
    INSERT INTO sanad.masaed_leads
        (source, external_id, url, title, body, city,
         phone, phone_hidden, listing_type, status)
    VALUES
        ('test','verify-tenant-01','https://masaed.wardyat.net/test',
         'أبحث عن شقة للإيجار في جدة',
         'أبحث عن شقة 3 غرف في جدة، ميزانية 28000 ريال سنوياً، أفضل مفروشة',
         'جدة','966550688470',false,'wanted','new')
    ON CONFLICT (source,external_id)
        DO UPDATE SET status='new', phone='966550688470'
    RETURNING id
""")
lead_id = cur.fetchone()[0]
conn.commit()

# 3. Match
cur.execute("""
    INSERT INTO sanad.masaed_matches
        (lead_id, listing_id, score, reason, missing,
         req_city, req_budget, req_phone,
         lst_city, lst_price, lst_phone, status)
    VALUES
        (%s,%s,85,
         'نفس المدينة (جدة) • 3 غرف متطابقة • نوع العقار: شقة • السعر مناسب (25,000 ≤ 28,000)',
         '',
         'جدة',28000,'966550688470',
         'جدة',25000,'966548060060','pending')
    ON CONFLICT (lead_id, listing_id)
        DO UPDATE SET status='pending', score=85,
            req_phone='966550688470', lst_phone='966548060060'
    RETURNING id
""", (lead_id, lst_id))
match_id = cur.fetchone()[0]
conn.commit()
conn.close()

print(f"LISTING_ID={lst_id}")
print(f"LEAD_ID={lead_id}")
print(f"MATCH_ID={match_id}")
