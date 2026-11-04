#!/usr/bin/env bash
# Chrome-container entrypoint: generate the extension's runtime config from env,
# start a virtual display + VNC (for the one-time manual login), then launch a
# real Chrome with the betting extension loaded against a persistent profile.
set -euo pipefail

: "${BRIDGE_URL:=ws://pipeline:8765}"
: "${BRIDGE_TOKEN:=}"
# Bumped with reconnect-hardening releases so the bridge's hello log
# ("extension connected (version=...)") proves which worker code is live
# after a deploy + profile cache clear.
: "${EXTENSION_VERSION:=0.8.3}"
: "${CONFIRM_ENABLED:=false}"      # money gate — stays false until Phase 4
: "${TIPSPORT_ORIGIN:=https://www.tipsport.cz}"

if [ -z "${BRIDGE_TOKEN}" ]; then
  echo "FATAL: BRIDGE_TOKEN is empty — refusing to start an unauthenticated actuator" >&2
  exit 1
fi

# Generate /ext/config.js from env (see extension/config.default.js for shape).
cat > /ext/config.js <<EOF
globalThis.OTS_CONFIG = {
  BRIDGE_URL: "${BRIDGE_URL}",
  BRIDGE_TOKEN: "${BRIDGE_TOKEN}",
  EXTENSION_VERSION: "${EXTENSION_VERSION}",
  CONFIRM_ENABLED: ${CONFIRM_ENABLED},
  TIPSPORT_ORIGIN: "${TIPSPORT_ORIGIN}",
};
EOF
echo "entrypoint: config.js written (confirm_enabled=${CONFIRM_ENABLED}, bridge=${BRIDGE_URL})"

# Virtual display.
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
sleep 1

# VNC for the one-time Tipsport login. NO -localhost here: inside a container
# that binds the container's own loopback, which Docker's port-forward cannot
# reach (verified 2026-08-10: host-published 5900 accepted and dropped every
# connection). Privacy comes from compose publishing to the VM's 127.0.0.1
# only + the SSH tunnel (see DEPLOY.md), not from the in-container bind.
# VNC_PASSWORD is a convenience, not a security boundary: macOS's built-in
# Screen Sharing client refuses password-less VNC servers, so set it to any
# throwaway value to use that client (verified 2026-08-10).
if [ -n "${VNC_PASSWORD:-}" ]; then
  x11vnc -display :99 -forever -shared -quiet -passwd "${VNC_PASSWORD}" &
else
  x11vnc -display :99 -forever -shared -quiet -nopw &
fi

# Launch Chrome for Testing (NOT --headless — a genuine windowed browser under
# Xvfb is the whole point). Flags that matter:
#   --test-type            : marks this an automation session so modern Chrome
#                            still honors --load-extension (the reason we use CfT).
#   --no-sandbox           : required to run as root in a container; not page-visible.
#   --window-size/--window-position: fill the 1280x1024 Xvfb screen. The
#                            profile had saved a 1050x1004 window (~920px
#                            viewport) that put the /co-se-sazi main market
#                            below the fold (2026-09-20/23 offer_gone).
#   --remote-debugging-port: loopback CDP for health probes (the extension's own
#                            chrome.debugger actuation is a separate client).
exec /opt/cft/chrome \
  --test-type \
  --no-sandbox \
  --no-first-run --no-default-browser-check \
  --window-size=1280,1024 --window-position=0,0 \
  --disable-background-timer-throttling \
  --disable-renderer-backgrounding \
  --remote-debugging-port=9222 \
  --user-data-dir=/profile \
  --load-extension=/ext \
  --disable-extensions-except=/ext \
  "${TIPSPORT_ORIGIN}/kurzy"
