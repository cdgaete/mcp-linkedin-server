# LinkedIn MCP — Fallback Cookie Login via Workstation

## When to use

When `login_linkedin` / `login_linkedin_save` fails — typically because LinkedIn triggers an email verification challenge that opens in a different browser, leaving the Playwright session on pangui stuck.

## Prerequisites

- The target account (claudia or carlos) must be logged into LinkedIn in **Firefox** on the workstation (carlos-omen).
- Chrome 146+ encrypts cookies with AES-GCM tied to GNOME keyring — **cannot be decrypted from Desktop Commander context**. If the account is only logged into Chrome, open Firefox on the workstation, navigate to linkedin.com, log in there, then proceed.
- The `workstation` MCP connection must be activated in Claude Settings → Connections.

## Steps

### 1. Export cookies from Firefox on workstation

Run via `workstation:start_process`:

```python
python3 << 'PYEOF'
import sqlite3, json, time

ACCOUNT = "claudia"  # ← change to "carlos" if needed
FF_PROFILE = "/home/carlos/.mozilla/firefox/fzf99jwi.default-esr/cookies.sqlite"

db = sqlite3.connect(FF_PROFILE)
rows = db.execute('''
    SELECT name, value, host, path, expiry, isSecure, isHttpOnly
    FROM moz_cookies WHERE host LIKE '%linkedin%'
''').fetchall()

pw_cookies = []
for name, value, host, path, expiry, is_secure, is_http_only in rows:
    pw_cookies.append({
        'name': name, 'value': value, 'domain': host, 'path': path,
        'expires': expiry, 'httpOnly': bool(is_http_only),
        'secure': bool(is_secure), 'sameSite': 'None' if is_secure else 'Lax'
    })

cookie_data = {"timestamp": int(time.time()), "cookies": pw_cookies, "account": ACCOUNT}
outfile = f'/tmp/{ACCOUNT}_linkedin_cookies.json'
with open(outfile, 'w') as f:
    json.dump(cookie_data, f)

li_at = any(c['name'] == 'li_at' for c in pw_cookies)
print(f"Exported {len(pw_cookies)} cookies for {ACCOUNT}, li_at={li_at}")
if not li_at:
    print("WARNING: No li_at cookie — session not valid. Log into LinkedIn in Firefox first.")
db.close()
PYEOF
```

**Verify:** Output must show `li_at=True`. If not, the account is not logged in on Firefox.

### 2. SCP to pangui

Run via `workstation:start_process`:

```bash
scp -o StrictHostKeyChecking=no /tmp/<ACCOUNT>_linkedin_cookies.json carlos@192.168.1.86:/tmp/<ACCOUNT>_linkedin_cookies.json
```

### 3. Encrypt and save on pangui

Run via `pangui:start_process`:

```python
python3 << 'PYEOF'
import json
from cryptography.fernet import Fernet
from pathlib import Path

ACCOUNT = "claudia"  # ← change to "carlos" if needed

sessions_dir = Path('/home/carlos/repos/mcp-linkedin-server/sessions')
with open(sessions_dir / 'encryption.key', 'rb') as f:
    key = f.read()
fernet = Fernet(key)

with open(f'/tmp/{ACCOUNT}_linkedin_cookies.json') as f:
    cookie_data = json.load(f)

encrypted = fernet.encrypt(json.dumps(cookie_data).encode())
cookie_file = sessions_dir / f'linkedin_{ACCOUNT}_cookies.json'
with open(cookie_file, 'wb') as f:
    f.write(encrypted)

dec = json.loads(fernet.decrypt(encrypted))
print(f"Saved {len(dec['cookies'])} cookies for {ACCOUNT}, li_at={any(c['name']=='li_at' for c in dec['cookies'])}")
PYEOF
```

### 4. Test

Call any `linkedin-browser:` tool with the target account to verify the session works:

```
linkedin-browser:interact_with_linkedin_post(account="claudia", action="read", post_url="<any_post>")
```

A successful read with full post content confirms the session is valid.

## Reference

| Item | Value |
|------|-------|
| Cookie data format | `{"timestamp": <unix_ts>, "cookies": [...], "account": "<name>"}` |
| Encryption | Fernet, key at `sessions/encryption.key` |
| Session files | `sessions/linkedin_<account>_cookies.json` |
| Session expiry | 24 hours (checked in `load_cookies`) |
| Firefox profile | `/home/carlos/.mozilla/firefox/fzf99jwi.default-esr/cookies.sqlite` |
| pangui SSH | `carlos@192.168.1.86` |

## Why not Chrome?

Chrome 146+ uses AES-GCM cookie encryption with keys stored in GNOME keyring. Decryption requires `secretstorage` + a D-Bus session (`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`). Even with the correct keyring key, decryption fails in the Desktop Commander context — likely due to Chrome's newer key derivation or app-bound encryption. Firefox stores cookies in plain SQLite, making it the reliable extraction path.
