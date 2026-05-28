# Legal & Compliance Notice

**مساعد** is an educational/prototype system for AI-powered real estate negotiation. Before deploying to production, address these critical legal issues:

---

## 1. Real Estate Broker Licensing (REGA)

**Problem**: In Saudi Arabia, acting as a real estate broker requires certification from REGA (Real Estate General Authority). Since مساعد negotiates on behalf of parties, it may be classified as unlicensed brokerage.

**Action Required**:
- [ ] Consult a Saudi legal team specializing in real estate
- [ ] Determine if مساعد requires REGA certification
- [ ] If yes, either:
  - Apply for broker license (lengthy, expensive)
  - Operate only as a "matching" tool (neutral, no negotiation)
  - Restrict to consulting/advisory only (not binding agreements)

**Risk Level**: 🔴 **HIGH** — Violating licensing laws = fines + account closure

---

## 2. Haraj.com Data Scraping

**Problem**: `scraper/haraj_scraper.py` automatically scrapes property listings from Haraj.com.sa without permission.

**Issues**:
- Violates Haraj's Terms of Service
- May infringe on copyright/IP (listing text, photos)
- No consent from property owners to use their data in AI negotiation
- Possible PDPA (Saudi data protection) violation

**Action Required**:
- [ ] Request explicit written permission from Haraj management
- [ ] If denied, disable scraper and use opt-in only:
  ```python
  # Instead of scraping:
  # - User manually enters property details via /registrar.html
  # - Owner confirms they own/manage the property
  # - Session created only after double opt-in
  ```

**Risk Level**: 🟠 **MEDIUM** — Cease & desist letters, IP claims

---

## 3. Phone Number Privacy & Confidentiality

**Problem**: مساعد collects and stores phone numbers for both parties. If the dashboard is exposed or database breached, phone numbers leak.

**Action Required**:
- [ ] Add password/JWT authentication to dashboard
- [ ] Encrypt phone numbers in DB (at rest)
- [ ] Limit API access with rate limiting + API keys
- [ ] Audit logs for all data access
- [ ] Comply with PDPA: user consent + deletion requests

**Risk Level**: 🟠 **MEDIUM** — Privacy violations, PDPA fines

---

## 4. AI-Generated Contract Terms

**Problem**: مساعد may suggest payment terms, move-in dates, or contract clauses that are:
- Not requested by either party
- Contradictory or unenforceable under Saudi law
- Binding if both parties accept them without human review

**Example**: "Lower rent (2,400 SR) in exchange for 2 months advance payment + 6-month auto-renewal clause"
- Parties may accept without realizing the auto-renewal commitment
- If dispute arises, unclear who is liable (AI, system operator, or platform)

**Action Required**:
- [ ] Reduce LLM temperature to 0.3 (✅ **done in v3**)
- [ ] Require human admin review before any financial term is sent
- [ ] Add explicit disclaimer: "This is a suggestion, not a binding contract"
- [ ] Store all AI-generated terms for liability defense

**Risk Level**: 🟠 **MEDIUM** — Contract disputes, liability claims

---

## 5. Green API WhatsApp Compliance

**Problem**: Green API is an unofficial third-party wrapper for WhatsApp Business API. WhatsApp regularly detects and bans accounts.

**Action Required**:
- [ ] Use official WhatsApp Business API (requires pre-approval from Meta/Facebook)
- [ ] Or accept that the service may be disrupted without notice
- [ ] Have a fallback communication channel (SMS, email, Telegram)

**Risk Level**: 🟠 **MEDIUM** — Service disruption, account bans

---

## 6. Data Retention & PDPA Compliance

**Problem**: Messages and negotiation history are stored indefinitely.

**PDPA Requirements**:
- Collect only necessary data
- Delete on user request ("right to be forgotten")
- Retention period: max ~1 year unless legally required

**Action Required**:
- [ ] Implement data deletion API: `DELETE /negotiate/{neg_id}` (removes chat_log, phone, etc.)
- [ ] Auto-purge sessions older than 90 days
- [ ] Privacy policy explaining data use
- [ ] Consent checkbox before OTP

**Risk Level**: 🟠 **MEDIUM** — PDPA fines

---

## 7. Liability & Dispute Resolution

**Problem**: If a deal falls through or disputes arise, unclear who bears liability:
- مساعد suggests 2,500 SR but owner meant 2,800 SR
- Parties claim they were misrepresented
- Contract dispute on move-in date

**Action Required**:
- [ ] Add terms of service explicitly disclaiming liability for:
  - Inaccurate AI suggestions
  - Miscommunication
  - Deal disputes or breaches
- [ ] Require both parties to sign acknowledgment before negotiation starts
- [ ] Preserve full audit trail (all messages, timestamps, intent detection results)

**Risk Level**: 🟠 **MEDIUM** — Liability lawsuits

---

## Summary: Production Readiness Checklist

| Issue | Priority | Status | Owner |
|-------|----------|--------|-------|
| REGA licensing review | 🔴 CRITICAL | ❌ TODO | Legal team |
| Haraj scraper permission | 🔴 CRITICAL | ❌ TODO | Business |
| Dashboard authentication | 🟠 HIGH | ❌ TODO | Engineering |
| AI disclaimer + T&C | 🟠 HIGH | ❌ TODO | Legal + Product |
| Data deletion API | 🟠 HIGH | ❌ TODO | Engineering |
| PDPA compliance audit | 🟠 HIGH | ❌ TODO | Legal |
| Green API fallback plan | 🟠 MEDIUM | ❌ TODO | Engineering |
| Phone encryption | 🟠 MEDIUM | ❌ TODO | Engineering |

**Do not deploy to production until all CRITICAL items are resolved.**

---

## Contact

For legal questions, consult:
- Saudi real estate attorney (REGA licensing)
- Data protection officer (PDPA compliance)
- Meta/WhatsApp business relations (API compliance)
