# Real (headful-under-Xvfb) Chrome-for-Testing running the OddsIQ betting
# extension. A real browser + persistent logged-in profile is the
# anti-detection actuator (page executed with Tipsport's own session). See the
# P1 design spec.
#
# We run Chrome for Testing (Google's automation build) rather than consumer
# google-chrome-stable because stable Chrome (>=137) removed `--load-extension`
# for unpacked extensions; CfT + `--test-type` still honors it (confirmed on the
# VM 2026-08-10 against Chrome 151, which silently ignored the flag). The
# google-chrome-stable package is still installed — ONLY for its runtime library
# + font dependency closure — and is not the browser we launch.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      wget gnupg ca-certificates xvfb x11vnc tini fonts-liberation unzip jq \
    && wget -q -O /usr/share/keyrings/google.pub https://dl.google.com/linux/linux_signing_key.pub \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.pub] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Chrome for Testing (Stable channel, pinned at build time — the built image is
# immutable, matching how google-chrome-stable was "latest at build"). Unzipped
# to /opt/cft/chrome.
RUN CFT_URL="$(wget -qO- https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json \
        | jq -r '.channels.Stable.downloads.chrome[] | select(.platform=="linux64") | .url')" \
    && echo "Chrome for Testing: ${CFT_URL}" \
    && wget -q -O /tmp/cft.zip "${CFT_URL}" \
    && unzip -q /tmp/cft.zip -d /opt \
    && mv /opt/chrome-linux64 /opt/cft \
    && rm /tmp/cft.zip \
    && /opt/cft/chrome --version

# The extension is baked into the image (read-only). The persistent profile
# (logged-in session) lives on a mounted volume at /profile, NOT in the image.
COPY extension/ /ext/
COPY docker/chrome/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Profile volume + the display.
VOLUME ["/profile"]
ENV DISPLAY=:99

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
