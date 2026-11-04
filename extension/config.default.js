// Extension runtime config. The Chrome container's entrypoint generates the
// real config.js from environment variables at startup (see
// docker/chrome/entrypoint.sh); this default is the shape + safe fallbacks and
// is what the DOM-contract tests / local loads see.
//
// CONFIRM_ENABLED is the money gate: false (Phase 2) means the placement
// routine builds the slip and stops BEFORE clicking submit. Phase 4 flips it
// true for real placement. It defaults false so a misconfigured build can
// never place a bet.
globalThis.OTS_CONFIG = {
  BRIDGE_URL: "ws://pipeline:8765",
  BRIDGE_TOKEN: "",
  EXTENSION_VERSION: "0.8.3",
  CONFIRM_ENABLED: false,
  TIPSPORT_ORIGIN: "https://www.tipsport.cz",
};
