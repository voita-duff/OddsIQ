# Deploying OddsIQ 24/7 (Docker on a Windows VM)

OddsIQ runs as a **Linux container** via Docker Desktop (WSL2 backend) on the
Windows VM. The pipeline is one long-lived process; the dashboard is optional.

## Prerequisites
- Docker Desktop on the Windows VM with the **WSL2** backend enabled.
- An Odds API key.

## One-time setup
1. Clone the repo onto the VM (or copy the working tree).
2. Create `.env` from `.env.example` and set at least `ODDS_API_KEY`
   (optionally `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, an independent second
   recipient `TELEGRAM_BOT_TOKEN_2` / `TELEGRAM_CHAT_ID_2`, `TZ=Europe/Prague`,
   and `HEARTBEAT_PING_URL=<healthchecks.io-style URL>`). The container exits
   fast if `ODDS_API_KEY` is unset, so a missing key surfaces immediately in
   the logs.
3. Build the image: `docker compose build`.

## Run
- Pipeline only: `docker compose up -d`
- Pipeline + dashboard: `docker compose --profile dashboard up -d`
- Logs: `docker compose logs -f pipeline`
- Stop (graceful, ≤30s): `docker compose down`

State (DB, backups, logs, raw responses) persists in the `oddsiq-data` named
volume across restarts and rebuilds. Keep it on the VM's **local disk** (a named
volume) — SQLite WAL breaks on SMB/network mounts.

## Updating
On any code change — and especially a `playwright` version bump (which requires
bumping the `FROM` tag in `Dockerfile` in lockstep) — rebuild and recreate:
`docker compose build && docker compose up -d`.

## Monitoring / dead-man's-switch
`restart: unless-stopped` auto-restarts the pipeline on crash. For true
dead-process paging set **`HEARTBEAT_PING_URL`**: the scheduler GETs that URL on
every heartbeat (~60s), so a healthchecks.io-style monitor pages you when the
pings *stop* — which is exactly what a dead container or VM looks like. (A
push-based "alive" ping cannot report its own death; the old proactive Telegram
health ping was removed in the 2026-08-10 rebrand for that reason.) The ping is
best-effort — a failing ping never disturbs the poll loop. Belt-and-braces
alternatives: an external uptime monitor pointed at the dashboard via tunnel, or
a small cron on the VM that pages if `docker inspect` shows the pipeline
unhealthy/stopped.

**What Telegram will and won't tell you.** Since 2026-08-10 the phone carries
real-money events only: bet placed; won/lost with real-Kč P&L (only for a bet
that actually got placed — paper value bets settle silently); bet
rejected/failed; auto-betting stopped by the circuit breaker or loss cap;
Tipsport login lost/restored; and the "skipped, not logged in" alert
(rate-limited to one per 6h). Detections, CLV, coverage gaps, budget warnings,
and backup failures are **log/dashboard only** — do not wait for a Telegram that
will never come.

## Backups off-host
Backups land in `/data/backups` inside the volume — same disk as the DB. For
durability against VM loss, periodically copy the volume's `backups/` off-host,
e.g.:

```bash
docker run --rm -v oddsiq_oddsiq-data:/data -v "$PWD/offhost":/out \
  alpine sh -c "cp -r /data/backups /out/"
```

## Dashboard access
Read-only pages remain public at the app layer. The three money-affecting
`/betting` POST endpoints have app-level HTTP Basic auth plus a same-origin
check. When `DASHBOARD_CONTROLS_ENABLED=1`, set both
`DASHBOARD_CONTROLS_USERNAME` and `DASHBOARD_CONTROLS_PASSWORD`; empty values
fail closed with 503. Keep nginx authentication in front as defense in depth.

### Dashboard controls credentials
```dotenv
DASHBOARD_CONTROLS_ENABLED=1
DASHBOARD_CONTROLS_USERNAME=<user>
DASHBOARD_CONTROLS_PASSWORD=<long unique secret>
```
Then recreate the dashboard so it reads them:
```bash
docker compose --profile dashboard --profile betting up -d --no-deps dashboard
```

- **Empty or unset credentials fail closed**, they do not fall back to open: the
  write affordances are not rendered and `POST /betting/{arm,disarm,config}`
  returns 503 (`dashboard control credentials not configured`). Controls are
  simply unavailable until the secrets exist. The CLI kill switch is unaffected —
  `docker compose exec -T pipeline oddsiq betting --disarm` works regardless of
  the dashboard's state, and is the fallback whenever the controls are down.
- **`GET /betting` itself challenges** (401 + `WWW-Authenticate: Basic realm="OddsIQ
  betting controls"`) once controls are enabled AND credentialed, so the browser
  is holding credentials before the first htmx POST. Without the page challenge
  an arm click would be an XHR that 401s, and htmx does not swap a non-2xx body.
  The other tabs (`/live`, `/upcoming`, `/analysis`, `/settings`) stay public.
- **Behind a reverse proxy, forward the browser's host.** The same-origin check
  compares `Origin`/`Referer` against `X-Forwarded-Host` when present, else
  `Host`; with nginx's default `proxy_set_header Host $proxy_host;` the app sees
  the upstream bind (`127.0.0.1:8080`) while `Origin` carries the public name and
  every control POST 403s. Pass one of:
  ```nginx
  proxy_set_header Host $http_host;              # preferred
  proxy_set_header X-Forwarded-Host $http_host;  # equivalent for this check
  ```
- **Basic auth is not a substitute for TLS.** Over plain HTTP the credentials
  travel base64-encoded in every request — readable by anything on the path.
  Terminate TLS at the proxy (or keep the dashboard on a tunnel) before putting
  real credentials on a public binding.

The repo default (this `docker-compose.yml`) maps the dashboard port as
`127.0.0.1:8080:8080`: loopback only, reached through an SSH tunnel. Keep it
that way unless an authenticating, TLS-terminating proxy sits in front.

## Auto-betting actuator (Chrome extension)
The `chrome` service (compose profile `betting`) runs **Chrome for Testing**
under Xvfb, launched with `--test-type` so it still honours `--load-extension`
for the unpacked OddsIQ betting extension (stable Chrome ≥137 silently ignores
that flag; `google-chrome-stable` is installed in the image only for its runtime
library/font dependency closure). The extension drives the Tipsport bet slip.
It is inert until deliberately enabled and, even then, cannot place a bet until
three independent gates are cleared:

1. `BETTING_ENABLED=1` + `BETTING_BRIDGE_TOKEN=<secret>` in `.env` (the pipeline
   builds the engine + WebSocket bridge; the token authenticates the extension).
2. `oddsiq betting --arm` (persistent armed flag; auto-disarms on any fault).
3. `BETTING_CONFIRM_ENABLED=true` in `.env` (the extension's final submit click).
   With it `false` the slip is built and validated but never submitted — a dry
   run. **It is `true` on the VM** (since 2026-08-10): real money moves.

Beyond the three gates, `betting_sports` in the `meta` table decides *which*
sports the engine may bet (CSV; `mlb,nfl` since the 2026-09-06 deploy), and the
stake/cap knobs — flat stake, bets per day, open stake, daily loss limit — also
live in `meta`, editable from `/betting` when `DASHBOARD_CONTROLS_ENABLED=1`.
Four immutable `.env` ceilings bound those runtime knobs even after direct DB
edits: `BETTING_MAX_STAKE_CZK`, `BETTING_MAX_BETS_PER_DAY`,
`BETTING_MAX_OPEN_STAKES_CZK`, and `BETTING_MAX_DAILY_LOSS_CZK`.

Bring it up (adds to the pipeline + dashboard):
```
docker compose --profile dashboard --profile betting up -d
```
Read the state on the `heartbeat:` log line: `betting=off` (not enabled) /
`disconnected` (enabled, extension not connected) / `disarmed` / `armed`.

**Tipsport login** (the profile persists on the `oddsiq-chrome-profile` volume,
so this is normally done once — but sessions do expire, and the extension
reports the loss, which reaches you as a Telegram): tunnel to the loopback-only
VNC and log in by hand — credentials are entered by you, never by the system.
```
ssh -L 5900:127.0.0.1:5900 root@<vm>      # then point a VNC viewer at localhost:5900
```
macOS's built-in Screen Sharing client refuses password-less VNC servers — set
`VNC_PASSWORD=<any throwaway>` in the VM `.env` (compose passes it to the
chrome service) and enter that at the prompt; it's a client-compatibility
convenience, the SSH tunnel is the actual access control. Log in to
tipsport.cz in the browser shown; confirm the session persisted (`betting=`
flips to `disarmed`/`armed` reporting `logged_in`), then close the viewer.
The container keeps the session across restarts.

**Config drift caution:** `docker compose pull` ships only the images — changes
to `docker-compose.yml` and `.env` must be copied to the VM by hand. (This exact
drift once hid the pipeline healthcheck for weeks.)
