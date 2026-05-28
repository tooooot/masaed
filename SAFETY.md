# 🛑 Safety & Testing Guidelines

**Critical Rule**: Never send real WhatsApp messages to non-sandbox phone numbers without explicit authorization.

---

## The Incident

❌ **What happened**: Rogue messages were sent to real phone numbers without user consent.

✅ **What changed**: 
- Added `MASAED_SANDBOX_PHONES` env var with whitelisted test numbers
- Added `MASAED_ALLOW_REAL_SEND` kill switch (default: false)
- wa_send() now raises an error if trying to send to non-sandbox numbers when real sends are disabled

---

## Testing Properly

### ✅ Safe Testing (Sandbox Mode)

**Default behavior** — No real messages are sent:

```bash
# 1. Set sandbox phones (any 050* or 966500000* numbers work)
export MASAED_SANDBOX_PHONES="966500000000,966500000001"
export MASAED_ALLOW_REAL_SEND=false

# 2. Use /bot/test endpoint (dry-run, never sends)
curl -X POST http://localhost:5555/bot/test \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "966500000000",
    "text": "مرحبا"
  }'

# Response: {"wa_sent": [...], "reply": "..."}
# No actual WhatsApp messages sent.
```

### ⚠️ Real Testing (Production Mode)

**Only enable when ready to send real messages**:

```bash
# DANGER: This actually sends WhatsApp messages
export MASAED_ALLOW_REAL_SEND=true
export MASAED_SANDBOX_PHONES="966500000000"

# Now /bot/webhook will send real messages
# GET EXPLICIT USER CONSENT FIRST
```

---

## Phone Number Whitelist

### Sandbox (Safe for Testing)
```
966500000000
966500000001
0500000000
0500000001
050XXXXX88  ← test numbers (fake)
050XXXXX99  ← test numbers (fake)
```

### Production (Real Users)
- Add after user registers via OTP
- Verify phone in masaed_registrations table
- Only send after explicit consent

---

## Safety Checklist Before Production

- [ ] All developers tested with sandbox numbers only
- [ ] .env has MASAED_ALLOW_REAL_SEND=false by default
- [ ] No real phone numbers hardcoded in code
- [ ] OTP verification working (user confirms their own phone)
- [ ] Terms of Service signed by user
- [ ] Admin approval required to enable real sends
- [ ] Audit log records all messages (phone, text, timestamp)

---

## Environment Variable Reference

| Var | Default | Safe? | Purpose |
|-----|---------|-------|---------|
| `MASAED_SANDBOX_PHONES` | `966500000000,966500000001` | ✅ | Whitelisted test phones |
| `MASAED_ALLOW_REAL_SEND` | `false` | ✅ | Enable real WhatsApp sends |
| `MASAED_GREEN_INSTANCE` | `` | ⚠️ | Green API instance (needed for real sends) |
| `MASAED_GREEN_TOKEN` | `` | 🔴 | Secret token (never commit!) |

---

## If You See This Error

```
[WA BLOCKED] ❌ CRITICAL: Attempted to send to 966XXXXXXXXX (not in sandbox)
[WA BLOCKED] Set MASAED_ALLOW_REAL_SEND=true to send to real phones
ValueError: Real send to 966XXXXXXXXX blocked by safety check
```

**Meaning**: The code tried to send a message to a real phone, but it's blocked.

**Fix**:
1. If testing: use a sandbox number (050000000X) instead
2. If production: Set `MASAED_ALLOW_REAL_SEND=true` in .env + deploy with approval

---

## Git Safety

```bash
# Never commit real credentials or phone numbers
echo ".env" >> .gitignore
echo ".htpasswd" >> .gitignore
git rm --cached .env  # if already committed

# Review before pushing
git diff --cached | grep -i "966\|050"  # check for phone numbers
```

---

## Contact

🚨 If you accidentally send a real message:
1. Screenshot the error/logs
2. Note the phone number and timestamp
3. Report to admin immediately
4. We may need to apologize to the recipient

**This is preventable with the safety checks now in place.**
