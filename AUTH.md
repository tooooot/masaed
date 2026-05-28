# Dashboard Authentication Setup

The مساعد Dashboard (`/index.html`, `/profile.html`, `/registrar.html`, and `/api/`) require **HTTP Basic Auth** to prevent unauthorized access to:
- Active negotiations + phone numbers
- Tenant/owner profiles
- Chat history & payment terms

---

## Quick Setup

### Option 1: Using htpasswd (Recommended)

```bash
# Install htpasswd (included in apache2-utils)
apt-get install apache2-utils

# Create .htpasswd with default admin user
htpasswd -c .htpasswd admin
# Prompted: Enter password twice (e.g., "masaed_secure_2024")

# Result: .htpasswd file with bcrypt hash
cat .htpasswd
# admin:$apr1$XXXXXXXXXXXXX
```

### Option 2: Manual MD5 Hash (Less Secure)

```bash
# Generate MD5 hash
echo -n "admin:MySecurePassword" | openssl dgst -md5
# Output: f8cf10......

# Create .htpasswd manually
echo "admin:admin:5f4dcc3b5aa765d61d8327deb882cf99" > .htpasswd
```

---

## Docker Integration

### Mount .htpasswd in Compose

```yaml
services:
  nginx:
    image: nginx:latest
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf
      - ./.htpasswd:/etc/nginx/.htpasswd  # ← Add this
    ports:
      - "80:80"
```

### Or create in container at startup

```bash
docker exec masaed-nginx htpasswd -c /etc/nginx/.htpasswd admin
```

---

## Access Control Matrix

| Path | Public? | Auth? | Purpose |
|------|---------|-------|---------|
| `/` | ❌ | ✅ | Admin dashboard |
| `/index.html` | ❌ | ✅ | Dashboard UI |
| `/profile.html` | ❌ | ✅ | Negotiation admin panel |
| `/registrar.html` | ❌ | ✅ | User OTP registration form |
| `/api/*` | ❌ | ✅ | Admin API |
| `/bot/*` | ✅ | ❌ | Green API webhook (no auth) |
| `/photos/*` | ✅ | ❌ | Public photos |
| `/p/<id>` | ✅ | ❌ | Public negotiation profile links |

---

## Testing

```bash
# Without credentials → 401 Unauthorized
curl https://masaed.wardyat.net/index.html
# <html><body><h1>401 Authorization Required</h1></body></html>

# With credentials → 200 OK
curl -u admin:password https://masaed.wardyat.net/index.html
```

---

## Password Best Practices

1. ✅ Use **strong, unique** password (20+ chars, mixed case + numbers)
2. ✅ Store .htpasswd **outside** web root (e.g., `/etc/nginx/.htpasswd`)
3. ✅ Never commit `.htpasswd` to git (add to `.gitignore`)
4. ✅ Rotate password **every 6 months**
5. ✅ Use **HTTPS only** (HTTP Basic Auth sends credentials in Base64 = easily decodable)
6. ✅ Consider upgrading to JWT or OAuth for production

---

## .gitignore Update

```bash
# Add to .gitignore
.htpasswd
.htpasswd.bak
*.env.local
```

---

## Troubleshooting

### `401 Unauthorized` on all requests
- Check `.htpasswd` file exists in nginx container: `docker exec masaed-nginx ls -la /etc/nginx/.htpasswd`
- Check nginx config reload: `docker exec masaed-nginx nginx -t`
- Check user/pass in .htpasswd: `cat .htpasswd`

### Can't find nginx container
```bash
docker ps | grep nginx
docker-compose logs nginx
```

### Update password without recreating file
```bash
htpasswd .htpasswd admin  # Overwrites only this user
```

---

## Future: JWT or SSO

For enterprise deployments, replace Basic Auth with:
- **JWT tokens** (store in localStorage, send in Authorization header)
- **OAuth2** (via Google, GitHub)
- **SAML** (for corporate SSO)

See `docs/advanced-auth.md` for implementation.
