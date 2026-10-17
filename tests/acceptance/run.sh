#!/usr/bin/env bash
# Athena acceptance harness (plan rung R5).
#
# One run, in order, against the REAL plugin and a REAL Hermes install: enable the
# plugin, bring the engine up, build+install one known-good static app from the
# catalogue, prove its URL answers, stop it, uninstall it, stop the engine, disable
# the plugin, and assert the machine is left as it was found.
#
# This is NOT tests/live/run_all.sh. That suite regenerates desktop/plugin.js and
# owns the repo's own checks; this one installs nothing of its own and rewrites
# nothing — it drives the shipped plugin the way a stranger's machine would, over
# the plugin's HTTP API, and every step names the observable it asserted.
#
# Every route and every CLI flag below was read out of the tree; the file:line for
# each is in the comment beside it. Nothing here was guessed.
#
#     bash plugins/athena/tests/acceptance/run.sh
#
# Exit status is 0 only if all eight steps pass. The first failure prints
#   FAIL step <n> (<name>): <why>
# and stops — later steps would only report the wreckage of the earlier one.
set -u

HERE="$(cd "$(dirname "$0")" && pwd -W)"
# Same climb as tests/live (run_all.sh:20-23): acceptance/ and live/ sit at the same
# depth, so ../.. is the plugin and its ../.. is the Hermes root the plugin hangs off.
PLUG="$(cd "$HERE/../.." && pwd -W)"
HERMES_ROOT="$(cd "$PLUG/../.." && pwd -W)"

# The interpreter and Node Hermes installed for itself, found the same way the
# live suite finds them (tests/live/run_all.sh:29-43) — a bare `python` on PATH is
# often a different install that cannot import the backend. ATHENA_PY/ATHENA_NODE
# override both.
find_py() {
  for c in "$HERMES_ROOT/hermes-agent/venv/Scripts/python.exe" \
           "$HERMES_ROOT/hermes-agent/venv/bin/python"; do
    [ -x "$c" ] && { printf '%s' "$c"; return; }
  done
  command -v python3 || command -v python
}
find_node() {
  for c in "$HERMES_ROOT"/tools/node-22*/node.exe "$HERMES_ROOT"/tools/node-22*/bin/node; do
    [ -x "$c" ] && { printf '%s' "$c"; return; }
  done
  command -v node
}
PY="${ATHENA_PY:-$(find_py)}"
NODE="${ATHENA_NODE:-$(find_node)}"

# Which profile to drive. Empty means the active one. `hermes -p <profile> <cmd>` is
# the documented form ("hermes --help": *Run any command against a named profile's
# home (also --profile)*), so a fresh-profile run only has to set this one knob.
HERMES_PROFILE="${ATHENA_ACCEPT_PROFILE:-}"
hermes_cli() {
  if [ -n "$HERMES_PROFILE" ]; then hermes -p "$HERMES_PROFILE" "$@"; else hermes "$@"; fi
}

# The app this run installs. octocat/Spoon-Knife is the proven static case — a
# repository with no entry point at all, which the ladder must still run and prove
# (tests/live/t_ladder.py:5,27,29,80). The name is chosen so it cannot collide with
# a real install and so slug_for() leaves it exactly as written
# (dashboard/buildapp.py:107-109 strips only [^A-Za-z0-9._-]).
APP="${ATHENA_ACCEPT_APP:-athena-acceptance-static}"
REPO="${ATHENA_ACCEPT_REPO:-https://github.com/octocat/Spoon-Knife}"
BUILD_TIMEOUT="${ATHENA_ACCEPT_BUILD_TIMEOUT:-420}"
POLL=3

BASE=""    # http://127.0.0.1:<port> of the Hermes backend serving the plugin
TOKEN=""   # its session token, read from the rendezvous record's sibling .token file
HOME_DIR=""  # the Athena home, captured before the plugin is disabled
ENGINE_PORT=""  # the engine's port, captured before the plugin is disabled
APP_PATH=""     # the app folder the delete job reported removing
ENGINE_DIR="$PLUG/engine"
# The engine's node child carries a native path on its command line, so the check
# below matches on the backslash form (`pwd -W` hands us forward slashes).
ENGINE_DIR_WIN="${ENGINE_DIR//\//\\}"

STEP=""
begin() { STEP="$1"; printf '\n== %s\n' "$STEP"; }
note()  { printf '   %s\n' "$*"; }
ok()    { printf '   ok    %s\n' "$1"; }
die()   { printf '\nFAIL %s\n       %s\n' "$STEP" "$*" >&2; exit 1; }

# --- small JSON reader -------------------------------------------------------
# jget '<json>' dotted.path  -> the value, or empty when absent.
jget() {
  printf '%s' "$1" | "$PY" -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
except Exception:
    d = None
for k in sys.argv[1].split("."):
    d = d.get(k) if isinstance(d, dict) else None
print("" if d is None else d)
' "$2"
}

# --- HTTP --------------------------------------------------------------------
# api METHOD PATH [JSON-BODY]  -> sets RESP to the body and CODE to the status.
# curl is told to print the status on its own line after the body, so a body that
# contains newlines survives the split.
api() {
  local method="$1" path="$2" body="${3:-}" out code
  local args=(-s -m 90 -X "$method" -H "X-Hermes-Session-Token: $TOKEN" -w $'\n%{http_code}')
  [ -n "$body" ] && args+=(-H "Content-Type: application/json" --data "$body")
  if ! out="$(curl "${args[@]}" "$BASE/api/plugins/athena$path" 2>/dev/null)"; then
    CODE=000; RESP=""; return 1
  fi
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  RESP="$out"; CODE="$code"
  return 0
}

# Where does a running Hermes backend live, and what is its session token?
#
# The dashboard backend binds an OS-assigned port, so it cannot be hardcoded — but
# it publishes a rendezvous record the moment it is up: `host-<role>.json` (host,
# port) and `host-<role>.token` in the cross-profile lock dir
# (gateway/host_rendezvous.py:162,216,224; the dir is
# $HERMES_GATEWAY_LOCK_DIR else $XDG_STATE_HOME/hermes/gateway-locks —
# gateway/status.py:319-335). Requests carry that token in X-Hermes-Session-Token
# (hermes_cli/web_server.py _SESSION_HEADER_NAME); the plugin routes are mounted
# under /api/plugins/athena (README.md:89). Roles, in the order desktop and
# headless servers claim them: desktop-serve, serve (gateway/host_rendezvous.py:71,65).
discover() {
  local lock role rec tok port code
  lock="${HERMES_GATEWAY_LOCK_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/hermes/gateway-locks}"
  [ -d "$lock" ] || return 1
  for role in desktop-serve serve gateway; do
    rec="$lock/host-$role.json"
    [ -f "$rec" ] || continue
    port="$(jget "$(cat "$rec")" port)"
    [ -n "$port" ] || continue
    tok=""; [ -f "$lock/host-$role.token" ] && tok="$(cat "$lock/host-$role.token")"
    BASE="http://127.0.0.1:$port"
    TOKEN="$tok"
    code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' \
            -H "X-Hermes-Session-Token: $TOKEN" \
            "$BASE/api/plugins/athena/status" 2>/dev/null)"
    if [ "$code" = "200" ]; then
      note "backend: $BASE (role $role)"
      return 0
    fi
  done
  return 1
}

# A node process holding the engine: on Windows the engine is node.exe whose
# command line names the plugin's engine directory (verified here — the live engine
# matched this filter); elsewhere pgrep over the same path.
node_engine_procs() {
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      powershell -NoProfile -Command \
        "(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | Where-Object { \$_.CommandLine -like '*$ENGINE_DIR_WIN*' } | Measure-Object).Count" \
        2>/dev/null | tr -d '\r'
      ;;
    *)
      pgrep -af node 2>/dev/null | grep -c "$ENGINE_DIR" 2>/dev/null
      ;;
  esac
}

# Is anything listening on a TCP port? Windows has netstat; POSIX prefers ss.
port_listeners() {
  local port="$1" n
  if command -v ss >/dev/null 2>&1; then
    n="$(ss -Hltn 2>/dev/null | awk '{print $4}' | grep -c ":$port\$")"
  else
    n="$(netstat -ano 2>/dev/null | grep -i "LISTENING" | grep -c ":$port ")"
  fi
  printf '%s' "${n:-0}"
}

# ============================================================================
begin "step 0 — preflight (toolchain, backend, plugin reachable)"
[ -n "$PY" ]   || die "no python found — set ATHENA_PY (looked under $HERMES_ROOT)"
[ -n "$NODE" ] || die "no node found — set ATHENA_NODE (looked under $HERMES_ROOT)"
command -v hermes >/dev/null 2>&1 || die "the 'hermes' CLI is not on PATH"
discover || die "no running Hermes backend answers /api/plugins/athena/status.
       Start one first: the desktop app, or 'hermes serve' (hermes serve --help)."
api GET /status || die "GET /status failed on $BASE"
[ "$CODE" = "200" ] || die "GET /status returned HTTP $CODE"
ok "preflight: python=$PY"
ok "preflight: node=$NODE"
ok "preflight: $BASE/api/plugins/athena/status -> 200"

# ============================================================================
# The plugin's own state, captured before anything here touches it. Step 7 disables the
# plugin on purpose — that is the clean end state for a machine that had it disabled —
# but a machine that had it *enabled* is a machine somebody is using, and a harness that
# turned Athena off and walked away would be the exact bug it exists to catch. Restored
# on the way out including after a failure partway through, because the failures worth
# caring about are the ones that end the run early.
WAS_ENABLED=""
if hermes_cli plugins show athena 2>&1 | grep -q "Status: enabled"; then WAS_ENABLED=1; fi
restore_plugin() {
  [ -n "$WAS_ENABLED" ] || return 0
  hermes_cli plugins enable athena </dev/null >/dev/null 2>&1 || true
  printf 'restored: athena left enabled, as it was found\n'
}
trap restore_plugin EXIT

begin "step 1 — hermes plugins enable athena"
# CLI verb from README.md:87 and `hermes plugins --help`; the observable is the
# Status line `hermes plugins show athena` prints (hermes plugins show --help).
hermes_cli plugins enable athena </dev/null || die "hermes plugins enable athena exited non-zero"
show="$(hermes_cli plugins show athena 2>&1)"
printf '%s\n' "$show" | grep -q "Status: enabled" \
  || die "plugin show does not report 'Status: enabled':
$(printf '%s\n' "$show" | tail -4)"
ok "hermes plugins show athena -> Status: enabled"

# ============================================================================
begin "step 2 — start the Athena engine"
# POST /engine/start -> engine.start() (dashboard/plugin_api.py:338-340). The
# payload is engine.status() (:320-334): running/serving/port/url/home/node.
api POST /engine/start || die "POST /engine/start failed"
[ "$CODE" = "200" ] || die "POST /engine/start returned HTTP $CODE — $RESP"
running="$(jget "$RESP" running)"
ENGINE_PORT="$(jget "$RESP" port)"
HOME_DIR="$(jget "$RESP" home)"
[ "$running" = "True" ] || die "engine did not start (running=$running): $RESP"
[ -n "$ENGINE_PORT" ] || die "engine reported no port: $RESP"
ok "engine running on port $ENGINE_PORT (url http://127.0.0.1:$ENGINE_PORT)"
ok "athena home: $HOME_DIR"

# ============================================================================
begin "step 3 — install one known-good static app from the catalogue"
# POST /catalog/auto -> auto_build(url, name, force, wait)
# (dashboard/plugin_api.py:1434-1441, body model AutoIn :1248-1253). The ladder's
# job clones, scaffolds, installs and SMOKES the app; the result carries
# verified/proved (:1399-1405). This is the route the live suite proves
# octocat/Spoon-Knife through (tests/live/t_ladder.py:27,80 — '/catalog/auto is
# mounted' is asserted at :73): /catalog/install would fail on this repo, which
# ships no launcher for it to run.
note "building $REPO as '$APP' (this clones and can take a minute)"
api POST /catalog/auto "{\"url\":\"$REPO\",\"name\":\"$APP\",\"force\":true,\"wait\":90}" \
  || die "POST /catalog/auto failed"
[ "$CODE" = "200" ] || die "POST /catalog/auto returned HTTP $CODE — $RESP"
JOB="$(jget "$RESP" job)"
[ -n "$JOB" ] || die "catalog/auto returned no job: $RESP"
note "job $JOB"

# Poll GET /jobs/{job_id} (dashboard/plugin_api.py:4370) until the state leaves
# "running" (:4230 sets it, :4287 clears it).
waited=0; state="running"
while [ "$waited" -lt "$BUILD_TIMEOUT" ]; do
  api GET "/jobs/$JOB" || die "GET /jobs/$JOB failed"
  [ "$CODE" = "200" ] || die "GET /jobs/$JOB returned HTTP $CODE — $RESP"
  state="$(jget "$RESP" state)"
  [ "$state" = "running" ] || break
  sleep "$POLL"; waited=$((waited+POLL))
done
[ "$state" != "running" ] || die "build job $JOB still running after ${BUILD_TIMEOUT}s"
# Dotted paths straight into the reply, never an intermediate `result` string. `jget`
# reads a path; a nested object pulled out on its own is printed as a Python dict repr,
# not as JSON, so re-parsing it yields nothing — `verified` came back empty, and a build
# that had succeeded was reported as one that had not. The empty `stage=` and `reason=`
# in the failure message were the same bug twice.
verified="$(jget "$RESP" result.verified)"
app_id="$(jget "$RESP" result.app)"
if [ "$verified" != "True" ]; then
  die "build did not verify (state=$state stage=$(jget "$RESP" result.stage) reason=$(jget "$RESP" result.reason))"
fi
[ -n "$app_id" ] || app_id="$APP"
ok "installed '$app_id' — job state=$state verified=true proved=$(jget "$RESP" result.proved)"

# ============================================================================
begin "step 4 — probe the app's URL until it answers"
# The ladder stops the app once it has its verdict, deliberately
# (dashboard/plugin_api.py:1395-1399), so 'installed' is not 'running'. Launch it
# with POST /apps/{app_id}/launch (dashboard/plugin_api.py:7779, APP_SCRIPTS
# :716-721 maps 'launch' to no script) and read where it is from
# GET /apps/{app_id}/status (:994-1019, fields running/url/port).
# No body: ActionBody's two fields are both optional (:1236-1246), and an app with
# a single default entry needs neither.
api POST "/apps/$app_id/launch" || die "POST /apps/$app_id/launch failed"
[ "$CODE" = "200" ] || die "launch returned HTTP $CODE — $RESP"

waited=0; url=""; app_running=""
while [ "$waited" -lt 90 ]; do
  api GET "/apps/$app_id/status" || die "GET /apps/$app_id/status failed"
  [ "$CODE" = "200" ] || die "app status returned HTTP $CODE — $RESP"
  app_running="$(jget "$RESP" running)"
  url="$(jget "$RESP" url)"
  if [ "$app_running" = "True" ] && [ -n "$url" ]; then break; fi
  sleep "$POLL"; waited=$((waited+POLL))
done
[ -n "$url" ] || die "app '$app_id' reported no url after ${waited}s (running=$app_running)"

waited=0; http_code=000
while [ "$waited" -lt 60 ]; do
  # `|| printf '000'` inside the substitution appended rather than replaced when curl
  # printed a code *and* exited non-zero, which produced the message "HTTP 200000" — a
  # code that is not a code, in the one line an operator reads. Last three digits, so
  # the number in the message is the number curl actually reported.
  http_code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)"
  http_code="${http_code: -3}"
  [ -n "$http_code" ] || http_code=000
  case "$http_code" in 2*|3*) break ;; esac
  sleep "$POLL"; waited=$((waited+POLL))
done
case "$http_code" in
  2*|3*) ok "app answered: $url -> HTTP $http_code" ;;
  *)     die "app url $url did not answer within ${waited}s (last HTTP $http_code)" ;;
esac

# ============================================================================
begin "step 5 — stop the app"
# POST /apps/{app_id}/stop (dashboard/plugin_api.py:7790-7796 -> runtime.stop,
# dashboard/runtime.py:262). Asserted by watching running fall to false on
# GET /apps/{app_id}/status, not by trusting the stop reply alone.
api POST "/apps/$app_id/stop" || die "POST /apps/$app_id/stop failed"
[ "$CODE" = "200" ] || die "stop returned HTTP $CODE — $RESP"
waited=0; app_running="True"
while [ "$waited" -lt 60 ]; do
  api GET "/apps/$app_id/status" || die "GET /apps/$app_id/status failed after stop"
  app_running="$(jget "$RESP" running)"
  [ "$app_running" = "False" ] && break
  sleep "$POLL"; waited=$((waited+POLL))
done
[ "$app_running" = "False" ] || die "app '$app_id' still reports running after stop"
ok "app stopped (running=false)"

# ============================================================================
begin "step 6 — uninstall the app"
# DELETE /apps/{app_id} (dashboard/plugin_api.py:3300) runs as a job and returns
# {"ok":true,"job":...} (:3395). The job's verdict is result.removed / result.ok
# (:3388-3392) — the folder actually went, which is what is asserted here.
api DELETE "/apps/$app_id" || die "DELETE /apps/$app_id failed"
[ "$CODE" = "200" ] || die "delete returned HTTP $CODE — $RESP"
djob="$(jget "$RESP" job)"
if [ -n "$djob" ]; then
  waited=0; dstate="running"
  while [ "$waited" -lt 180 ]; do
    api GET "/jobs/$djob" || die "GET /jobs/$djob failed"
    dstate="$(jget "$RESP" state)"
    [ "$dstate" = "running" ] || break
    sleep "$POLL"; waited=$((waited+POLL))
  done
  [ "$dstate" != "running" ] || die "delete job $djob still running after 180s"
  # Dotted paths again, for the same reason as step 3: an intermediate `result` string
  # is a dict repr rather than JSON, so `removed` read as empty and a delete that had
  # emptied the folder was reported as one that had not. The folder really was gone —
  # which is the worst kind of false failure, because it points at the plugin.
  [ "$(jget "$RESP" result.removed)" = "True" ] \
    || die "delete did not remove the folder (state=$dstate, $(jget "$RESP" result.error))"
  # The job reports the path it deleted (:3334) — the real on-disk name, which need
  # not be the app id. Step 8 checks against this, not against a guess.
  APP_PATH="$(jget "$RESP" result.path)"
fi
[ -n "${APP_PATH:-}" ] || APP_PATH="$HOME_DIR/api/$app_id"
# Folded into step 6 on purpose: step 2's engine outlives the routes it is started
# through, so it must be stopped while they are still live — step 8 could not
# otherwise find the engine port clean, and `hermes plugins disable` does NOT stop
# it (hermes_cli/plugins_cmd.py:753-767 only edits the allow-list).
api POST /engine/stop || die "POST /engine/stop failed"
[ "$CODE" = "200" ] || die "engine stop returned HTTP $CODE — $RESP"
ok "app '$app_id' uninstalled; engine stopped"

# ============================================================================
begin "step 7 — disable the plugin"
hermes_cli plugins disable athena </dev/null || die "hermes plugins disable athena exited non-zero"
show="$(hermes_cli plugins show athena 2>&1)"
printf '%s\n' "$show" | grep -q "Status: disabled" \
  || die "plugin show does not report 'Status: disabled':
$(printf '%s\n' "$show" | tail -4)"
ok "hermes plugins show athena -> Status: disabled"

# ============================================================================
begin "step 8 — assert the machine is left clean"
# Nothing listening on the engine port the plugin reported in step 2, and no node
# process wearing the engine's directory on its command line.
[ -n "$ENGINE_PORT" ] || die "no engine port was captured in step 2"
listeners="$(port_listeners "$ENGINE_PORT")"
[ "$listeners" = "0" ] || die "something is still listening on the engine port $ENGINE_PORT"
ok "port $ENGINE_PORT has no listener"
procs="$(node_engine_procs)"
case "$procs" in
  ''|*[!0-9]*) note "node-process check unavailable on this platform — port check stands alone" ;;
  *) [ "$procs" = "0" ] || die "$procs node process(es) still hold the engine dir" ;
     ok "no node process holds the engine directory" ;;
esac

# Say plainly what happened to the Athena home: uninstall removes the app, not the
# house. The home and its caches belong to every installed app, so they stay.
if [ -d "$HOME_DIR" ]; then
  if [ -d "$APP_PATH" ]; then
    die "the app folder still exists: $APP_PATH"
  fi
  ok "athena home kept: $HOME_DIR (the app folder $APP_PATH is gone)"
else
  note "athena home is absent: $HOME_DIR (nothing was left behind)"
fi

printf '\nACCEPTANCE PASSED — 8/8 steps, the machine is as it was found.\n'
