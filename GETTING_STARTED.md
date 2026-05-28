# البدء بـ مساعد — دليل عملي

## 🚀 بدء سريع (5 دقائق)

### الخطوة 1: تثبيت المتطلبات
```bash
cd /root/masaed
pip install -r requirements.txt  # أو يدويّاً:
pip install flask psycopg2-binary anthropic openai playwright
```

### الخطوة 2: إعداد قاعدة البيانات

**أ. إذا كنت تستخدم Postgres محلي:**
```bash
# تسجيل الدخول
psql -U postgres

# إنشاء المستخدم والقاعدة
CREATE USER sanad PASSWORD 'your_password';
CREATE DATABASE sanad OWNER sanad;
```

**ب. تشغيل Schema:**
```bash
psql -U sanad -d sanad -f database/schema.sql
```

**ج. التحقق:**
```bash
psql -U sanad -d sanad -c "\dt sanad.*"
# يجب تظهر الجداول:
# masaed_negotiations
# masaed_registrations  
# masaed_contacts
```

### الخطوة 3: تشغيل Flask API
```bash
cd scraper
POSTGRES_PASSWORD=your_password python3 -u scraper_api.py
# Output: 
# [INIT] Sandbox phones for testing: {'966500000000', '966500000001'}
# [INIT] Flask running on 0.0.0.0:5555
```

### الخطوة 4: اختبار أول رسالة
```bash
# في terminal آخر:
curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "966500000000",
    "text": "مرحبا، أنا أبحث عن شقة في جدة"
  }' | jq .
```

**Output المتوقع:**
```json
{
  "reply": "أهلاً وسهلاً! أنا مساعد العقاري، وسيط إلكتروني...",
  "wa_sent": [
    {
      "to": "966500000000",
      "text": "أهلاً وسهلاً!..."
    }
  ]
}
```

---

## 🧪 سيناريو اختبار كامل

### سيناريو 1: تسجيل باحث جديد

```bash
# الرسالة 1: تقديم النفس
curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "966550858330",
    "text": "السلام عليكم، أنا محمد، أبحث عن شقة في جدة، ميزانيتي 2200 ريال"
  }'

# مساعد يرد:
# "شكراً محمد! وجدت. كم عدد الغرف التي تحتاج؟"

# الرسالة 2: إضافة معلومات
curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "966550858330",
    "text": "3 غرف على الأقل، ويفضل مفروشة"
  }'

# مساعد يرد:
# "تمام محمد! وجدت بيانات جديدة. سأبحث لك عن شقة
#  3 غرف مفروشة في جدة بـ 2200 ريال."

# ✓ التسجيل اكتمل!
```

### سيناريو 2: بدء تفاوض

```bash
# 1. إنشاء جلسة تفاوض يدويّاً
curl -X POST http://localhost:5555/api/start-negotiation \
  -H "Content-Type: application/json" \
  -d '{
    "lead_phone": "966550858330",
    "listing_phone": "966541234567",
    "listing_title": "شقة 3 غرف مفروشة",
    "listing_city": "جدة",
    "listing_price": 2800
  }'

# Response: {"ok": true, "neg_id": 1}

# 2. الباحث يرد
curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "966550858330",
    "text": "السعر غالي! أنا أعرض 2200 بس"
  }'

# مساعد يرد:
# "فهمت عرضك 2200. سأتحدث مع المالك..."

# 3. يُرسل للمالك
# "مستأجر جاد يعرض 2200 ريال. هل تقبل؟"

# 4. المالك يرد
curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "966541234567",
    "text": "لا، أنا أطلب 2600 على الأقل"
  }'

# مساعد يرد:
# "شكراً. سأتحدث مع المستأجر..."

# 5. يقترح وسط
# للمستأجر: "المالك نزل لـ 2400. أقرب لميزانيتك. موافق؟"
# للمالك: "المستأجر يقبل بـ 2400. موافق أنت؟"
```

### سيناريو 3: تعديل بيانات

```bash
# الباحث يغيّر رأيه
curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "966550858330",
    "text": "عدّل الميزانية، في مال دلوقتي، تقدر تبحث عن 3500 وفوق"
  }'

# مساعد يرد:
# "تمام محمد! غيّرت ميزانيتك من 2200 لـ 3500.
#  سأبحث عن عروض أفضل."
```

---

## 📊 مراقبة قاعدة البيانات

### فحص البيانات المدخلة

```bash
psql -U sanad -d sanad

# 1. مشاهدة التسجيلات
SELECT phone, role, status FROM sanad.masaed_registrations;

# 2. مشاهدة التفاوضات النشطة
SELECT id, lead_phone, listing_phone, status, 
       lead_max_price, owner_min_price 
FROM sanad.masaed_negotiations 
WHERE status = 'active';

# 3. مشاهدة سجل الرسائل
SELECT role, text, ts FROM sanad.masaed_negotiations 
WHERE id = 1 
-- جدول chat_log (JSON) يحتوي على كل الرسائل
CROSS JOIN jsonb_to_recordset(chat_log) 
AS logs(role text, text text, ts text);

# 4. مشاهدة الذاكرة
SELECT phone, name, notes, last_seen FROM sanad.masaed_contacts;
```

---

## 🔍 تصحيح الأخطاء (Debugging)

### مشكلة: "Connection refused على DB"

```bash
# تحقق من Postgres يعمل
psql -U sanad -d sanad -c "SELECT 1"

# إذا لم ينجح:
sudo systemctl start postgresql
# أو في Docker:
docker start sanad-postgres
```

### مشكلة: "No such module anthropic"

```bash
pip install --upgrade anthropic openai

# تحقق من المفاتيح
echo $ANTHROPIC_API_KEY
echo $DEEPSEEK_API_KEY
```

### مشكلة: "SafetyCheckError: Real send blocked"

```bash
# هذا الخطأ **مقصود**!
# لا يمكن الإرسال إلى أرقام حقيقية إلا بـ:

export MASAED_ALLOW_REAL_SEND=false  # default (آمن)
# أو
export MASAED_ALLOW_REAL_SEND=true   # فقط عند الإنتاج!
export MASAED_SANDBOX_PHONES="966500000000"
```

### مشكلة: "LLM returned invalid JSON"

```bash
# مساعد يحاول الـ parsing
# في intent_parser.py:
try:
    result = json.loads(llm_response)
except:
    return _DEFAULT  # {"intent": "other", ...}
```

---

## 📈 اختبارات متقدمة

### اختبار Race Condition

```bash
# أرسل رسالتين في نفس الثانية
(curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{"phone":"966500000000","text":"رسالة 1"}') &

(curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{"phone":"966500000000","text":"رسالة 2"}') &

wait

# يجب تُعالج آمناً (لا تضيع رسالة)
```

### اختبار الذاكرة

```bash
# أرسل رسالة
curl -X POST http://localhost:5555/bot/test \
  -d '{"phone":"966500000000","text":"أنا محمد"}'

# أرسل رسالة بعد ساعة
sleep 3600  # محاكاة

curl -X POST http://localhost:5555/bot/test \
  -d '{"phone":"966500000000","text":"هل وجدت شيء؟"}'

# مساعد يجب يرد:
# "أهلا محمد، كنت تبحث عن شقة في جدة..."
```

### اختبار الـ LLM Fallback

```bash
# إذا فشل DeepSeek، جرّب Claude
DEEPSEEK_API_KEY=""  # فارغ
ANTHROPIC_API_KEY="sk-ant-..." # صحيح

curl -X POST http://localhost:5555/bot/test \
  -d '{"phone":"966500000000","text":"مرحبا"}'

# يجب يعمل مع Claude فقط
```

---

## 🎯 Checklist الاختبار الشامل

### قبل الإطلاق، تحقق من:

- [ ] **التسجيل يعمل**
  - [ ] تسجيل باحث
  - [ ] تسجيل مالك
  - [ ] استخراج البيانات صحيح

- [ ] **التفاوض يعمل**
  - [ ] بدء جلسة
  - [ ] نقل الأسعار
  - [ ] اقتراح وسط
  - [ ] إغلاق الصفقة

- [ ] **الأمان آمن**
  - [ ] لا رسائل لأرقام حقيقية (تلقائياً)
  - [ ] sandbox phones تعمل
  - [ ] MASAED_ALLOW_REAL_SEND=true يحتاج approval

- [ ] **الأداء مقبول**
  - [ ] RPS ≥ 100 (رسالة/ثانية)
  - [ ] Database response < 100ms
  - [ ] LLM response < 2 seconds

- [ ] **الـ Logs واضحة**
  - [ ] كل رسالة مسجّلة
  - [ ] خطأ واحد = لا رسالة حقيقية
  - [ ] Admin escalation مكتوبة

---

## 🔌 الربط مع Green API (الخطوة الفعلية)

### 1. احصل على Green API account
```
https://green-api.com
سجّل رقم WhatsApp خاص بك
احصل على:
  - idInstance: 7107624780
  - apiTokenInstance: abc123...
```

### 2. اضبط Webhook
```bash
# في لوحة تحكم Green API:
Webhook URL = https://your-domain.com/bot/webhook
Enable: incomingWebhook
```

### 3. اختبر اتصال حقيقي
```bash
export MASAED_GREEN_INSTANCE=7107624780
export MASAED_GREEN_TOKEN=abc123...
export MASAED_ALLOW_REAL_SEND=true

# الآن عندما ترسل رسالة لرقم مساعد على WhatsApp:
# أرسل واقعي يحدث!
```

### 4. اختبر من WhatsApp
```
افتح WhatsApp
أرسل رسالة: "مرحبا"

ترد مساعد:
"أهلاً وسهلاً! أنا مساعد العقاري..."
```

---

## 📈 النسخة الاختبارية vs الإنتاج

| المعيار | الاختبار | الإنتاج |
|--------|---------|---------|
| MASAED_SANDBOX_PHONES | 966500000000 | 966550858330,... |
| MASAED_ALLOW_REAL_SEND | false | true (بعد approval) |
| DATABASE | محلي أو تطوير | production server |
| Green API | تطوير account | production account |
| LLM temperature | 0.3 (آمن) | 0.3 (محفوظ) |
| Admin notifications | off (لا تزعج) | on (عاجل) |

---

## 🎓 مثال كامل: من التسجيل للإغلاق

```bash
#!/bin/bash

set -e  # توقف عند أول خطأ

echo "🧪 اختبار مساعد الكامل"
echo "========================="

# البيانات
LEAD_PHONE="966500000000"
OWNER_PHONE="966500000001"
API="http://localhost:5555"

# 1. تسجيل الباحث
echo "1️⃣ تسجيل الباحث..."
curl -s -X POST $API/bot/test \
  -H "Content-Type: application/json" \
  -d "{\"phone\":\"$LEAD_PHONE\",\"text\":\"مرحبا، أنا محمد، أبحث عن شقة 3 غرف في جدة، ميزانيتي 2200\"}" \
  | jq .reply

# 2. تسجيل المالك
echo ""
echo "2️⃣ تسجيل المالك..."
curl -s -X POST $API/bot/test \
  -H "Content-Type: application/json" \
  -d "{\"phone\":\"$OWNER_PHONE\",\"text\":\"أنا عندي شقة 3 غرف في جدة، الإيجار 2800 ريال\"}" \
  | jq .reply

# 3. بدء تفاوض
echo ""
echo "3️⃣ بدء التفاوض..."
NEG=$(curl -s -X POST $API/api/start-negotiation \
  -H "Content-Type: application/json" \
  -d "{
    \"lead_phone\":\"$LEAD_PHONE\",
    \"listing_phone\":\"$OWNER_PHONE\",
    \"listing_title\":\"شقة 3 غرف\",
    \"listing_city\":\"جدة\",
    \"listing_price\":2800
  }")

NEG_ID=$(echo $NEG | jq .neg_id)
echo "Negotiation ID: $NEG_ID"

# 4. عرض من الباحث
echo ""
echo "4️⃣ الباحث يعرض 2200..."
curl -s -X POST $API/bot/test \
  -H "Content-Type: application/json" \
  -d "{\"phone\":\"$LEAD_PHONE\",\"text\":\"أنا أعرض 2200 ريال\"}" \
  | jq .reply

# 5. رد المالك
echo ""
echo "5️⃣ المالك يرفض ويعرض 2600..."
curl -s -X POST $API/bot/test \
  -H "Content-Type: application/json" \
  -d "{\"phone\":\"$OWNER_PHONE\",\"text\":\"لا، أطلب 2600 على الأقل\"}" \
  | jq .reply

# 6. مساعد يقترح 2400
echo ""
echo "6️⃣ مساعد يقترح وسط 2400..."
echo "(يرسل للباحث والمالك)"

# 7. الباحث يقبل
echo ""
echo "7️⃣ الباحث يقبل!"
curl -s -X POST $API/bot/test \
  -H "Content-Type: application/json" \
  -d "{\"phone\":\"$LEAD_PHONE\",\"text\":\"موافق على 2400\"}" \
  | jq .reply

# 8. المالك يقبل
echo ""
echo "8️⃣ المالك يقبل!"
curl -s -X POST $API/bot/test \
  -H "Content-Type: application/json" \
  -d "{\"phone\":\"$OWNER_PHONE\",\"text\":\"تمام، موافق على 2400\"}" \
  | jq .reply

# ✅ النهاية
echo ""
echo "✅ اكتملت الصفقة!"
echo "========================="
```

---

## 📞 الدعم والمساعدة

إذا واجهت مشكلة:

1. **تحقق من السجلات:**
   ```bash
   tail -f scraper_api.log | grep ERROR
   ```

2. **اختبر الاتصالات:**
   ```bash
   # Database
   psql -U sanad -d sanad -c "SELECT 1"
   
   # LLM
   python3 -c "import anthropic; print('✓')"
   
   # Flask
   curl http://localhost:5555/health
   ```

3. **فعّل وضع Debug:**
   ```bash
   DEBUG=1 python3 scraper_api.py
   ```

---

**ابدأ الآن! 🚀**
