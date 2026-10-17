# Athena's test suite

This is where the suite lives. It ran from `%LOCALAPPDATA%/hermes/cache/scratch/`
until 2026-10-13, when Hermes pruned that directory after 24 idle hours and took
`t_appcall.py`, `t_ui_routes.py`, `mk_ui_test.py` and `t_theme.py` with it. The
scripts now sit here, beside the plugin they test, because a directory that gets
pruned is not a home for the only copy.

Run it from the plugin directory:

    bash tests/live/run_all.sh

Nothing needs copying anywhere first: the tests resolve the plugin by absolute
path and rewrite any generated file they own.

`uitest/` is the headless UI harness. `driver.mjs` mounts the real merged plugin
with the plugin-sdk atoms and a stub DOM, and `mk_ui_test.py` refreshes the copy
it imports — it is a copy rather than a link, so refreshing it is mandatory or
the suite tests an old merge. `uitest/node_modules` holds the harness's own
dependencies and ships with the suite.

**What the harness cannot see.** `useState` returns a no-op setter, `useMemo`
ignores its deps, effect cleanup never runs, and components are called as plain
functions. So it proves which functions exist and what they return, and it cannot
catch state-update bugs, effect-cleanup leaks, conditional or looped hooks, CSS or
layout, or anything that needs a real DOM event. A green run means the panel
mounts and answers; it does not mean the panel looks right. That boundary is
stated here because a green suite gets read as "the UI works", and it only says
"the UI is shaped like this".

Every file in this directory either runs inside `run_all.sh` or is named there as
not-a-test with the reason why — a manual probe that needs a machine, a fixture, a
generated artefact. `run_all.sh` fails if a file appears that is neither, because
that is how `machine_provision_check.py` came to be documented in the README and
reachable from nowhere.

