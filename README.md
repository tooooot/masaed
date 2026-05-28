# مساعد — AI Real Estate Negotiator (v3)

> WhatsApp bot that acts as an anonymous intermediary and **active AI negotiator** between property owners and tenants in the Saudi real estate market.

⚠️ **Legal Disclaimer**: This system requires REGA (Saudi real estate regulator) licensing for legitimate brokerage. Scraping from Haraj.com without permission violates their ToS. Use only in authorized/educational contexts.

---

## What Makes مساعد Different

Most chatbots relay messages. مساعد **negotiates**.

| Feature | Simple Relay Bot | مساعد v3 |
|---------|-----------------|-----------|
| Forwards messages | ✅ | ✅ |
| Understands context | ❌ | ✅ |
| Stores property details | ❌ | ✅ |
| Proposes middle-ground prices | ❌ | ✅ |
| Creates urgency | ❌ | ✅ |
| Builds trust with both sides | ❌ | ✅ |
| Invents win-win terms | ❌ | ✅ |
| Pushes toward deal closure | ❌ | ✅ |
| User identity verification (OTP) | ❌ | ✅ |
| Admin escalation routing | ❌ | ✅ |
| Session expiry (7 days) | ❌ | ✅ |

### Real Example

```
Owner:     "Rent is 2,800 SAR, 3 months advance"
Requester: "My budget is 2,200 SAR"

مساعد → Owner:     "I pushed back for you. Client is serious. 
                     Could you do 2,300 + 2 months advance?
                     That gets you fast cash and keeps the deal alive."

مساعد → Requester: "Owner came down significantly. I negotiated 
                     a middle ground: 2,300 + 2 months advance.
                     That's below your budget. Want to lock it in?"
```

Neither party knows the other's phone number. مساعد is the sole trusted intermediary for both sides.

---

## Architecture

```
Owner (WhatsApp) ──┐
                    ├──► Green API ──► n8n Webhook ──► AI (DeepSeek)
Requester (WhatsApp)┘         ▲                              │
                               └──────── Smart Replies ───────┘
                                    (negotiation, not relay)
```

**Full architecture**: [docs/architecture.md](docs/architecture.md)

---

## Stack

| Layer | Technology |
|-------|-----------|
| WhatsApp | [Green API](https://green-api.com) (cloud-hosted, no QR needed) |
| Orchestration | n8n (self-hosted) |
| AI | DeepSeek Chat (primary) / Claude Haiku (fallback) |
| Database | PostgreSQL (shared sanad-postgres) |
| Dashboard | Static HTML + vanilla JS |
| Hosting | Docker + Traefik reverse proxy |

---

## Negotiation Tactics (v3)

مساعد uses 6 built-in tactics, executed automatically with low temperature (0.3 to prevent hallucination):

1. **الوسطية الذكية** — Proposes a middle-ground offer instead of relaying numbers directly
2. **الإلحاح المحسوب** — Hints at competing interest to nudge hesitant parties
3. **ربط المكاسب** — Pairs a price concession with a benefit ("lower rent, early move-in")
4. **إعادة التأطير** — Presents every offer in the most favorable light
5. **الخطوة التالية** — Ends every message with a question or proposal to keep momentum
6. **التسلسل المنطقي** — Confirms location/specs interest before opening price negotiation

**Key v3 Changes**:
- Temperature reduced from 0.7 → **0.3** (prevents AI hallucinating prices/terms)
- Identity verification (OTP) required before negotiation starts
- Admin notification for: near-agreement (<10% gap), stalled negotiations (8+ rounds), firm rejections
- Session auto-expires after 7 days

**Full prompt documentation**: [prompts/negotiator_v1.md](prompts/negotiator_v1.md)

---

## Project Structure

```
masaed/
├── README.md
├── .env.example
├── dashboard/
│   ├── index.html              # Monitoring dashboard (⚠️ needs password)
│   ├── profile.html            # User profile editor
│   └── registrar.html          # OTP registration form
├── database/
│   └── schema.sql              # PostgreSQL v3: masaed_negotiations, masaed_registrations, masaed_contacts
├── docs/
│   └── architecture.md         # Full system architecture
├── prompts/
│   └── negotiator_v1.md        # AI system prompt + tactics documentation
├── scraper/
│   ├── bot.py                  # WhatsApp registration bot (OTP collection)
│   ├── negotiator.py           # Main negotiation logic v3
│   ├── intent_parser.py        # NLU for intent detection
│   ├── haraj_scraper.py        # ⚠️ Haraj.com scraper (legal risk)
│   └── [other utilities]
└── workflows/
    └── masaed_ai_coordinator_v1.json   # n8n workflow (needs v3 update)
```

---

## Setup Guide

### 1. Prerequisites
- n8n instance (self-hosted or cloud)
- Green API account with authorized WhatsApp number
- PostgreSQL database
- DeepSeek API key

### 2. Database
```sql
-- Run schema.sql on your PostgreSQL instance
\i database/schema.sql
```

### 3. n8n Workflow
1. Open n8n → Workflows → Import
2. Upload `workflows/masaed_ai_coordinator_v1.json`
3. Update credentials:
   - **Sanad Postgres**: your PostgreSQL connection
   - **DeepSeek Bearer**: `Authorization: Bearer YOUR_KEY`
4. Update Green API URL in Send/Message nodes with your instance + token
5. Activate workflow

### 4. Green API Webhook
In your Green API dashboard:
- Set webhook URL: `https://your-n8n.com/webhook/masaed-inbound`
- Enable: `incomingWebhook`

### 5. Create a Session
```sql
INSERT INTO sanad.masaed_sessions (requester_phone, owner_phone)
VALUES ('966XXXXXXXXX', '966YYYYYYYYY');
```

Both parties can now WhatsApp the bot number and مساعد handles everything.

---

## n8n Workflow Nodes

| # | Node | Type | Purpose |
|---|------|------|---------|
| 1 | Webhook | Webhook | Receive Green API events |
| 2 | Extract | Code | Parse sender + text |
| 3 | Load Session | Postgres | Fetch session + history |
| 4 | Build Prompt | Code | Construct AI prompts |
| 5 | DeepSeek | HTTP Request | AI inference |
| 6 | Parse Response | Code | Extract reply + context patch |
| 7 | Update Context | Postgres | Persist context JSON |
| 8 | Save Message | Postgres | Message audit log |
| 9 | Reply to Sender | HTTP Request | Send AI reply |
| 10 | Msg Other? | IF | Route to other party? |
| 11 | Message Other Party | HTTP Request | Send negotiation message |

---

## Context Memory

مساعد builds a persistent JSON profile per session:

```json
{
  "negotiation_stage": "negotiating",
  "property": {
    "city": "Jeddah",
    "district": "Al-Rawdah",
    "bedrooms": 3,
    "rent": 2800,
    "furnished": false,
    "available_from": "2025-07-01"
  },
  "requirements": {
    "city": "Jeddah",
    "budget": 2200,
    "bedrooms": 3
  },
  "last_offer": {
    "from": "owner",
    "price": 2500,
    "terms": "3 months advance"
  }
}
```

This context is loaded on every message, so مساعد remembers the full history and doesn't ask for the same information twice.

---

## Roadmap

### v1.1
- [ ] Automatic intake: owner sends property details → session created automatically
- [ ] Requester intake: WhatsApp "start" message → guided property search
- [ ] Deal closure detection → send agreement summary to both parties

### v1.2
- [ ] Multi-property sessions: match one requester to multiple owners
- [ ] Haraj.com.sa scraper: auto-populate owner sessions from listings
- [ ] AI matching: score compatibility before creating session

### v2.0
- [ ] Voice message support (transcribe → negotiate)
- [ ] Image understanding (property photos → extract details)
- [ ] Contract generation on deal close

---

## Security & Legal Warnings (v3)

### ✅ Fixed in v3
- **Temperature reduced to 0.3**: Prevents AI hallucinating prices/contract terms in negotiations
- **OTP verification**: Both parties must verify phone before session starts (prevents spam/impersonation)
- **Session expiry**: Auto-closes after 7 days to prevent stale negotiations
- **Admin escalation**: Alerts admin on: near-agreement, too many rounds, firm rejections
- **Audit log**: All messages stored in `chat_log` JSONB for dispute resolution

### ⚠️ Known Risks (Must Address Before Production)
1. **Haraj.com scraper violates ToS**: Scraping data without permission = legal liability. Either:
   - Get explicit scraping permission from Haraj
   - Use opt-in only (users register manually)
   - Disable scraper entirely
   
2. **Green API is unofficial**: WhatsApp regularly bans accounts using unofficial APIs. No guarantee of 24/7 uptime.

3. **REGA licensing required**: Saudi real estate brokerage requires REGA certification. Negotiate on behalf of unlicensed users = legal risk.

4. **Dashboard has no password**: Anyone with the URL sees all negotiations + phone numbers. Add HTTP Basic Auth or JWT before production.

5. **AI may still invent terms**: Even at 0.3 temperature, LLM may suggest payment terms, move-in dates, or contract clauses not requested by either party. Always require admin approval before finalizing.

### ✅ Setup Checklist for Safe Deployment
- [ ] Database schema v3 running on PostgreSQL
- [ ] Green API webhook configured + tested
- [ ] DeepSeek API key secured in .env (not committed)
- [ ] Dashboard protected with password/JWT
- [ ] OTP SMS provider configured (Twillio / AWS SNS / local testing)
- [ ] Admin notification channel active (WhatsApp or email)
- [ ] Legal review: Confirm REGA status & Haraj ToS compliance
- [ ] Load test: Verify n8n can handle 10+ concurrent negotiations

---

## License

MIT — built for the Saudi real estate market. Use responsibly and legally.

---

## Built With

- [n8n](https://n8n.io) — workflow automation
- [DeepSeek](https://deepseek.com) — AI model
- [Green API](https://green-api.com) — WhatsApp integration
- [PostgreSQL](https://postgresql.org) — session + message storage
