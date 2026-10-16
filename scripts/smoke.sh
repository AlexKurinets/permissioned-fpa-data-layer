#!/usr/bin/env bash
# Runs every operator sequence the README documents against a running Compose stack.
#   bash scripts/smoke.sh
# Ports come from FPA_API_PORT and FPA_DASHBOARD_PORT (default 8000 and 8501).
# Set PYTHON if python3 on this machine is not a real interpreter.
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash on Windows would rewrite container paths such as /tmp

API="http://localhost:${FPA_API_PORT:-8000}"
DASH="http://localhost:${FPA_DASHBOARD_PORT:-8501}"
PY="${PYTHON:-python3}"

json() { "$PY" -c 'import json, sys; d = json.load(sys.stdin); print(eval(sys.argv[1], {"d": d, "len": len}))' "$1"; }
token() {
  curl -fsS -X POST "$API/auth/dev-token" -H 'content-type: application/json' -d "{\"person_id\": \"$1\"}" \
    | json 'd["access_token"]'
}
get() { curl -fsS -H "Authorization: Bearer $1" "$API$2"; }
ask() {
  curl -fsS -X POST "$API/ask" -H "Authorization: Bearer $1" -H 'content-type: application/json' \
    -d "{\"question\": \"$2\"}" | json 'd["answer"]'
}
check() {
  if [ "$1" != "$2" ]; then echo "FAIL  $3: expected [$2], got [$1]"; exit 1; fi
  echo "ok    $3"
}
starts() {
  case "$1" in "$2"*) echo "ok    $3" ;; *) echo "FAIL  $3: got [$1]"; exit 1 ;; esac
}
pipeline() { docker compose run --rm --no-deps -T pipeline "$@"; }

echo "== Health"
check "$(curl -fsS "$API/health" | json 'd["latest_run"]["status"]')" published "latest refresh is published"
check "$(curl -fsS "$DASH/_stcore/health")" ok "dashboard is up"

CFO=$(token maya.ortiz@example.com)
ENG=$(token dana.whitfield@example.com)
PLATFORM=$(token omar.haddad@example.com)
NEWS=$(token leo.marsh@example.com)
LEAD=$(token sam.keller@example.com)

echo "== Scope"
check "$(get "$ENG" /me | json 'd["cost_centers"]')" "['4110', '4120', '4130']" "VP Engineering sees three cost centers"
check "$(get "$CFO" /variances | json 'len(d["lines"])')" 10 "CFO sees all ten material lines"
check "$(get "$ENG" /variances | json 'len(d["lines"])')" 2 "VP Engineering sees two"
check "$(get "$PLATFORM" /variances | json 'len(d["lines"])')" 0 "cost center manager sees no department commentary"
check "$(get "$ENG" /headcount | json 'd["salary_visible"]')" False "salaries hidden without comp_detail"
check "$(get "$LEAD" /headcount | json 'd["salary_visible"]')" True "salaries visible to the FP&A lead"
check "$(get "$NEWS" /forecast | json '[r["unit_id"] for r in d["revenue"]]')" "['LN']" "Local News GM sees only the LN forecast"

echo "== Ask"
starts "$(ask "$ENG" "What did we spend on contractors in Engineering in September?")" \
  'Contractors (6050) for Engineering, September 2026: $80,100 actual against $36,000 budget, $44,100 over' \
  "contractors question answered in scope"
check "$(ask "$ENG" "Display advertising in Local News vs budget")" "You don't have access to Local News." \
  "question outside scope refused"

echo "== Package"
check "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $ENG" "$API/packages/2026-09.pptx")" 200 \
  "Engineering package builds"

echo "== README: break the tie-out, watch publishing stop, put it back"
pipeline fpa simulate drop-line --period 2026-09
set +e
pipeline fpa refresh >/dev/null
status=$?
set -e
check "$status" 2 "refresh exits 2 and does not publish"
check "$(curl -fsS "$API/health" | json 'd["latest_run"]["status"]')" failed_tieout "latest run is failed_tieout"
check "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $CFO" "$API/packages/2026-09.pptx")" 409 \
  "package refused while the books don't tie"
check "$(get "$CFO" /status | json 'len(d["tieout_breaks"])')" 2 "two breaks reported"
pipeline fpa simulate restore
pipeline fpa refresh >/dev/null
check "$(curl -fsS "$API/health" | json 'd["latest_run"]["status"]')" published "republished after restore"

echo "== README: CLI"
check "$(pipeline fpa access review --as sam.keller@example.com | json 'len(d)')" 12 "access review lists every grant"
starts "$(pipeline fpa ask "net revenue year to date" --as dana.whitfield@example.com | json 'd["answer"]')" \
  "Net revenue for everything you have access to" "CLI ask runs under the asker's grants"
pipeline fpa package --period 2026-09 --as dana.whitfield@example.com --out /tmp/engineering.pptx >/dev/null
echo "ok    CLI package builds"
GRANT_ID=$(pipeline fpa access grant grace.lin@example.com department CORP-ENG   --reason "interim Engineering budget owner" --by sam.keller@example.com --expires 2030-12-31 | json 'd["granted"]')
GRACE=$(token grace.lin@example.com)
check "$(get "$GRACE" /me | json '"4110" in d["cost_centers"]')" True "a new grant takes effect on the next request"
pipeline fpa access revoke "$GRANT_ID" --by sam.keller@example.com >/dev/null
check "$(get "$GRACE" /me | json '"4110" in d["cost_centers"]')" False "revoking it takes effect too"

echo "smoke passed"
