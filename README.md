# SmartThings PAT Auto-Rotation for `samsung_familyhub_fridge`

## Background

SmartThings [changed PAT expiry to 24 hours on 2024-12-30](https://developer.smartthings.com/docs/getting-started/authorization-and-permissions).
The `samsung_familyhub_fridge` integration stores the PAT in the HA config entry, so it
breaks every day unless you generate a new one manually.

### Why OAuth doesn't fix this

The integration calls two endpoints that require a PAT with **full scopes**:

| Endpoint | Auth requirement |
|---|---|
| `client.smartthings.com/udo/file_links/{id}` | PAT — `audiovideo:*` / OCF scopes |
| `client.smartthings.com/devices/status` | PAT — broad scope |

SmartThings OAuth app tokens cannot carry `audiovideo:*` or similar OCF-tier scopes;
those are PAT-only.  There is also **no documented `api.smartthings.com` equivalent**
for downloading Family Hub camera images — `client.smartthings.com/udo` is a Samsung
internal ("Unified Data Object") API, and it is the only path.

The "smartthings reuse" option in `config_flow.py` borrows the OAuth token from the HA
core SmartThings integration; because that token lacks the required scopes it silently
fails against `client.smartthings.com`.

**Conclusion: PAT rotation is the only viable workaround until Samsung exposes these
endpoints through their documented OAuth-capable API.**

---

## Solution overview

```
┌──────────────────────────────────────────────────────────┐
│                       Docker stack                       │
│                                                          │
│  ┌──────────────────┐  every 20 h   ┌───────────────┐    │
│  │   pat-rotator    │──────────────▶│  Playwright   │    │
│  │  (supercronic)   │               │  Chromium     │    │
│  └────────┬─────────┘               └───────┬───────┘    │
│           │  POST /api/services/            ▲            │
│           │  input_text/set_value          PAT           │
│           │  new token                      │            │
└───────────┼─────────────────────────────────┘────────────┘
            │
            ▼
  ┌────────────────────────────────────────-─┐
  │  Home Assistant                          │
  │                                          │
  │  input_text.smartthings_pat  ◀── write   │
  │         │                                │
  │  samsung_familyhub_fridge api.py         │
  │  (reads token on every 10-s poll)  ◀───  │
  └──────────────────────────────────────────┘
```

---

## Setup

### 1. Patch the integration

Copy the patched `api.py` from this directory over the existing one:

```bash
cp api.py /config/custom_components/samsung_familyhub_fridge/api.py
```

The only change: `_headers` is now a `@property` that always reads from
`self.token`, and `DataCoordinator._async_update_data` syncs `self.api.token`
from `input_text.smartthings_pat` before every API call.  The integration
continues to work with whatever token is in the config entry until the helper
entity exists and contains a value.

### 2. Add HA config

Merge `ha_config_snippets.yaml` into your `configuration.yaml` (or wherever you
keep `input_text:`, `shell_command:`, and `automation:` blocks):

```bash
# Review the file first, then paste or include as appropriate
cat ha_config_snippets.yaml
```

Restart HA once.  The `input_text.smartthings_pat` entity will now exist.

**Initial seed:** manually set the entity to a valid PAT right now:

```
Developer Tools → Services → input_text.set_value
entity_id: input_text.smartthings_pat
value:      <paste a freshly generated PAT here>
```

The integration will pick it up within 10 seconds — no restart required.

### 3. Deploy the rotator

```bash
cd /path/to/pat_rotator

# Copy the env template and fill in your values
cp .env.example .env
$EDITOR .env

# Build and start
docker compose up -d

# Watch the first run
docker compose logs -f
```

### 4. Verify the first rotation

After the container starts it runs immediately, then every 20 hours.
Check that the `input_text.smartthings_pat` entity in HA updates to a new UUID.

---

## Troubleshooting

### "Could not find 'Generate new token' button"

Samsung occasionally updates their UI.  Run a debug session to see what the page
actually looks like and adjust the selectors in `pat_rotator.py`:

```bash
# On a machine with a display (or via VNC into your home lab):
docker compose run --rm pat-rotator python pat_rotator.py --debug
```

A screenshot is saved to `/data/debug_tokens_page.png` (mapped to the Docker volume).

### Samsung 2FA

If your Samsung account has TOTP (Google Authenticator / Aegis) 2FA enabled, set
`SAMSUNG_TOTP_SECRET` in `.env` to the base32 secret for that account.  To find it:
in Aegis, long-press the SmartThings entry → Edit → reveal the secret.

If you use SMS or email 2FA, you'll need to either switch to TOTP or handle the
prompt differently (Playwright can wait for you to type it in `--debug` mode).

### Clearing cached session

If login fails and you want a fresh start:

```bash
docker compose run --rm pat-rotator python pat_rotator.py --clear-state
```

### Checking rotation health from HA

The `smartthings_pat_stale_alert` automation (in `ha_config_snippets.yaml`) fires a
persistent notification if the token hasn't been refreshed in 23 hours — giving you
a ~1 h window to investigate before the PAT actually expires.

---

## Files

| File | Purpose |
|---|---|
| `pat_rotator.py` | Playwright browser automation; runs on cron |
| `Dockerfile` | Image based on `mcr.microsoft.com/playwright/python` |
| `docker-compose.yml` | Service definition with supercronic scheduler |
| `.env.example` | Environment variable template |
| `api.py` | Drop-in replacement for the integration's `api.py` |
| `ha_config_snippets.yaml` | `input_text`, `automation`, `shell_command` snippets |
