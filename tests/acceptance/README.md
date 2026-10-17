# Athena acceptance harness (rung R5)

One shell script that drives the **real, shipped** Athena plugin through a complete
life cycle against a real Hermes install — enable, start the engine, install a real
app from the catalogue, prove its URL answers, stop it, uninstall it, stop the
engine, disable the plugin — and then proves the machine is exactly as it was found.
Each step asserts one observable and exits non-zero naming the step that failed.

```bash
bash plugins/athena/tests/acceptance/run.sh
```

A full pass prints, as its last line:

```
ACCEPTANCE PASSED — 8/8 steps, the machine is as it was found.
```

## This is not `tests/live/run_all.sh`

`tests/live/run_all.sh` is the repo's own suite: it regenerates `desktop/plugin.js`
from its sources, runs every `t_*.py`, and owns checks that only mean something on a
machine holding the source tree. **Do not run it to check this** — it rewrites the
built panel (`run_all.sh:83-107`).

This harness is the opposite kind of test. It installs nothing of its own, edits
nothing, and never touches the source tree. It talks to a **running** Hermes backend
over HTTP and to the `hermes` CLI, in the order a stranger's machine would. It lives
in `tests/acceptance/` rather than `tests/live/` on purpose: the `inventory` check in
`run_all.sh:150-181` asserts every file under `tests/live/` either runs or declares why
it does not, and this script is neither a `t_*.py` nor part of that suite.

## What a run needs

| Requirement | Why | Where it comes from |
|---|---|---|
| A running Hermes backend | the plugin's routes are mounted by it, not by the engine | `README.md:89` — *the server logs `Hot-mounted plugin API routes: /api/plugins/athena/`* |
| `hermes` on `PATH` | steps 1 and 7 are CLI verbs | `hermes plugins --help` |
| Python + Node under `$HERMES_ROOT` | JSON is read with the venv python; Node is only located, never spawned by this script | `run_all.sh:29-43` (same discovery) |
| Network | step 3 clones a real repository | — |

The backend is **already running** when you start — the desktop app is one; headless
is another. The script finds it itself (see *Discovery* below) and says so:

```
   backend: http://127.0.0.1:62875 (role desktop-serve)
```

If it cannot find one, it stops at step 0 and tells you to start one:

```
FAIL step 0 — preflight (toolchain, backend, plugin reachable)
       no running Hermes backend answers /api/plugins/athena/status.
       Start one first: the desktop app, or 'hermes serve' (hermes serve --help).
```

### Discovery — how the backend's port and token are found

The backend binds an OS-assigned port, so it cannot be hardcoded. It publishes a
rendezvous record the moment it is up: `host-<role>.json` (`host`, `port`) and a
sibling `host-<role>.token`, in the cross-profile lock directory
(`gateway/host_rendezvous.py:162,216,224`). That directory is `$HERMES_GATEWAY_LOCK_DIR`
if set, else `$XDG_STATE_HOME/hermes/gateway-locks`, which defaults to
`~/.local/state/hermes/gateway-locks` (`gateway/status.py:319-335`). Requests carry the
token in `X-Hermes-Session-Token` (`hermes_cli/web_server.py`, `_SESSION_HEADER_NAME`);
without it the route answers `401`. The script tries the roles a server claims —
`desktop-serve`, then `serve` (`gateway/host_rendezvous.py:65,71`) — and uses the first
that answers `/api/plugins/athena/status` with `200`.

## The eight steps

Every command below was read out of the tree. The right-hand column names the source
the script cites in its own comments; nothing was guessed.

| # | Proves | Command | Source |
|---|---|---|---|
| 0 | toolchain, backend, plugin reachable | `GET /status` | `dashboard/plugin_api.py:321` |
| 1 | the plugin can be switched on | `hermes plugins enable athena` → `hermes plugins show athena` reports `Status: enabled` | `README.md:87`; `hermes plugins show --help` |
| 2 | the engine comes up and reports where | `POST /engine/start` | `dashboard/plugin_api.py:338-340`; payload `engine.status()` `:320-334` |
| 3 | a real repository becomes an installed, **verified** app | `POST /catalog/auto {url,name,force,wait}` then poll `GET /jobs/{job}` for `result.verified` | `dashboard/plugin_api.py:1434-1441`, `:1248-1253`; proven on `octocat/Spoon-Knife` by `tests/live/t_ladder.py:27,80` |
| 4 | the installed app actually serves | `POST /apps/{id}/launch` then `GET /apps/{id}/status` for `running`+`url`, then `curl $url` | `dashboard/plugin_api.py:7779`, `:716-721`; status `:994-1019` |
| 5 | the app can be stopped on demand | `POST /apps/{id}/stop`, asserted by `running` falling to `false` | `dashboard/plugin_api.py:7790-7796` → `dashboard/runtime.py:262` |
| 6 | the app's files are really removed, and the engine it needed is put away | `DELETE /apps/{id}` (a job; verdict is `result.removed`), then `POST /engine/stop` | `dashboard/plugin_api.py:3300`, `:3388-3395`; engine stop `:343` |
| 7 | the plugin can be switched off | `hermes plugins disable athena` → `Status: disabled` | `hermes_cli/plugins_cmd.py:753-767` |
| 8 | nothing was left running | engine port has no listener; no `node` process holds the engine dir; the app folder is gone | — |

Two things in that table are worth knowing before you read a run:

* **Step 3 uses `/catalog/auto`, not `/catalog/install`.** `octocat/Spoon-Knife` is a
  repository with *no entry point* — it is the static case the ladder exists to handle
  (`tests/live/t_ladder.py:5`). `/catalog/auto` clones, scaffolds a launcher, installs
  and smokes the result, so a static repo becomes a running app
  (`dashboard/plugin_api.py:1420-1431`). `/catalog/install` installs a repository that
  already ships a launcher, and would fail on this one.
* **Step 6 also stops the engine.** The engine outlives the routes it is started
  through, and `hermes plugins disable` only edits the allow-list — it does **not** stop
  it (`hermes_cli/plugins_cmd.py:753-767`). So the engine is stopped while the routes are
  still live, which is what makes step 8's port check meaningful.

## Expected output

A passing run, in order. The `>>>` lines are quoted from a real run on this machine;
the job lines are the shapes `dashboard/plugin_api.py` produces, with the app id
substituted.

```
== step 0 — preflight (toolchain, backend, plugin reachable)
   backend: http://127.0.0.1:62875 (role desktop-serve)
   ok    preflight: python=C:/Users/<you>/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe
   ok    preflight: node=C:/Users/<you>/AppData/Local/hermes/tools/node-22.21.1-win32-x64/node.exe
   ok    preflight: http://127.0.0.1:62875/api/plugins/athena/status -> 200

== step 1 — hermes plugins enable athena
>>> ✓ Plugin athena enabled. Takes effect on next session.      (or: Plugin 'athena' is already enabled.)
>>> ok    hermes plugins show athena -> Status: enabled

== step 2 — start the Athena engine
   ok    engine running on port 42001 (url http://127.0.0.1:42001)
   ok    athena home: C:\Users\<you>\AppData\Local\hermes\athena

== step 3 — install one known-good static app from the catalogue
   building https://github.com/octocat/Spoon-Knife as 'athena-acceptance-static' (this clones and can take a minute)
   job build-<id>
   ok    installed 'athena-acceptance-static' — job state=done verified=true proved=http://127.0.0.1:<port>

== step 4 — probe the app's URL until it answers
   ok    app answered: http://127.0.0.1:<port> -> HTTP 200

== step 5 — stop the app
   ok    app stopped (running=false)

== step 6 — uninstall the app
   ok    app 'athena-acceptance-static' uninstalled; engine stopped

== step 7 — disable the plugin
>>> ⊘ Plugin athena disabled. Takes effect on next session.
>>> ok    hermes plugins show athena -> Status: disabled

== step 8 — assert the machine is left clean
   ok    port 42001 has no listener
   ok    no node process holds the engine directory
   ok    athena home kept: C:\Users\<you>\AppData\Local\hermes\athena (the app folder C:\Users\<you>\AppData\Local\hermes\athena\api\athena-acceptance-static is gone)

ACCEPTANCE PASSED — 8/8 steps, the machine is as it was found.
```

Ports and job ids vary between runs; the words `enabled`, `disabled`, `verified=true`,
`running=false`, `removed`/no listener, and the final `ACCEPTANCE PASSED` do not.

### A failure looks like this

```
== step 4 — probe the app's URL until it answers
FAIL step 4 — probe the app's URL until it answers
       app 'athena-acceptance-static' reported no url after 90s (running=False)
```

The exit status is non-zero and the step name is in the message, so a run is diffable
against the template above without reading the whole transcript.

### What is left behind

The **Athena home** (step 2 prints it; `C:\Users\<you>\AppData\Local\hermes\athena` by
default) is **kept**. Uninstall removes the app's folder — `$ATHENA_HOME/api/<app>` — and
its logs and engine cache entries (`dashboard/plugin_api.py:3330-3395`), but the home
itself belongs to every installed app, so it stays. Step 8 says which of the two
happened, in one line.

## Running it elsewhere

**A second machine (Linux/macOS).** Nothing in the script is Windows-specific except the
node-process check, which branches on `uname` and falls back to `pgrep`; the port check
falls back from `ss` to `netstat`. The lock directory follows `$XDG_STATE_HOME`
(`gateway/status.py:319-335`), so it is found in the same place. Run it from the plugin:
`bash plugins/athena/tests/acceptance/run.sh`.

**A fresh Hermes profile.** Start a backend for that profile, then point the harness at
it with one knob — steps 1 and 7 run through `hermes -p <profile>` for you:

```bash
hermes -p trial serve --port 9119 --skip-build      # in one terminal
ATHENA_ACCEPT_PROFILE=trial bash plugins/athena/tests/acceptance/run.sh
```

(`hermes -p <profile> <cmd>` — *Run any command against a named profile's home* — is
from `hermes --help`.) A profile that has never seen Athena exercises the two things this
harness is really about: that `enable` is enough to make the plugin usable, and that
`disable` plus the teardown leaves no engine, no listener and no orphan Node behind.

**Knobs** (all optional):

| Variable | Default | Effect |
|---|---|---|
| `ATHENA_ACCEPT_APP` | `athena-acceptance-static` | the app name to install |
| `ATHENA_ACCEPT_REPO` | `https://github.com/octocat/Spoon-Knife` | the repository to build |
| `ATHENA_ACCEPT_BUILD_TIMEOUT` | `420` | seconds to wait for the build job |
| `ATHENA_ACCEPT_PROFILE` | (active profile) | run steps 1 and 7 as `hermes -p <profile>` |
| `ATHENA_PY`, `ATHENA_NODE` | discovered under `$HERMES_ROOT` | interpreter / node |
| `HERMES_GATEWAY_LOCK_DIR` | `$XDG_STATE_HOME/hermes/gateway-locks` | where the rendezvous records are |

## What this harness does not prove

It runs **one** app, chosen for being small and static. It does not exercise a
multi-gigabyte install, the store's own catalogue resolution, app scheduling, or the
agent tool surface — those are `tests/live/`'s job (`t_store.py`, `t_ladder.py`,
`t_schedule.py`, `t_agent_tools.py`). It proves the seam a user crosses first: that a
catalogue app can be installed, run, reached, stopped and removed on a real machine, and
that turning Athena off again puts the machine back.
