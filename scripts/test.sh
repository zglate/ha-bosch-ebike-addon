#!/usr/bin/env sh
# Build the add-on image, then run every test (unit + integration) inside it.
# Usage: scripts/test.sh [platform]     e.g. scripts/test.sh linux/arm64
set -eu
ROOT="$(dirname "$0")/.."
PLATFORM="${1:-linux/amd64}"

docker build --platform "$PLATFORM" -t bosch_ebike_ldi:dev "$ROOT/bosch_ebike_ldi"
docker build --platform "$PLATFORM" -f "$ROOT/tests/Dockerfile.test" \
    -t bosch_ebike_ldi:test "$ROOT"
docker run --rm --platform "$PLATFORM" bosch_ebike_ldi:test
