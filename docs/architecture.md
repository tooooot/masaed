# مساعد — System Architecture v1

## Overview

مساعد is a WhatsApp AI negotiation agent that acts as an anonymous intermediary between real estate owners and tenants. Neither party knows the other's phone number; all communication flows through مساعد, which uses AI to negotiate actively rather than simply relay messages.

```
Owner (WhatsApp)
      │
      ▼
 Green API ──► n8n Webhook ──► Extract ──► Load Session
      ▲                                          │
      │                                          ▼
      │                                    Build Prompt
      │                                          │
      │                                     DeepSeek AI
      │                                          │
      │                                   Parse Response
      │                                    ┌────┴────┐
      │                           Update Context   Save Message
      │                                          │
      └──────────────── Reply to Sender ◄────────┤
      │                                          │
      └────────── Message Other Party ◄── Msg Other?
      │
Requester (WhatsApp)
```

## Components

### 1. WhatsApp Gateway — Green API
- **Instance**: 7107624780
- **Webhook**: `POST https://n8n.../webhook/masaed-inbound`
- **Outbound**: REST API `sendMessage`
- Both parties message the same bot number; routing is handled by session lookup.

### 2. n8n Workflow — "مساعد — AI Coordinator"
- **ID**: `8xwBqOkQdXuI0Slw`
- **Webhook path**: `masaed-inbound`
- **11 nodes** (see workflow JSON for full details)

| Node | Type | Purpose |
|------|------|---------|
| Webhook | n8n webhook | Receive Green API events |
| Extract | Code (JS) | Parse sender + message text |
| Load Session | Postgres | Get session + last 12 messages |
| Build Prompt | Code (JS) | Construct AI system/user prompts |
| DeepSeek | HTTP Request | Call DeepSeek Chat API |
| Parse Response | Code (JS) | Extract reply, msgOther, context patch |
| Update Context | Postgres update | Persist new context JSON |
| Save Message | Postgres insert | Audit log |
| Reply to Sender | HTTP Request | Send AI reply via Green API |
| Msg Other? | IF | Route if AI wants to message other party |
| Message Other Party | HTTP Request | Send negotiation message to other party |

### 3. Database — sanad-postgres
- **Host**: `sanad-postgres:5432`
- **Database**: `sanad`
- **Schema**: `sanad`
- **Tables**: `masaed_sessions`, `masaed_messages`

### 4. AI — DeepSeek Chat
- **Model**: `deepseek-chat`
- **Role**: Active negotiator, not message relay
- **Output**: Structured JSON with `reply_to_sender`, `message_other_party`, `context_update`

### 5. Dashboard
- **URL**: `https://masaed.wardyat.net`
- **Stack**: Static HTML + vanilla JS
- **Data source**: n8n webhook `masaed-api` (future)

## Message Flow

```
1. Party sends WhatsApp message to bot number
2. Green API fires webhook → n8n
3. Extract: parse sender phone + text
4. Load Session: find active session for this phone
   → also fetches last 12 messages as history string
5. Build Prompt: determine role (owner/requester), build system + user prompt
6. DeepSeek: AI decides reply + whether to message other party + context updates
7. Parse Response: extract structured fields, deep-merge context
8. Parallel execution:
   a. Update Context → persist merged JSON to session
   b. Save Message → audit log
   c. Reply to Sender → send AI reply to the person who messaged
   d. Msg Other? → if AI generated a message for the other party:
      → Message Other Party → send negotiation message to other side
```

## Session Lifecycle

```
Session Created (manually or via future intake flow)
        │
        ▼
  info_gathering ──► negotiating ──► near_deal ──► closed
        │
    [AI learns property details from owner]
    [AI learns requirements from requester]
    [AI bridges gap autonomously]
```

## Infrastructure

- **Reverse proxy**: Traefik on Docker
- **Domain**: `masaed.wardyat.net` → static dashboard
- **n8n**: `n8n.srv1506241.hstgr.cloud`
- **Postgres**: shared `sanad-postgres` container (port 5432 internal, 5433 external)

## Environment Variables Required

```
N8N_API_KEY=          # n8n API access
DEEPSEEK_API_KEY=     # DeepSeek Chat API
GREEN_API_INSTANCE=   # 7107624780
GREEN_API_TOKEN=      # Green API token (in workflow URL)
POSTGRES_HOST=        # sanad-postgres
POSTGRES_DB=          # sanad
POSTGRES_USER=        # sanad
POSTGRES_PASSWORD=    # (in .env, never committed)
```
