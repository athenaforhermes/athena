# Changelog

Athena is a Hermes plugin, versioned with it. This file begins at 1.0.0 — the
first release with a public home. Everything before it was built and verified on
one machine and shipped nowhere.

## 1.0.1

Three defects found before launch, and one sentence of launch copy that could not
be supported.

- **The idle reaper no longer stops an app that is working.** Idleness is now
  "no work in flight" — a call, an in-flight hold, a running Athena job, or a
  live connection — and the window only runs when all of them are gone. A hold
  is taken from *before* an app is brought up, so booting a large model (which
  can take minutes and answers on no port while it happens) is no longer
  mistaken for an unused app. An unused app is still stopped, so the VRAM it was
  holding comes back.
- **A store scan survives an app folder that vanishes mid-scan.** A user
  uninstalling an app while the panel refreshes listed and read the same folder,
  and the read raised `FileNotFoundError`. `publish_store.apps_in`, `app_files`
  and `describe` now skip an entry that disappeared between listing it and
  reading it, and a new test reproduces the race deterministically.
- **The launch copy no longer claims an unverified catalogue size.** "a
  catalogue of thousands of AI apps" is now "a constantly growing catalogue of
  apps", in the manifest, the catalogue entry and the README.

The version is bumped because the product description and the claim surface
changed, not because the API did.

## 1.0.0

The local AI app store for Hermes. Browse a catalogue of thousands of AI apps,
install one from any git repository, launch it, read its logs, stop it — and let
an agent drive every one of those through 24 `athena_*` tools. No API key and no
account of its own: it reuses the credentials Hermes already holds.

### What 1.0 is

- **A store front end of its own.** The upstream catalogue is a data source;
  every card, list and button is drawn here, and no upstream branding appears.
- **A panel and a CLI and an agent, three doors into one engine.** The engine
  boots headless and serves HTTP on loopback; the panel only renders and calls.
- **A file browser and a system monitor, ported in.** Both were standalone
  plugins; both now live inside Athena's own panel as tabs, and their donor
  halves are folded in at build time (see `docs/merge-ui.md`).
- **Machines you can watch and use.** A QEMU/WSL guest's screen is in the panel,
  read from the guest's own VNC server, with a **control lease** (`dashboard/lease.py`)
  so a person holding the keyboard makes an agent's keystroke refuse with
  `human_has_control` rather than racing it.
- **An app the agent built is a run, not a job.** What the agent ran here is a
  record (`Runs`), separate from the terminal it ran in.
- **The promises the plugin makes about itself are tests.** `t_release.py`
  re-derives the version from every file that carries one, the tool surface three
  ways, the dependency windows and the vendored-licence inventory.

### How it got here

The 1.0 work was a ten-rung ladder, each rung verified by the suite before the
next began. In order: release invariants (R1); upgrade proof (R4); threat model
and refusal tests (R6); a performance budget with numbers instead of impressions
(R7); the UI finish a stranger's theme finds — theme tokens and a failure surface
that says something a person can read (R8); and the split of both monoliths (R9:
`dashboard/plugin_api.py` 9,400 → 8,291 lines, by domain — credentials, engine,
settings, node runtime, the capability runner — with the suite's check count
identical before and after each move).

### Explicitly not in 1.0

- **A sandbox.** Apps run as the user. Stated, not mitigated.
- **macOS.** Windows and Linux.
- **Other languages.** English-only, with the dictionary structure ready.
- **The `pinokio` → `athena` rename.** The shim stays, documented.
- **Telemetry of any kind.**

### Not yet proven at the time of this entry

Two rows of the definition-of-done table in `docs/release.md` were still open
when the version was set: the **store rehearsal** (publish → install from the real
store → unpublish, against a live store and token) and the **physical split of the
desktop panel** (the source of truth is still one file; the fold contract is
written but the parts are not yet cut). Everything else the table names has a
passing artefact.
