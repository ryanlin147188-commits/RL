#!/usr/bin/env bash
##
## Casdoor seed script (REST API).
##
## Idempotent post-boot seeding for the Casdoor sidecar. casdoor/init_data.json
## handles the first-boot path (org + app + roles + admin user), but that file
## is only consulted on a fresh database. Use this script when:
##   * you've already booted Casdoor once and want to (re)apply policy
##   * you need to add a new role / provider after deployment
##   * the init_data.json contents diverged from the seed catalog in
##     backend/app/main.py:_seed_default_roles
##
## Requirements: jq, curl, a running Casdoor sidecar reachable on
## ${CASDOOR_ENDPOINT:-http://localhost/casdoor}, and a Casdoor client
## credentials pair (CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET) for the
## "rl-platform" application.
##
## Usage:
##   CASDOOR_ENDPOINT=http://localhost/casdoor \
##   CASDOOR_CLIENT_ID=xxx CASDOOR_CLIENT_SECRET=yyy \
##   scripts/seed-casdoor.sh
##
## Exit 0 on success (or "already seeded — nothing to do"); non-zero on a
## genuine API failure so it can be wedged into a CI healthcheck.

set -euo pipefail

ENDPOINT="${CASDOOR_ENDPOINT:-http://localhost/casdoor}"
CLIENT_ID="${CASDOOR_CLIENT_ID:?CASDOOR_CLIENT_ID is required}"
CLIENT_SECRET="${CASDOOR_CLIENT_SECRET:?CASDOOR_CLIENT_SECRET is required}"
ORG_NAME="${CASDOOR_ORG:-autotest}"
APP_NAME="${CASDOOR_APP:-rl-platform}"

# Casdoor REST endpoints all accept Basic auth using the app's client
# credentials, so we don't need to perform an OAuth dance just to seed.
AUTH=(-u "${CLIENT_ID}:${CLIENT_SECRET}")
HDR=(-H "Content-Type: application/json")

log() { printf '[seed-casdoor] %s\n' "$*"; }

api() {
  # api METHOD PATH [JSON_BODY]
  local method="$1" path="$2"
  shift 2
  if [[ $# -gt 0 ]]; then
    curl -fsS "${AUTH[@]}" "${HDR[@]}" -X "${method}" "${ENDPOINT}${path}" -d "$1"
  else
    curl -fsS "${AUTH[@]}" "${HDR[@]}" -X "${method}" "${ENDPOINT}${path}"
  fi
}

upsert_role() {
  # upsert_role NAME DISPLAY_NAME
  local name="$1" display="$2"
  local payload
  payload=$(jq -n \
    --arg owner "${ORG_NAME}" \
    --arg name "${name}" \
    --arg display "${display}" \
    '{owner:$owner, name:$name, displayName:$display, users:[], roles:[], isEnabled:true}')

  # GET first; if it 404s, fall through to add-role. Casdoor's /api/get-role
  # returns status="error" with msg containing "not exist" when missing.
  local existing
  existing=$(curl -fsS "${AUTH[@]}" \
    "${ENDPOINT}/api/get-role?id=${ORG_NAME}/${name}" || echo '{"status":"error"}')
  if echo "${existing}" | jq -e '.status == "ok"' >/dev/null 2>&1; then
    log "role ${name}: already present, skipping"
  else
    api POST "/api/add-role" "${payload}" >/dev/null
    log "role ${name}: created"
  fi
}

main() {
  log "endpoint=${ENDPOINT} org=${ORG_NAME} app=${APP_NAME}"

  # The 7 system roles mirror backend/app/main.py:_seed_default_roles().
  # Casbin policies (the permission grid) live in the casbin_rule table —
  # Casdoor only owns the role *names* + which users hold them.
  upsert_role "Admin"            "Admin"
  upsert_role "QA"               "QA Engineer"
  upsert_role "Viewer"           "Viewer"
  upsert_role "Project-Admin"    "Project Admin"
  upsert_role "Project-Tester"   "Project Tester"
  upsert_role "Project-Reviewer" "Project Reviewer"
  upsert_role "Project-Viewer"   "Project Viewer"

  log "done."
}

main "$@"
