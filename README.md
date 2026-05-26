# مساعد — AI Real Estate Negotiator

> WhatsApp bot that acts as an anonymous intermediary and **active AI negotiator** between property owners and tenants in the Saudi real estate market.

---

## What Makes مساعد Different

Most chatbots relay messages. مساعد **negotiates**.

| Feature | Simple Relay Bot | مساعد v1 |
|---------|-----------------|-----------|
| Forwards messages | ✅ | ✅ |
| Understands context | ❌ | ✅ |
| Stores property details | ❌ | ✅ |
| Proposes middle-ground prices | ❌ | ✅ |
| Creates urgency | ❌ | ✅ |
| Builds trust with both sides | ❌ | ✅ |
| Invents win-win terms | ❌ | ✅ |
| Pushes toward deal closure | ❌ | ✅ |

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

## Negotiation Tactics (v1)

مساعد uses 6 built-in tactics, executed automatically by the AI:

1. **الوسطية الذكية** — Proposes a middle-ground offer instead of relaying numbers directly
2. **الإلحاح المحسوب** — Hints at competing interest to nudge hesitant parties
3. **ربط المكاسب** — Pairs a price concession with a benefit ("lower rent, early move-in")
4. **إعادة التأطير** — Presents every offer in the most favorable light
5. **الخطوة التالية** — Ends every message with a question or proposal to keep momentum
6. **التسلسل المنطقي** — Confirms location/specs interest before opening price negotiation

**Full prompt documentation**: [prompts/negotiator_v1.md](prompts/negotiator_v1.md)

---

## Project Structure

```
masaed/
├── README.md
├── .env.example
├── dashboard/
│   └── index.html              # Monitoring dashboard (masaed.wardyat.net)
├── database/
│   └── schema.sql              # PostgreSQL tables: masaed_sessions, masaed_messages
├── docs/
│   └── architecture.md         # Full system architecture
├── prompts/
│   └── negotiator_v1.md        # AI system prompt + tactics documentation
└── workflows/
    └── masaed_ai_coordinator_v1.json   # n8n workflow (import-ready)
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

## License

MIT — built for the Saudi real estate market.

---

## Built With

- [n8n](https://n8n.io) — workflow automation
- [DeepSeek](https://deepseek.com) — AI model
- [Green API](https://green-api.com) — WhatsApp integration
- [PostgreSQL](https://postgresql.org) — session + message storage
