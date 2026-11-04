#!/usr/bin/env bash
#
# build-and-push.sh - build the OddsIQ image for the Linux VM and push it to
# the registry.
#
# Builds a linux/amd64 image (Apple Silicon Mac -> amd64 VM run under Docker)
# and pushes three tags: :latest, :<pyproject version>, :<git short-sha[-dirty]>.
# The VM then deploys/upgrades with:  docker compose pull && docker compose up -d
#
# Usage:
#   ./scripts/build-and-push.sh                 # or: make push
#   PUSH=0 ./scripts/build-and-push.sh          # build only, don't push
#   NO_CACHE=0 ./scripts/build-and-push.sh      # allow layer cache (faster rebuilds)
#   REGISTRY=registry.example.com IMAGE=oddsiq ./scripts/build-and-push.sh
#
# Pushing needs REGISTRY set and a prior login to it:
#   docker login registry.example.com
# Without REGISTRY the images are tagged locally (oddsiq, oddsiq-chrome).
#
# Note: keep this file ASCII-only. A multibyte char (e.g. a Unicode ellipsis)
# placed directly after a $var glues onto the name under `set -u` and aborts
# with "unbound variable". Braced expansions below are also defensive.
set -euo pipefail

# --- Config (override via env) ----------------------------------------------
REGISTRY="${REGISTRY:-}"
IMAGE="${IMAGE:-oddsiq}"
PLATFORM="${PLATFORM:-linux/amd64}"
PUSH="${PUSH:-1}"
NO_CACHE="${NO_CACHE:-1}"
REPO="${REGISTRY:+${REGISTRY}/}${IMAGE}"
# The betting Chrome-actuator image (P1 Phase 2). Built + pushed alongside the
# app image; the VM's `chrome` compose service pulls it. CHROME=0 skips it.
CHROME="${CHROME:-1}"
CHROME_IMAGE="${CHROME_IMAGE:-oddsiq-chrome}"
CHROME_REPO="${REGISTRY:+${REGISTRY}/}${CHROME_IMAGE}"

# Run from the repo root regardless of where the script was invoked from.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- Derive tags ------------------------------------------------------------
# Version from pyproject.toml. The [project] version is the only line starting
# with `version = ` at column 0 (build-system uses `requires`, ruff uses
# `target-version`). `|| true` keeps `set -o pipefail` from killing the
# assignment so the empty-check below can print a friendly error.
VERSION="$(grep -m1 '^version = ' pyproject.toml | sed -E 's/^version = "(.*)".*/\1/' || true)"
[ -n "${VERSION}" ] || { echo "ERROR: could not read version from pyproject.toml" >&2; exit 1; }

# Git short-sha for traceability - the static 0.1.0 alone can't tell two builds
# apart. Mark a dirty working tree so a non-reproducible image is obvious.
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  GIT_SHA="${GIT_SHA}-dirty"
fi

# --- Preflight --------------------------------------------------------------
if [ "${PUSH}" = "1" ] && [ -z "${REGISTRY}" ]; then
  echo "ERROR: PUSH=1 needs REGISTRY (e.g. REGISTRY=registry.example.com), or run with PUSH=0." >&2
  exit 1
fi
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not reachable. Start Docker Desktop and retry." >&2; exit 1; }

# --- Build ------------------------------------------------------------------
build_flags=(--platform "${PLATFORM}" --pull --force-rm)
[ "${NO_CACHE}" = "1" ] && build_flags+=(--no-cache)

echo "Building ${REPO}  (platform=${PLATFORM} version=${VERSION} sha=${GIT_SHA} no_cache=${NO_CACHE})..."
docker build "${build_flags[@]}" \
  -t "${REPO}:latest" \
  -t "${REPO}:${VERSION}" \
  -t "${REPO}:${GIT_SHA}" \
  --label "org.opencontainers.image.version=${VERSION}" \
  --label "org.opencontainers.image.revision=${GIT_SHA}" \
  -f Dockerfile .

if [ "${CHROME}" = "1" ]; then
  echo "Building ${CHROME_REPO}  (betting Chrome actuator)..."
  docker build "${build_flags[@]}" \
    -t "${CHROME_REPO}:latest" \
    -t "${CHROME_REPO}:${GIT_SHA}" \
    --label "org.opencontainers.image.revision=${GIT_SHA}" \
    -f docker/chrome/Dockerfile .
fi

# --- Push -------------------------------------------------------------------
if [ "${PUSH}" = "1" ]; then
  for tag in latest "${VERSION}" "${GIT_SHA}"; do
    echo "Pushing ${REPO}:${tag}..."
    docker push "${REPO}:${tag}"
  done
  if [ "${CHROME}" = "1" ]; then
    for tag in latest "${GIT_SHA}"; do
      echo "Pushing ${CHROME_REPO}:${tag}..."
      docker push "${CHROME_REPO}:${tag}"
    done
  fi
  echo
  echo "Pushed ${REPO}  (latest, ${VERSION}, ${GIT_SHA})"
  [ "${CHROME}" = "1" ] && echo "Pushed ${CHROME_REPO}  (latest, ${GIT_SHA})"
  echo "  On the VM:  docker compose pull && docker compose up -d"
else
  echo
  echo "Built ${REPO}  (latest, ${VERSION}, ${GIT_SHA}) - PUSH=0, not pushed."
fi
