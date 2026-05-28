# مساعد — شرح الكود الشامل

## 🎯 الفكرة الأساسية

**مساعد** هو وسيط عقاري ذكي يربط بين:
1. **الباحثون عن سكن** (يريدون شقة)
2. **مالكو العقارات** (يريدون مستأجرين)

بدلاً من مجرد **نقل الرسائل**، مساعد **يفاوض نشطاً** لإتمام الصفقة.

---

## 📋 المشكلة التي نحل

### الوضع الحالي (بدون مساعد):
```
مالك: "الإيجار 2800 ريال"
باحث: "ميزانيتي 2200 فقط"
↓
[لا حد يتنازل، الصفقة تنهار]
```

### مع مساعد:
```
مالك: "الإيجار 2800 ريال"
باحث: "ميزانيتي 2200 فقط"
↓
مساعد (للمالك): "المستأجر جاد. هل تقبل 2400 + دفع مقدم؟"
مساعد (للباحث): "المالك نزل لـ 2400. أقرب لميزانيتك. موافق؟"
↓
[اتفاق! الصفقة انعقدت]
```

---

## 🏗️ معمارية النظام

### الطبقات الرئيسية:

```
┌─────────────────────────────────────────────────────────────────┐
│                     WhatsApp (المستخدمون)                       │
│        [مالك] ──────────────── [باحث]                           │
└────────────────────┬──────────────────────────────────────────┘
                     │
                     ▼
         ┌──────────────────────────┐
         │   Green API              │ ← gateway WhatsApp غير رسمي
         │   (webhook: /bot)        │
         └──────────┬───────────────┘
                    │
                    ▼
    ┌───────────────────────────────────────┐
    │   Flask API (port 5555)               │
    │   scraper_api.py                      │
    │  ┌─────────────────────────────────┐  │
    │  │ Route Message (_route_message)  │  │
    │  │  ↓                              │  │
    │  │ 1. negotiator.py    ← تفاوض    │  │
    │  │ 2. editor.py        ← تعديل    │  │
    │  │ 3. bot.py (register)← تسجيل   │  │
    │  └─────────────────────────────────┘  │
    └───────────────┬──────────────────────┘
                    │
                    ▼
    ┌───────────────────────────────────────┐
    │      PostgreSQL (sanad schema)        │
    │  ┌─────────────────────────────────┐  │
    │  │ masaed_negotiations             │  │
    │  │  id, lead_phone, listing_phone  │  │
    │  │  lead_max_price, owner_min_price│  │
    │  │  chat_log (JSONB)               │  │
    │  └─────────────────────────────────┘  │
    │  ┌─────────────────────────────────┐  │
    │  │ masaed_registrations (OTP)      │  │
    │  │  phone, role, status            │  │
    │  └─────────────────────────────────┘  │
    │  ┌─────────────────────────────────┐  │
    │  │ masaed_contacts (ذاكرة العميل) │  │
    │  │  phone, name, notes, last_seen  │  │
    │  └─────────────────────────────────┘  │
    └───────────────────────────────────────┘
```

---

## 🔄 تدفق الرسالة (من البداية للنهاية)

### الخطوة 1️⃣: المستخدم يرسل رسالة WhatsApp
```
👤 الباحث: "أبغى شقة في جدة، السعر 2200 ريال"
    ↓ (Green API webhook)
```

### الخطوة 2️⃣: استقبال الرسالة في Flask
```python
# /bot/webhook في scraper_api.py
@app.route("/bot/webhook", methods=["POST"])
def bot_webhook():
    phone = data["from"]  # 966550858330
    text = data["message"]  # "أبغى شقة في جدة، السعر 2200 ريال"
    _route_message(phone, text)  # ← نقطة الدخول الرئيسية
```

### الخطوة 3️⃣: التوجيه الذكي (routing)
```python
# في scraper_api.py: _route_message()
def _route_message(phone, text):
    # 1. هل تفاوض نشط؟
    if handle_negotiation_message(phone, text):
        return  # تم معالجته من قبل negotiator.py
    
    # 2. هل تعديل بيانات قديمة؟
    if handle_edit_message(phone, text):
        return  # تم معالجته من قبل editor.py
    
    # 3. إذاً: تسجيل جديد أو متابعة تسجيل
    reply = handle_message(phone, text)  # bot.py
    wa_send(phone, reply)
```

---

## 🎭 المكونات الرئيسية

### 1️⃣ **bot.py** — بوت التسجيل (الحافظ)

**الوظيفة**: جمع بيانات المستخدم عند أول تواصل

**الحالات التي يتعامل معها:**
- تسجيل جديد (مستأجر/مالك)
- تجميع البيانات تدريجياً عبر محادثة
- تعديل البيانات القديمة

**المعادلة:**
```
المستخدم الجديد
      ↓
سؤال تفصيلي بـ LLM (يتحدث العربية الطبيعية)
      ↓
يستخرج البيانات: (city, budget, bedrooms, name, etc)
      ↓
يحفظها في masaed_registrations + masaed_contacts
      ↓
ينتقل لـ negotiator.py عند اكتمال البيانات
```

**الكود الأساسي:**
```python
def handle_message(phone, text):
    # 1. حمّل أو أنشئ registration
    reg = get_active_reg(phone)
    
    if not reg:
        # تسجيل جديد
        return start_registration(phone)
    
    # 2. LLM يستخرج البيانات من الرسالة
    extracted = ai_extract(text, reg["current_data"])
    
    # 3. احفظ البيانات
    update_reg(reg["id"], extracted)
    
    # 4. هل التسجيل مكتمل؟
    if is_complete(updated_reg):
        return "تم حفظ بياناتك. سأبحث عن عروض!"
    else:
        return ask_next_field(updated_reg)
```

**معلومات مهمة:**
- ✅ يحفظ سياق المحادثة (chat_history)
- ✅ لا يسأل نفس السؤال مرتين
- ✅ يتذكر البيانات السابقة (masaed_contacts)

---

### 2️⃣ **intent_parser.py** — محلل النوايا (قارئ الأفكار)

**الوظيفة**: فهم ماذا يقصد المستخدم من رسالته

**النوايا التي يكتشفها:**
```python
{
    "intent": "price_offer",      # عرض سعر
    "amount": 2200,               # الرقم المذكور
    "sentiment": "positive",      # الموقف (إيجابي/سلبي)
    "is_firm": False             # هل الموقف نهائي؟
}
```

**أمثلة:**
```
"10000 وبس" 
  → {"intent":"price_offer", "amount":10000, "is_firm":true}

"موافق!"
  → {"intent":"accept", "sentiment":"positive"}

"لا يهمني" 
  → {"intent":"cancel", "sentiment":"negative", "is_firm":true}

"في مصعد؟"
  → {"intent":"question"}
```

**كيفية العمل:**
```python
def parse_intent(text):
    # 1. تحقق من regex السريع أولاً (لا شبكة)
    if text in _ACCEPT_EXACT:
        return {"intent": "accept", ...}
    
    # 2. إذا غامض → استدعِ LLM
    response = llm(text)
    return json.loads(response)
```

**الفائدة**: توجيه سريع لماذا يريد المستخدم (قبول/رفض/تعديل سعر)

---

### 3️⃣ **negotiator.py** — المفاوض (محامي الصفقة)

**الوظيفة**: إتمام الصفقة بناءً على نوايا الطرفين

**التكتيكات الـ 6:**
1. **الوسطية الذكية**: اقترح سعراً وسطاً بذكاء
2. **الإلحاح المحسوب**: "فيه مهتم آخر بنفس الشقة"
3. **ربط المكاسب**: "إذا قبلت، يمكنك الدخول مبكراً"
4. **إعادة التأطير**: عرض العرض بصيغة إيجابية
5. **الخطوة التالية**: اختم برسالة تحافظ على الزخم
6. **التسلسل المنطقي**: الموقع أولاً، ثم السعر

**معادلة التفاوض:**
```python
def handle_negotiation_message(phone, text):
    neg = load_negotiation(phone)
    intent = parse_intent(text)
    
    # حالة 1: قبول
    if intent["intent"] == "accept":
        notify_admin(neg, "ready_to_close")
        return
    
    # حالة 2: عرض سعر
    if intent["intent"] == "price_offer":
        amount = intent["amount"]
        save_price(neg, amount)
        
        # هل قريبوا السعرين؟
        if is_near(lead_max, owner_min):
            propose_middle_price(neg)  # اقتراح وسط
        else:
            relay_price(neg, amount)   # نقل العرض للطرف الآخر
    
    # حالة 3: رفض حازم
    if intent["intent"] == "reject" and intent["is_firm"]:
        notify_admin(neg, "party_leaving")
```

**تقليل المخاطر:**
- ✅ Temperature = 0.3 (لا اختراع عشوائي)
- ✅ لا يقترح شروط ما لم يقلها الطرفان
- ✅ يطلب موافقة الإدارة قبل الإغلاق

---

### 4️⃣ **editor.py** — محرر البيانات

**الوظيفة**: تعديل البيانات المسجلة سابقاً

**مثال:**
```
👤 مالك: "عدّل السعر لـ 3000 بدل 2800"
      ↓
editor.py: يحمّل التسجيل القديم
           يستخرج "السعر = 3000"
           يحفظ التعديل
           يرسل تأكيد: "تم تعديل السعر!"
```

**الكود:**
```python
def handle_edit_message(phone, text):
    # 1. هل هناك طلب تعديل؟
    if not _EDIT_TRIGGERS.search(text):
        return None
    
    # 2. حمّل آخر تسجيل
    old_reg = get_contact_registrations(phone)[-1]
    
    # 3. LLM يستخرج البيانات الجديدة من الرسالة
    extracted = ai_extract_changes(text, old_reg)
    
    # 4. احفظ
    update_reg(old_reg["id"], extracted)
    return "تم التعديل ✓"
```

---

### 5️⃣ **auto_extract.py** — استخراج البيانات التلقائي

**الوظيفة**: جلب أرقام الهواتف من الإعلانات (حراج)

**المشكلة:** حراج يخفي الأرقام × لكننا نحتاجها للتواصل

**الحل:** Playwright يفتح الصفحة ويحفر عن الرقم
```python
async def extract_phone(page, url):
    await page.goto(url)
    
    # 1. جرّب React Router (يستخرج من JSON)
    # 2. جرّب فتح الرقم (كلك الاتصال)
    # 3. جرّب HTML parsing
    
    return phone  # 966550858330
```

**⚠️ تحذير قانوني:** استخراج الأرقام من حراج قد ينتهك ToS — نحتاج إذن كتابي!

---

### 6️⃣ **rules_engine.py** — محرك القواعد

**الوظيفة**: قرارات حتمية (بدون LLM)

**الأسئلة التي يجيب عليها:**
```python
def is_near(lead_max, owner_min, listing_price):
    """هل السعران متقاربان بما يكفي للاقتراح؟"""
    gap = owner_min - lead_max
    
    # شروط التقارب:
    return (
        lead_max >= owner_min or          # الطلب أعلى من السعر!
        gap <= 1500 or                    # فجوة ≤ 1500 ريال
        (gap / listing_price) <= 0.12     # أو ≤ 12% من السعر
    )
```

**متى نستخدمه:**
- ✅ تقرير الفجوة (سريع)
- ✅ إذا طلب إدارة (رسمي)
- ❌ لا للرسائل الإبداعية (استخدم LLM)

---

## 🗄️ البيانات — Database Schema v3

### جدول 1: masaed_negotiations (التفاوضات)
```sql
CREATE TABLE masaed_negotiations (
    id              SERIAL PRIMARY KEY,
    lead_phone      TEXT,                    -- رقم الباحث
    listing_phone   TEXT,                    -- رقم المالك
    
    -- بيانات العقار
    listing_title   TEXT,                    -- "شقة 3 غرف"
    listing_price   INT,                     -- 2800
    
    -- التفاوض
    lead_max_price  INT,                     -- أعلى عرض الباحث (2200)
    owner_min_price INT,                     -- أقل سعر المالك (2400)
    proposed_price  INT,                     -- السعر المقترح (2300)
    
    -- الموافقات
    lead_accepted   BOOLEAN DEFAULT false,   -- وافق الباحث؟
    owner_accepted  BOOLEAN DEFAULT false,   -- وافق المالك؟
    
    -- الإدارة
    needs_admin     BOOLEAN DEFAULT false,   -- يحتاج تدخل إدارة؟
    admin_notified  BOOLEAN DEFAULT false,   -- تم إشعار الإدارة؟
    
    -- السجل
    chat_log        JSONB DEFAULT '[]',      -- [{"role":"مستأجر","text":"...","ts":"..."}]
    status          TEXT DEFAULT 'active',   -- active|cancelled|closed
    
    expires_at      TIMESTAMPTZ,             -- نهاية الصلاحية (7 أيام)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### جدول 2: masaed_registrations (التسجيلات)
```sql
CREATE TABLE masaed_registrations (
    id          SERIAL PRIMARY KEY,
    phone       TEXT UNIQUE,                 -- رقم المستخدم
    role        TEXT,                        -- 'tenant'|'owner'
    status      TEXT,                        -- 'active'|'verified'|'completed'
    
    -- البيانات المستخرجة
    data        JSONB,                       -- {city, budget, bedrooms, name, ...}
    
    -- التحقق
    otp_code    TEXT,
    otp_sent_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    
    created_at  TIMESTAMPTZ
);
```

### جدول 3: masaed_contacts (ذاكرة العميل)
```sql
CREATE TABLE masaed_contacts (
    id          SERIAL PRIMARY KEY,
    phone       TEXT UNIQUE,
    name        TEXT,                       -- "محمد"
    notes       TEXT,                       -- "مسجّل مرتين"
    last_seen   TIMESTAMPTZ,               -- آخر نشاط
    created_at  TIMESTAMPTZ
);
```

**ملاحظة مهمة:** chat_log مخزنة في JSON واحد → صعب الاستعلام → نحتاج جدول منفصل للرسائل لاحقاً

---

## 🚨 المشاكل والحلول

### المشكلة 1️⃣: **نسيان العميل** (السياق المفقود)

**الوصف:**
```
اليوم: "أريد شقة في جدة"
غداً: "هل وجدت شيء؟"
مساعد: "من أنت؟ ما احتياجاتك؟" ← خطأ! نسي العميل
```

**الحل v3:**
```python
# 1. جدول masaed_contacts يحفظ:
contact = {
    "phone": "966550858330",
    "name": "محمد",
    "notes": "باحث عن 3 غرف، ميزانية 2200",
    "last_seen": "2026-05-28 22:35"
}

# 2. عند كل رسالة:
contact = get_contact(phone)  # حمّل البيانات
contact = update_contact(phone, {"last_seen": NOW()})

# 3. في الـ prompt:
_SYS_QUESTION = """
...
اسم المتحدث: {contact['name']}
ملاحظات: {contact['notes']}
"""
```

**✅ النتيجة**: مساعد يتذكر: "آهلا محمد، كنت تبحث عن شقة بـ 2200"

---

### المشكلة 2️⃣: **جلب البيانات** (من أين نأتي بالعروض؟)

**الوصف:**
```
الباحث: "أريد شقة في جدة"
مساعد: "تمام! سأبحث... لكن أين؟"
```

**الحلول الممكنة:**

| الحل | الكود | الإيجابيات | السلبيات |
|-----|------|-----------|---------|
| **حراج scraper** | haraj_scraper.py | عروض حقيقية كثيرة | ينتهك ToS ⚠️ |
| **n8n workflow** | /api/matches | يمكن جدولتها | بطيء |
| **أدرج يدويّاً** | Flask form | آمن قانونياً | عمل يدوي |

**الحل الحالي:** 
```python
# haraj_scraper.py يستخدم Playwright
async def run_scrape(cities):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        
        for city in cities:
            for query in ["ابحث عن شقة", "محتاج إيجار"]:
                page = await browser.new_page()
                await page.goto(f"https://haraj.com.sa/s/{query}/{city}")
                
                # استخرج النتائج
                listings = await extract_listings(page)
                
                # احفظ في DB
                for listing in listings:
                    upsert_listing(listing)
```

**⚠️ التوصية:** احصل على إذن من حراج أولاً!

---

### المشكلة 3️⃣: **تعديل البيانات** (غيّرت رأيي!)

**الوصف:**
```
الباحث (اليوم): "الميزانية 2200"
الباحث (غداً): "في المال دلوقتي، الميزانية 2500"
```

**الحل:**
```python
# editor.py يلتقط: "عدّل الميزانية"
if _EDIT_TRIGGERS.search(text):
    # 1. حمّل التسجيل القديم
    old = get_registrations(phone)[-1]
    
    # 2. استخرج التغييرات فقط
    extracted = ai_extract_changes(text, old)
    # → {"budget": 2500}
    
    # 3. احفظ
    update_reg(old["id"], extracted)
    # النتيجة: {budget: 2500, city: "جدة", ...}
```

**✅ النتيجة**: مساعد يحدّث بياناتك بدون إعادة التسجيل

---

### المشكلة 4️⃣: **فتح حوار** (كيف تعرف مساعد نفسه؟)

**الوصف:**
```
الباحث: "من أنت؟ كيف وجدت رقمي؟"
```

**الحل v3:**
```python
# في negotiator.py: _SYS_INTRO
_SYS_INTRO = """
أنت "مساعد" — وسيط عقاري إلكتروني.
أجب بوضوح:
١. عرّف نفسك: "أنا مساعد العقاري"
٢. اشرح المصدر: "وجدت رقمك من طلبك المُسجَّل"
٣. اذكر العرض: "وجدت لك شقة 3 غرف في جدة"
"""

# في negotiate_message():
if is_identity_question(text):
    source = "وجدت رقمك من إعلانك في حراج" if role == "مالك" \
             else "وجدت رقمك من طلبك المُسجَّل"
    reply = llm(_SYS_INTRO.format(source=source), text)
```

**✅ النتيجة**: 
```
المستخدم: "من أنت؟"
مساعد: "أنا مساعد العقاري، وسيط إلكتروني. وجدت رقمك من 
        طلبك المُسجَّل للبحث عن سكن. وجدت لك شقة في جدة 
        بـ 2800 ريال/سنة. تحدّث معي مباشرة!"
```

---

### المشكلة 5️⃣: **تلبية الطلبات** (طريقة العمل)

**الوصف:**
```
الباحث: "أريد شقة 3 غرف في جدة، السعر 2000"
     ↓
كيف يعرف مساعد أن هذا طلب؟
كيف يبحث عن عروض مناسبة؟
كيف يبدأ التفاوض؟
```

**الحل (Pipeline):**

```
خطوة 1: استقبال الطلب
─────────────────────
الباحث: "أبغى شقة 3 غرف، ميزانية 2200"
         ↓ (Green API webhook)
         ↓ (Flask: /bot/webhook)

خطوة 2: استخراج البيانات
─────────────────────
LLM (من bot.py):
  extracted = {
    "city": "جدة",
    "rooms": 3,
    "budget": 2200,
    "name": "محمد"
  }

خطوة 3: حفظ في DB
─────────────────────
masaed_registrations:
  id=1, phone=966550858330, data={...}

خطوة 4: البحث عن عروض (حراج)
─────────────────────
haraj_scraper.py يجد:
  - شقة في الرياض 3 غرف بـ 1900 ✓ مناسبة
  - شقة في الدمام 2 غرف بـ 2500 ✗ غير مناسبة

خطوة 5: الربط والتفاوض
─────────────────────
start_negotiation(
  lead_phone=966550858330,
  listing_phone=966541234567,
  listing_title="شقة 3 غرف",
  listing_price=2200
)

✓ session created!

خطوة 6: التفاوض
─────────────────────
→ negotiator.py يتحكم بالحوار
→ يفاوض حتى اتفاق أو رفض
```

---

## ⚙️ العقبات المحتملة

### 1. **الذاكرة المحدودة**
```
❌ مشكلة: إذا كان chat_log كبير جداً، البحث يبطأ
✅ الحل: truncate chat_log أقدم من 100 رسالة
```

### 2. **LLM يختلق بيانات**
```
❌ مشكلة: LLM قد يقول "السعر = 500 مليار" (عشوائي)
✅ الحل: temperature = 0.3 + validation + admin check
```

### 3. **الرسائل المتزامنة** (race condition)
```
❌ مشكلة: رسالتان في نفس الثانية من نفس الرقم
✅ الحل: _phone_lock في bot.py
        with _phone_lock(phone):
            # معالجة آمنة
```

### 4. **Green API قد تتوقف**
```
❌ مشكلة: اتساب يحظر الحسابات غير الرسمية
✅ الحل: استخدم WhatsApp Business API الرسمي
```

### 5. **قاعدة البيانات ممتلئة**
```
❌ مشكلة: chat_log تراكم 1000 رسالة = بطء
✅ الحل: archive قديم من 90 يوم
```

### 6. **OTP لم يتم تطبيقه**
```
❌ مشكلة: أي شخص يمكنه ادّعاء أنه مالك عقار
✅ الحل: تطبيق Twilio SMS verification
```

---

## 🔧 الحالة الحالية — أيهما جاهز؟

### ✅ جاهز (Production-Ready)

| المكون | الحالة | ملاحظات |
|--------|--------|---------|
| **bot.py** | ✅ جاهز | تسجيل المستخدمين يعمل |
| **negotiator.py** | ✅ جاهز | التفاوض v3 يعمل |
| **intent_parser.py** | ✅ جاهز | كشف النوايا سريع |
| **editor.py** | ✅ جاهز | تعديل البيانات يعمل |
| **database/schema.sql** | ✅ جاهز | v3 توحيد كامل |
| **Safety guards** | ✅ جاهز | sandbox phones + temperature |
| **Dashboard auth** | ✅ جاهز | HTTP Basic Auth |

### 🟡 نصف جاهز (In Progress)

| المكون | الحالة | الناقص |
|--------|--------|---------|
| **haraj_scraper.py** | 🟡 | ✓ الكود موجود، لكن ينتهك ToS |
| **OTP verification** | 🟡 | ✓ schema جاهز، لكن SMS لم يُربط |
| **Admin notification** | 🟡 | ✓ logic جاهز، لكن WhatsApp/Email لم يُوصل |
| **n8n workflow** | 🟡 | ✓ v1 موجود، لكن لم يُحدّث لـ v3 |

### ❌ غير جاهز (To Do)

| المكون | المشكلة |
|--------|--------|
| **Messaging other party** | يحتاج اختبار شامل |
| **Contract generation** | لم يُكتب الكود |
| **Payment integration** | لم يُكتب الكود |
| **REGA compliance** | مراجعة قانونية مطلوبة |

---

## 📝 الخطوات المطلوبة قبل الإطلاق

### 1️⃣ **تشغيل موضعي** (اختبار على جهازك)
```bash
# 1. تثبيت المتطلبات
pip install flask psycopg2-binary anthropic openai playwright

# 2. إنشاء قاعدة البيانات
psql -h localhost -U sanad -d sanad -f database/schema.sql

# 3. تشغيل API
cd scraper
python3 -u scraper_api.py

# 4. اختبار
curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{"phone":"966500000000","text":"مرحبا"}'
```

### 2️⃣ **حل المشاكل القانونية**
```
[ ] استشر محامي عقاري سعودي عن REGA
[ ] احصل على إذن كتابي من Haraj.com
[ ] ادرس PDPA (حماية البيانات الشخصية)
```

### 3️⃣ **ربط الخدمات**
```
[ ] Green API: webhook URL صحيحة
[ ] Twilio: OTP SMS verification
[ ] Email/WhatsApp: إشعارات الإدارة
```

### 4️⃣ **اختبارات شاملة**
```
[ ] اختبار التسجيل الكامل
[ ] اختبار التفاوض من البداية للنهاية
[ ] اختبار race conditions
[ ] اختبار الأمان: لا رسائل لأرقام حقيقية
```

### 5️⃣ **التوثيق والتدريب**
```
[ ] توثيق الـ API
[ ] دليل المسؤول (كيفية قراءة السجلات)
[ ] تدريب الفريق
```

---

## 🎯 الخلاصة

| السؤال | الجواب |
|--------|--------|
| **ماذا يفعل مساعد؟** | يفاوض ذكياً بين الطرفين لإتمام الصفقة |
| **كيف يتذكر البيانات؟** | masaed_contacts + chat_log في DB |
| **كيف يجد العروض؟** | haraj_scraper.py (يحتاج إذن) |
| **هل الكود جاهز؟** | ✅ 60% جاهز، 40% يحتاج ربط خدمات + اختبار |
| **ما أكبر مشكلة؟** | 🔴 REGA licensing + Haraj ToS ⚠️ |
| **كم وقت حتى الإطلاق؟** | 2-3 أسابيع (اختبار + قانوني) |

---

## 📚 الملفات الضرورية

```
/root/masaed/
├── scraper/
│   ├── bot.py          ← التسجيل والذاكرة
│   ├── negotiator.py   ← التفاوض
│   ├── intent_parser.py ← كشف النوايا
│   ├── editor.py       ← تعديل البيانات
│   ├── haraj_scraper.py ← جلب العروض ⚠️
│   ├── scraper_api.py  ← Flask API
│   └── auto_extract.py ← استخراج الأرقام
├── database/
│   └── schema.sql      ← قاعدة البيانات
├── ARCHITECTURE_DETAILED.md ← (هذا الملف)
├── LEGAL.md           ← التحذيرات القانونية
├── SAFETY.md          ← أمان الرسائل
└── README.md          ← دليل سريع
```

---

## 🔗 الروابط المرجعية

- docs/architecture.md — معمارية النظام
- prompts/negotiator_v1.md — تكتيكات التفاوض
- LEGAL.md — REGA + Haraj + PDPA
- SAFETY.md — أمان WhatsApp
- AUTH.md — حماية Dashboard
