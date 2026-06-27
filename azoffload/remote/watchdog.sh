#!/usr/bin/env bash
# mjoff cloud-side self-destruct watchdog.
#
# This is the cost-safety guarantee that does NOT depend on the controller / your
# laptop. It runs from the very start of boot and self-deallocates this VM when:
#   1. the job signals completion (COMPLETE file), OR
#   2. an absolute max lifetime is exceeded (MAX_LIFETIME_SECONDS), OR
#   3. the job is stuck (no heartbeat update for STUCK_TIMEOUT_SECONDS).
#
# Deallocate is done via the Azure REST API using a token from IMDS
# (169.254.169.254) and the VM's managed identity — no `az` install, no stored
# secret. Deallocate (not just power-off) stops the expensive compute billing.
set -uo pipefail

ENV_FILE=/opt/mjoff/env
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a

LOG() { echo "[$(date -u +%FT%TZ)] watchdog: $*"; }

imds_token() {
  # append &client_id=... only for a user-assigned identity; empty -> system-assigned
  local q=""
  [ -n "${MJOFF_IDENTITY_CLIENT_ID:-}" ] && q="&client_id=${MJOFF_IDENTITY_CLIENT_ID}"
  curl -s -m 10 -H "Metadata:true" \
    "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/${q}" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])" 2>/dev/null
}

vm_resource_id() {
  curl -s -m 10 -H "Metadata:true" \
    "http://169.254.169.254/metadata/instance?api-version=2021-02-01" \
    | python3 -c "import sys,json;d=json.load(sys.stdin)['compute'];print('/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s'%(d['subscriptionId'],d['resourceGroupName'],d['name']))" 2>/dev/null
}

deallocate_self() {
  LOG "initiating self-deallocate (reason: $1)"
  for attempt in 1 2 3 4 5; do
    TOKEN=$(imds_token); RID=$(vm_resource_id)
    if [ -n "${TOKEN:-}" ] && [ -n "${RID:-}" ]; then
      CODE=$(curl -s -m 30 -o /dev/null -w "%{http_code}" -X POST \
        -H "Authorization: Bearer ${TOKEN}" -H "Content-Length: 0" \
        "https://management.azure.com${RID}/deallocate?api-version=2023-09-01")
      LOG "deallocate POST http=${CODE} attempt=${attempt}"
      case "$CODE" in 200|202) LOG "deallocate accepted by ARM"; return 0;; esac
    else
      LOG "could not get IMDS token / resource id (attempt ${attempt})"
    fi
    sleep 10
  done
  LOG "REST deallocate failed; falling back to guest poweroff (billing may continue until controller deletes the VM)"
  shutdown -h now || poweroff -f || true
}

START=$(date +%s)
MAXLIFE="${MAX_LIFETIME_SECONDS:-7200}"
STUCK="${STUCK_TIMEOUT_SECONDS:-600}"
RUNNER_STARTED="${MJOFF_RUNNER_STARTED:-/opt/mjoff/RUNNER_STARTED}"
COMPLETE="${MJOFF_COMPLETE:-/opt/mjoff/COMPLETE}"
HEARTBEAT="${MJOFF_HEARTBEAT_FILE:-/opt/mjoff/heartbeat}"

LOG "started: max_life=${MAXLIFE}s stuck=${STUCK}s"
while true; do
  NOW=$(date +%s); ELAPSED=$((NOW - START))

  if [ -f "$COMPLETE" ]; then
    deallocate_self "job-complete"; exit 0
  fi
  if [ "$ELAPSED" -ge "$MAXLIFE" ]; then
    deallocate_self "max-lifetime"; exit 0
  fi
  # Stuck detection only applies once the runner has actually started.
  if [ -f "$RUNNER_STARTED" ] && [ -f "$HEARTBEAT" ]; then
    HB=$(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo "$NOW")
    if [ $((NOW - HB)) -ge "$STUCK" ]; then
      deallocate_self "stuck-no-heartbeat"; exit 0
    fi
  fi
  sleep 15
done
