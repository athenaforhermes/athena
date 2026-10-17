# Naming — what Athena calls its own things

One table, so no task invents a second answer. The rule behind it: **Athena names
what Athena owns.** The vendored engine keeps its own internals (never forked),
so a legacy file the engine writes or reads is converted, shimmed, or whitelisted
— never renamed out from under it.

## The names

| Legacy | Athena | Status |
| --- | --- | --- |
| `pinokio.js` | `athena.js` | The launcher. A launcher Athena *writes* is `athena.js`, with a one-line legacy-named shim (`module.exports = require("./athena.js")`) beside it, because the engine resolves the legacy name literally. A launcher that belongs to the *project* (an imported app's own file) is never moved or rewritten — it stays as upstream wrote it, and Athena reads it through the compat reader. Upstream updates keep applying; nothing diverges. |
| `pinokio/pinokio.js` | `athena/athena.js` | The nested legacy shape the engine also probes. Same treatment. |
| `pinokio_meta.json` | `athena_meta.json` | Written by the engine on install. Athena reads either; the engine's own write is whitelisted. |
| `pinokio.json` | `athena.json` | Engine-written title/description. Same. |
| `PINOKIO_HOME` | `ATHENA_HOME` | Athena exports its own name to the engine child and drops the legacy ones, so a stale shell value cannot steer it. An app's own scripts are run *by the engine*, which exports its own names — so a converted script reads `process.env.ATHENA_HOME ?? process.env.PINOKIO_HOME`. |
| `PINOKIO_SCRIPT_AUTOLAUNCH` | `ATHENA_SCRIPT_AUTOLAUNCH` | Same. |
| `PINOKIO_SCRIPT_REQUIRES` | `ATHENA_SCRIPT_REQUIRES` | Same. |
| `pinokio_configs` | `athena_configs` | Shared-store config dir. |
| `pinokiod` (service, as displayed) | `athenad` | Display only. The npm package stays as published. |

Already Athena's, no change: the home (`<hermes>/athena`), the ports
(42001/42002), the store, the UI, the agent tools.

## Allowed exceptions

A guard test (`cache/scratch/t_names.py`) fails when the legacy name appears on a
visible surface. These are the only surfaces it may appear on:

1. **The MIT `LICENSE` and copyright headers** in the vendored engine. Keeping
   them is a condition of the license.
2. **Upstream repository URLs** in the store index (`github.com/pinokiofactory/…`,
   `deadjoe/yue2-groove-pinokio`). Install source and provenance.
3. **`README.upstream.md`** in store branches — a third-party project's own docs.
4. **The compat reader's own constants** and the engine's bundled UI (never
   rendered, never scanned).
5. **`docs/SPIKE-VERDICT.md`** — a historical record, marked as such.
6. **The import label**: "Import from a Pinokio install" names the source system
   the way "Import from ComfyUI" would — it is a feature name, not Athena's brand.
   The same goes for the four lines of copy in that panel (looking for / found /
   none detected).
7. **Source comments** that describe the engine's real behaviour (its home
   directory, its env vars). They document the vendored code, they are not
   rendered anywhere, and a comment that misnames a path is worse than one that
   names it correctly.
8. **The shim's own filename** in the agent's file list and in a doc's file tree.
   The agent has to know the file exists so it does not delete it.

## The shim, and when it can go

The one legacy-named file Athena writes is beside a launcher Athena wrote, and it
is the engine's filename, not a leftover of the rename. Measured, not assumed:
the engine resolves the launcher through its own literal (`kernel/api/index.js`,
`server/index.js`, `server/autolaunch.js` at the pinned version) and lists apps by
that filename. There is no constant and no config knob, so **dropping the shim is
not a rename — it is an engine patch**, which is the Option B lever
(`2026-10-13_124002-athena-native-naming.md`, Task 4.1) and was declined: the
engine stays stock so an upstream bump keeps applying.

The consequence, stated plainly: a repo-built app's folder carries `pinokio.js`.
It is not a public surface (nothing the user or the store reads names it except
exception 8 above, which exists so the agent does not delete it), and the guard
test `t_names.py` re-checks the reason every run rather than trusting this
paragraph — that the engine still names the legacy literal, and whether it has
started reading `athena.js`. The day it does, that run prints:

    ACTION: <files> now read athena.js — the shim can be dropped

**That day, in one move:** stop calling `naming.write_shim()` from `buildapp`'s
migrate and scaffold paths, delete the shim from apps that carry it, and drop
exception 8 here plus the shim clauses in `t_names.py` and `t_publish.py`. Until
then, deleting it makes a repo-built app invisible to the engine (no app in the
list) and unlaunchable — the failure looks like "the app vanished", not like a
missing file, which is why this is written down instead of discovered.

An app whose launcher belongs to the project keeps upstream's own file and gets no
shim at all — the compat reader reads it where it lies, so an imported app's
folder is exactly as upstream published it.

## Re-applying after an engine update

The engine is vendored, never patched (Option A). Bumping the engine is: install
the tarball → run `t_names.py`, `t_ladder.py`, `t_smoke.py`, `t_store.py`,
`t_build.py`, `t_agent_tools.py`. If `t_names.py` complains about a new engine
surface, the fix belongs at the boundary (the scrub pass), not in the engine.

Then read the top of that run for the shim line above: an engine that reads
`athena.js` is the one bump that lets Athena delete a legacy name from its own
apps, and it is worth taking in the same pass — with the shim's clauses removed
from the tests in the same commit, or they will guard a file that no longer
exists.
