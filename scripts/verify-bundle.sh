#!/usr/bin/env bash
# AutoTest v1.0 -- bundle integrity verifier (RFC-13).
#
# Customer-facing tool: validates a downloaded bundle directory before
# `docker load`. Exits non-zero on any tampering / corruption.
#
# Usage:
#   ./scripts/verify-bundle.sh ./bundle
#   COSIGN_PUB=cosign.pub ./scripts/verify-bundle.sh ./bundle   # also verify signatures

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <bundle-dir>" >&2
    exit 64
fi
BUNDLE="$1"
if [ ! -d "$BUNDLE" ]; then
    echo "ERROR: not a directory: $BUNDLE" >&2
    exit 66
fi

# ── 1) SHA256SUMS ─────────────────────────────────────────────────────────
echo "[verify] checksums ..."
( cd "$BUNDLE" && sha256sum -c SHA256SUMS )

# ── 2) Manifest sanity ────────────────────────────────────────────────────
if [ ! -f "$BUNDLE/manifest.json" ]; then
    echo "ERROR: manifest.json missing" >&2
    exit 1
fi
if command -v jq >/dev/null 2>&1; then
    schema="$(jq -r '.schema' "$BUNDLE/manifest.json")"
    if [ "$schema" != "autotest-bundle/v1" ]; then
        echo "ERROR: unexpected manifest schema: $schema" >&2
        exit 1
    fi
    version="$(jq -r '.version' "$BUNDLE/manifest.json")"
    echo "[verify] manifest version=$version"
fi

# ── 3) Optional cosign signature verification ────────────────────────────
if [ -n "${COSIGN_PUB:-}" ] && command -v cosign >/dev/null 2>&1; then
    if ! command -v jq >/dev/null 2>&1; then
        echo "ERROR: jq required for cosign verification" >&2
        exit 1
    fi
    echo "[verify] cosign verifying images ..."
    jq -r '.images[] | .tag' "$BUNDLE/manifest.json" \
        | while read -r tag; do
            cosign verify --key "$COSIGN_PUB" "$tag" >/dev/null
            echo "  OK $tag"
        done
fi

echo "[verify] bundle OK."
