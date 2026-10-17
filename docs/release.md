# Definition of done

What "1.0" means for Athena, as a table where every row points at an artefact —
a test, a log, or a document — rather than a feeling.

Run the whole suite with:

```bash
bash tests/live/run_all.sh
```

It finds its own toolchain, and `ATHENA_PY` / `ATHENA_NODE` /
`ATHENA_HERMES_ROOT` override it (the documented path for CI on a machine that
has never seen Hermes). `SUITE GREEN` is the gate; a `skip` is not a pass, and
the live probes say `SKIPPED` in the word itself when this machine is not in a
state where they can prove anything.

## The table

| Gate | Proof | Status |
| --- | --- | --- |
| Release invariants | `tests/live/t_release.py` (32 checks) | **passes** |
| Upgrade from 0.53 | `tests/live/t_upgrade.py` (13 checks) | **passes** |
| Refusals tested | `tests/live/t_security.py`, `docs/threat-model.md` | **passes** |
| Performance targets met | `tests/live/t_perf.py` → `docs/performance.md` | **passes** |
| Theme + failure surfaces | `tests/live/t_ui_theme.py`, `tests/live/t_ui_js.py` | **passes** |
| No monolith | `tests/live/t_release.py` size tripwire | **passes** |
| Acceptance, end to end, against the real install | `tests/acceptance/run.sh` (8 steps) | **passes** |
| Fresh-profile acceptance (never-seen profile) | three acceptance logs | **open** — see below |
| Suite green on Windows *and* Linux | CI logs | **open** — see below |
| Catalogue install | store rehearsal log | **open** — see below |

`tests/acceptance/run.sh` is the row above the open ones and it is not a fixture:
it enables the plugin through `hermes_cli`, starts the engine, builds and installs a
real catalogue app over the plugin's own HTTP API, proves its URL answers, stops it,
uninstalls it, stops the engine, disables the plugin, and then asserts the machine is
as it was found — no listener on the engine port, no node process holding the engine
directory, and the app's folder actually gone. It restores the plugin's own enable
state on the way out, including after a failure partway through, because a harness
that turned Athena off and walked away would be the bug it exists to catch.

What it is **not** is a fresh profile: it runs against the profile that is already
here, so it proves the path works end to end but not that it works on a machine
that has never seen Athena. That is the row below, and it still needs the store.

## The three open rows, and why

These are not failures. They are the rows that need something this machine cannot
provide by itself, and they are named here so they stop being implied questions.

- **Fresh-profile acceptance** — install into a profile that has never seen
  Athena and run an acceptance script end to end. Needs the store in the row
  below to be live, so it is blocked the same way. It also needs the harness to
  learn something it does not know yet: `ATHENA_ACCEPT_PROFILE` only redirects
  the `hermes plugins enable/disable` assertions, while the run itself
  **discovers a running backend from the gateway locks** and drives
  `/api/plugins/athena` there — and that backend serves the *active* profile.
  Tried on 2026-10-16 by copying the plugin into a fresh profile, which does
  give it a fresh home (`settings_home()` is `plugin_dir.parent.parent /
  "athena"`, so a copy under `profiles/<name>/plugins/` is self-contained): the
  run passed 8/8 but reported the **shared** home, which is how the gap surfaced.
  Closing this row therefore needs an explicit backend-URL override *and* a
  backend running under the fresh profile — not just the knob that exists.
- **Suite green on Windows and Linux** — this suite runs on Windows today. The
  Linux run is a CI job (the repo and the workflow are a separate rung); the
  suite is written for it — `ATHENA_HERMES_ROOT`/`ATHENA_PY`/`ATHENA_NODE` are
  the documented overrides, and the Windows-only branches refuse with a reason
  where a feature cannot exist on Linux.
- **Catalogue install** — publish to the real store, install *from the store*
  into a fresh profile, unpublish and re-publish. The publish path is already
  proven against a **local bare repo** harness (`tests/live/t_publish.py`), which
  stays as the regression test; the rehearsal moves the same contract to a real
  one, and needs a live store and a token.

## What is stable, in one paragraph

The engine, the panel, the CLI and the agent surface are frozen as of 1.0.1. The
public surface is the 24 `athena_*` tools and the plugin's own routes; the module
layout underneath them may move (that is what the splits were for) without
changing a single caller. Settings written by older versions are still read, key
by key. The catalogue is treated strictly as a data source.
