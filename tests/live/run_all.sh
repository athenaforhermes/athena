#!/usr/bin/env bash
# Athena's whole test suite, from its durable home.
#
# This lives inside the plugin (`plugins/athena/tests/live/`) rather than in the
# agent's scratch folder because a scratch folder is pruned after a day idle —
# and it took four test scripts with it once already. Everything here reaches the
# real plugin by absolute path, so the suite can be run from anywhere:
#
#     bash plugins/athena/tests/live/run_all.sh
#
# `set -e` is deliberately absent: the point is to run every check and report,
# not to stop at the first failure.
# This script finds its own toolchain. The interpreter and the Node it drives can
# be pointed anywhere with ATHENA_PY and ATHENA_NODE, and every Athena path is
# derived from this file's own location — a suite that only runs on the machine it
# was written on is a suite nobody else can check, and this one is in the repo.
# A Windows path, not an MSYS one: the file names are handed to a native python.exe
# and node.exe, and neither understands /c/Users/... — the tools report "can't open
# file" and the suite looks like it failed when it never ran.
HERE="$(cd "$(dirname "$0")" && pwd -W)"
cd "$HERE" || exit 1
PLUG="$(cd "$HERE/../.." && pwd -W)"
HERMES_ROOT="$(cd "$PLUG/../.." && pwd -W)"
# The settings file of the machine this suite is running on, taken away for the duration
# and compared at the end. This exists because it has already happened: `iso_dir` was
# pointed at a test's temp folder in the *real* `settings.json`, and the symptom was a
# person setting their image folder and finding it moved back to C: the next day — three
# or four times, with nothing in the suite ever saying a word. The cause was an isolation
# failure, and an isolation failure is exactly the kind of thing that is invisible from
# inside the test that caused it: every check it makes passes, because it is reading what
# it wrote. So the guard is outside all of them, comparing the file's bytes before and
# after, and it names the keys that moved rather than counting them.
MACHINE_SETTINGS="$HERMES_ROOT/athena/settings.json"
SETTINGS_GUARD=""
if [ -f "$MACHINE_SETTINGS" ]; then
  SETTINGS_GUARD="$(mktemp)"
  cp "$MACHINE_SETTINGS" "$SETTINGS_GUARD"
fi
# Prefer the interpreter and the Node that Hermes installed for itself — the
# venv holds the backend's dependencies and the tools directory holds the Node the
# engine actually runs on. A bare `python` on PATH is frequently some other
# install that has neither, and the suite then fails at import for a reason that
# has nothing to do with the plugin. ATHENA_PY and ATHENA_NODE override both.
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
DESK="$PLUG/desktop/plugin.js"
MIRROR="${ATHENA_MIRROR:-$HERMES_ROOT/desktop-plugins/athena/plugin.js}"
if [ -z "$PY" ] || [ -z "$NODE" ]; then
  echo "no python or no node on PATH — set ATHENA_PY and ATHENA_NODE" >&2
  exit 1
fi

# Scratch that a long session leaves behind: these are set by earlier probes in
# the same shell, and an inherited ATHENA_HOME or NODE_OPTIONS makes suites fail
# here that pass when run alone. The suite tests the plugin, not the shell.
SCRUB="-u PYTHONPATH -u ATHENA_HOME -u ATHENA_PORT -u NODE_OPTIONS -u BOOT_PATH -u ATHENA_STORE"

fails=0
skips=0
run() {
  local label="$1"; shift
  local out
  out="$(env $SCRUB "$@" 2>&1)"
  local rc=$?
  local last
  last="$(printf '%s' "$out" | grep -E "checks passed|ALL UI CHECKS PASSED|failed" | tail -1)"
  # The label and the line it printed, for the claim checker at the end.
  printf '%s\t%s\n' "$label" "$last" >> "${COUNTS:-/dev/null}"
  # A probe that needs something this machine is not doing right now says
  # SKIPPED and exits 0 — it is not evidence of a broken plugin, and counting it
  # as a failure is how a suite teaches people to ignore it.
  if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "SKIPPED"; then
    skips=$((skips+1))
    printf '  skip %-22s %s\n' "$label" "$(printf '%s' "$out" | grep "SKIPPED" | head -1)"
  elif [ $rc -eq 0 ]; then
    printf '  ok   %-22s %s\n' "$label" "$last"
  else
    fails=$((fails+1))
    printf '  FAIL %-22s rc=%s  %s\n' "$label" "$rc" "$last"
    printf '%s\n' "$out" | grep -E "FAIL|Error|Traceback" | head -6 | sed 's/^/         /'
  fi
}

echo "== build =="
# The committed build is the one that ships and the one Hermes loads. Every line
# below regenerates desktop/plugin.js from desktop/plugin.js.pre-merge — which is
# exactly why nothing below can notice that the committed file is stale: the suite
# always tests what it has just generated, and a green suite therefore said
# nothing at all about the file in the repo. So its state as it arrived is
# recorded first, and the build is required to reproduce it byte for byte.
# A failure here means one thing only: you changed the sources and did not
# rebuild — run `python merge_ui.py` and commit desktop/plugin.js.
shipped_before="$(md5sum "$DESK" | cut -d' ' -f1)"
# merge_ui regenerates desktop/plugin.js from plugin.js.pre-merge, so it has to
# run before anything that reads the build — and twice, because a generator that
# is not idempotent is a generator that rewrites your file when you breathe.
env $SCRUB "$PY" merge_ui.py >/tmp/merge1.log 2>&1 || { echo "  FAIL merge_ui"; fails=$((fails+1)); }
md5a="$(md5sum "$DESK" | cut -d' ' -f1)"
env $SCRUB "$PY" merge_ui.py >/tmp/merge2.log 2>&1 || { echo "  FAIL merge_ui (2nd)"; fails=$((fails+1)); }
md5b="$(md5sum "$DESK" | cut -d' ' -f1)"
if [ "$md5a" = "$md5b" ]; then echo "  ok   merge_ui idempotent   $(wc -c < "$DESK") bytes"
else echo "  FAIL merge_ui rewrote its own output"; fails=$((fails+1)); fi
if [ "$shipped_before" = "$md5b" ]; then
  echo "  ok   build committed        the shipped panel is what its sources generate"
else
  echo "  FAIL build committed        desktop/plugin.js is stale: the sources generate a different file"
  echo "         (run: python merge_ui.py  — then commit desktop/plugin.js)"
  fails=$((fails+1))
fi

# The UI harness is a copy of the built plugin plus an export block, so it must
# be regenerated from the build that was just made or it tests yesterday's panel.
run "mk_ui_test"        timeout 300 env $SCRUB "$PY" mk_ui_test.py

echo "== python suite =="
# Every test's own summary line is kept, because the README quotes those counts
# and nothing used to compare the two. `check_doc_counts.py` below is what does
# the comparing — the claims in the docs are checked against what the tests
# actually printed in this run, not against a number somebody typed once.
# A Windows path, for the reason in the header: this file name is handed to a native
# python.exe, and `$TMPDIR` here is an MSYS path it cannot open. The gate then reported
# "no transcript given" and compared nothing at all — ten documented counts, including one
# that had been wrong for two rounds, were quietly unchecked by the step written to check
# them. A gate that skips is not a gate.
COUNTS="$(cd "${TMPDIR:-/tmp}" && pwd -W)/athena_counts.txt"
: > "$COUNTS"
for t in "$HERE"/t_*.py; do
  [ -f "$t" ] || continue
  # t_no_agent_claims has its own step below, under its own heading: it guards a
  # promise about what the shipped copy may say, not the plugin's behaviour, and
  # a gate about launch wording is easier to keep than one buried in the batch.
  case "$(basename "$t")" in t_no_agent_claims.py) continue ;; esac
  # Every file here runs, including the live probes: those decide for themselves
  # whether this machine is in a state where they can prove anything, and say
  # SKIPPED when it is not.
  # `timeout` because a plugin that deadlocks must fail this suite in minutes rather
  # than hang it for a night — which is exactly what a lock taken twice did until one
  # of these tests called the same route twice in a row and stopped the suite dead.
  run "$(basename "$t" .py)"  timeout 900 env $SCRUB "$PY" "$t"
done

echo "== doc claims =="
# The README quotes a check count for many of these files, and the count is a
# claim like any other: it was 154 for t_agent_tools long after the file had 161,
# and `t_screen.py` was documented at 60 while it ran 62. This compares what the
# README says against what the tests printed a moment ago, and re-derives the
# package, licence and tool counts from the tree instead of trusting the prose.
run "check_doc_counts"  timeout 300 env $SCRUB "$PY" check_doc_counts.py "$COUNTS"

echo "== shipped claims =="
# What the launch is allowed to say. Agent control was cut from the scope and
# every claim that apps are already agent-driven was removed from the shipped
# copy; this is the gate that keeps the removal a property of the product rather
# than a fact about the afternoon it was done. It fails with the file, the line
# number and the line. `agent` on its own is legitimate (agent projects,
# agent.json, the agent tool surface) and is not matched.
run "t_no_agent_claims" timeout 300 env $SCRUB "$PY" t_no_agent_claims.py

echo "== node suite =="
run "probe_tabs"        timeout 300 "$NODE" probe_tabs.mjs
# probe_net_ui reads the built panel, so it is given one explicitly.
run "probe_net_ui"      timeout 300 "$NODE" probe_net_ui.mjs "$DESK"
run "driver"            timeout 600 "$NODE" uitest/driver.mjs

echo "== inventory =="
# Every file in this directory is either run by the lines above or declared here
# as not-a-test, with the reason. Nothing used to assert that, and the cost of
# that silence is in the tree: machine_provision_check.py was written up in the
# README and reachable from nowhere, and a stale 568-line driver.mjs sat beside
# the real one shadowing it. A renamed test or an unwired probe now shows up as a
# failing step instead of as a smaller suite.
#
# Declared non-tests, and why:
#   keep_engine.py             manual: holds the Hermes engine up while a person pokes at it
#   live_port_probe.py         manual: prints which ports answer, for a human reading it
#   probe_browser_env.py       manual: what this machine's browser can and cannot do
#   pull_stub.py / pull_stub.cmd  fixtures: a fake git remote that t_pull drives
#   machine_provision_check.py manual: needs a machine that is running to prove anything
#   athena.mjs                 generated: mk_ui_test.py rewrites uitest/ athena.mjs every run
#   vncfake.py                 fixture: the fake VNC server, imported by t_screen and t_perf
NOT_TESTS="keep_engine.py live_port_probe.py probe_browser_env.py pull_stub.py pull_stub.cmd machine_provision_check.py athena.mjs vncfake.py"
stray=""
for f in "$HERE"/*; do
  [ -f "$f" ] || continue
  b="$(basename "$f")"
  case "$b" in
    run_all.sh|merge_ui.py|mk_ui_test.py|check_doc_counts.py|t_*.py|probe_tabs.mjs|probe_net_ui.mjs) continue ;;
  esac
  case " $NOT_TESTS " in *" $b "*) continue ;; esac
  stray="$stray $b"
done
if [ -n "$stray" ]; then
  echo "  FAIL inventory            file(s) here that neither run nor declare why:$stray"
  fails=$((fails+1))
else
  echo "  ok   inventory            every file here runs, or says why it does not"
fi
for d in "$HERE"/*/; do
  [ -d "$d" ] || continue
  b="$(basename "$d")"
  case "$b" in uitest|__pycache__) continue ;; esac
  echo "  FAIL stray directory      $b (nothing here should write a directory into the suite)"
  fails=$((fails+1))
done
outs="$(ls "$HERE"/*.out "$HERE"/uitest/*.out 2>/dev/null | wc -l | tr -d ' ')"
if [ "$outs" != "0" ]; then
  echo "  FAIL stray logs           $outs *.out file(s) left in the tree"
  fails=$((fails+1))
fi

echo "== release gates =="
# `hermes plugins validate` is the catalogue's admission gate, and it catches what
# nothing else here can: a tool registered but not declared, a manifest that does
# not parse, a load that only works because of an import side effect. It was the
# gate that failed the day a tool was added and the manifest was not — the plugin
# worked perfectly and would have been rejected on upload, which is exactly the
# kind of failure a suite should shout about rather than discover in public.
if command -v hermes >/dev/null 2>&1; then
  vout="$(hermes plugins validate "$PLUG" 2>&1)"
  if printf '%s' "$vout" | grep -q "Validation passed"; then
    echo "  ok   validate             hermes plugins validate — passed"
  else
    echo "  FAIL validate             the catalogue would reject this plugin"
    printf '%s' "$vout" | grep -E "✗|failed" | head -6 | sed 's/^/         /'
    fails=$((fails+1))
  fi
else
  echo "  skip validate             hermes is not on PATH"
fi
# The other half of that gate. `validate` is the runtime truth and needs a Hermes;
# these are the repository's own promises — the version in every file that carries
# one, the tool surface listed three ways, the licence inventory checked against a
# fresh walk of `engine/node_modules`, the Hermes window the manifest claims — and
# they run anywhere, including in CI on a machine that has never seen Hermes.
rout="$(env $SCRUB "$PY" t_release.py 2>&1)"
rrc=$?
rlast="$(printf '%s' "$rout" | grep -E "checks passed" | tail -1)"
printf 't_release\t%s\n' "$rlast" >> "${COUNTS:-/dev/null}"
if [ $rrc -eq 0 ]; then
  echo "  ok   release invariants   $rlast"
else
  fails=$((fails+1))
  echo "  FAIL release invariants   $rlast"
  printf '%s\n' "$rout" | grep -E "  FAIL" | head -8 | sed 's/^/         /'
fi

echo "== mirror =="
# cmp first, then copy. The other order made this line a tautology — the files
# were identical by construction, so the gate could never fail and drift between
# the built panel and the copy Hermes actually loads was announced as
# "identical" while it was being quietly overwritten. The mirror is a deployment
# target, so a stale one is still refreshed — but it now says so.
if cmp -s "$DESK" "$MIRROR"; then
  echo "  ok   desktop mirror identical"
else
  cp "$DESK" "$MIRROR"
  stales=$((stales+1))
  echo "  note desktop mirror updated    the loaded copy was stale; it has been replaced"
fi

echo "== the machine this suite ran on =="
if [ -n "$SETTINGS_GUARD" ]; then
  if cmp -s "$SETTINGS_GUARD" "$MACHINE_SETTINGS"; then
    echo "  ok   the real settings.json was left exactly as it was found"
  else
    fails=$((fails+1))
    echo "  FAIL the suite changed the settings file of the machine it ran on"
    diff -u "$SETTINGS_GUARD" "$MACHINE_SETTINGS" | sed 's/^/         /' | head -20
  fi
  rm -f "$SETTINGS_GUARD"
else
  echo "  note no settings.json to watch   (nothing was at $MACHINE_SETTINGS)"
fi

echo
if [ $fails -eq 0 ] && [ $skips -eq 0 ]; then
  echo "SUITE GREEN"
elif [ $fails -eq 0 ]; then
  echo "SUITE GREEN ($skips live probe(s) skipped — nothing running to watch)"
else
  echo "SUITE: $fails failing step(s), $skips skipped"
fi
exit $fails
