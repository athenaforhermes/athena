# Changelog

Athena is a Hermes plugin, versioned with it. This file begins at 1.0.0 — the
first release with a public home. Everything before it was built and verified on
one machine and shipped nowhere.

## 1.0.1

Four defects found before launch, and one sentence of launch copy that could not
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
- **The declared Hermes window no longer expires.** `requires_hermes` was
  `">=0.21.0,<0.23.0"`, and a ceiling there is a
  self-destruct rather than caution: Hermes refuses a catalogue entry whose window
  excludes the version running here (`athena needs Hermes >=0.21.0,<0.23.0; this is
  0.23.0.`), and the loader's re-gate treats that mismatch as never-importable.
  Hermes reached 0.23.0, so installing Athena by name was refused — and from the
  catalogue that is the only way in. The floor stays and the ceiling is gone, which
  is what the catalogue itself does: 749 of its 753 entries declare a floor and
  nothing above it. A floor cannot expire, so this line never needs maintaining
  again. (Rule 10's floor-plus-ceiling advice is about `python_dependencies` —
  those remain bounded.)

The version is bumped because the product description and the claim surface
changed, not because the API did.

### Review pass — the ten things a plugin-store security review asked for

- **Credential route.** One function reads a token, from Hermes' own secret store; it is handed
  to an app as an environment variable for the life of that app's process, never logged and never
  written to disk in plaintext. Nothing reads another program's token store.
- **Environment allowlist.** The engine is spawned with the variables it needs, not with
  everything the user's shell happens to export.
- **Approval prompts.** Every tool that runs, writes, deletes or pushes asks through Hermes'
  approval hook first.
- **Write confinement.** A write outside Athena's home and the folders a person has named is
  refused, not created.
- **WSL.** The gate in front of a guest is tightened: a distribution Athena owns is hardened
  before anything runs in it, and a command that fails hardening is refused rather than run
  anyway; the flag meaning "a person said yes to *this* command" is not something the tool
  surface sets for itself.
- **A scheduled run is not a public route.** The script Hermes' own cron runs imports the
  plugin's code and calls the same function a hand-started run calls, so nothing that can reach
  a port can start one.
- **The engine is provisioned, not vendored.** Setup installs it from npm at the pinned version,
  from the committed lockfile, behind a consent step that names the version.
- **The file picker is the SDK's.** The panel asks the host through `ctx.os.pickOpenPath` instead
  of reaching into a window global that may not exist.
- **Publishing asks.** A push is confirmed before it happens and goes only to the repository the
  person named.
- **The description matches the plugin.** The README now says that third-party install scripts run
  with the user's privileges, that a background engine runs, which endpoints are contacted, that
  WSL and QEMU guests accept command execution, and that publishing exists — and it no longer
  claims a credential is reused in the way the review could not support.

### Review pass — the second review's four items

- **Three more children get the allowlist instead of this process's environment.** The first
  review's fix covered the engine and an app's own git; three other spawns still inherited
  everything Athena holds, because a `subprocess.run` without `env=` does that by default.
  They are the `menu.js` spawn — which `require()`s the app's own launcher, so the app's code
  ran inside the child — the app's virtualenv interpreter, whose `.pth` files execute before
  `-c` runs a line, and the HuggingFace download child, which built its environment by
  *subtracting* two names from `os.environ` and so leaked any key named after them. All three
  build from the allowlist now, which fails closed.
- **`git` is no longer run inside somebody else's checkout with this environment.** Deciding
  whether a file is the app's or the app's download asks a repository Athena did not write,
  and git reads that repository's own `.git/config` before it answers — where `core.fsmonitor`
  names a program to run. It gets the allowlist, and if the allowlist cannot be reached it is
  not run at all.
- **Starting an app is the question, whichever tool starts it.** The prompt was attached to
  `athena_launch`, and `athena_generate` and `athena_capabilities` reach the same start through
  `_ensure_app` — so an agent could put third-party code on the machine by asking what an app
  can do. The question is asked where the app is actually started, under the rule key
  `athena_launch` already uses: one standing answer covers both doors, and an app that is
  already running is not asked about again.
- **A GitHub token goes to GitHub, and to nobody who spells it that way.** The token was
  attached to any URL containing the word `github` — `https://github.evil.example/o/r.git` and
  `https://example.com/github.com/o/r.git` both satisfied it. The URL is parsed and the host
  named, in one function both call sites use.
- **The file that says where a write may go is not somewhere a write may go.** `write_roots` is
  read from `settings.json`, and that file sat inside Athena's own write root — so a write could
  widen the fence and then use it. It is refused by name.

Each of them has a test that fails the day it comes back. The class is checked too, not just
the sites: `tests/live/t_child_env.py` counts every spawn in the plugin's Python that passes no
`env=` against a table of the ones that do it deliberately, counts the spellings that would get
around such a check (an import alias, a name imported from `subprocess`), and fails on a new one
wherever it lands. The pull fixture was adapted rather than exempted: its stand-in CLI is
configured by environment variables, so `t_pull.py` hands those back at the one place the child
environment is built — production still passes no such thing, and the allowlist itself is
asserted in `t_child_env.py`. The same guard the installer already had was missing in one more
place — the store checkout ran `git clone` with the URL unseparated — so it is there now too, and
a test asks for it by argument, not by reading the source.

### Found by using it — a white pane where an app was already running

- **An app that refuses to be framed now says so.** Framing is a rule about the
  page that owns the `<iframe>`, never about the app inside it, and the desktop
  owns its frame from a `file://` document — an origin that no `http://` source can
  match. Maestro allows only this machine's loopback addresses and the launcher
  that started it, so it painted an empty rectangle while running perfectly and
  logging `Application startup complete`. The backend now reads the app's own
  `frame-ancestors` before the frame is drawn, and the panel shows the app's name,
  the reason and a way out instead of a white void. The answer is remembered and
  expires, because a poll every two and a half seconds must not ask an app that is
  busy generating.
- **The Browser button now opens the browser.** It tested
  `ctx.host.openExternal`, and the pane bridge has no such member: the call fell
  through to `window.open`, which the desktop denies. The button did nothing at
  all, and the only trace was `[window-open] denied` in the desktop log. It and the
  Open-repository link now go through `ctx.os.openExternal`, the SDK's own door —
  which is also the way around a refused frame, since a top-level document has no
  ancestor to check.

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
