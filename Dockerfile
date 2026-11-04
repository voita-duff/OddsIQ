# Base image tag is LOCKED to the playwright~=1.60.0 pin (bundled Chromium
# rev 1223 / 148.x). A Playwright bump MUST bump this tag in lockstep — see
# pyproject.toml and the 2026-05-30 outage note in oddsiq/scrapers/preflight.py.
# This image ships Chromium + all OS libs + tzdata, so no `playwright install`.
# Use -noble (Ubuntu 24.04 / Python 3.12): the -jammy variant ships Python
# 3.10, below our requires-python >=3.11 floor.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

# Editable install on purpose: the dashboard resolves its templates/ and static/
# via Path(__file__).parent (oddsiq/dashboard/...), and those are package DATA
# not declared in pyproject. An editable install keeps the COPYed source tree as
# the live package so they're always found; a plain `pip install .` would drop
# them and break the dashboard.
COPY pyproject.toml ./
COPY oddsiq ./oddsiq
RUN pip install --no-cache-dir -e .

# Runtime paths live under the mounted /data volume (see docker-compose.yml).
ENV DATABASE_PATH=/data/db/oddsiq.db \
    BACKUP_DIR=/data/backups \
    LOG_FILE=/data/logs/oddsiq.log \
    RAW_DATA_DIR=/data/raw

# Runs as root (the base image default); Chromium uses --no-sandbox (see
# oddsiq/scrapers/cloudflare.py / preflight.py).
CMD ["oddsiq"]
