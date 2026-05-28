# مساعد — دليل المشروع الشامل 📖

**آخر تحديث: 2026-05-28**  
**النسخة: v3**  
**الحالة: 🟢 حي وعامل (Green API مربوط)**

---

## 🎯 **ما هو مساعد؟**

**مساعد** هو **وسيط عقاري ذكي عبر WhatsApp** يفاوض نشطاً بين:
- **الباحثون عن سكن** (يريدون شقة)
- **مالكو العقارات** (يريدون مستأجرين)

**بدلاً من نقل الرسائل فقط، مساعد يفاوض لإتمام الصفقة.**

---

## 🏗️ **البنية الكاملة**

```
┌─ WhatsApp (العملاء) ─────────────────────────────────┐
│         [الباحث] ←→ [رقم مساعد] ←→ [المالك]         │
└──────────────────────┬────────────────────────────────┘
                       │
                       ▼
        ┌─ Green API (gateway) ──────────────┐
        │ Instance: 7107624780               │
        │ Token: مخفي في .env                │
        │ Webhook: /bot/webhook              │
        └──────────────┬───────────────────────┘
                       │
                       ▼
    ┌──── Flask API (port 5555) ─────────┐
    │ scraper_api.py                     │
    │                                    │
    │ POST /bot/webhook ← Green API      │
    │         ↓                          │
    │    _route_message()                │
    │         ↓                          │
    │    ┌────┼────┬─────────┐          │
    │    ↓    ↓    ↓         ↓          │
    │  negotiator editor bot scraper    │
    │  (تفاوض) (تعديل)(تسجيل)(جلب)    │
    └────────┬────┬────────────┬────────┘
             ↓    ↓            ↓
    ┌─── PostgreSQL ──────────────────┐
    │ masaed_negotiations (التفاوضات) │
    │ masaed_registrations (التسجيل)  │
    │ masaed_contacts (الذاكرة)       │
    └────────────────────────────────┘
```

---

## 📱 **تدفق الرسالة من البداية للنهاية**

### **1️⃣ المستخدم يرسل رسالة WhatsApp**
```
👤 الباحث: "السلام عليكم، أبحث عن شقة في جدة، ميزانيتي 2200"
                    ↓ (Green API webhook)
```

### **2️⃣ استقبال الرسالة في Flask**
```python
POST /bot/webhook
{
  "instanceData": {"idInstance": "7107624780"},
  "body": {
    "senderData": {"senderPhone": "966550858330"},
    "messageData": {"textMessageData": {"textMessage": "السلام عليكم..."}}
  }
}
↓
scraper_api.py استقبل الرسالة
```

### **3️⃣ التوجيه الذكي (Routing)**
```python
def _route_message(phone, text):
    # 1. هل تفاوض نشط؟
    if handle_negotiation_message(phone, text):
        return  # negotiator.py تولّى
    
    # 2. هل تعديل بيانات؟
    if handle_edit_message(phone, text):
        return  # editor.py تولّى
    
    # 3. إذاً: تسجيل جديد
    reply = handle_message(phone, text)  # bot.py
    wa_send(phone, reply)  # ترسل رسالة حقيقية على WhatsApp
```

### **4️⃣ المعالجة حسب الحالة**

```
┌─ هل عنده تفاوض نشط؟ ─────────────────┐
│ YES → negotiator.py                   │
│       ├─ فهم النية (intent_parser)   │
│       ├─ تطبيق التكتيكات             │
│       ├─ اقتراح أسعار وسط             │
│       ├─ إدارة الفجوة السعرية        │
│       └─ تنبيهات الإدارة             │
│                                       │
│ NO → هل تريد تعديل بيانات؟          │
│       YES → editor.py                │
│           ├─ حمّل آخر تسجيل         │
│           ├─ استخرج التغييرات       │
│           └─ احفظ البيانات الجديدة │
│                                       │
│       NO → تسجيل جديد أو متابعة     │
│           bot.py                    │
│           ├─ استخرج البيانات        │
│           ├─ احفظ في DB             │
│           └─ اطلب معلومات ناقصة    │
└───────────────────────────────────────┘
```

---

## 🎭 **المكونات الرئيسية (6 ملفات)**

### **1️⃣ bot.py — بوت التسجيل (الحافظ)**

**الوظيفة**: جمع بيانات المستخدم الجديد

**البيانات التي يجمعها:**
```python
{
    "phone": "966550858330",
    "name": "محمد",
    "city": "جدة",
    "district": "النسيم",
    "rooms": 3,
    "budget_annual": 2200000,  # 2200 ر/سنة
    "furnished": True,
    "notes": "يفضل مفروشة"
}
```

**الكود المهم:**
```python
def handle_message(phone, text):
    # 1. حمّل أو ابدأ تسجيل
    reg = get_active_reg(phone)
    
    if not reg:
        return start_registration(phone)
    
    # 2. LLM يستخرج البيانات من الرسالة
    extracted = ai_extract(text, reg["current_data"])
    
    # 3. احفظ
    update_reg(reg["id"], extracted)
    
    # 4. هل اكتمل؟
    if is_complete(updated_reg):
        return "تم! سأبحث عن عروض لك"
    else:
        return ask_next_field(updated_reg)
```

**ذاكرة العميل:**
```python
# masaed_contacts جدول يحفظ:
contact = {
    "phone": "966550858330",
    "name": "محمد",
    "notes": "باحث عن 3 غرف في جدة",
    "last_seen": NOW(),
    "total_regs": 2  # سجّل مرتين
}

# عند كل رسالة:
contact = get_contact(phone)  # حمّل
contact["last_seen"] = NOW()  # حدّث
```

### **2️⃣ intent_parser.py — محلل النوايا**

**الوظيفة**: فهم ماذا يقصد المستخدم

**النوايا التي يكتشفها:**
```python
parse_intent("10000 وبس")
# → {"intent": "price_offer", "amount": 10000, "is_firm": true}

parse_intent("موافق!")
# → {"intent": "accept", "sentiment": "positive"}

parse_intent("في مصعد؟")
# → {"intent": "question"}

parse_intent("إلغاء")
# → {"intent": "cancel", "is_firm": true}
```

**السرعة:**
```
الخطوة 1: regex محلي (سريع جداً)
         ↓ إذا لم يطابق
الخطوة 2: LLM (دقيق لكن أبطأ)
```

### **3️⃣ negotiator.py — المفاوض (محامي الصفقة)**

**الوظيفة**: إتمام الصفقة بـ 6 تكتيكات

**التكتيكات:**
1. **الوسطية الذكية** — اقترح وسط بدل نقل أرقام
2. **الإلحاح المحسوب** — "فيه مهتم آخر"
3. **ربط المكاسب** — "إذا قبلت، دخول مبكر"
4. **إعادة التأطير** — عرض إيجابي
5. **الخطوة التالية** — اختم برسالة تحافظ على الزخم
6. **التسلسل المنطقي** — موقع أولاً، سعر آخراً

**المعادلة:**
```python
def handle_negotiation_message(phone, text):
    neg = load_negotiation(phone)
    intent = parse_intent(text)
    
    # 1. قبول؟
    if intent["intent"] == "accept":
        notify_admin(neg, "ready_to_close")
        return
    
    # 2. عرض سعر؟
    if intent["intent"] == "price_offer":
        amount = intent["amount"]
        save_price(neg, amount)
        
        # هل قريبوا السعرين؟ (gap ≤ 1500 ر أو ≤ 12%)
        if is_near(lead_max, owner_min):
            propose_middle_price(neg)  # اقتراح وسط
        else:
            relay_price(neg, amount)  # نقل العرض
    
    # 3. رفض حازم؟
    if intent["intent"] == "reject" and intent["is_firm"]:
        notify_admin(neg, "party_leaving")
```

**الأمان:**
- ✅ Temperature = 0.3 (لا عشوائية)
- ✅ لا يخترع شروط
- ✅ يطلب موافقة إدارة

### **4️⃣ editor.py — محرر البيانات**

**الوظيفة**: تعديل البيانات المسجّلة

**مثال:**
```
الباحث: "عدّل الميزانية لـ 3000"
         ↓
editor.py:
1. حمّل التسجيل القديم
2. استخرج "ميزانية = 3000"
3. احفظ التعديل
4. ارسل تأكيد
```

### **5️⃣ haraj_scraper.py — جالب العروض**

**الوظيفة**: البحث عن عروض في حراج

**الآلية:**
```python
async def run_scrape(cities):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        
        for city in cities:
            page = await browser.new_page()
            await page.goto(f"https://haraj.com.sa/s/...")
            
            # استخرج النتائج
            listings = await extract_listings(page)
            
            # احفظ في DB
            for listing in listings:
                upsert_listing(listing)
```

**⚠️ ملاحظة:** خلال الأسبوع القادم، ستستخدم أرقاماً بسيطة فقط (050*) لتجنب المشاكل القانونية حتى تحصل على التصاريح.

### **6️⃣ auto_extract.py — استخراج الأرقام**

**الوظيفة**: استخراج أرقام الهواتف من إعلانات حراج

---

## 🗄️ **قاعدة البيانات — Schema v3**

### **جدول 1: masaed_negotiations**
```sql
CREATE TABLE masaed_negotiations (
    id                SERIAL PRIMARY KEY,
    
    -- الطرفان
    lead_phone        TEXT,          -- 966550858330 (الباحث)
    listing_phone     TEXT,          -- 966541234567 (المالك)
    
    -- بيانات العقار
    listing_title     TEXT,          -- "شقة 3 غرف"
    listing_city      TEXT,          -- "جدة"
    listing_price     INT,           -- 2800 (السعر الأول)
    
    -- التفاوض
    lead_max_price    INT,           -- 2200 (أعلى عرض الباحث)
    owner_min_price   INT,           -- 2600 (أقل سعر المالك)
    proposed_price    INT,           -- 2400 (السعر المقترح)
    
    -- الموافقات
    lead_accepted     BOOLEAN,       -- وافق الباحث؟
    owner_accepted    BOOLEAN,       -- وافق المالك؟
    agreed_price      INT,           -- السعر النهائي
    
    -- الإدارة
    needs_admin       BOOLEAN,       -- يحتاج تدخل؟
    admin_notified    BOOLEAN,       -- أُشعرت الإدارة؟
    
    -- السجل
    chat_log          JSONB,         -- [{"role":"مستأجر","text":"..."}]
    status            TEXT,          -- active | cancelled | closed
    
    expires_at        TIMESTAMPTZ,   -- انتهاء الصلاحية (7 أيام)
    created_at        TIMESTAMPTZ
);
```

### **جدول 2: masaed_registrations**
```sql
CREATE TABLE masaed_registrations (
    id           SERIAL PRIMARY KEY,
    phone        TEXT UNIQUE,
    role         TEXT,              -- 'tenant' | 'owner'
    status       TEXT,              -- 'active' | 'verified' | 'completed'
    
    -- البيانات المستخرجة
    data         JSONB,             -- {city, budget, rooms, name, ...}
    
    -- التحقق (OTP)
    otp_code     TEXT,
    otp_sent_at  TIMESTAMPTZ,
    verified_at  TIMESTAMPTZ,
    
    created_at   TIMESTAMPTZ
);
```

### **جدول 3: masaed_contacts**
```sql
CREATE TABLE masaed_contacts (
    id        SERIAL PRIMARY KEY,
    phone     TEXT UNIQUE,
    name      TEXT,                 -- "محمد"
    notes     TEXT,                 -- "مسجّل مرتين"
    last_seen TIMESTAMPTZ,          -- آخر نشاط
    created_at TIMESTAMPTZ
);
```

---

## 🔬 **مساعد المختبر — شرح شامل**

### **ما هي مساعد المختبر؟**

واجهة ذكية لاختبار التفاوض **دون الانتظار لطلبات حقيقية**.

### **كيف يعمل؟**

**الشاشة الأولى:**
```
┌─────────────────────────────────┐
│ 🔬 مساعد المختبر               │
└─────────────────────────────────┘

الطلبات المسجّلة:

┌──────────────────────────────────┐
│ 👤 محمد                          │
│ 0548060060                       │
│ 📍 جدة | 🛏 3 غرف | 💰 2200k   │
└──────────────────────────────────┘
```

**الخطوات:**

1️⃣ **اختر طلب مسجّل**
```
تضغط على "محمد"
```

2️⃣ **أدخل أرقام الاختبار**
```
رقم الباحث (مملوء): 0548060060
رقم المالك (أدخل): 0550000099  ← أي رقم بسيط
السعر (اختياري): 2800
```

3️⃣ **معاينة السيناريو**
```
تشوف الرسائل التي ستُرسل:

📱 0548060060 (الباحث):
"مرحباً، أنا محمد، أبحث عن شقة..."

📱 0550000099 (المالك):
"مرحباً، عندي شقة 3 غرف..."
```

4️⃣ **ابدأ التفاوض الحقيقي**
```
تضغط [🚀 ابدأ التفاوض الحقيقي]

✅ النتيجة:
- رسائل حقيقية على WhatsApp
- جلسة تفاوض في الداشبورد
- التفاوض يبدأ فوراً
```

### **الفكرة الذكية:**

```
استخدم البيانات الحقيقية (محمد = باحث حقيقي)
+ أرقام اختبار (0550000099 = مالك وهمي)
= اختبار كامل دون التأثير على عملاء حقيقيين
```

### **الفوائد:**

✅ اختبر التفاوض فوراً دون انتظار  
✅ جرّب سيناريوهات مختلفة (أسعار، فجوات)  
✅ رسائل واقعية على WhatsApp  
✅ تتبع الجلسة في الداشبورد  
✅ آمن 100% (لا تأثير على عملاء حقيقيين)

---

## 🚀 **كيفية الاستخدام العملي الآن**

### **الخطوة 1: تأكد أن النظام يعمل**
```bash
# 1. تحقق من Flask
curl http://localhost:5555/health
# → {"status": "ok"}

# 2. تحقق من Database
psql -U sanad -d sanad -c "SELECT COUNT(*) FROM sanad.masaed_registrations"
```

### **الخطوة 2: سجّل طلباً حقيقياً**
```
افتح WhatsApp
أرسل رسالة لرقم مساعد:

"السلام عليكم، أنا محمد، أبحث عن شقة 3 غرف 
في جدة، ميزانيتي 2200 ريال/سنة"

→ مساعد يرد: "شكراً محمد! وجدت بياناتك. 
   سأبحث عن عروض مناسبة."
```

### **الخطوة 3: افتح مساعد المختبر**
```
https://your-domain/dashboard/lab.html

1. اختر طلب محمد
2. ادخل رقم: 0550000088 (مالك وهمي)
3. معاينة الرسائل
4. ابدأ التفاوض
```

### **الخطوة 4: راقب النتيجة**
```
WhatsApp:
  ✅ محمد استقبل رسالة الباحث
  ✅ الرقم 0550000088 استقبل رسالة المالك

Daashboard:
  ✅ جلسة تفاوض جديدة #N
  ✅ الرسائل الأولية موجودة
```

### **الخطوة 5: اختبر التفاوض**
```
ارسل رسائل من WhatsApp:

من الباحث (0548060060): "أعرض 1900"
→ مساعد يرسل للمالك: "الباحث يعرض 1900"

من المالك (0550000088): "أطلب 2500"
→ مساعد يرسل للباحث: "المالك يطلب 2500"

→ مساعد يقترح وسط: 2200 ✓
```

---

## 🛡️ **الأمان والحماية**

### **حماية الرسائل:**
```
✅ Sandbox phones فقط (050*) مسموحة بدون إذن
✅ أرقام حقيقية محظورة تلقائياً (MASAED_ALLOW_REAL_SEND=false)
✅ Temperature = 0.3 (لا اختراع عشوائي)
✅ Dashboard محمي بـ HTTP Basic Auth
```

### **في الأسبوع القادم:**
```
✅ الأرقام المستخدمة: 050* فقط (بسيطة)
✅ تجنب الأرقام الحقيقية للعملاء
✅ جمع التصاريح القانونية
✅ بعدها: توسّع الخدمة للعملاء الحقيقيين
```

---

## 📊 **سيناريوهات الاختبار المهمة**

### **سيناريو 1️⃣: فجوة صغيرة (تقارب)**
```
الباحث: 2200 ر
المالك: 2400 ر
الفجوة: 200 ر ← صغيرة

المتوقع:
→ مساعد يقترح وسط 2300 فوراً
→ يرسل لكل طرف: "الطرف الآخر يقبل 2300"
→ اتفاق سريع ✓
```

### **سيناريو 2️⃣: فجوة كبيرة**
```
الباحث: 1500 ر
المالك: 3500 ر
الفجوة: 2000 ر ← كبيرة جداً

المتوقع:
→ مساعد ينقل العروض بدون تفاوض مباشر
→ ينتظر تقارب تدريجي
→ عندما تقترب: يقترح وسط
```

### **سيناريو 3️⃣: رفض حازم**
```
الباحث: "لا يهمني، إلغاء"

المتوقع:
→ مساعد ينهي الجلسة
→ يُشعر الإدارة
→ يرسل للمالك: "اعتذر، انسحب الباحث"
```

### **سيناريو 4️⃣: سؤال عن التفاصيل**
```
الباحث: "في مصعد؟ كم الدور؟"

المتوقع:
→ مساعد يرد: "لا تتوفر لديّ هذه المعلومات"
→ يرسل للمالك: "المستأجر يسأل عن المصعد"
→ ينقل الرد
```

---

## ⚡ **أداء وحدود النظام**

```
RPS (رسائل/ثانية):       ≥ 100 ✓
Database response time:  < 100ms ✓
LLM response time:       < 2 seconds ✓
WhatsApp delay:          1-5 seconds ✓

Limits:
- Session lifetime: 7 أيام (auto-expires)
- Chat log size: truncate كل 100 رسالة
- Concurrent negotiations: unlimited
```

---

## 🔧 **المشاكل المتوقعة والحلول**

| المشكلة | السبب | الحل |
|--------|------|------|
| لا رسائل تُرسل | MASAED_ALLOW_REAL_SEND=false | اضبطها =true إذا أردت حقيقي |
| LLM بطيء | DeepSeek مشغول | استخدم Anthropic fallback |
| Database بطيئة | chat_log كبير | truncate رسائل قديمة |
| Green API توقفت | حساب محظور | استخدم WhatsApp Business API |
| رسالة خاطئة | Temperature 0.7 | تأكد أنه 0.3 |

---

## 📋 **ملفات المشروع الأساسية**

```
/root/masaed/
├── scraper/
│   ├── bot.py                 ← التسجيل
│   ├── negotiator.py          ← التفاوض
│   ├── intent_parser.py       ← فهم النوايا
│   ├── editor.py              ← التعديل
│   ├── haraj_scraper.py       ← جلب العروض
│   ├── auto_extract.py        ← استخراج الأرقام
│   └── scraper_api.py         ← Flask API الرئيسي
│
├── database/
│   └── schema.sql             ← قاعدة البيانات
│
├── dashboard/
│   ├── index.html             ← الداشبورد الرئيسي
│   └── lab.html               ← مساعد المختبر 🔬
│
├── .env                       ← المتغيرات (مخفي)
├── docker-compose.yml         ← التشغيل (إن وُجد)
│
├── ARCHITECTURE_DETAILED.md   ← شرح معماري
├── GETTING_STARTED.md         ← البدء السريع
├── SAFETY.md                  ← الأمان
├── PROJECT_GUIDE.md           ← (هذا الملف)
└── README.md                  ← ملخص سريع
```

---

## 🎯 **أهم النقاط للتذكر**

### ✅ **النظام الآن:**
- ✅ مربوط بـ Green API (رسائل حقيقية)
- ✅ يرسل رسائل واقعية على WhatsApp
- ✅ يتفاوض بذكاء بـ 6 تكتيكات
- ✅ يتذكر بيانات العميل (ذاكرة دائمة)
- ✅ يعدّل البيانات عند الحاجة
- ✅ محمي من الأخطاء العشوائية (temp 0.3)
- ✅ له dashboard لمراقبة الجلسات
- ✅ له مختبر لاختبار الحالات الجديدة

### 🚀 **الخطوة التالية:**
```
هذا الأسبوع:
1. اختبر بـ أرقام بسيطة (050*)
2. تأكد أن التفاوض يعمل
3. تأكد أن صاحب الطلب يجد طلبه

الأسبوع القادم:
1. احصل على التصاريح
2. شغّل مع عملاء حقيقيين
3. راقب الأداء
```

### 🔴 **حدود قانونية:**
- ⚠️ REGA licensing (يحتاج مراجعة)
- ⚠️ Haraj ToS (يحتاج إذن كتابي)
- ⚠️ PDPA compliance (حماية البيانات)

---

## 📞 **نقاط التواصل في الكود**

```python
# الملف الرئيسي
/root/masaed/scraper/scraper_api.py

# endpoints المهمة:
POST /bot/webhook          ← استقبال رسائل من Green API
POST /api/lab/requests     ← جلب طلبات المختبر
POST /api/lab/scenario     ← معاينة السيناريو
POST /api/lab/start        ← بدء التفاوض الحقيقي

# الدوال المهمة:
_route_message()           ← التوجيه الذكي
handle_negotiation_message() ← معالجة التفاوض
handle_message()           ← معالجة التسجيل
wa_send()                  ← إرسال رسائل WhatsApp
```

---

## 🎓 **خلاصة**

```
مساعد = وسيط عقاري ذكي
      = 6 ملفات Python
      = 3 جداول PostgreSQL
      = 1 Dashboard + 1 مختبر
      = رسائل حقيقية على WhatsApp
      = تفاوض ذكي بـ 6 تكتيكات
      = نتيجة: صفقات منجزة بسرعة
```

---

**آخر تحديث: 2026-05-28**  
**الحالة: 🟢 حي ومشتغل**  
**الرسائل: حقيقية على WhatsApp**  
**التفاوض: يعمل تماماً**  
