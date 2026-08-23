#!/usr/bin/env bash
# Trigger a build and wait for its result.
#
# Written as a script rather than inline in the Makefile because the polling
# needs JSON parsing, and JSON parsing inside a make recipe means quoting the
# same string for make, then the shell, then Python.
#
#   ./infra/jenkins/build.sh [job]
#
# Exits non-zero when the build is not SUCCESS, so it can gate anything else.

set -euo pipefail

JOB="${1:-resumeforge}"
BASE="http://localhost:${JENKINS_PORT:-8080}"
AUTH="${JENKINS_ADMIN_ID:-admin}:${JENKINS_ADMIN_PASSWORD:-admin}"

cookie=$(mktemp)
trap 'rm -f "$cookie"' EXIT

# Jenkins requires a CSRF crumb, and the crumb is bound to the session that
# issued it -- hence the cookie jar.
crumb=$(curl -fsS -c "$cookie" -u "$AUTH" "$BASE/crumbIssuer/api/json" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["crumb"])')

# The POST returns the queue item, not the build: a build has no number until an
# executor picks it up. Polling `lastBuild` instead would race the queue and
# report on the previous build -- which looks exactly like a build that finished
# suspiciously fast.
queue=$(curl -fsS -b "$cookie" -u "$AUTH" -H "Jenkins-Crumb: $crumb" \
  -X POST "$BASE/job/$JOB/build" -o /dev/null -D - \
  | tr -d '\r' | awk 'tolower($1) == "location:" { print $2 }')
echo "queued: $queue"

number=""
for _ in $(seq 1 60); do
  number=$(curl -fsS -u "$AUTH" "${queue}api/json" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("executable", {}).get("number", ""))')
  [ -n "$number" ] && break
  sleep 2
done
[ -n "$number" ] || { echo "the build never started (still queued?)"; exit 1; }

echo "build #$number started; following..."
until curl -fsS -u "$AUTH" "$BASE/job/$JOB/$number/api/json?tree=building" | grep -q '"building":false'; do
  sleep 10
done

curl -fsS -u "$AUTH" "$BASE/job/$JOB/$number/api/json?tree=number,result,duration" \
  | python3 -c '
import json, sys
build = json.load(sys.stdin)
number, result, ms = build["number"], build["result"], build["duration"]
print("build %s -> %s in %.0fs" % (number, result, ms / 1000))
sys.exit(0 if result == "SUCCESS" else 1)
'
