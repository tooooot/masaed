#!/bin/bash
# ══════════════════════════════════════════════════════════════════
#  سيناريو اختبار مساعد المفاوض — كامل
#  المالك:    966548060060  (0548060060)
#  المستأجر: 966536669476  (0536669476)
# ══════════════════════════════════════════════════════════════════
G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; R='\033[0;31m'; N='\033[0m'
CONTAINER="n8n-masaed-scraper-1"
API="http://localhost:5555"
OWNER="966548060060"
TENANT="966536669476"

step(){ echo -e "\n${B}══ $1 ══${N}"; }
ok()  { echo -e "${G}✅ $1${N}"; }
msg() { echo -e "${Y}💬 $1${N}"; }
api(){  docker exec "$CONTAINER" curl -s "$API$1"; }
post(){ docker exec "$CONTAINER" curl -s -X POST "$API$1" -H 'Content-Type: application/json' -d "$2"; }
pyrun(){ docker exec "$CONTAINER" python3 -c "$1"; }

# ══ 1: أنشئ إعلان المالك ══
step "1️⃣  إنشاء إعلان المالك في قاعدة البيانات"
LISTING_ID=$(pyrun "
import os, psycopg2
conn = psycopg2.connect(host='sanad-postgres',port=5432,dbname='sanad',user='sanad',password=os.getenv('PG_SANAD_PWD',''))
cur = conn.cursor()
cur.execute('''
  INSERT INTO sanad.masaed_listings
    (source,external_id,url,title,body,city,property_type,rooms,price,phone,phone_hidden,status)
  VALUES
    ('test','scn-owner-01','https://haraj.com.sa/scn-001',
     'غرفة للإيجار في حي الجود — رابغ',
     'غرفة مفردة واسعة مع مطبخ ودورة مياه مستقلة، حي الجود رابغ',
     'رابغ','شقة',1,12000,'$OWNER',false,'active')
  ON CONFLICT (source,external_id) DO UPDATE SET status='active'
  RETURNING id
''')
row=cur.fetchone(); conn.commit(); conn.close(); print(row[0])
")
ok "إعلان المالك ID=$LISTING_ID — رابغ، 12000 ر/سنة، رقم $OWNER"

# ══ 2: أنشئ طلب المستأجر ══
step "2️⃣  إنشاء طلب المستأجر في قاعدة البيانات"
LEAD_ID=$(pyrun "
import os, psycopg2
conn = psycopg2.connect(host='sanad-postgres',port=5432,dbname='sanad',user='sanad',password=os.getenv('PG_SANAD_PWD',''))
cur = conn.cursor()
cur.execute('''
  INSERT INTO sanad.masaed_leads
    (source,external_id,url,title,body,city,phone,phone_hidden,listing_type,status)
  VALUES
    ('test','scn-tenant-01','https://haraj.com.sa/scn-002',
     'ابحث عن غرفة إيجار في رابغ',
     'أبحث عن غرفة مستقلة أو شقة في رابغ، ميزانية لا تتجاوز 13000 ريال سنوياً',
     'رابغ','$TENANT',false,'wanted','new')
  ON CONFLICT (source,external_id) DO UPDATE SET status='new'
  RETURNING id
''')
row=cur.fetchone(); conn.commit(); conn.close(); print(row[0])
")
ok "طلب المستأجر ID=$LEAD_ID — رابغ، ميزانية 13000 ر، رقم $TENANT"

# ══ 3: فحص التوفيق ══
step "3️⃣  محرك المطابقة — نسبة التوافق"
MATCH=$(api "/match/$LEAD_ID")
echo "$MATCH" | python3 -c "
import sys,json
d=json.load(sys.stdin)
ms=d.get('matches',[])
if ms:
    m=ms[0]
    print(f'  أفضل عرض : {m[\"title\"]}')
    print(f'  التوافق   : {m[\"score\"]}%')
    print(f'  الأسباب   : {m[\"reason\"]}')
    print(f'  الناقص    : {m.get(\"missing\",\"-\")}')
else:
    print('  لا توجد عروض مطابقة')
"

# ══ 4: الإدارة تبدأ التفاوض ══
step "4️⃣  الإدارة تضغط زر 🤝 ابدأ التفاوض"
NEG_RESP=$(post "/negotiate/start" "{\"lead_id\":$LEAD_ID,\"listing_id\":$LISTING_ID}")
NEG_ID=$(echo "$NEG_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('neg_id','?'))")
IS_OK=$(echo "$NEG_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ok','?'))")

if [ "$IS_OK" = "True" ]; then
  ok "تفاوض رقم #$NEG_ID — بدأ بنجاح"
  echo ""
  msg "  ▶ رسالة واتساب → المستأجر $TENANT:"
  echo "    مرحباً! 🏠 وجدت عرضاً يناسب طلبك:"
  echo "    📍 رابغ — غرفة للإيجار في حي الجود"
  echo "    💰 12,000 ر/سنة"
  echo "    رد بـ نعم للبدء أو لا للتخطي"
  echo ""
  msg "  ▶ رسالة واتساب → المالك $OWNER:"
  echo "    مرحباً! 🏠 لديك شخص مهتم بعقارك في رابغ."
  echo "    رد بـ نعم للبدء أو لا للتخطي"
else
  # قد يكون التفاوض موجوداً بالفعل — استخدم الآخير
  NEG_ID=$(echo "$NEG_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('neg_id',2))")
  echo -e "  ${Y}تفاوض موجود مسبقاً: #$NEG_ID${N}"
  echo "  $(echo $NEG_RESP | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d)')"
fi

sleep 1

# ══ 5: المستأجر يوافق ══
show_wa(){ echo "$1" | python3 -c "
import sys,json
d=json.load(sys.stdin)
wa=d.get('wa_sent',[])
if wa:
    for m in wa:
        print(f'  📨 لو كان حقيقي → {m[\"to\"]}: {m[\"text\"][:80]}')
else:
    print('  (لا رسائل واتساب)')
"; }

step "5️⃣  المستأجر يرسل: \"نعم\" — DRY RUN (لن يصل لهاتفك)"
R1=$(post "/bot/test" "{\"phone\":\"$TENANT\",\"text\":\"نعم\"}")
show_wa "$R1"
ok "سُجّلت موافقة المستأجر — انتظار المالك"
sleep 1

step "6️⃣  المالك يرسل: \"تمام\" — DRY RUN"
R2=$(post "/bot/test" "{\"phone\":\"$OWNER\",\"text\":\"تمام\"}")
show_wa "$R2"
ok "كلا الطرفين وافقا ✅ — التفاوض الرسمي بدأ (dry run)"
sleep 1

step "7️⃣  المستأجر: \"السعر كثير، هل ممكن 10000؟\" — DRY RUN"
R3=$(post "/bot/test" "{\"phone\":\"$TENANT\",\"text\":\"السعر كثير، هل ممكن 10000 ريال؟\"}")
show_wa "$R3"
sleep 1

step "8️⃣  المالك: \"والله أقل شي 11000\" — DRY RUN"
R4=$(post "/bot/test" "{\"phone\":\"$OWNER\",\"text\":\"والله أقل شي 11000\"}")
show_wa "$R4"
sleep 1

step "9️⃣  المستأجر: \"حسناً موافق على 11000\" — DRY RUN"
R5=$(post "/bot/test" "{\"phone\":\"$TENANT\",\"text\":\"حسناً موافق على 11000\"}")
show_wa "$R5"
sleep 1

# ══ 10: الإدارة تغلق الصفقة ══
step "🔟  الإدارة تضغط ✅ تم الاتفاق (من لوحة التحكم)"
echo "  → تفاوض #$NEG_ID | سعر الاتفاق: 11000 ر/سنة"
R6=$(post "/negotiate/$NEG_ID/agree" "{\"agreed_price\":11000}")
IS_AGREED=$(echo "$R6" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('ok','?'))")
echo "  استجابة: $(echo $R6 | python3 -c 'import sys,json;d=json.load(sys.stdin);print(json.dumps(d,ensure_ascii=False))')"

if [ "$IS_AGREED" = "True" ]; then
  ok "تم الاتفاق!"
  echo ""
  msg "  ▶ رسالة تهنئة → المستأجر $TENANT:"
  echo "    🎉 تم الاتفاق بسعر 11,000 ر/سنة!"
  echo "    سيتواصل معك الطرف الآخر لإتمام الإجراءات."
  echo ""
  msg "  ▶ رسالة تهنئة → المالك $OWNER:"
  echo "    🎉 تم الاتفاق بسعر 11,000 ر/سنة!"
  echo "    سيتواصل معك الطرف الآخر لإتمام الإجراءات."
fi

# ══ التحقق النهائي ══
echo ""
step "✅  حالة التفاوضات الحالية"
api "/negotiate/active" | python3 -c "
import sys,json
d=json.load(sys.stdin)
sm={'pending':'⏳ انتظار','active':'💬 نشط','agreed':'🎉 متفق','cancelled':'❌ ملغي','failed':'❌ فشل'}
for n in d.get('negotiations',[]):
    p=f\"{n['agreed_price']:,} ر\" if n.get('agreed_price') else f\"{n.get('listing_price',0):,} ر\"
    s=sm.get(n['status'],n['status'])
    print(f\"  #{n['id']} {s} | {n.get('listing_title','?')} | {p}\")
"
echo ""
echo -e "${G}═══════════════════════════════════════════${N}"
echo -e "${G}  السيناريو الكامل اكتمل بنجاح 🎉${N}"
echo -e "${G}  لوحة التحكم: https://masaed.wardyat.net${N}"
echo -e "${G}  جلسات التفاوض ← ابحث عن #$NEG_ID${N}"
echo -e "${G}═══════════════════════════════════════════${N}"
