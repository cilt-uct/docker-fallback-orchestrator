#!/usr/bin/env bash
# End-to-end test of the controller against a real Opencast, using a fake venue.
#
#   scripts/demo_test.sh run       start a test camera, register a fake capture agent,
#                                  mark it offline, schedule a short lecture on it, and
#                                  watch the controller start a backup that records it
#   scripts/demo_test.sh status    show where the test is up to
#   scripts/demo_test.sh cleanup   stop and delete the backup, the agent, the event and
#                                  the test camera
#
# Run it from the repository root on the controller's host. Opencast details and the
# recordings folder are read from .env. Override with environment variables:
#   AGENT=orchestrator-test   fake agent ID (also the backup's venue name)
#   LEAD_SECONDS=240          time until the lecture starts
#   DURATION_SECONDS=120      length of the lecture
#   WORKFLOW=fast             Opencast workflow the event uses
#   RTSP_URL=...              use a real camera instead of the test stream
#   TEST_RTSP_PORT=8554       host port for the test stream
#   CONTROLLER_URL=http://localhost:8090
#   OC_URL=https://...        Opencast as seen from this host
set -euo pipefail

cd "$(dirname "$0")/.."

AGENT=${AGENT:-orchestrator-test}
LEAD_SECONDS=${LEAD_SECONDS:-240}
DURATION_SECONDS=${DURATION_SECONDS:-120}
WORKFLOW=${WORKFLOW:-fast}
TEST_RTSP_PORT=${TEST_RTSP_PORT:-8554}
CONTROLLER_URL=${CONTROLLER_URL:-http://localhost:8090}
RTSP_CONTAINER=orchestrator-test-rtsp
RTSP_IMAGE=bluenviron/mediamtx:1.9.3-ffmpeg
STATE_FILE=${TMPDIR:-/tmp}/orchestrator-demo-test.state

log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { log "FAIL: $*"; exit 1; }

env_value() {
  # Last NAME=value in .env, without surrounding quotes. Empty if unset.
  [[ -f .env ]] || return 0
  grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

OC_URL=${OC_URL:-$(env_value OPENCAST_PROTOCOL)://$(env_value OPENCAST_HOST)}
OC_USER=$(env_value OPENCAST_USERNAME)
OC_PASS=$(env_value OPENCAST_PASSWORD)
RECORDINGS_DIR=$(env_value RECORDINGS_HOST_DIR)
RECORDINGS_DIR=${RECORDINGS_DIR:-/data/pyca-recordings}

oc() {
  # oc METHOD PATH [curl args...] - Opencast request with the system account; prints the
  # body, then the status code on its own line.
  local method=$1 path=$2
  shift 2
  curl -s --digest -u "$OC_USER:$OC_PASS" -H 'X-Requested-Auth: Digest' \
    -X "$method" -w '\n%{http_code}' "$@" "$OC_URL$path"
}

status_of() { tail -n1 <<<"$1"; }
body_of() { sed '$d' <<<"$1"; }

utc_in() {
  # ISO 8601 UTC time N seconds from now (GNU date, or BSD date on macOS).
  date -u -d "+$1 seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+"$1"S +%Y-%m-%dT%H:%M:%SZ
}

json() {
  # json EXPR - evaluate a Python expression over the JSON on stdin (bound to `d`).
  python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"
}

instance_field() {
  # instance_field FIELD - a field of the test venue's instance, empty if there is none.
  curl -sf "$CONTROLLER_URL/instances" |
    json "next((str(i['$1']) for i in d if i['venue'] == '$AGENT'), '')"
}

host_address() {
  # How the backup's containers reach a port published on this host.
  if [[ $(uname) == Darwin ]]; then
    echo host.docker.internal
  else
    hostname -I | awk '{print $1}'
  fi
}

recordings() {
  find "$RECORDINGS_DIR/$AGENT" -name '*.mp4' 2>/dev/null || true
}

save_state() { printf '%s=%s\n' "$1" "$2" >>"$STATE_FILE"; }
load_state() { [[ -f $STATE_FILE ]] && grep -E "^$1=" "$STATE_FILE" | tail -1 | cut -d= -f2- || true; }

preflight() {
  command -v python3 >/dev/null || fail "python3 is needed to read JSON"
  [[ -n $OC_USER && -n $OC_PASS ]] || fail "OPENCAST_USERNAME / OPENCAST_PASSWORD missing from .env"

  local ready
  ready=$(curl -s -o /dev/null -w '%{http_code}' "$CONTROLLER_URL/monitoring/ready" || true)
  [[ $ready == 200 ]] || fail "controller not ready at $CONTROLLER_URL/monitoring/ready (HTTP $ready)"
  log "Controller ready at $CONTROLLER_URL"

  local resp
  resp=$(oc GET /capture-admin/agents.json)
  [[ $(status_of "$resp") == 200 ]] || fail "Opencast at $OC_URL refused the system account (HTTP $(status_of "$resp"))"
  log "Opencast reachable at $OC_URL"

  [[ -z $(instance_field id) ]] || fail "an instance for '$AGENT' already exists - run: $0 cleanup"
  [[ -d $RECORDINGS_DIR ]] || fail "recordings folder $RECORDINGS_DIR doesn't exist"
}

start_test_stream() {
  if [[ -n ${RTSP_URL:-} ]]; then
    log "Using camera $RTSP_URL"
    return
  fi
  docker rm -f "$RTSP_CONTAINER" >/dev/null 2>&1 || true
  # mediamtx serves RTSP; ffmpeg in the same container publishes a test pattern to it.
  docker run -d --name "$RTSP_CONTAINER" -p "$TEST_RTSP_PORT:8554" --entrypoint sh "$RTSP_IMAGE" -c \
    '/mediamtx & sleep 2; exec ffmpeg -nostats -re -f lavfi -i testsrc=size=1280x720:rate=25 \
       -f lavfi -i sine=frequency=440 -c:v libx264 -preset veryfast -tune zerolatency \
       -pix_fmt yuv420p -g 50 -c:a aac -f rtsp rtsp://localhost:8554/test' >/dev/null
  RTSP_URL="rtsp://$(host_address):$TEST_RTSP_PORT/test"
  sleep 5
  docker ps --filter "name=^$RTSP_CONTAINER$" --format '{{.Status}}' | grep -q Up ||
    fail "test stream container exited: $(docker logs --tail 20 "$RTSP_CONTAINER" 2>&1)"
  log "Test stream running: $RTSP_URL"
}

register_agent() {
  local resp config
  resp=$(oc POST "/capture-admin/agents/$AGENT" --data state=idle)
  [[ $(status_of "$resp") == 200 ]] || fail "could not register agent '$AGENT': $(body_of "$resp")"

  config="{\"capture.device.names\":\"presenter\",\"capture.device.presenter.src\":\"$RTSP_URL\"}"
  resp=$(oc POST "/capture-admin/agents/$AGENT/configuration" --data-urlencode "configuration=$config")
  [[ $(status_of "$resp") == 200 ]] || fail "could not set the agent's camera: $(body_of "$resp")"

  resp=$(oc POST "/capture-admin/agents/$AGENT" --data state=offline)
  [[ $(status_of "$resp") == 200 ]] || fail "could not mark the agent offline: $(body_of "$resp")"
  log "Fake agent '$AGENT' registered with camera $RTSP_URL, and marked offline"
}

schedule_event() {
  local start end resp event_id
  start=$(utc_in "$LEAD_SECONDS")
  end=$(utc_in $((LEAD_SECONDS + DURATION_SECONDS)))
  # /api/events only accepts multipart form data (anything else is a 415).
  resp=$(oc POST /api/events \
    --form-string "metadata=[{\"flavor\":\"dublincore/episode\",\"fields\":[{\"id\":\"title\",\"value\":\"Orchestrator test $(date '+%Y-%m-%d %H:%M')\"}]}]" \
    --form-string 'acl=[{"allow":true,"role":"ROLE_ADMIN","action":"read"},{"allow":true,"role":"ROLE_ADMIN","action":"write"}]' \
    --form-string "scheduling={\"agent_id\":\"$AGENT\",\"start\":\"$start\",\"end\":\"$end\",\"inputs\":[\"default\"]}" \
    --form-string "processing={\"workflow\":\"$WORKFLOW\",\"configuration\":{}}")
  [[ $(status_of "$resp") == 201 ]] || fail "could not schedule the event (HTTP $(status_of "$resp")): $(body_of "$resp")"
  event_id=$(body_of "$resp" | json "d['identifier']")
  save_state EVENT_ID "$event_id"
  save_state END "$end"
  log "Scheduled event $event_id: $start -> $end UTC (workflow '$WORKFLOW')"
}

show_status() {
  local status port files
  status=$(instance_field status)
  port=$(instance_field ui_port)
  files=$(recordings)
  log "Backup: ${status:-not created yet}${port:+ (PyCA page on port $port)}"
  if [[ -n $files ]]; then
    while read -r f; do log "Recording: $f ($(du -h "$f" | cut -f1))"; done <<<"$files"
  fi
}

watch_test() {
  local end_epoch deadline status seen_running=""
  end_epoch=$(($(date +%s) + LEAD_SECONDS + DURATION_SECONDS))
  deadline=$((end_epoch + 120))
  log "Watching until the lecture has ended (about $(((LEAD_SECONDS + DURATION_SECONDS) / 60 + 2)) minutes)..."
  while (($(date +%s) < deadline)); do
    status=$(instance_field status)
    if [[ $status == running && -z $seen_running ]]; then
      seen_running=1
      log "PASS: controller started a backup for '$AGENT' on its own"
    fi
    show_status
    # Done once the lecture is over and its file has stopped growing.
    if (($(date +%s) > end_epoch + 30)) && [[ -n $(recordings) ]]; then
      break
    fi
    sleep 20
  done

  echo
  [[ -n $seen_running ]] || fail "the controller never started a backup - check: docker compose logs controller"
  [[ -n $(recordings) ]] || fail "no recording in $RECORDINGS_DIR/$AGENT - check: docker logs pyca-$AGENT-capture"
  log "PASS: recording made in $RECORDINGS_DIR/$AGENT"
  echo
  echo "Next, on the dashboard ($CONTROLLER_URL):"
  echo "  - Audit log: the system's start of '$AGENT' and its reason"
  echo "  - Recordings: the new recording and when it will be deleted"
  echo "  - Instances: open the PyCA page, ingest the recording, and watch it in Opencast"
  echo "When you're done: $0 cleanup"
}

cleanup() {
  local id event_id resp
  id=$(instance_field id)
  if [[ -n $id ]]; then
    curl -sf -X POST "$CONTROLLER_URL/instances/$id/stop" >/dev/null || log "stop failed (may already be stopped)"
    curl -sf -X DELETE "$CONTROLLER_URL/instances/$id" >/dev/null && log "Backup for '$AGENT' stopped and deleted" ||
      log "could not delete the backup - do it from the dashboard"
  fi

  event_id=$(load_state EVENT_ID)
  if [[ -n $event_id ]]; then
    resp=$(oc DELETE "/api/events/$event_id")
    log "Event $event_id deleted (HTTP $(status_of "$resp"))"
  fi

  resp=$(oc DELETE "/capture-admin/agents/$AGENT")
  log "Agent '$AGENT' removed (HTTP $(status_of "$resp"))"

  docker rm -f "$RTSP_CONTAINER" >/dev/null 2>&1 && log "Test stream removed" || true
  rm -f "$STATE_FILE"
  log "Recordings left in $RECORDINGS_DIR/$AGENT - delete them on the Recordings page, or leave them to expire"
}

case ${1:-} in
  run)
    rm -f "$STATE_FILE"
    preflight
    start_test_stream
    register_agent
    schedule_event
    watch_test
    ;;
  status) show_status ;;
  cleanup) cleanup ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
