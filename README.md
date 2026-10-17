# Athena — the local AI app store for Hermes

Athena browses a constantly growing catalogue of apps (image, video, audio, LLM, agent projects)
and installs them **natively inside Hermes Desktop**. It is a single unified Hermes plugin: one
package, one engine, one credential set.

No second window to drive. No second login. If Hermes already holds a GitHub or HuggingFace
credential, Athena uses it.

## What you get

- **Local AI apps, installed and run from inside Hermes.** Browse a catalogue of ~6,400 apps,
  install one, launch it, watch its log, stop it. Nothing needs an account and Athena has no API
  keys of its own; an app that wants one is labelled *needs a key* before you launch it, and it
  reuses the credential Hermes already holds when it can.
- **The apps you already have come with you.** Point it at a folder of apps installed elsewhere and
  they appear as cards — **linked, not copied, by default**, so the originals stay where they are.
  See `Import from an existing install` in Settings.
- **Everything is one click.** Install from any git repository, launch it, read its log, stop
  it — the whole loop without a terminal, and a stopped app's own log names the cause.
- **Agent control is in active development.** This release is the features above, done well;
  driving all of it from a single sentence is the next one.
- **Make your own.** A git repository becomes an app you can run from the panel:
  `docs/build-an-app.md`.

## Install

Athena is a Hermes plugin, so installing it is Hermes' own job:

```bash
hermes plugins install <this repository's URL>     # then restart Hermes Desktop
```

Until it is published, the equivalent is a copy: put the `athena` folder in
`<hermes>/plugins/` and restart. Either way it arrives complete — the vendored engine is
committed with it, `node_modules` included, so nothing is fetched at install time and
there is no post-install build step that can fail behind a proxy.

Then open Hermes Desktop and switch to the Athena panel. Nothing runs until you press
something: the engine starts the first time you launch an app, and stops when nothing
needs it.

## What it needs

| | |
| --- | --- |
| **Hermes** | 0.21.0 through 0.22.x — declared in `plugin.yaml` as `requires_hermes: ">=0.21.0,<0.23.0"`, and the suite fails the day the Hermes on this machine steps outside that window |
| **Node** | 22–24, which Hermes already downloads for its own tooling. Athena finds it on Windows, Linux and macOS (`dashboard/plugin_api.py::_find_node`); `ATHENA_NODE` points it at your own build if you have one. Versions 25+ are refused on purpose: the engine's native addons (`node-pty`, `better-sqlite3`) ship prebuilt binaries only up to Node 24, and Node 26 fails at install *and* at boot |
| **Python** | Hermes' own interpreter. Four packages are declared — `send2trash`, `psutil`, `Pillow`, `numpy` |
| **Disk** | ~200 MB for the plugin, most of it the vendored engine. Installed apps and model weights are the size they are |
| **GPU** | none needed to browse, install, or run the panel. An app that generates needs whatever that app needs — `athena_app_health` will say so before you launch it |
| **OS** | Windows 11 and Linux, both exercised end to end. macOS should work — the Node search covers `~/Library/Application Support/hermes/tools` — but nobody has run it there yet, and this table says so rather than implying otherwise |

## When something goes wrong

Six failures, in the words the panel uses, because that is the string people paste into a
search. Every one of these is a five-second read once you know which failure you are holding.

**`Start: No compatible Node runtime found. Athena needs Node 22.x`**
Node 22–24 is a hard requirement — the engine's native addons ship prebuilt binaries only up
to Node 24, and Node 26 fails at install *and* at boot. Athena looks for one in this order:
`ATHENA_NODE`, `HERMES_TOOLS`/`ATHENA_NODE_DIR`, the Hermes tools folder for your platform,
then `PATH`. Install Node 22 LTS in any of those places, or point `ATHENA_NODE` at a build you
already have. The message names what it found, so a Node 26 on `PATH` is visible rather than
mysterious.

**`Start: Engine did not answer on http://127.0.0.1:42001 within 60s`**
Usually an orphan: a previous engine that outlived a hard kill and still holds the port. Press
**Restart** — it reaps the port by pid (`lsof` on Linux, `ss` where `lsof` is absent, `netstat`
on Windows) before starting again. If it still refuses, the port is held by something that is
not an Athena engine, and it is worth checking what:

```bash
# Windows
netstat -ano | findstr :42001
# Linux
ss -lptn 'sport = :42001'
```

**The panel draws, but every call answers `404 {"detail":"Plugin not found"}`**
The plugin is **disabled** — and the desktop half still drawing is what makes this look like a
UI bug instead of a plugin that is off. Hermes disables a plugin whose declared dependencies
cannot be resolved against its own pins. Ask, don't guess:

```bash
hermes plugins show athena     # Status: disabled | enabled
hermes plugins enable athena   # then the routes hot-mount on the next request
```

No restart is needed: the server logs `Hot-mounted plugin API routes: /api/plugins/athena/`.

**An app installs but will not launch**
The card already says why, in its chip: `no launcher` (the app has no `start.js`), `needs 1 key`
(the app declares a credential nobody has entered — see `## Credentials`), or `nothing read
yet` (nobody has read its scripts, so Athena will not guess at its inputs). **Read its scripts**
in the app's `⋯` menu tells you what it wants; `athena_logs` reads an app that has *stopped*
and names the reason it stopped instead of handing you a wall of output.

**Where did the output go**
An app's own log lives in `<athena home>/logs/shell/api/<app>` while it is running, and is
moved to `logs/shell/cleaned/api/<app>` when the engine tidies up; many apps also write their
own `logs/` inside their folder. `<athena home>` is `<hermes>/athena` unless `ATHENA_HOME`
says otherwise — see `## Where everything lives` for the full map.

**The Store is empty or will not refresh**
The catalogue is fetched once a day and cached; with no network, the cached copy is what you
get. `athena_settings` reports the source it is using, and `athena_search` searches the cache
rather than the network, so search keeps working offline. If the list is empty on a first run
there is no cache yet, and that is the one case that needs a connection.

## What it does on your machine

Written down because a plugin that installs and runs other people's code should say
what it touches.

**In English, for now, and this is the statement of it:** the panel's words are English. The
dictionaries exist and are kept honest by `tests/live/t_i18n.py`, so a translation is a table of
strings rather than a rewrite — what is missing is one, and the README saying so is the honest
alternative to a language picker that renders English in every language. `docs/accessibility.md`
lists the keyboard behaviour that is asserted and the handful a person has to check by hand.

**The one-line version:** an installed app runs as you and is not sandboxed; the rules below are
the places where Athena constrains *itself*, and the rest is named as an accepted weakness rather
than left implied. The whole account — what an app can reach, where credentials live, what the
lease actually is, and the weaknesses this does not defend — is in
**[`docs/threat-model.md`](docs/threat-model.md)**, and what Athena is made of is in
**[`docs/supply-chain.md`](docs/supply-chain.md)**. `tests/live/t_security.py` (63 checks) is
is where the refusals below are tested, including the ones a string-prefix check would pass.
The panel's own failure paths are checked the same way: `tests/live/t_ui_js.py` (8 checks)
requires every call that *changes* something to read the answer before believing it, and then
*executes* the function that does the reading against five shapes of answer — a refusal with a
reason, with a detail instead, with no words at all, a success, a `null`. It found nine mutating
call sites that treated a refusal as success. `docs/accessibility.md` is the keyboard account.

The panel's own colours are checked the same way: `tests/live/t_ui_theme.py` (10 checks) allows a
literal only when it is a *status* colour — green is healthy, red is broken, amber is careful, and
a theme that re-tinted those would be lying about a state — and otherwise requires the app's own
variables, with one retired name allowed only as a fallback. It found that 45 uses of twelve names
behind the retired standalone plugins' theme resolved to **nothing** at all, which is why the
memory meter's fill had no colour.

**An agent at work is visible.** Hermes Desktop draws a travelling arc around a sidebar session while
that session is working, and the panel wears the same arc on the card an agent is using — the app, the
store entry, or the machine a tool call is about — so you can see *what* is being touched and not just
that something is. It is Hermes' own animation, not a lookalike: the same keyframes, retargeted to this
panel's theme variables, drawn while a call is in flight and for a moment after it lands. Nothing moves
when nobody is working, and the ring is motionless (not absent) for a reader who has asked for reduced
motion. If the agent touches something whose tab is not the one you are looking at, the panel follows it
there — the same way it already follows a machine's console — because an arc on an unmounted card is no
arc at all.

- **One port, on loopback.** The engine binds `127.0.0.1:42001` (change the port in
  Settings). Nothing binds a public interface, and there is no inbound service of
  Athena's own.
- **Your files, read widely and written narrowly.** The file browser reads Athena's
  home (`<hermes>/athena`, which holds `api/`, `bin/`, `cache/`, `settings.json`) **and
  every fixed disk**, because picking a file to hand an app is something you can already
  do in Explorer. It *writes* only inside Athena's home and your own user folder: a
  browser a model can drive should not be able to delete through `C:/Windows`, and reads
  are what "browse" means. **Credential stores are refused wherever they are** — `~/.ssh`,
  `~/.aws`, `~/.gnupg`, browser password stores (`Login Data`, `Cookies`, `Local State`),
  the Windows hives, and Hermes' own `sessions/`, `memories/` and `config.yaml`
  (`dashboard/files.py::_deny_secret`), by design, not by accident. A refusal names what
  is allowed, so it can be acted on rather than retried.
- **Your credentials, if you have them.** Cloning a private repo or pulling weights
  uses the GitHub/HuggingFace credentials Hermes already holds. Athena does not copy
  them, does not store them, and does not send them anywhere else.
- **Network calls:** GitHub (`api.github.com`, `raw.githubusercontent.com`,
  `github.com`) for the catalogue and for clones; the upstream catalogue host
  (`https://api.pinokio.co`) as the catalogue's source; `huggingface.co` when an app pulls
  weights. Then the installed app's own traffic, which is the app's business, not
  Athena's. **No telemetry, no analytics, and no update check of its own** — the text
  you are reading is the whole list.
- **One child process.** A Node process for the engine, started and stopped by
  Athena. Stop kills the process tree, not just the top of it.
- **Reading, not calling, the things an app asks for.** When Athena says an app
  "needs 1 key", it read that from the app's own files. It never calls the service.
- **Importing reaches outside its home, and says so.** Importing an existing install in **link**
  mode creates a junction onto the original, which stays exactly where it is; **copy** mode copies
  it in and repoints the copy's virtualenvs at Athena. Either way the imported app's model-cache
  settings are rewritten to the shared store, because a folder-private cache is why two apps each
  download the same 8 GB — and in link mode that edit lands in **the original folder**, which is
  someone else's directory, so the job log names the files it changed. One setting
  (`share_caches`) turns it off.

## Uninstall

```bash
hermes plugins remove athena
```

That removes the plugin. It deliberately leaves `<hermes>/athena` in place: that folder
holds the apps you installed and any weights they pulled, which are yours rather than
the plugin's. Delete it to reclaim the space — `athena_space` shows what is in it
before you do.

## Testing

Every claim in this file about the suite's shape — how many `athena_*` tools are
registered, how many checks each test file makes, how many packages the vendored
engine contains and which of them have no licence — is re-derived by
`tests/live/run_all.sh` from the tree and from the run it just made, and the panel
it ships is checked against the sources it was generated from. Prose that no
script can read is not checked, and is meant to be read as prose. The script:
the Python probes, a headless render of the desktop half, the engine's own routes, and
`hermes plugins validate` as a gate. It finds its own interpreter and Node, so it runs
from a checkout rather than only on the machine it was written on — `ATHENA_PY`,
`ATHENA_NODE` and `ATHENA_HERMES_ROOT` override what it discovers.

Two of those gates do not need a running Hermes, because they are about the repository
rather than the loaded plugin: `t_release.py` makes 32 checks — the version in every file
that carries one, the tool surface in all three places it is written down, the release
files, and the Hermes window `requires_hermes` claims — and the third-party inventory is
checked against a fresh walk of `engine/node_modules`, including the claim that every
licence in the tree is permissive. `t_upgrade.py` makes 13 checks about the settings file a
person brings with them when they upgrade — the old location still being read, the new one
winning key by key, and a save that carries every key the old file held instead of
replacing them. `t_lease.py` makes 65 more about a guest's one keyboard — who is driving it,
what an agent is refused while a person is, and that a refused keystroke never reaches the
guest at all. `t_security.py` makes 63 about the trust boundary: the file browser's scope, the
credential-store rule underneath it, the app-id-is-a-name rule with a delete on the end of it,
a token that must be findable by name and unfindable by value, and a push that must not write
its own credential down — with the *non*-refusals checked too, because a guard that refuses an
app's own `.env` is a broken feature rather than a strict one.

## Why it exists

Hermes already has the agent, the tools, and the credentials. What it lacked was a place to
*run* the rest of the local AI ecosystem. Athena embeds a proven MIT-licensed app runtime
([upstream here](https://www.npmjs.com/package/pinokiod)) in `engine/`, vendored unmodified, and
puts Hermes' own UI, naming and agent interface on top of it.

## Architecture

Athena is **its own front end over a shared runtime**:

- **Own UI.** `desktop/plugin.js` draws every card, list, and button. Nothing upstream is
  embedded, framed, or re-skinned, and no upstream branding appears anywhere in the panel.
- **Catalogue as a data source.** `dashboard/catalog.py` reads the public store index and
  serves clean records (name, repo, version, icon, description). Athena renders them.
- **Shared runtime.** The vendored engine does the actual installing and running;
  Apps install as ordinary git clones, so upstream fixes to the runtime benefit Athena
  automatically.
- **Bring your own app.** Any git repository can become an Athena app from the Store tab.

## Layout

| Path | Role |
| --- | --- |
| `plugin.yaml` | Plugin manifest; declares the agent tools, the desktop half, and the bundled previews. |
| `__init__.py` | Agent-tool layer. Registers Athena's tools and loads the backend so both halves share one engine. |
| `dashboard/manifest.json` | UI manifest for the Hermes desktop. |
| `dashboard/plugin_api.py` | FastAPI router + `EngineManager` — supervises the engine child process. |
| `dashboard/catalog.py` | The catalogue client: index, search, and repo resolution. |
| `desktop/plugin.js` | The panel, tabs, and status chip rendered in Hermes Desktop. |
| `engine/` | Vendored runtime plus `boot.js`, the headless launcher. |
| `docs/SPIKE-VERDICT.md` | Phase 0 findings that this design rests on. |
| `tools/` | Standalone helpers — the headless browser stand-ins, the store publisher. |
| `tests/live/` | The probe suite `run_all.sh` drives; includes the release gates. |
| `LICENSE`, `NOTICE`, `THIRD-PARTY.md` | Athena is MIT; the vendored engine and its 501 transitive packages are attributed in full. |

## Agent tools

| Tool | Purpose |
| --- | --- |
| `athena_status` | Engine state, port, home, credentials. |
| `athena_apps` | List installed apps, whether each is running — and what each one will ask you for (a key, a cloud endpoint, or nothing). |
| `athena_search` | Search the whole store catalogue, not just installed apps. Returns an `install_with` handle. |
| `athena_install` | Install by git URL, `owner/repo`, or a store handle from `athena_search`. |
| `athena_launch` | Start an app. |
| `athena_stop` | Stop a running app. |
| `athena_logs` | Read an app's output — and what its log says went wrong: the named cause plus the one action that answers it. |
| `athena_store_detail` | The full store record for one app, before installing it. |
| `athena_publish` | Publish an app you built here to the catalogue. |
| `athena_delete` | Uninstall: stop it and delete its folder, reporting what was freed. |
| `athena_fix` | Repair in place — structure, copied environments, submodules, install script. |
| `athena_report` | What needs you right now, in one call: apps that cannot run, apps that stopped recently, updates already known to be waiting, apps that cannot generate, weights nothing reads, and launches queued for the card. Local reads only. |
| `athena_app_health` | Every health check without changing anything, including an informational `Access` line saying what the app asks for (never a failure — an app that wants a key you have not given it is not broken). A failing check carries `next_step`, and `failure` carries the log's own account of why the app last stopped. |
| `athena_models` | Weights: with no arguments, this machine's whole shelf (every app's verdict, every library and its size, what nothing reads, what is already cached); with `app`, one verdict plus the folders that fix it; with `repo`, what a HuggingFace repository holds and costs — and with `download`, pulls it into the library the app actually reads. |
| `athena_settings` | Read or change home, port, autostart, shared model cache. |
| `athena_files` | Browse and edit files — Athena's folders, and your own machine. |
| `athena_space` | Drives, per-app usage, and duplicate scan/dedupe/free/separate. |
| `athena_job` | Where a long action got to, with the tail of its log. |
| `athena_capabilities` | Ask an app what it can actually do — inputs, files it takes, saved workflows. |
| `athena_generate` | Have an app make something. |
| `athena_runs` | Everything an app has already made here. |
| `athena_schedule` | Arrange for one of an installed app's capabilities to run on a schedule — and read back what is already arranged. |
| `athena_build_app` | Turn a git repository into an installable app. |
| `athena_machines` | A real OS Athena can start, stop and run commands in — its own WSL distro, or a QEMU machine booted from an image you supply. `list`/`setup` say what is here; `isos`/`set_iso_dir` read and set the one folder Athena takes images from; `fit` sizes a machine for this computer when nobody said otherwise; a missing prerequisite comes back as a question with the exact command rather than an install. |

## Machines

Some questions cannot be answered by reasoning about this computer — only by having the
other operating system. *Does this install on Linux? What about Arch? Does the Bazzite image
actually boot on the graphics card?* A machine is a real OS Athena starts, stops and runs
commands in, and it exists for exactly those questions.

Open the **Machines** tab, or use `athena_machines`. Both drive the same host.

- **WSL by default.** On Windows a machine is its own WSL distro (`athena-<name>`),
  imported from a root filesystem tarball on first start — about 340 MB for Ubuntu. It is
  Athena's distro, not yours, so a command run inside it cannot see the folders you are
  working in.
- **Any image you give it.** A machine's `image` is a first-class field: `rootfs` (a tarball
  to import), `iso` (a bootable installer), or `disk` (a virtual disk). Point it at a local
  file or a URL — Omarchy, Bazzite, a Debian netinst, a Windows ISO, or a disk you already
  built. Presets cover Ubuntu 24.04, Debian and Arch; everything else is the same field with
  a different path.
- **QEMU is the escape hatch.** Anything WSL cannot boot — Windows, a distro that does not
  ship a WSL rootfs, a raw ISO — runs under QEMU, installed only if you say yes. You hand it
  an ISO or a disk image by path or URL (`image: {kind: iso|disk, …}`), say what will boot
  (`os: linux|windows`), and it starts. A Windows guest automatically gets IDE disks and an
  e1000 NIC, because an installer has no virtio driver and would otherwise report a machine
  with no hard drive.
- **Attaching is a decision, never a default.** A distro you already have can be used
  instead of a new one, but only when you say so, and the card says why that matters: a
  command in an attached distro can see your files, and Athena will not stop or delete it.
- **A QEMU install is a download you can watch.** `winget show` names the installer and its
  published hash; Athena fetches that file itself, so the card grows a real progress bar —
  `downloading qemu-w64-setup-20260811.exe · 48 MB / 197 MB` — and the file only becomes the
  installer after the size and the sha256 match. Nothing winget will not name is guessed at:
  the plan falls back to the command and says so (see 54).
- **Nothing installs itself.** A missing WSL or QEMU arrives as a card asking a question and
  carrying the command. Set it up is one button; on Windows enabling WSL raises Windows' own
  consent prompt, and a step that needs a password is shown to you rather than typed for you.
- **Your images are one folder, and the menu is drawn from it.** Athena reads exactly one
  folder and never goes looking through your drives: `athena/isos` by default, changed in the
  tab or with `set_iso_dir` (or `ATHENA_ISO_DIR`). The picker lists what is in it — each file
  labelled with the kind its *name* suggests (`win11.iso — installer, 6.0 GB`), `kind not
  obvious from the name` when it does not — plus a way out of the folder for a path typed by
  hand. Picking a file fills the path, the kind, and a suggested guest OS, all three visible
  in the form, because a name is evidence and not an answer. The folder line tells "not there
  yet" apart from "no images yet" (see 50), and pointing at a folder that does not exist is a
  question — *Make it?* — rather than a `mkdir` of somebody's typo.
- **Subfolders count, and they are read as subfolders.** A folder sorted into `windows/`,
  `linux/`, `mac/` is not a folder with nothing in it: Athena walks three levels down (see 52)
  and the images come back grouped by the folder they were found in — `windows (3)`,
  `linux/arch (1)`, the top folder first and unlabelled. The folder line says it out loud
  (`E:/isos — 6 images in 3 folders`), the menu carries the folder on each entry
  (`windows/win11.iso — installer, 6.0 GB`), and the list under it is what you pick from:
  each image has its own *New machine* button that fills the same three fields the menu does.
  Two files called `install.iso` in two folders are two files everywhere in the tab.
- **The folder is walked to, not typed.** `Change folder…` opens a browser that lists the
  *folders* inside one folder, one level per click, with `Up` that stops at the root, the
  drives offered as a way in, and `Use this folder` meaning the folder you are standing in —
  the path box stays for a path you already have on the clipboard (`Type a path`). It reads
  when you ask and what you asked about: no drive sweep, no files, and nothing walked on its
  own (see 53). Where the desktop app exposes a native picker it is offered as an extra door;
  the browser is what always works.
- **The first machine is sized for this computer, and says why.** The cores, memory and disk
  fields start *empty*, and empty means "take the fit": half this machine's cores (up to eight),
  a quarter of its RAM, 40 GB of free space — computed from what is actually here, with the
  reason printed next to the numbers (`24 cores here → 8 for the guest, leaving the host 16`).
  Type your own number and it is used instead; nothing is decided for you that you cannot see.
- **The pointer is absolute, so it moves.** QEMU is given a USB tablet
  (`-usb -device usb-tablet`), not just a mouse: VNC sends the pointer's position, and a PS/2
  emulation has to turn that into relative motion, which only works while the guest draws a cursor
  it can push around — a Wayland desktop draws none, and the pointer then arrives without ever
  moving. Nothing to configure, and the same is true of every remote console: VirtualBox calls it
  `--mouse usbtablet`.
- **A guest is a guest.** A WSL machine shares this computer's localhost, so a server inside
  it is reachable as if it were here. A QEMU machine does not — its card names where it *is*
  reachable (`8080→80`, `2222→22`, loopback only) and its console is at `console.log`. There
  is no command box for a QEMU guest, because there is no shell channel into one: the card
  says how to get in instead of offering a field that would refuse.
- **Its disk is its own.** A machine Athena creates gets a disk it owns; a machine pointed at
  a disk image you already have boots *that*, and no empty disk is made beside it. Snapshots
  are refused while a machine is running — a copy of a disk being written to is not a backup —
  and stopping one that already stopped reports `stopped`, not a failure.

Each machine is a folder under `athena/machines/<name>/` holding its spec, its disk, its
snapshots (`exports/`) and its console log. Ports are data — `8080:80` in the form becomes
`{host: 8080, guest: 80}` in the spec, and a malformed pair is refused while you are still
looking at the form, because a dropped port forward is a machine that looks dead from outside. Snapshots are `wsl --export` / `wsl --import`
underneath, which is why a snapshot is a real file you can copy to another computer rather
than a promise to be able to roll back.

## Where everything lives

Athena keeps its whole world inside the Hermes data directory, so one folder is the entire
story:

```
hermes/athena/            app home — every installed app, its env, its model cache
  settings.json           home, port, autostart, shared model cache (per machine, never in the repo)
  cache/athena-catalog/   the catalogue index and resolved app records
  machines/<name>/        a machine: machine.json, its disk, exports/, console.log
  isos/                   the image folder, unless you pointed the profile somewhere else
```

The catalogue index (every app the store lists) is fetched once and cached for a day;
individual app records are cached for a week. Browse works with no network at all.

## Design constraints discovered the hard way

These are load-bearing; changing them breaks the plugin.

1. **Node 22–24 is pinned, and the pin is checked rather than assumed.** The engine depends on
   `node-pty` and `better-sqlite3`, which ship prebuilds only up to Node 24: a Node 26 installs
   nothing and a Node 26 boot dies outright. Athena therefore *looks* for a Node it can trust —
   `ATHENA_NODE`/`HERMES_TOOLS` if you name one, then the Node Hermes already keeps for its own
   tooling on this platform, then `PATH` — and validates the ABI before using it, instead of
   naming a path (see 35).
2. **Isolation is done with `USERPROFILE`, not an env var.** The kernel reads its store
   from a dot-directory under `os.homedir()`, and the `home` key in its own config wins
   over the environment. Overriding `USERPROFILE` for the child process is the only clean
   way to keep Athena's home and any other install from colliding.
3. **Readiness is an HTTP probe, never stdout.** A forked `node-pty` child emits a
   non-fatal `AttachConsole failed` on Windows and the launcher's stdout dies shortly after.
   Log parsing gives false negatives; `GET /` is authoritative.
4. **The port is set on the server object.** The kernel hardcodes port 42000 and ignores
   `config.port`. Assigning `server.port` before `start()` is honored. Default here: 42001.
5. **The store does have a JSON API.** The upstream catalogue endpoint
   (`https://api.pinokio.co/apps`) serves the whole catalogue as
   data — title, description, `iconUrl`, tags, platform flags, paging — so `catalog.py` reads
   fields rather than reconstructing them from markup. `perPage` is clamped to 100, and the
   ~6,400 apps walk in about 40 seconds, which is cached and refreshed in the background.
   The shopfront's `RSC: 1` payload is still used for one thing only: the site's own tag
   list, whose counts are authoritative and cannot be reproduced locally (see 13).
6. **App control is not HTTP.** The engine exposes no REST route for installing or running an
   app; its own UI drives everything over a raw WebSocket on the same port. `runtime.py`
   speaks that protocol directly, which is also what keeps Athena's interface its own — there
   is no upstream page to embed or re-skin.
7. **A run request must name a concrete script.** Handing the socket a bare app directory
   (`~/api/<app>`) sends the engine down a branch that calls a launcher method this build
   never defines, and the process dies. Resolve the default script first, or pass an explicit
   `install.js` / `update.js`.
8. **A stop request is keyed by `id`, and it must be a `~/` path.** The engine overwrites the
   request's `uri` with its `id` and then resolves it as a path, so a bare app name throws
   inside the kernel. Use `~/api/<app>`.
9. **A wrong slug is not an error, it is a page.** Every slug under `/apps/` renders HTTP 200,
   including ones that do not exist, and the "not found" page parses into a plausible-looking
   record. Resolution therefore treats a record with no repo as a miss and falls back to
   searching the index — where the real slug lives, because slugs carry the owner
   (`github-com-<owner>-<repo>`) and a bare app name cannot be guessed at.
10. **The engine's toolchain is a prerequisite, not a nicety.** Every command an app runs is
    prefixed with a conda activation by the engine's shell wrapper. With no Miniforge
    installed that activation fails, and because the app's own command is chained after it
    with `&&`, the command never executes — an install clones its repo, builds nothing, and
    still reports success. `POST /environment/provision` installs it through the engine's own
    kernel API (`kernel.bin.mod.conda.install` — note the extra `mod` level; without it the
    method silently matches nothing).
11. **Output is read from the engine's log files, not the live socket.** Scripts run inside
    the engine's process tree; the socket is only a subscription. Closing it does not stop the
    work, which is what lets a long install run while the panel stays responsive — so the logs
    have to come from disk to be readable when nobody was connected.
12. **Importing an existing install must default to linking, not copying.** A real install of
    eight apps measured **264 GB**; copying it duplicates every downloaded model for no gain on
    the same disk. `mklink /J` creates an NTFS junction (reparse tag `0xa0000003`) without
    administrator rights — unlike a Windows symlink, which needs developer mode — and reads and
    writes pass through to the original folder, so the app behaves identically.
13. **Store tags cannot be counted locally.** Each record carries only its leading few tags, so
    a local tally undercounts against the store on every single tag (`voice`: 9 locally vs 16
    on the store; `tts`: 26 vs 60). The store's `tag=` filter is also a *prefix* match, so
    `tag=voice` includes `voice-clone`. Browse, search and filter therefore ask the store and
    use the cached crawl only as a fallback — showing a count that disagrees with the store it
    is filtering is worse than showing none.
14. **A readiness verdict has to carry its own next step.** "ComfyUI cannot generate" is true,
    useless, and the *normal* state of a fresh install — a clean checkout ships an empty
    `models/` while the machine's weights sit in one folder somewhere else. The probe
    therefore returns `next_step` as data (`{"action": "point_models", …}`) rather than a
    sentence, so a button and an agent tool can both act on it without guessing. The write
    itself is `app/extra_model_paths.yaml`, which upstream neither ships nor tracks.
15. **`extra_model_paths.yaml` maps *leaves*, and the library is their parent.** The parser
    answers with the folder a key points at (`…/models/checkpoints`), not the folder that
    holds it. Asking such a leaf for its own `checkpoints/` finds nothing, so a machine whose
    weights ComfyUI could already reach offered zero candidates — found only by running the
    discovery against the real machine, not in the unit test that asserted a synthetic one.
16. **"Nothing reads it" and "it is cached" are different facts about the same bytes.** A
    HuggingFace cache entry is read constantly — an app asks for a repository by name and the
    cache answers — so folding it into an "unreferenced" list accuses the one thing on the disk
    that is doing its job. Library folders an app is not pointed at are unread (and their size
    is the whole reason to say so); cache entries are *reusable*, and the useful thing to say
    is that a pull for them will not touch the network.
17. **A byte count has to come from the folder, not from the progress bar.** `hf` draws with
    carriage returns, which arrives as one enormous line that says nothing a job log can show.
    Progress bars are switched off and the destination is weighed instead — which also means a
    resumed pull's bar starts at what is already on disk rather than at zero, and that the
    reported `bytes_written` is what this run actually wrote (a second pull of the same file
    into the same folder writes **0**).
18. **A download that cannot be reached is storage, not a download.** So the destination is
    resolved, never asked for: the library the named app is already pointed at, filed under the
    key ComfyUI reads (`checkpoints/`, or `loras`, …). With nothing named it is a cache entry
    and says so, because a caller who wanted an app to be able to generate should learn they
    did not get that. Downloads also run one at a time: a disk and a link are both shared, and
    two multi-gigabyte pulls at once make neither faster — a second request queues, and the
    count of what is ahead includes the pull *running*, not only the ones waiting.
19. **A claim made in the store has to be true of the store, and a filter has to say what it
    counts.** Athena does still ask an installed app what it can do and write the answer beside
    it — that mapping is what the agent tools read — but the store no longer advertises it as a
    property of everything installed. The badge, the line of copy and the filter that said so are
    gone, because the promise is not yet true of every app: a transferred app whose launcher keeps
    a menu of its own cannot even be started correctly. What remains is the `Windows + NVIDIA`
    chip, which narrows the *loaded page* and says so: the store's ranking cannot be asked for a
    check-in flag across 4,289 apps, and a count that quietly describes one page is worse than one
    that admits it.

20. **A failure is read from the sentence the machine printed, and a past failure is not
    breakage.** Triage names a cause only when it matches a string captured from this box or
    read out of the source of the program that prints it — a pattern written from memory
    matches the message someone remembers. Two rules keep it honest: the *last* reading in a
    log wins (a crash is at the end; a warning from a run that then succeeded is not the
    reason this one stopped), and a signature must not match inside a longer word
    (`\bMemoryError\b`, because PyTorch's own `OutOfMemoryError` contains it, and reading a
    full card as a full RAM was the first bug this caught). The log's reading is filed
    `informational`: an app that failed an hour ago and has run since is not broken now.
21. **A fix button dispatches on what the cause is, not on the commonest cause.** Every
    `next_step` used to run the weights flow, which is right for exactly one action out of
    six: a card another app is holding is closed, a broken environment is repaired, a gated
    download seeds the credentials this machine already has, weights are pointed at, and some
    causes have no button because the answer is a decision rather than a click. A reading that
    names a neighbour app must name which one, or the button it offers is unanswerable.

22. **A launch can be queued for when the card frees, and the queue is in memory on
    purpose.** The VRAM gate warns and lets a launch through — no app declares what it
    needs, so a refusal would be a guess — which leaves one case with no answer: the card
    is full now and the work is wanted later. `POST /apps/{id}/wait` is that answer, and it
    becomes an ordinary job, so the Terminal already shows what it is waiting for. It is
    deliberately *not* durable: a queue that survived a restart would start apps nobody is
    watching, and `/waiting` says so in the payload. The pump asks the same `gpu_gate` the
    launch path asks, treats its `None` ("nothing to warn about") as *go*, and takes the
    entry off the queue **before** it starts — an entry still listed while the launch is in
    flight reads as waiting, and the pump would start it twice. A cause with two true
    answers offers both: stopping the app that holds the card frees it now and throws that
    work away, waiting does not.

23. **The report is a reading, not a check.** `GET /report` (and `athena_report`) folds what
    is already known — failing checks, the cached update answers, the weight inventory, the
    wait queue — into one list in the order it matters: cannot run, stopped and not restarted,
    update waiting, cannot generate, disk nothing reads. Two rules keep it honest. It never
    reaches the network: the update answers are the cached ones, because a report that runs a
    `git fetch` per call is a report nobody can ask often — and an app whose update check has
    never run is counted as **unchecked**, never as up to date. And it says what it read, so an
    empty list is not mistaken for a clean machine. A stopped app whose log names the reason is
    never filed under "broken": a log is a record, and the fix it carries belongs to the app
    that stopped, not to the box.

24. **"Runs on this box" is read off the app's own scripts, and only counts when it was read.**
    `dashboard/keyscan.py` walks an app folder for what its scripts *name* — an environment read,
    an `.env.example`-style template line, an assignment left blank or holding a placeholder — and
    for a literal cloud host. Three rules keep the answer worth believing. **Evidence or silence:**
    a README naming `OPENAI_API_KEY` is a README talking, so documentation is skipped; a script that
    writes a *real* value into the file is not asking for anything, because it already has one.
    **A capped scan says it was capped:** the walk stops at `MAX_FILES`/`MAX_FILE_BYTES`, and
    hitting either sets `complete=False` and `local=False` — "runs entirely on this box" is not a
    claim you can make off half a read. An oversized dataset is skipped without weakening the
    answer; an oversized *script* is the one skip that does, since that is where a key read or a
    cloud host could hide. **No value ever leaves the scan:** names and lines only, redacted
    before they reach the cache or a route. The badge, the filter and the card's button all read
    one cached answer (`_keys_cached`) because the app list is polled and a polled route must not
    walk a folder — and `scanned: false` (nobody has read it) is kept distinct from `local: false`
    (read, and it calls out) all the way to the screen. The store has no such badge on purpose: a
    catalogue row has no folder here to read, so the card says nothing about keys rather than
    guessing at them.

25. **The catalogue has no VRAM figure, so "will it run on my rig" is answered from check-ins.**
    A store row carries `checkinsPlatforms` (`windows`/`mac`/`linux`) and `checkinsGpus`
    (`nvidia`/`amd`/`apple`) — booleans meaning *somebody has reported running this from one* —
    and nothing else about hardware. So the store card says exactly that: `runs on Windows`
    (someone checked in from Windows on NVIDIA), `Windows, no NVIDIA`, `no Windows check-in`, or
    `nobody has run it`. Every one of those is **evidence, not a requirement**: the wording
    promises no speed, and a row with no check-ins is an *absence* of evidence, said as an
    absence rather than as a verdict. A `Windows + NVIDIA` chip narrows the loaded page to those
    rows and prints how many of the page it kept, because the store's ranking cannot be asked for
    a check-in flag across the whole catalogue. VRAM fit is a different question and is answered
    where the truth lives: the weights shelf and the GPU gate, not the marketing row.

26. **A finished run says what it made, and only speaks up if you may have looked away.**
    An app the agent drives leaves a run record (`state`, `files`, `likely_output`), but it is not
    an engine job — it never passes through `_job_end`, so it never got the completion broadcast
    that installs and pulls do. `appcall._announce_run` now emits `plugin.athena.run.done` carrying
    the artifact, and the panel names the file instead of announcing "finished". Three restraints
    keep the notice worth reading. It is **best effort twice** — the event bus import *and* the
    call are inside the try, because the work is already done and on disk and a missing or broken
    listener must not fail a finished run. It stays **quiet under 20s**: a four-second call the
    agent just watched needs no notification, a quarter-hour render does (the record lands in Runs
    either way — the threshold only decides whether it also makes a noise). And an **unknown
    duration is omitted, never zero**, so a run whose stamp is missing cannot read as instant.
    The two ways this could lie are both text — naming a file the run never produced, or inventing
    a duration — so the wording is a pure exported function and the driver asserts both, including
    that the app is named the way the Runs tab names it rather than by its `.git` folder on disk.

27. **A running job says how long it has been going and how long since it last spoke.**
    The Terminal header printed `working…` for every job that had not ended — a sentence equally
    true of an install grinding through a checkout and one that hung forty minutes ago. The job
    record now carries `last_output` and `moved` beside `started`, `job_status` derives three ages
    from them (`elapsed`, `since_output`, `since_move`) so every watcher computes them the same
    way, and a pure `jobPace` turns them into words: `working — output 2s ago` (green),
    `quiet for 40s` (plain), `no output for 6m 12s — a clone walks the whole repository before it
    copies anything` (warning), `took 4m 12s` / `gave up after 1m 3s` once it ends. Two rules keep
    it from lying. **A moving bar counts as speaking** — a copy reports progress and prints
    nothing, so `since_move` is read as a sign of life and the header says `working — progress
    1s ago` rather than accusing a working job of silence. And **there is no expected duration**:
    Athena keeps no history of how long an install took, so the header never says "usually 3
    minutes" — it states the two ages it has and, for a silence worth explaining, names what that
    kind of job is normally doing while quiet. An age that cannot be computed is left null and the
    header falls back to `running`, because a zero would read as "it just spoke".

28. **Going back to a commit is answered from the app's own repository, and only a commit it
    has actually stood on.** The reflog *is* the record: git writes an entry for every move,
    including the resets this route itself performs, so a put-back needs no bookkeeping file of
    Athena's that could go stale, be deleted with Athena, or disagree with the repository — and
    what is offered is never a guess at a "previous version". The stamp comes out of the ref
    selector (`%gd`) and not `%cd`: in reflog output `%cd` is the *commit's* own date, verified on
    a real app here where the reflog read 2026-10-10 against a 2026-03-30 commit. Four things are
    refused rather than done — a folder that is not a repository, a sha that is not a commit in
    it, a commit it has never stood on (which is how a fetch would otherwise become an install),
    and the commit it is already on. What it costs is counted before anyone agrees: tracked files
    with changes in no commit, and commits made in the folder. Untracked files are deliberately
    *not* counted, because `reset --hard` does not touch them and a local `.env` on top of a clone
    is a normal way to run an app. The job does not run the app's own update afterwards, because
    an update script's first act is normally `git pull`, which would fast-forward straight back to
    the commit being rescued from — so the terminal says that updating again moves the app
    forward, and names the app's own Install for when its environment needs rebuilding.

29. **What the card has cost is a running total folded out of the monitor's own samples, and it
    never claims more than it holds.** There is no power meter to read, so the feature is really a
    set of decisions about what may be said. The reading exists in exactly one place — the sampler
    thread in `system.py` — so power draw rides along in the *same* `nvidia-smi` call as
    utilisation rather than a second one, because the reason that call is on a thread at all is
    that it can block for seconds talking to the driver. The sampler then offers `on_sample` and
    the cost record subscribes to it; the monitor is never made to know a cost total exists, and a
    record that starts raising cannot take the monitor down (the hook call is guarded).
    **A gap is not idle time.** Hermes closed, the machine asleep, a suspended process — the power
    over that stretch is *unknown*, so nothing is credited for it and the count of gaps is kept, so
    "why is this total small" has an answer. A backwards clock contributes nothing rather than a
    negative, and the next sample re-anchors. **A watt-second is not a kilowatt-hour**: W·s ÷
    3,600,000, not ÷ 3,600 — the wrong divisor is the one that looks right, and it would report a
    total a thousand times too large with a cost to match. Both are worth stating because the
    right answer for watt-hours is the wrong one here. Idle draw *is* counted (a card holding a
    loaded model still draws), but it is reported apart from the energy drawn while the card was
    actually working, so neither number has to be inferred from the other. **What the card cannot
    tell us stays absent**: a card that reports `[N/A]` for power gets its time counted and its
    energy left `null`, never a zero standing in for a reading nobody took, and energy with no
    price set yields a `null` cost rather than "$0.00" — money appears only once someone says what
    a kWh costs, and the price is sanity-checked (0–1000) because a 100× typo would read as a real
    cost in the panel. Resolution follows the same rule: eight decimals in the record, because at
    six a short session's energy rounds to a number that did not happen, and the panel then keeps
    places below a cent instead of rounding to `$0.00`. The record lives beside its price in
    `cache/gpu-cost.json` rather than in settings — it is a figure about this machine's running
    total, written atomically so a crash cannot leave a half-written number — and it is reached
    through `plugin_api._cost_module()`, keyed in `sys.modules` like `idle.py`, because this file
    is loaded twice and two module objects would mean two totals that the UI cannot tell apart. Its
    lock is reentrant: `reset` reads the price through `get_price`, which takes the same lock, and
    a plain lock deadlocked the route on itself until the suite timed out on it.


30. **"Which app would take this file?" is answered from the manifests already on disk — and
    answering it starts nothing.** The question is the other half of "what can this app do", and the
    tempting implementation is the wrong one: probing an app means launching it, so a right-click on
    a file would land a model on the card. The saved `agent.json` beside each app has the answer
    already, and `POST /accepts` reads only that.
    **The match is component-level, not intent-level.** A capability declares which *components* it
    wants; an input only says it takes a local path. Gradio's component name is the only thing that
    distinguishes "an image" from "anything", so `Image`, `Video`, `Audio` and `Model3D` are matched
    against the file's own kind (read from its suffix, by the same map the uploader uses) while
    `File`/`Files` are the app saying it does not care. A typed input that wants a different kind is
    not offered, and the refusal *says which is which* — "it takes an image input, and this is a
    video" — because pointing someone at a plausible-looking wrong app costs them more than an honest
    no. For the same reason a capability that wants a **link** is not a candidate for a file on this
    machine, however well its intent matches, and a capability whose required input is typed is
    ranked above one that takes anything. **An app that has never been probed is reported as
    unprobed**, never assumed able, and the panel names the apps in that state. The refusals are
    shown beside the offers: an empty list is not an answer, and "nothing installed here would take
    `take.wav` — it is an audio file" is.
    **The hand-over refuses everything it can before a job exists.** `POST /apps/{app_id}/call` is
    the one door the desktop half uses, and it answers 404 for an app that is not installed, lists
    the intents it does have when the named capability is not in the manifest, and lists the inputs
    it needs when required ones were not passed — none of which creates a job or starts an app. A
    real call runs as a job, because a generation takes as long as it takes; the app is started if it
    is not serving (and the engine first, if *it* is down), the manifest is re-probed **when the url
    moved**, since a restarted app owns a new port and a stale url is the difference between a call
    that works and one that hangs, and the app is held busy for the whole call — otherwise the idle
    reaper reads "no new output" as "nobody wants this" while a twenty-minute generation is in
    flight. A file input that wants **many** files gets the single path wrapped in a list by the
    caller, because one path handed to a list input throws. The record of what happened is the job's
    own log, with the app's output in it and each produced artifact named as it lands.
    **The door is the file's own right-click menu**, which belongs to the ported browser — so it
    cannot call into Athena's half. It calls one global function (`window.__athenaSendToApp`) that
    Athena's panel installs while the Files tab is on screen and removes when it goes; one menu item
    is the whole of the donor surgery, asserted present in the merge so a donor bump fails loudly
    rather than silently.


31. **A chain of apps is checked whole before any of it starts, and the one thing that cannot be
    known up front is said out loud rather than promised.** `POST /apps/{id}/call` takes `then` —
    stages that run in turn on what the stage before them made — and the whole chain is one job,
    because a person watching a pipeline wants one bar and one log. Before a job exists, every stage
    must name an app that is installed, a capability that is in that app's manifest, and a handover
    the two manifests actually support: `appcall.links()` compares what one capability declares it
    *produces* against what the next declares it *takes*, component-to-component, and a mismatch is
    refused by number and by name — "stage 2 (wanish — text_to_video): it produces an image and the
    next one takes a video" — rather than discovered halfway through with a job already running. What
    cannot be known up front is *how many* files a stage will make, so that is answered at run time
    and never guessed: a stage that produces no files **stops the chain and says so** ("stage 1
    produced no files, so stage 2 was not started"), because silence would leave a step that looks
    like it ran and did nothing, and a stage that fails ends the answer failed with that stage named
    while still reporting the files that did come out — they are on disk either way. The same rule
    covers the two other things a stage may need: an app that does not type what it produces may
    still be chained, but the handover is marked *not certain* in the log, and a stage that needs
    more than the files it can be handed is refused before anything runs, with the sentinels
    (`{files}`, `{file}`) named so the caller can give it the rest.


32. **A repeat is a script, not an agent turn, and the index Athena keeps is checked
    against the cron store every time it is read.** `POST /apps/{id}/schedule` writes a
    small script that imports the plugin's own `run_one()` and registers it with Hermes'
    own cron as a `no_agent` job — so an app that runs every morning costs no model call
    to start, and a run that needs nobody to think does not wake anybody up. The same
    parser that will fire the job is the one that reads the schedule, so a schedule
    Hermes cannot read is refused with Hermes' own words before a job or a script exists;
    a build with no cron store answers 503 rather than storing a job nobody will run; and
    the app, capability and required inputs are refused here for exactly the reasons a
    call is refused. `GET /schedules` reads the index *and* the store, because those are
    two different records: a job that has been removed while the index still lists it is
    reported as `gone` — "the cron job is not in the store any more, so this will not
    run" — rather than as waiting, and a schedule whose script has gone missing says so,
    because a schedule that will never fire is the worst thing to be wrong about.
    `run_one()` is the job-less runner a scheduled run calls: it takes the same
    bring-up, the same app call and the same re-probe as a hand-started run, so a
    scheduled run is not a second implementation of anything — and the record a person
    reads afterwards is the recipe the call writes beside its outputs, which is what the
    Runs tab shows. How often a schedule has actually run is counted from those same recipes.


33. **A repeat asked for in chat is the same object as one asked for in the panel, and a
    chain asked for by an agent runs the same runner a scheduled run does.** The agent tools
    do not re-implement either: `athena_schedule` (create / list / stop) calls the same route
    functions the desktop calls, so its refusals are the route's refusals in the route's
    words — an unreadable time is refused with Hermes' own sentence, a missing app or
    capability, or a store with no cron in it, are refused exactly as they would be from the
    panel — and a repeat it creates lands in the same index the panel lists. `athena_generate`
    takes `then`, and when it is given, the chain is checked with `_chain_check` before
    anything starts and then run by `run_one()`: the same job-less runner a scheduled run
    calls, which is the same code the route's job body runs for each stage. So there is one
    rule for what a chain is, one runner for what a stage does, and one store for what a
    repeat is, whichever door it came through.

34. **A card says its state once and offers one thing to do.** Ten buttons of equal
    weight sat on every app whatever its state, which made the description and the
    tags — the four words that tell two similarly named apps apart — the quietest
    things on the card, and most of those buttons could not do anything from where
    they were. So: one chip (`running · :7860`, `no launcher`, `runs here`,
    `nothing read yet`, `needs 1 key`), one button for what you came to do (`Open`
    when it runs, `Install` when there is no launcher to start, otherwise `Launch`),
    one for what you reach for at the same moment (`Stop` while it runs, otherwise
    `Terminal`), and everything else behind `⋯`. The chip's words come from
    `keyBadge`, so a card and the "needs a key" filter can never disagree about what
    an app is asking for. An action that could only refuse in this state is not
    rendered at all, and the menu is not in the tree until it is opened, so nothing
    can pass a check by finding a button that was never on screen. Rows are the same
    card with one class added — same chip, same button, same menu, same handlers,
    one `appHandlers` table — chosen in Settings (`apps_layout`, `cards` or `rows`),
    because a second renderer for the same app is a second place to be wrong.

35. **A path that names one machine is a bug, not a default.** Node discovery was two
    hard-coded strings, one of them the author's own home directory: on any other
    machine — and on every Mac and Linux — the search found nothing, the engine never
    started, and the plugin looked broken in a way that had a one-line cause. The
    search is now derived (this platform's Hermes tools directory, then `PATH`, with
    `ATHENA_NODE`/`HERMES_TOOLS` to say otherwise) and it *checks* the version it
    finds, because the engine's native addons have no prebuild past Node 24 and a Node
    that installs is not the same as a Node that boots. The same rule caught the
    settings file, which lived in the plugin directory: a plugin directory is code,
    `hermes plugins update` replaces it wholesale, so a setting written there
    disappears on the next update and travels with the repository besides. Settings
    now live in Athena's home, the old location is read once so an existing install
    keeps what it had, and the harness derives its interpreter, Node, and plugin path
    the same way. The test is not "it works here" — it is `grep -r` for a user name
    coming back empty, plus the same functions run on Linux.

36. **A stop that only works on Windows is not a stop.** Reaping a stale engine used
    `netstat` and `taskkill`, and neither exists off Windows: on Linux the sweep
    returned an empty set, which is indistinguishable from "nothing is holding the
    port" — the failure mode of a check that silently finds nothing. It reads `lsof`
    or `ss` there instead, and kills the process *session* rather than the top of the
    tree, because the engine forks and the child is the thing holding the port. Its
    browser stand-in had the same shape: `no-browser.cmd` is a Windows batch file, so
    a POSIX host got no `$BROWSER` and apps opened a real browser outside the window
    they were supposed to be drawn in. Both now branch on the platform, and the Linux
    half is exercised rather than assumed.


37. **A declared dependency carries an upper bound.** `hermes plugins doctor` warns on every
    unbounded entry, and the warning is the right one: `psutil>=5.9` with no ceiling is how a
    plugin that imports today stops importing after a major release. A warning nobody acts on
    is noise that hides the one that matters — the same argument as an incomplete
    `provides_tools` list, and the reason both are now empty of warnings rather than merely
    non-fatal.

38. **A dependency with no platform marker lands in every platform's split.** Hermes core pins
    `psutil` twice — `==7.2.2` everywhere except Android, and a **git URL** on Android. Athena's
    `psutil>=5.9` carried no marker, so it applied to the Android split too, where a URL source
    cannot satisfy a version range; the resolution failed, admission **disabled the plugin**, and
    the panel kept rendering from the desktop half while every call answered
    `404 {"detail":"Plugin not found"}`. Marker-scoping the requirement (`; sys_platform !=
    'android'`, which is where core's pin applies anyway) fixed it. Two lessons in one: a
    plugin's dependency declaration is resolved against core's pins, not in isolation, and
    "the UI draws but nothing works" is a *disabled plugin*, not a UI bug — `hermes plugins show
    <name>` says which.

39. **A 200 is not a success.** The engine's routes answer `200 {ok: false, error: "…"}` when a
    start cannot find a usable Node, when the spawn dies, when the port never answers — six
    distinct failure returns, each carrying a sentence written for a person. The header read the
    status code and threw the body away, so a click that failed flashed `Starting…`, went back to
    `stopped`, and said nothing: the exact "it seems broken" report, produced by the code meant to
    prevent it. Read the body, show the sentence, and keep the reason on the stopped header
    (it is in `/status` as `last_error`) so it survives the notification. `stop()` returns
    `{ok: true}` in all three of its cases, which is what makes "any `ok: false` is a failure" a
    safe rule here rather than a guess.

40. **The merge owns the tab bar, and `plugin.js` is the file you edit.** The panel source is
    `desktop/plugin.js`; `merge_ui.py` reads it, rewrites the tab list and the render chain to
    splice in the tabs that come from the Athena/desktop half, and takes a **backup** called
    `plugin.js.pre-merge` the first time it runs. That backup is not an input, and editing it
    looks like it worked — the file changes, the bytes are there — right up until the next merge
    fails with `tab list not found`, because it is still reading the file you did not touch. Two
    additions therefore belong in `merge_ui.py` (`NEW_TABS`, the render chain) and everything
    else in `plugin.js`. The merge also refuses a raw `<pre>` styled as a terminal: a live
    terminal is an `AutoTerm`, and a `<pre>` is a terminal that stopped following.
41. **Nothing installs itself, and a missing prerequisite is a question.** A machine action that
    needs something absent — `wsl.exe`, a distro, QEMU — returns `needs_setup` carrying the
    question, the *platform's own* command and what happens after, and the agent is told to ask
    rather than to run it. Three ways to answer, kept distinct on purpose: `elevated` (Windows'
    consent prompt is the answer, so the button starts the install), `job` (a long command worth
    watching, so it becomes a job with a progress line), and `manual` (a command needing a
    password, shown and never typed — a plugin that cannot talk to you must not pretend to have
    sudo).
42. **A machine is claimed, never shared; attaching is a decision.** Athena's machines get their
    own distro and their own folder, which is what makes "a command inside it cannot see your
    files" a fact rather than a hope. Attaching to a distro that already exists is offered as a
    button and never as a default, because that is the one setting where an agent's shell is
    standing in your workspace. In the UI it is a checkbox with the consequence written next to
    it; in the spec it is `attach: true`, and the card says whose machine it is.
43. **A machine that cannot start is refused when it is made.** A QEMU machine with no image can
    never boot, so the spec raises at creation time rather than at `start`, where the failure
    would arrive as an opaque QEMU error minutes later. This is the rule behind "give Athena
    an ISO instead of WSL": an image is `{kind: rootfs|iso|disk, path|url}`, a QEMU spec must
    have one and must say which kind, and every preset names both. A spec is refused before
    anything reaches the disk.
44. **The kind of an image is never guessed from the filename.** A `.iso` and a WSL rootfs
    tarball are both "a path somebody typed", and guessing right most of the time is how a
    machine ends up booting nothing. The panel asks, the agent tool has `image_kind`, and the
    only default is the one implied by the provider. The same rule runs the other way: the
    provider follows the preset (`custom-iso` *is* QEMU) unless it is set by hand.
45. **The QEMU command line is a pure function, because it is the only part that can be proved
    without QEMU.** `qemu_argv(spec, dir)` builds an argv and touches nothing. The mistakes
    that live there all look the same from outside — a machine that boots and then does
    nothing — so each one is a checked assertion instead of a habit: the image is a file and
    never the URL it came from, the accelerator is discovered (WHPX / KVM / TCG) rather than
    assumed, a Windows guest gets IDE disks and an e1000 NIC because an installer has no
    virtio driver, the console is a file rather than a pipe nobody reads, and every forwarded
    port is loopback-only.
46. **State is the process table, not the pidfile.** `qemu.pid` is a claim; whether that pid
    is alive *and* is QEMU is the fact. A pidfile left by a crash reads as "not running",
    because the alternative is a panel offering to stop a machine that died last week — and
    stopping one that is already stopped reports `stopped`, not a failure.
47. **A guest that cannot be reached is named, not shrugged at.** A QEMU guest does not share
    this computer's localhost, so a command you cannot run inside it is refused with the way
    in: the forwarded port and `ssh -p <port> user@127.0.0.1`, or the sentence saying it has
    no forwarded port yet. Ports are validated and normalised where you can still see the
    typo (1..65535, `{host, guest}`), and numbers arriving as strings are normalised rather
    than refused, because that is what JSON from a form looks like.
48. **A tab that draws while its calls 404 is a stale copy, not a broken tab.** Two caches sit
    between a file on disk and what you see. The Desktop app loads `desktop-plugins/<name>/plugin.js`
    — a *copy* of the package's `desktop/plugin.js`, refreshed only by Capabilities → Plugins →
    Rescan (or a restart) — and the plugin host imports `dashboard/plugin_api.py` **once per
    process** (`asgi_apps`, keyed by path, never invalidated), so a route added after that
    import is a 404 carrying `{"detail": "Not Found"}` until the host restarts. The tab renders,
    the backend is live, the two halves disagree. `hermes plugins doctor` names the first of
    these outright; the second is why a backend change needs a restart even when the doctor
    says OK.
49. **"Sized for this machine" is a decision made by a pure function, not a mood.** `host_facts()`
    reads the machine — cores, RAM, and free space on the disk the folder is really on — and
    `fit_spec(cpus, memory_mb, free_gb, provider)` decides from those numbers alone. Keeping the
    two apart is what makes the claim checkable on a computer with no VM support at all, and it
    keeps a reason next to every number: a host too small for a comfortable guest says so rather
    than quietly promising a guest more memory than it has.
50. **An image folder that does not exist and a folder with no images are two different facts.**
    The panel says `… — not there yet` or `… — no images yet`, and never "0 images" for a path
    that was never there. One is a question (*make it?*), the other an answer (*nothing in it*).
51. **A filename suggests a kind; it never decides one.** `image_kind_for()` reads the name for
    what it can (`iso`; `qcow2`/`img`/`raw`/`vhdx`/`vmdk` → disk; `tar.*`/`tgz` → rootfs) and
    returns nothing when the name says nothing, which the menu states instead of hiding. The
    spec still refuses an image whose `kind` was never written down, so a suggestion cannot
    become a decision by accident — and the same rule serves the menu, the panel's parser and
    the API (see 45).
52. **Reading a folder's subfolders is still reading one folder, and it has an end.** `scan_isos()`
    walks three levels — `windows/`, `linux/`, `mac/`, and one version level under them — and
    stops, recording what it did not walk into (`skipped`) rather than quietly leaving images
    out. Every file carries `rel`, the folder it came from, so the panel can group by it and two
    files named `install.iso` in two folders stay two files. The depth is a constant and not a
    mood: five levels down is somebody else's directory layout, not an image folder.
53. **Choosing a folder is walking to it, and the walk is read-only.** `/machines/folders` lists
    the *folders* inside one folder somebody named — never files, never a drive sweep, never on
    its own initiative — and `Up` stops at the root. "Use this folder" means the folder you are
    standing in, so nothing depends on what a text box says. The desktop may expose a native
    picker; a plugin cannot count on one, so the browser is the path that always exists.
54. **A progress bar needs bytes, and bytes need a downloader Athena owns.** `winget download`'s
    own output was measured on this machine and cannot be split into a percentage reliably,
    while `winget show` hands over the installer's URL and hash — so the fetch is ours:
    `download_with_progress()` reports `done`/`total` as they arrive, writes `<name>.part`, and
    renames it only once the size and (when winget published one) the sha256 match. The plan
    names this method `download` and the *existing* job poll draws it, so the panel keeps one
    progress path instead of two.
55. **A test that moves the home has to move the settings file too.** Settings are read from —
    and written back to — a file whose path was decided at import, so a suite that patches only
    `_athena_home` still answers with the folder the person using this machine really set *and*
    writes its temporary home into the file they own. This was found by doing exactly that:
    `t_agent_tools.py` now redirects `SETTINGS_FILE` and `LEGACY_SETTINGS_FILE` at a temporary
    file, restores both, and asserts it wrote there.
56. **A port with nothing behind it does not refuse you — it makes you wait.** On this machine a
    connect to `127.0.0.1` on a port nothing listens on does not come back with a refusal; the SYN
    is dropped and the caller sits there until its timeout (measured: **2.0s** against the address,
    **4.0s** against a name that resolves to `::1` first). So every HTTP probe of a stopped engine
    cost its whole timeout — and `/status` is polled on a clock, `/apps` asked whether the engine
    is up to decide if it should draw a launch button, and every app health check asked the same
    question. The rule that came out of it: **ask the cheap question first and bound the cheap
    question.** `listening()` is a connect with a 0.4s budget — a connect that has not landed in a
    fraction of a second is not an engine answering HTTP — and `probe()` only spends a request
    once something is actually accepting. On a host that refuses properly this costs a
    millisecond; on this one it is the difference between a poll and a stall. A **NO is
    remembered for two seconds** (a YES never is, so a live engine is never missed), because a
    down engine is asked about by every health check and every tick.
57. **A poll is a deadline, not a metronome.** The panel asked `/status` every 5 s whether the
    engine was two seconds into a boot or sitting idle — 5 s of silence after the engine was
    already up, and a spinner that kept turning for seconds after the answer had changed. The
    delay is now the schedule's own decision (`pollDelay(st)`, a pure function the UI tests
    check): 1 s while the engine is coming up, 5 s once it is serving, 8 s for the header chip.
    And the engine is asked for **when the panel loads** rather than when someone first clicks —
    one fire-and-forget `GET /status` in `register()` — so the boot overlaps with reading the
    window instead of starting at the click.
58. **A button that reports "done" without doing anything is worse than a button that fails.**
    Start on a stopped machine did nothing and said `Stop it — done`. The handler tried a *stop*
    first and started only if that failed: on a stopped machine the stop succeeded (there was
    nothing to stop), the success was read as the whole job being finished, and no start ever
    happened — with a success line printed over it, so nothing looked wrong. The action is
    decided by the machine's state now, before the click (`words.what`), and there is no order in
    which a verb button can silently do the other verb. **An action with nowhere to go must say
    so**: a start that cannot happen returns `needs_setup`, and the card offers the button that
    fixes it rather than a click that does nothing.
59. **With no opinion given, a screen lives in the panel — not in a window.** A window that cannot
    open exits *immediately* (a QEMU built without SDL, a session over RDP, a bare X server), and
    every one of those reads as "this machine will not start". So the answer is a fact about the
    host and this build's own reader: `default_display()` is `"panel"`, and it is recorded on the
    machine when it is made rather than re-decided at each start — see 65 for where that screen is
    then drawn, and for what a person asking for a window gets. A machine that *did* ask for a
    window and died anyway gets one more try headless with VNC plus a sentence saying so
    (`display_fallback`), because a silent restart into a different display is a lie about what
    happened.
60. **A guest's screen is read by the plugin, server-side, over VNC.** The panel is a browser
    context: it cannot open a socket to a guest's VNC server, and these routes are buffered HTTP
    with no websocket upgrade. So the plugin is the VNC client (`dashboard/screen.py`) and the
    panel gets a picture (`GET /machines/{name}/screen`, `athena_machines op: screen`) — a JPEG,
    because a live picture is not an archive copy of it (constraint 93). A frame is read
    fresh rather than held open, because a client that is never connected cannot go stale behind a
    machine that was stopped or killed. The pixel format is chosen by us — 32-bit, blue in the low
    byte — which is the order Pillow decodes directly, and the order a real QEMU frame was checked
    against QEMU's own `screendump` for (100.00% of 1,024,000 pixels identical).
61. **A guest's console is read from the end, and shown.** A QEMU guest has no shell to type into
    — its serial console is the only part of it anything can read, and it is where the boot
    loader, the installer's first screen and a kernel panic appear. An install's log is megabytes,
    so the tail is what is served (`console_tail`, `GET /machines/{name}/console`, `athena_machines
    op: console`). A machine that starts with no visible output is indistinguishable from one that
    did not start, which is why Start opens the screen, and the console is one button away.
62. **Input goes the other way along the same protocol.** A screen nobody can type into is a
    picture, so pointer and key events travel back over RFB (`POST /machines/{name}/screen/input`):
    press *and* release for a click, shift held around a shifted character (RFB has no keysym for
    `@`, so `@` is shift+2), modifiers held across a combo. Every message is built by a pure
    function and proved against a fake RFB server in `t_screen.py` — a screenshot cannot be tested
    by taking one.
63. **The accelerator and the CPU model are one decision, and it is measured.** `-cpu host` under
    WHPX on this Windows build leaves the vCPU frozen at its reset vector while QEMU itself is
    alive: the process is running, the guest never executes, and the screen shows QEMU's "guest has
    not initialized the display (yet)" placeholder forever. `qemu_cpu_args()` gives WHPX no `-cpu`
    at all (QEMU picks a model that works, still accelerated), `-cpu host` only to KVM, and
    `-cpu max` to TCG. A live process is not a running machine, and that difference is written down
    where the command line is built.
64. **A settings save merges onto the file; it never replaces it.** One file holds both the
    engine's keys (`autostart`, `idle_stop_seconds`, the disk warnings) and the machines tab's
    (`iso_dir`), and a save that wrote only what its caller happened to be holding deleted the
    rest. Found live: an ISO folder pointing at eight images had been erased, and the panel
    listed an empty folder as though it had never been chosen. Saving is a read-modify-write
    now, and **a key set to `None` is how a caller says "remove this"** — a key simply absent
    is left alone, because "not mentioned" and "should be gone" are different answers.
65. **A machine's screen is in the panel, and only a person asking moves it out.** Where a screen
    lives is decided on QEMU's command line, which is decided at start, so "pop out" on a running
    machine is a restart into the other display and the reply says exactly that (`POST
    /machines/{name}/display`). The default answers with the panel (`default_display()` is
    `"panel"`), and a VNC display number is picked from the ones nothing else holds — two machines
    quietly sharing display :0 is one machine drawing into a screen nobody is watching.
66. **A listing opens; it does not truncate.** Images were listed by folder with the first six
    shown and "…and 6 more" under them — so a folder somebody had organised was a wall they could
    not get through. A folder is now a line you click (`isoGroupsShown`): the top folder open,
    subfolders shut, one button for all of them (`groupMap`), and **nothing held back inside a
    folder you opened yourself** — an "…and more" under a click that asked for exactly that is a
    worse answer than a long list.

67. **A machine you are watching gets a tab of its own, at the size of the pane.** The screen was
    first drawn inside the machine's card, and a 640x480 installer in a card is a *picture of* a
    computer rather than a computer you can use — the reported shape of it was "a tiny little
    screen", and the buttons around it were the only way to do anything. So the picture is not in
    the card at all: pressing Start opens the machine's screen as its own tab (`watchMachine`,
    `MachineScreenTab`), the card's button opens the same tab, and the tab is what "watching"
    means — closing it closes the watch. It is put in the same bar as an app's output, by the same
    registry (`shellTabs`), because a guest is the third thing here that has output worth a tab.
    The picture keeps its own aspect, scales to the pane, and the input half
    (`POST /machines/{name}/screen/input`) is unchanged by any of it: a click lands where it landed.
68. **A stop is almost always followed by a start, so a stop waits for the file to be free.** A
    restart is what moving a screen between the panel and a window *is*, and it arrives at QEMU's
    serial chardev a moment after the previous QEMU was killed. QEMU takes the guest's console file
    hard enough that a second open fails, and a failed open is fatal — `could not connect serial
    device to character backend 'file:console.log'`, then QEMU exits before the guest exists.
    Measured on a real machine: the move out to a window died on its first try and the headless
    fallback boot 1.5 s later succeeded with the same console line, so the race was the whole bug —
    and the person watching saw a machine that had crashed. `stop()` now waits, bounded, for the
    process to be gone, `boot()` waits on the console file itself (`wait_for_console`) rather than
    napping, and one more attempt is made only when QEMU's own words say the file was held
    (`console_was_held`) — never for a missing image, where the second answer would be the same.
69. **A fetch is written down where the machine can find it again.** Turning an image URL into a
    local file is a job — hundreds of megabytes, minutes — and it ends by putting the file's path
    on the spec *in memory*. That was where it stopped, so the file on disk still said "this
    machine points at a URL", and **every later start decided there was something to fetch and ran
    the whole job again**: measured on a real machine, pressing Start on a finished ISO machine
    re-provisioned it (the job's own log line: "image already here (61 MB), not fetching it
    twice") instead of booting it, and a screen move — which is a stop and a start — landed inside
    that job instead of restarting the guest. `_provision_qemu` saves the spec once the image is
    resolved, so the URL is kept deliberately (a machine rebuilt from this spec where there is no
    network still finds the file beside it) and the path is what stops the second fetch. The rule
    is the general one: **a fact a job discovers is written down, or the next run discovers it
    again.** `t_move.py` checks the save, the URL that survives it, and that a second prepare then
    reuses the file with no fetcher at all.
70. **A picture that has to have focus to hear you is not a computer you can use.** The guest's
    picture was the only thing listening for keys, so it needed focus — and in a panel focus moves.
    The reported shape of it was "I had to click into the window before every key I pressed", which
    is an installer asking for a hostname, a password and a username one click per letter. So the
    **tab** listens on the window for as long as it is open (`onWindowKey`), and the picture keeps
    its own handler only for the combinations a window listener must not take (`Ctrl+Alt+F2`,
    `Alt+F2`) — it marks the event it has sent (`e.__athenaSent`) so one keypress is one keypress,
    not two. The guards are the point, not decoration: a key aimed at a field of this app
    (`closest('input,textarea,select,[contenteditable="true"]')`) stays in that field, and
    `Ctrl`/`Alt`/`Meta` shortcuts still belong to the app. Wording had to change with it — the
    panel said "whenever this picture has focus", which is a sentence about the bug.
71. **A pane that owns a keyboard must claim the scope Hermes respects, or the chat keeps taking
    it back.** Hermes' composer has a reflex called type-to-focus: a printable key that lands on
    ordinary chrome focuses the chat composer and types *there*. That is the whole bug the user
    reported in one sentence — "I have to click into the vm window for every key I need to press
    and then it automatically resets into our session for me to type over here" — and it is why a
    click bought exactly one key: the click moved focus to the picture, the app's next keypress
    found the picture to be ordinary chrome, and the composer won. The app documents the way out
    in its own remote-screen pane: focus inside a `[data-terminal]` scope keeps its keys
    (`composerFocusBlockedBySurface()` stands the reflex down for `isFocusWithin('[data-terminal]')`).
    So the guest's picture and its host both carry `data-terminal` **and** `data-remote-screen` (the
    sibling marker that tells the close-tab router this is not a local terminal), and opening a
    machine's tab puts focus in that scope without a click — guarded, so it cannot pull the caret
    out of a draft the user is writing. The window listener added in 0.30.1 is the belt to that
    brace: it acts only on a key that is *this picture's business* (aimed at it, or pressed while
    it holds the keyboard), because a letter typed into the chat with the tab open must not be
    typed into the guest as well. And a screen moved into a window of its own is no longer polled
    at all (`screenLive` requires `display === 'panel'`) — reading a frame over VNC every 1.2 s for
    a picture that is not in this pane was waste with keys attached.
72. **A marker on one event is not a marker on the other.** Plugin panes render in the app's own
    React singleton, so the picture's `onKeyDown` is handed a *synthetic* event while the tab's
    `window.addEventListener('keydown')` sees the *native* one beneath it. Marking the event the
    handler was given — `e.__athenaSent = true` — therefore marked an object the other listener
    never sees, and every key was sent twice: measured by the user, typing `zack` produced
    `zzaacckk`. The guard is now on both (`if (e.nativeEvent) e.nativeEvent.__athenaSent = true`)
    **and** on the target (`classList.contains('ah-screenimg')` — a key whose target is the picture
    has already been sent by the picture), which does not depend on either event object surviving.
    The general law: when two listeners see one user action through different wrappers, dedupe on
    something both can name — the native target — not on a property you set on the wrapper you were
    handed. The driver checks it by firing both handlers at one keypress and counting the requests.
73. **A machine that has just installed an operating system must boot it.** The failure this
    prevents was reported from a real install: omarchy finished, said "reboot now", and the guest
    came back to the installer's own boot menu — because the machine was started `-boot order=d`,
    the medium first. To the person watching, a finished install had vanished. The order is now
    the disk first with the medium as the fallback (`order=cd`), so a machine with nothing on its
    disk still reaches its installer while a finished one starts its own system; `boot:
    "medium"` (`order=dc`) is there for a live or rescue image, where booting the CD-ROM *is* the
    machine's purpose. Two things about that flag were learned the hard way: the drive letters
    concatenate — `-boot order=c,d` is a **syntax error** ("Expected '=' after parameter 'd'",
    measured on QEMU 10.2 here), the comma separates parameters and not devices — and a value
    that QEMU rejects is a machine that will not start at all, so the argument is built by one
    pure function (`boot_args`) that a test can read. `POST /machines/{name}/boot` changes it and
    restarts a running machine, because where a machine starts is decided on the command line;
    the tab offers the other way in words ("Start on the installer" / "Start on its own system")
    and says which way it currently starts, so the default is never a secret.
74. **An installer that has installed something is not an installer any more.** Asking the
    question was not enough: the machine has to make the change itself. "It should replace the
    machine with the installed version after it's installed automatically" is implemented as one
    reading and one decision, taken where QEMU's command line is built — the first start after the
    install finishes writes `boot: "system"` on the spec and drops `-cdrom` entirely, so the
    machine that comes up is the system on its disk and there is no ISO left to win a boot. The
    reading is `disk_written_bytes()`: a qcow2 grows as it is written to, and measurements here
    (2026-10-14) put a never-started 40 GB disk at 193 KiB, a booted live session at a few MiB, and
    omarchy's finished install at 5.89 GiB — so `INSTALLED_BYTES` is 256 MiB, above "still empty"
    and below anything a person would call installed. Two things keep that guess from being a trap:
    it is **derived from the file every time and never stored** (a spec that said `installed: true`
    would still say it after the disk was deleted), and **an explicit choice is never overruled** —
    a spec that already says `boot: "medium"` is left alone, however much is on the disk, because
    a live or rescue image is exactly the machine where booting the medium is the point. The way
    back is visible rather than remembered: the row carries `installed` and `medium_attached`, the
    tab says "it holds an installed system, and it starts on that" with a button that reads
    "Attach the installer again", and the sentence the start returns says what was adopted and why.
75. **The guest answers far faster than the panel used to ask.** Reported as "the lag on input
    to the VM is way too high — when I type it's taking a while for letters to pop up", and it was
    not the guest: measured on this host, 2026-10-14, a keystroke leaves in **20 ms**, the guest
    has drawn its answer in **139 ms**, and one frame costs **30 ms** to read. The panel was the
    slow part, asking for a picture every 1.2 s and every 2.5 s once the screen had gone quiet —
    a fixed habit of its own, unrelated to whether anything had just happened. Measured end to
    end, the same keystroke appeared **1313 ms** later under the old cadence and **197 ms** later
    under the new one. So the cadence follows the person first: every event sent to the guest
    schedules the next look itself (`INPUT_ECHO_MS` 80 ms after the event, `INPUT_FRAME_MS` 180 ms
    while the burst lasts — just above the guest's own echo, so the picture is never read twice
    for one character), and only then does the guest's behaviour decide (`MOVING_FRAME_MS` 1200,
    `STILL_FRAME_MS` 2500 once two frames are byte-identical). The burst has to end on its own or
    a quiet machine would be read five times a second forever — which is why it is a window and
    not a mode.
76. **Two buttons that promise different things must do different things.** The panel has
    offered "Forget it (keeps the distro)" and "Destroy it and its disk" since the list existed,
    and for a QEMU machine the code did the same destructive thing behind both: every path ended
    in `shutil.rmtree(folder)`, and a QEMU distro *is* the file in that folder — so a button that
    promised to keep your distro deleted it. For WSL the labels were true by accident (the distro
    is registered with WSL, not stored in the folder). Forgetting is now what it says: the
    machine's folder moves to `forgotten/<name>`, the disk travels with it, the machine leaves the
    list, and `POST /machines/forgotten/{name}/restore` (or the "Bring it back" button on the row
    that says how much was kept) moves the whole folder back — nothing is copied, and the machine
    is the machine it was. `unregister=1` is the only path that removes files, which is what
    "destroy" means. The panel's confirmations now say which is which ("Nothing is deleted" versus
    "This is the one that deletes"), because a destructive button and a reversible one that look
    alike are a trap regardless of which way round they are.
77. **A route is a method and a path — check both.** Reported as "tried to destroy it and got an
    error", and the error was `405 Method Not Allowed`: the panel's verb helper `act()` always
    sends POST, while the machine's destructive verb was declared only as
    `DELETE /machines/{name}`. So "Destroy it and its disk" had never worked — it could not
    delete anything, which is also why the machine was still there, disk and all. The paths were
    checked and the methods were not, so the mismatch was invisible: `probe`/`t_*` confirmed every
    URL the panel names existed, and every URL did. Two outcomes, two routes, both POST:
    `/machines/{name}/forget` and `/machines/{name}/destroy`, with the DELETE left for callers
    that speak HTTP verbs (`unregister=1` is still honoured). `t_move.py` now scans every literal
    call in the built panel — `ctx.rest(...)` with its own method, `act(...)`/`actBusy(...)` as
    POST — resolves each against the router as a *(method, path)* pair with parameters matched,
    and fails on any call whose route exists but does not answer that method. It is checked with a
    positive control (a POST to `/machines/{name}` must still be a mismatch), because a check that
    cannot fail is not a check.
78. **A ref is a name in one scope — put it where it is used.** Reported as "i clicked make it and
    got the failed to render message and athena stopped working", and the error the workspace
    showed was `ReferenceError: liveRef is not defined` at the tab's read loop. The picture's
    `kick` moved four names — `lastInput`, `timerRef`, `liveRef`, `tickRef` — that the tab's read
    loop had to own, and they had been declared in the picture instead. Moving the *name* into the
    function that uses it is the fix; declaring it twice is not. This cost the whole pane: a
    component that throws while mounting is not a tab that is broken, it is the workspace failing
    to render, which is what made it read as "Athena stopped working". The picture now takes `kick`
    as a prop, and the tab hands it down.
79. **Presence is not resolution, so run the component.** Every check in `probe_tabs.mjs` passed
    while this was live, because each of them asked whether a string occurred in the built file —
    and every string did. `probe_tabs.mjs` now resolves each `X.current` use against the scopes that
    enclose it (a name that is also a plain local somewhere is left alone, or `busy` — a `useRef` in
    one function and `const busy = o.busy || ''` in another — makes it cry wolf), and
    `uitest/driver.mjs` mounts `MachineScreenTab`, runs its effects, and fails if mounting throws.
    A check that cannot fail is not a check: both carry a positive control built from the shape
    that actually crashed.
80. **The first field anyone meets has to look like one.** Reported: "making it a little more
    obvious where the user is supposed to type in the name of the machine. it's just greyed out
    really and took even me a minute to figure out why i couldn't click make it." It had no label,
    its hint read like a value already in the box, and the dead `Make it` said nothing. So the row
    is labelled (`Name`, `Kind`), the hint is an instruction (`type a name — like linux-box`), the
    empty field draws a dashed slot that turns solid once it has something in it, the cursor starts
    in it, Enter submits, and a disabled `Make it` carries `give it a name first`. The row is a pure
    function (`newMachineRow`), so the driver draws all of that rather than trusting the source.

81. **A held connection is worth more than a cheaper one.** Reported: "the input in the
    virtual machine was still a little laggy. & just laggy in general." Both halves were the
    client, not the guest. Every frame *and every keystroke* opened its own RFB session — a full
    handshake per letter — and every frame was a whole-screen transfer, decoded a row at a time in
    Python and re-encoded as a PNG, most of it pixels that had not changed; and because QEMU's VNC
    server is `share=off`, the frame reader and the input sender raced the single slot. So
    `dashboard/screen.py` keeps a `ScreenSession`: one connection per machine, held, asking for
    only what changed after the first full frame, sending input down the socket that is already
    open, and handing back the last PNG untouched when no rectangle arrived — which is also the
    signal the tab already used to slow down. The registry lives beside the class (`session_for`,
    `reap_sessions`, `close_session`) instead of in the route file, so the suite can make one,
    watch it, and let it go in milliseconds: `t_screen.py` holds one connection open across many
    frames and keystrokes and counts the handshakes (one), the re-encodes (one across three polls
    of a still screen), and ends a session whose guest changes resolution rather than blitting
    outside its buffer.
82. **A flag is a request; ask the binary.** `-device usb-tablet` was in the command line, three
    checks read it, and all three passed — while QEMU refused the line and exited instantly:
    `-device usb-tablet: No 'usb-bus' bus found for device 'usb-tablet'`, because this machine
    type (`-machine q35`) has no USB controller until `-usb` asks for one. A test that reads a
    command line checks the intention, not the acceptance. `t_machines.py` now starts the real
    line and fails if QEMU refuses it, with QEMU's own words as the reason, and its positive
    control is that line without `-usb` — which fails while every string check stays green.
    (Driving QEMU's monitor over a pipe was the first instinct and does not work: `info usb` and
    `quit` on its stdin left it hanging. Whether it starts is the question that answers itself.)
83. **An absolute pointer needs an absolute device.** Reported: "once i got into omarchy i have no
    mouse passthrough so i cant move the mouse on the vm desktop." With `-vnc` and no pointing
    device, QEMU emulates a PS/2 mouse and has to turn VNC's absolute pointer position into
    relative motion — which only works while the guest draws a cursor the emulation can push
    around, and a Wayland guest draws none, so the pointer arrives and never moves.
    `-usb -device usb-tablet` gives the guest an absolute device and libinput reads it 1:1; it is
    what every remote console does, and VirtualBox spells the same thought `--mouse usbtablet`.
    There was nothing here worth reverse-engineering: the lesson is one flag, and it is public.

84. **"A while" is not a number; the panel is the only thing that can count it.** Reported: "athena
    took a while to start up and show apps again on restarting hermes." It is true, and it is one
    thing: the engine is a child of Hermes' own process, so a restart leaves nothing serving and the
    panel cold-boots one. Measured from this machine's own logs rather than guessed — plugin mounted
    22:21:16, engine spawned 22:21:19, `Server listening` ≈22:21:48: **~30s on a home with apps
    installed** against 5.0s for an empty one booted for comparison, while the plugin's own work is
    0.44s to import, 0.14s for `/status`, 0.09s for `/apps` — and the app list already draws from
    disk without waiting for any of it. The fix asked for was to stop leaving the person guessing,
    so `/status` carries `starting_ms` (measured from the spawn when the engine is ours, from the
    first poll that caught it coming up when it is not, and `null` when there is nothing honest to
    measure) and the header reads `starting… 14s`. Two rules came out of it: a number the process
    did not measure is not invented, and `t_runtime.py` drives the silent-port case so the field is
    checked where a real boot is not available.

85. **A passthrough you cannot leave is a trap.** Asked: "make sure if i have mouse passthrough to
    control the VM that i have a way to get out of it also and go back to my regular desktop
    mouse." The mouse half was already true and nothing said so: the picture never locks the
    pointer, so moving off it is enough, and `probe_tabs.mjs` now fails if `requestPointerLock` or
    a pointer lock ever appears — scoped to `GuestScreen()` for `setPointerCapture`, because the
    one other use in the plugin is the system tab's splitter grip, which is a drag handle and not
    a screen. The keyboard half was the real gap: the tab holds the keyboard while it is open
    (that is what makes typing work with no click in front of every letter), and nothing named a
    way back. So three ways out, none depending on the other: **Ctrl+Alt on its own**, a
    **Release keyboard** button beside the note, and clicking anywhere outside the picture. The
    gesture is two-step on purpose — it fires on the way *up*, when both modifiers are off and
    no third key arrived in between, so `Ctrl+Alt+F2` still reaches the guest — and the rule is a
    pure function (`keyOwnerStep`) checked in the driver. One more trap closed with it: a drag
    that ends outside the picture never delivers the mouseup the guest is waiting for, leaving the
    guest holding a button while the person's mouse is back on their desktop, so the window's own
    `mouseup` sends the release.

86. **A held connection has to be told when the thing it holds has changed — and asking inside your
    own picture can never tell you.** Reported: "the aspect ratio is all wrong on my vm now with no
    way to change it or fix it." Two measurements settled it, and neither was a reading of a
    screenshot. First: the picture in the panel is drawn at the guest's own ratio (a 720x400 frame
    landed at 1161x645 on screen — 1.800 exactly), so the viewer was not the thing squashing it.
    Second, and this is the bug: the panel keeps **one** VNC connection per watched machine
    (v0.37.0), and a session that opened while the guest was still at its firmware's screen kept
    asking for updates *inside those bounds*. QEMU answers a clipped region with clipped pixels, so
    the session never found out. Measured on a real guest: a session opened at 640x480 was still
    640x480 a hundred and fifty seconds later, one connection, `alive=True`, while a fresh read of
    the same machine said 1280x800 — a permanent picture of the first second of a boot, at the
    wrong ratio, with nothing in the panel able to correct it because nothing was broken about the
    panel. The fix has three parts, each checked separately: **ask to be told** (list the
    `DesktopSize` encoding, `-223`, and act on the rectangle it arrives as — the case a clipping
    server hides completely), **ask over the guest's whole possible screen rather than over our own
    picture of it** (a server clips a region to its framebuffer, so asking wide is free, and a guest
    that outgrew the session then answers too wide for the canvas, which is a signal instead of
    silence), and **make a new canvas of the new size and ask for a whole frame** rather than
    reusing one. Proven both ways: the two cases are unit tests against a fake server that
    announces a resize the RFB way, and the same live probe that reproduced the stale session
    (`640x480` for 150s) now follows `640x480 -> 720x400 -> 1280x800` on one connection with zero
    disagreement against a fresh read. `ScreenSession.resizes` is the counter a test reads.
    A related trap that this investigation killed before it shipped: the guest's screen size looks
    like a setting to expose (`-global VGA.edid=on -global VGA.xres=... -global VGA.yres=...`), and
    a machine started with `1280x800` *stated* came up at **640x384** and stayed there for the two
    minutes it was watched, where the same machine with nothing stated reached 1280x800 by itself in
    25 seconds. QEMU's `xres`/`yres` are the adapter's *initial* mode, not a hint to the guest, so
    stating one is not a bigger screen — it is a different machine, sometimes one that does not get
    as far. So there is no size setting here: what the guest is at is the guest's business, the
    panel follows it, and it says the size it is showing.

87. **A read that is already inside a message waits for progress, not for the clock.** Reported
    alongside the aspect-ratio fault: "the keyboard is still laggy", plus a black flash between
    two pictures of the same screen. Measured: one socket timeout was doing two unrelated jobs —
    noticing that a still screen has nothing to say, and giving up inside a rectangle that was
    still arriving — and giving up at the second job is not a slow picture, it is a message
    abandoned half-read. A guest mid-boot can pause for seconds inside a large rectangle; the
    read raised `the guest's screen stopped answering (23/76800 bytes)`, the session was marked
    dead, and the next poll threw away a working connection and paid for a whole new handshake.
    The bytes the guest still owed were also still on the wire, so re-requesting a frame after
    abandoning one would have read pixels as a header — which is why the fix is to keep waiting
    for the bytes that message owes rather than to retry the request. Every read after the first
    byte of a message is `patient`: any byte at all restarts the countdown, and only
    `READ_PATIENCE_SECONDS` (20 s) with nothing arriving counts as gone. The connection's own
    short timeout still governs the first byte, where nothing has been read and stopping costs
    nothing. Proven both ways: the fake server stalls mid-rectangle now
    (`FakeVnc(stall=(seconds, bytes))`), and against the pre-fix code the same check reproduces
    `died="the guest's screen stopped answering (4/32 bytes)"` where the fixed code keeps the
    connection *and* the frame.

88. **Connecting to a machine is not the session registry's business.** Found by audit, then
    measured before it was believed: `session_for` connected *inside* the lock that guards the
    registry, and a handshake is up to eight seconds of a guest's own time (`_ask_for_a_frame`
    retries while a booting guest keeps changing size). So a VNC port that accepted and then said
    nothing froze `/screen` **and** `/screen/input` for every machine on the panel — one sick
    guest presenting as a broken plugin. Measured with a silent listener and a second, healthy
    machine: **4.51 s** to get the second machine's screen before, effectively instant after. The
    handshake now runs outside the lock and the connection is stored under it with a
    double-check; if somebody else finished connecting first, this one closes its own socket
    rather than leaving a second VNC client on the guest.

89. **A combination is sent whole, or it is reported — never sent with a piece missing.**
    Reported: "Omarchy keyboard shortcuts act on Hermes instead of the guest." Omarchy's
    shortcuts live on Super, and Super never arrived: the window listener dropped every modified
    key outright, and the picture's own handler tested `ctrlKey || altKey` only, so `Super+T`
    fell through to a bare `t` — a *different* keystroke, which the guest acts on. The keysym
    matters as much as the forwarding: the guest key for the Windows/Command key is `Super`
    (0xFFEB), not `Meta` (0xFFE7), and a client that sends `Meta_L` has sent a key nothing on
    that desktop is listening for. Modifiers are now mapped rather than filtered — every
    spelling a caller may use (`ctrl`, `win`, `cmd`, `mod4`, `super`) reaches the same keysym —
    and the combination is sent **whole or not at all**: a modifier that cannot be mapped
    refuses that keystroke and names itself, because `Super+Enter` arriving as a bare `Enter` is
    a line submitted. `messages_for` grew a third value for exactly this: what could not be sent.
    The route passes it on as `dropped`, and the panel says it out loud rather than reporting
    `ok` over a keystroke the guest never got.

90. **A pending look is never pushed further away than it already is.** Reported: "keyboard
    input still laggy." Each keystroke cleared the reader's pending timer and re-armed it from
    the clock, so a person typing faster than `INPUT_ECHO_MS` (80 ms) cleared it before it could
    ever fire: `tick` — the only thing that reads a frame — did not run at all until they
    stopped, and the picture went stale exactly while they were looking at it. Input never needs
    to move a look *later*; it needs one *sooner*, and the earliest look already asked for is
    the one that matters. `kick` keeps the deadline and `kickDelay` answers `null` when a look
    is already due at least that soon. Pure, so the rule is testable without a machine.

91. **One batch of input is out at a time, and what arrives behind it keeps its order.** One
    keystroke used to be one HTTP request of its own, fired the moment the DOM event happened,
    so a fast typist had several in the air at once and the guest saw them in whatever order the
    replies came back. Now the second event waits: at most one request is in flight, everything
    the person does meanwhile joins a queue behind it, and the queue goes as a single request the
    moment that one lands. Nothing is dropped on the way, which matters most for the event that
    ends a drag — a button the guest is holding while the mouse is back on the desktop.

92. **The picture is compressed where the reader can still work.** The encode ran while
    holding the lock the reader thread takes to apply arriving rectangles, so on a large screen
    the reader was stopped for the length of a compression run and the frame a person was waiting
    for landed after the picture of the frame before it. What is taken under the lock is a
    snapshot — the pixels of this moment, copied once; the encode runs on the copy. A generation
    counter tells a picture that still describes the screen from one whose pixels have since been
    replaced: the first is cached, the second is handed over once and thrown away. Measured by
    asking for that same lock from inside the encode — if it can still be taken, the encode is not
    holding it.

93. **A live picture is not an archive copy of it.** Measured with Pillow here: a 1920x1080 screen
    of flat colour encodes in 11ms and travels as 61KB, and the *same size* showing anything
    photographic takes 105ms and 6.4MB — per frame, on the thread a person is waiting behind, and
    the panel asks several times a second. That asymmetry is the reported lag, and why it only
    sometimes showed: typing into a terminal was fine and typing into anything busy was not,
    because the echo waits for the picture and the picture waits for a lossless encoder with 6MB to
    write. A remote console is a look at a live thing, not a copy of it, so the live picture is
    JPEG (`screen._JPEG_QUALITY`) and the payload says what it is (`image` plus `mime`) instead of
    the panel assuming. `frame_png` stays for callers that want a still they can keep, and `save`
    writes `screen.jpg`: the name matches the bytes.

94. **The pointer follows the mouse; nothing is captured.** Reported: "the click-to-drag mouse
    thing only exists docked in Hermes; in a window of its own there is perfect mouse passthrough."
    The guest's pointer moved only while a host button was held, so hovering did nothing and the
    next click landed wherever the last drag had left it. A USB tablet reports an absolute
    position, so the guest's pointer can simply follow the host's — which is what passthrough
    means, and it needs no pointer lock to get there: 24ms of throttle is the whole policy, and
    moving off the picture is the whole escape, because nothing was ever taken. A right click is a
    right click (`onDown` sent `['left']` whatever was pressed), leaving the picture releases every
    button the guest was told about, and the wheel is a native `passive: false` listener, because
    React attaches `wheel` passively and its `preventDefault` was a promise the browser had already
    decided not to keep.

95. **A screen size is a hint to the guest, and one that works.** Reported: "even though the docked
    version is smaller it should launch with a 1080p resolution by default." It had been tried and
    reverted as broken, and the knob was the wrong one: `-global VGA.xres/yres` is an
    initialisation hint to the emulated *std VGA*, not a mode a Wayland guest takes. Measured
    against omarchy's own ISO on a throwaway disk, reading the size back out of the VNC handshake
    (`cache/scratch/res_probe.py`):

        default std VGA ............................ 1280x800 by 39s
        -global VGA.xres=1920/yres=1080 (reverted) .. 640x384, and stuck there
        -vga none -device virtio-vga,xres/yres ...... 1920x1080 by 37s

    So the hint comes from the device whose EDID a Linux guest reads, and `-vga none` turns the
    default card off rather than leaving the guest two of them. It is still only a hint — a guest
    that wants another mode sets one and keeps it — which is why this is a default and not a
    setting. A Windows guest is not asked at all: its installer has no virtio-gpu driver either,
    and a blank screen is not a resolution.

96. **A drive the panel draws as a shortcut is a drive the browser can open.** Reported: "the file
    browser tab. i can still only access athenas folders and the recycle bin on my computer". One
    predicate did it — `_inside_home` — while `/roots` had been advertising `drives` all along and
    the panel had been drawing them as buttons, so every click on `C:` navigated and then answered
    403. The drives were visible and unusable, which is worse than either. The predicate is now
    `_in_roots` against a list, and there are two lists because reading and writing are not the
    same act: **reads** reach Athena's home and every fixed disk (a person picking a file for an
    app is doing in this browser what they can already do in Explorer, and the agent's tool calls
    run with the same rights the person has); **writes** reach Athena's home and the person's own
    folder only. The asymmetry is the point: there is no confirmation step between a model's tool
    call and `os.remove`, and "browse my disk" was the request, not "let an app loose on it". A
    network share, a card reader and an optical drive are not in either list —
    `psutil.disk_partitions(all=False)` is the fixed set, and enumerating a sleeping share inside a
    path check is a hang waiting to happen.
97. **Widening the roots is only safe because the deny list grew with them.** A file browser a
    person drives is not the risk; a file browser a *model* drives, on a disk it can now enumerate,
    is. So `_deny_secret` names what must not be handed over wherever it sits: `~/.ssh` and
    `~/.aws`/`~/.gnupg`/`~/.kube`/`~/.docker`/`~/.config-gcloud`, browser stores by name on any
    disk (`Login Data`, `Cookies`, `Local State`, `logins.json`, `key*.db` — the pair that
    decrypts every saved password), `NTUSER.DAT`, the Windows hives
    (`\Windows\System32\config`), Athena's own `cache/HF_AUTH`, and — new, because this is the
    door it would come through — Hermes' `sessions/`, `memories/`, `config.yaml`, `settings.json`.
    Not the whole Hermes tree: this plugin's own source lives under it and a person browsing their
    machine should be able to read it. The refusal says which of the two scopes it hit.
98. **A refusal is a raised exception, and both doors have to see the same one.** `sandbox_dep` is
    the floor under HTTP: it applies **read** scope to every request so a new read route cannot
    forget it, and a route that changes something enforces write scope itself. That was the shape
    before; what was missing is that the *in-process* caller runs no dependency at all, so the tool
    layer now calls `files.enforce(paths)` and then `files.enforce(paths, write=True)` for the ops
    that change something — before dispatching, so a refused write has not already happened by the
    time the refusal is discovered. `enforce` raises `HTTPException(403, detail={...})` whose
    detail carries the sentence, the list of what *is* allowed and the reason; the tool layer
    recognises it by status code, not by matching a phrase, and hands the model all three.
99. **A refusal the caller reads as success is not a refusal.** Found while wiring the above: only
    the write ops went through the route's verdict, so `read`, `list`, `stat`, `search` and `grep`
    spread their route body straight into `_ok` — a read the deny list turned down came back as
    `success: true` carrying an `error` key nothing in the schema mentions, and an agent reading
    that would have concluded it had the file. Every op now reports the route's own verdict, and a
    body carrying `error` is marked `refused` as well as failed: to an agent, "not that path" and
    "that did not work" are different things to do next.
100. **`/watch` compares Athena's home, not the person's.** An empty path meant nothing and was
    read as `Path.home()`, so a caller asking about no folder in particular got a live answer about
    a folder it never named. An empty path is a caller saying nothing — defaulting it to the home
    this browser belongs to is the only reading that is not a guess about the person.
101. **An image boots as itself, so the format follows the file.** Both `-drive` lines said
    `format=qcow2` — right for the disk we make, wrong for every disk somebody picks: the menu
    offers `.vmdk`, `.vhdx`, `.vhd` and `.img`, and QEMU answers a `.vmdk` with `Invalid
    parameter 'qcow2'`, which names its own driver rather than the mistake. One map
    (`_DISK_FORMATS`) answers for both lines, `.img` is raw because a disk image means a
    byte-for-byte copy, and a suffix the map does not know leaves `format=` off so QEMU probes the
    file — guessing a driver from nothing is how a machine boots nothing.
102. **A stop that did not finish is not a stop.** `qemu_stop` killed, waited, and then reported
    `ok`/`stopped` regardless — and deleted the pidfile — so a guest that outlived its kill kept
    writing to its disk while the panel offered to snapshot it, and the next stop found nothing to
    stop. `ok` now means *stopped*: liveness is re-asked of the process table (`qemu_pids_for`,
    which matches QEMU's command line against the machine's own folder — never by name), a machine
    still up after the wait answers `ok: false` / `state: "stopping"` with its pid, and the pidfile
    is removed only once the process is really gone. A missing pidfile no longer means "stopped"
    either: the process table is what says so.
103. **A setting is a rule, not a form field.** `PATCH /machines/{name}` (and the `edit` op an
    agent calls) accepts any subset of cores, memory, disk, screen, boot, image, ports, ssh and
    notes; what a value may *be* is answered by one pure function, `machines.apply_patch`, so the
    panel's box and an agent's JSON cannot be told two different things about `cpus: 0`. A name,
    a provider and an OS are not settings: they are the shape of the machine, and changing one
    under a disk that already has a system on it describes a machine that does not exist. A disk
    may only grow — shrinking a filesystem from outside the guest is how a disk is destroyed —
    and it grows with `qemu-img resize`, from outside a running guest, because a resize under a
    live filesystem is a torn one. The answer keeps **saved** and **in effect** apart: a change
    that lands in QEMU's command line restarts a running machine rather than being stored as a
    number that quietly does nothing until the next boot, which is exactly what `set_display`
    and `set_boot` already did.

104. **The floor asks what a path *is*, not only where it lives.** The read scope reaches
    every fixed disk and the write scope includes your own folder — which contains `.ssh`, your
    browser's profile and every `.env` you have ever written. Scope alone would therefore hand
    over a private key, so `enforce()` asks `_secret_reason` as well and a credential store is
    refused wherever it sits. The routes keep their own `_deny_secret` calls, because they carry
    the wording the panel already draws; nothing depends on them any more. A refusal says both
    where the path is and what was refused, because "you cannot read that" without the reason is
    a sentence a person cannot act on.
105. **A refusal that names the wrong thing is a bug.** `trash-restore` resolves its destination
    from the bin's own record rather than from the request, so that record is what has to pass
    the gate: a file deleted from a folder Athena does not write to is refused, and the refusal
    says where it came from and that Windows' own Recycle Bin will put it back. Reading the item
    happens *first*, because the check dereferences it — asking about an id that does not exist
    used to raise on `None` and answer 500 where the honest answer is 404.
106. **A manifest is read whole or not parsed.** `package.json` was read through an 8 KB window
    and then handed to `json.loads`; past 8 KB the text is half a document, and half a JSON
    document is a `JSONDecodeError`, not a smaller manifest. That exception came out of the
    build route: no scaffold, no launcher, and a traceback where a sentence belongs. Manifests
    are now read up to `JSON_MANIFEST_LIMIT` and parsed through `_parse_json`, which reports what
    it could not read instead of raising — the same treatment every other input in that module
    already had.
107. **A reap that reaps half is a leak with a log line.** `taskkill /F /PID` takes the process
    that holds the port and leaves the app it spawned, which keeps both the port and the VRAM and
    answers the next start with "in use". The POSIX path already killed the whole session; the
    Windows path now passes `/T` and means the same thing.
108. **A job that is still running is not old.** The TTL sweep keyed on `ended or started`, so a
    job running longer than six hours was deleted *while it was running*: the panel's next poll
    answered "no such job", a finished-looking failure appeared that never happened, and the
    `_job_running` guard that stops a second install on the same target went with it. Long jobs
    are the normal case — a torch install, a model download — so only finished jobs are swept.
109. **An argument that is not forwarded is worse than no argument.** `publish_store.git()` took
    `env=` and dropped it, so every caller that built an environment — the GitHub token,
    `GIT_TERMINAL_PROMPT=0` — ran without it: a `git fetch` asking a terminal that is not there,
    and a private repository answering 404. A caller reads the call site and believes it.
110. **One question, one function.** Three resolvers answered "which folder is this app" and had
    drifted: `_app_folder_for` tried two spellings out of three, so a publish or an update asked
    for `foo` while the folder on disk was `foo.git` and was told the app was not installed.
    There is one candidate list now, and `_app_folder_for` *is* `_app_folder`.
111. **A cap that is applied after the work is not a cap.** The key scanner's first line was
    `sorted(folder.rglob("*"))`, and sorting forces the entire tree into memory before the file
    cap can apply — so `MAX_FILES = 1500` bounded the answer and not the work, and a virtualenv
    or a model cache beside the app (the normal shape of these folders) was walked, stat'ed and
    sorted in full to then throw nearly all of it away. The walk is a lazy generator now, prunes
    dependency folders instead of filtering them afterwards, and is stopped by the caller.

112. **A click is measured against the picture, not the box around it.** The guest's screen is
    drawn with `object-fit: contain`, so the element and the picture are two different
    rectangles: when the pane is a different shape from the guest, the image is centred in the
    box and the difference above, below or beside it is empty space. Dividing by the element's
    box then read every click as further down the screen than it was, by exactly the size of
    that margin — a click on a menu item landed on the item below it — so the drawn
    sub-rectangle is worked out first and the division happens against it. The box is also
    sized to the picture (`width: auto; max-width: 100%`), because two ways of describing the
    same rectangle is how they drift apart.
113. **A search box is one question per pause, and only the newest answer is drawn.** Every
    keystroke in the store's search fired its own `/catalog` request with a 90-second timeout,
    and the replies arrived in whatever order the store finished them — so the list under the
    box was whichever answer happened to land last, which is not the word you typed. The
    keystroke now arms a timer and the request carries the sequence number it belongs to; a
    reply from a question that has been superseded is dropped rather than drawn. A tag click
    or a sort change cancels anything still being typed, because it is a newer question.
114. **A port that is taken is named.** QEMU answers a busy forwarded port with a line in its
    own log and an immediate exit — `Could not set up host forwarding rule` — which names
    neither the port nor what to do, and on a first boot reads as "this machine will not
    start". One reader produces the forwarded-port list for both the command line and that
    diagnosis, so what is checked cannot drift from what is opened, and the refusal names the
    port and says whether to close what holds it or change it in the machine's settings.
115. **A form that posts every field back overwrites what it never showed.** The settings form
    for a machine that already exists sends the fields that *differ*, and nothing else: a value
    nobody touched is not a value the panel should be re-asserting, and a form that sends the
    whole object is a form that can silently undo an edit made a moment earlier somewhere else.
    A blank number means "leave it as it is" rather than zero, ports are compared as pairs rather
    than as text (`8080:80, 9000:9000` and the same two on separate lines are the same machine),
    and a number no box can hold — a word, a fraction, a disk smaller than the one it already has
    — is refused here in words rather than sent off to be refused somewhere less useful.
116. **The panel and an agent are told the same sentence about the same machine.** Changing a
    machine is one pure `apply_patch` in the backend, and the panel is only a way of typing into
    it: the bounds live there, the form types numbers and reports what came back, and *saved*
    versus *in effect* stay two answers. The one thing a form can get wrong on its own is which
    text a port pair is, so there is exactly one reader for that question — the create form takes
    the pairs and drops what it cannot read, the settings form takes the same pairs and refuses
    the bad line by name, and both come out of the same function.

117. **A stage's own verdict is the job's state.** A job that keeps running until something inside
    it has finished cannot be honest on its own: "done" only means the call returned. A job whose
    *answer* is a failure is deliberately still `done` — the panel reads the answer — but a stage
    whose failure means nothing happened at all (a snapshot with no disk to copy, an export that
    could not write its file) hands its verdict over through `_job_verdict`, which ends the job
    failed *and* keeps the answer. The alternative is a finished snapshot that was never taken,
    with the reason visible only in a log nobody was reading.
118. **A tab that only exists while something runs cannot be where the running ends.** The
    Terminal is drawn while there is an app to watch or a job to see, so ending a job and landing
    on it leaves the panel on a tab that is no longer in the bar. "Update all" goes back to the
    shelf it was started from, and the per-app flows clear the app as well as the job on the way
    out — otherwise the tab outlives its reason and shows the log of an app nobody asked to watch.
119. **Nothing is polled until it has a name.** A job that has been asked for and not yet created
    is not a job: the panel used to point the Terminal at an id that did not exist and read
    `/jobs/null` for as long as the request took. While the request is in flight the tab says
    what it is waiting for, and the poll starts when there is something to poll.
120. **An install is not finished when the clone lands.** Installing from a URL clones a
    repository and *then* builds it, and reading the route's `ok` as "installed" is a sentence
    about work that has not happened yet. The route answers with a job, the panel watches it, and
    the sentence comes from what the job did — a failure stays where the output is, because that
    is the only place the reason is.
121. **A button that deletes copies, or spends the space it just saved, asks first — and says how
    much.** "Free unshared" deletes stored copies and "Restore independent copies" uses the disk
    back up, and both are one click away from a table of numbers the reader is looking at. The
    confirmation carries the figure, because a confirmation without one is a question the reader
    cannot answer.

122. **A question asked on a timer is not a question to walk the disk for.** The panel reads
    `/shared` every 20 seconds, and each fresh answer sums every app's private folders on disk — so
    the poll was paying for a disk scan on a schedule nobody chose. The report is kept for about one
    interval, says whether it was reused rather than pretending to be new, and is dropped by
    anything that moves files: a share pass that ended a moment ago must not be followed by the
    picture from before it, with apps drawn as still holding copies that just moved.
123. **A fact about the machine that costs a process to learn is worth keeping — for a while.** One
    answer about WSL is two `wsl.exe` launches, and the panel asks for the machine list every few
    seconds. It is cached for a short window and forgotten by every operation that changes a
    distribution (attach, import, unregister, terminate), *including a failed import* — a pass that
    timed out may still have landed, and a cached "that distribution does not exist" would then be
    the plugin arguing with WSL. The window is short enough that a distribution somebody installs by
    hand turns up on its own.

124. **A refusal names the field, not the server.** `machine.json` is a text file a person can
    open, and starting a machine builds a command line out of it — so `"memory_mb": "8GB"` used to
    raise inside the builder and come back as a 500 with a traceback in the log. Somebody who edited
    a text file was told the server broke. `spec_problem(spec)` asks the same questions first and
    answers with the field and the range it may be; *starting* is where it is asked, because
    starting is what builds the command line — a machine whose spec has a typo still has to be
    stoppable, and a snapshot reads the disk rather than the numbers.

125. **A word a tab asks for by name has to be in that tab's dictionary, and nothing else
    can tell you.** The Files and System tabs are two whole plugins' UIs folded into this one by
    `merge_ui.py`, each carrying its own dictionary, and the registration spreads one over the other
    (`{ ...AthenaFiles.EN, ...AthenaSystem.EN }`). Three ways that goes wrong, all silent: a key the
    code asks for and the dictionary lost renders the *key* on screen — `t('modified')` with no
    `modified` in the dictionary draws the word "modified", which reads like English and looks like a
    styling problem, which is exactly what the one real instance of this did until `t_i18n.py` was
    written; a key both tabs name with different words has the later dictionary's word used in both
    tabs, with neither dictionary looking wrong; and a word nobody uses hides the fact that a near
    neighbour is a misspelling. The suite reads the *built* panel for this, not the sources, because
    the splice is what produces the failure.
126. **A number the user can set must not be read in two places independently.** The port the engine
    listens on comes from `ATHENA_PORT`, then the settings file, then the default. The System tab
    read a constant `42001` instead, so a second Athena on any other port drew its app rows as though
    the engine were down while it was running perfectly well — a wrong answer that looks like an
    empty screen rather than an error. It cannot import `plugin_api.py` to borrow the answer (that
    module imports *this* one to mount the router, so the direction is a cycle), which is precisely
    why the second resolution exists and why `t_system.py` asks both modules every case and requires
    the same number back. Drift fails the suite rather than one tab.

127. **A promise nothing executes is a promise that has already broken.** The version in
    `plugin.yaml`, in `docs/catalog-entry.yaml` and in the changelog; the tool surface written down
    in three places; the licence inventory an audit rests on; the Hermes window the manifest
    claims — all of it was true by hand, and the day a tool was registered without being declared
    showed what "by hand" is worth: the plugin worked perfectly and the catalogue would have
    rejected it, and every other check in the suite stayed green. `tests/live/t_release.py` checks
    them out of the repository, without Hermes on the machine at all, so the half of the release
    gate that does not need a running agent fails in the terminal instead of on upload. It re-walks
    `engine/node_modules` and checks every number the inventory states — the package count, the
    licence table, the full list, the one package that declares no licence field — and the claim
    that not one of them is copyleft, which is the sentence a licence audit actually rests on. A
    same-name package at several versions (`debug@2.6.9` beside `debug@4.4.3`) is deliberately
    *not* compared line for line: the committed order there comes from a hash-ordered set and
    cannot be restated as a rule, and staleness is the failure worth catching.

128. **An upgrade is a settings file written by a version that no longer exists.** The settings
    file used to live in the plugin folder; it moved into Athena's own home so the plugin directory
    holds code and nothing else, and the old location is still read. Two rules make that a migration
    rather than a break: the old file is read first and the new one laid over it *key by key*, and a
    save merges onto what is already on disk instead of writing what its caller happened to be
    holding. Both had to be found the hard way. Reading the first readable file whole meant a new
    file holding only a port hid the old file's ISO folder — and it made `plugin_api.py` answer 42001
    while `system.py`, which cannot import it, read the same two files per key and answered the old
    file's 42700. Two resolvers disagreeing about which file wins is the same failure as two
    resolvers disagreeing about the port, one level down. `tests/live/t_upgrade.py` asks both the
    same questions and requires the same answers, including under a `None` that removes a key, an
    absent key that must be left alone, a key from a newer version that must survive, and a
    half-written file that must not take the rest with it.

129. **Two hands on one keyboard, and nothing that noticed.** A guest has one keyboard. A person
    clicking at the panel and an agent typing an installer's answers are two writers on it, and the
    guest acts on whatever arrives, in the order it arrives — which is how an agent's `Enter` lands
    after a person clicked something else, and the guest does something neither of them meant. This
    was not a race anyone could lose politely: nothing recorded who had the keyboard, so there was
    nothing to refuse. `dashboard/lease.py` is that record. A person's input always goes through and
    takes the hold; an agent's is refused **before anything is sent**, in the words
    `human_has_control`, naming the machine, how long the person has had it, when it frees itself,
    and both ways back — `force` on the tool, or the Release button on the screen. The hold lives on
    the *machine*, not on the screen connection: a socket is reaped after `SESSION_IDLE_SECONDS` and
    dies with the guest, and a person watching an installer must not lose the keyboard because a
    socket aged out. It renews on every keystroke rather than on the first one, so typing for two
    minutes does not hand the keyboard out at the ten-minute mark. A lapsed hold is nothing: the
    next agent call goes through rather than waiting on a person who walked away. `t_lease.py` tests
    the rules against an explicit clock and the wiring against a fake screen that counts what it was
    given — including the control that matters, that with no lease consulted the same keystroke does
    reach the guest, so "it was refused" is evidence rather than a return value nobody checks.

130. **A guard that refuses everything is not a guard, it is a broken feature — so the
    non-refusals get tested too.** The file browser's scope rule is easy to write and easy to
    get wrong in both directions. Written too loose, `~/.ssh` walks out through your own user
    folder (it is inside it) and `..` on the uninstall route resolves to the whole install.
    Written too tight, an app's own `.env` — which the rule about credentials *looks* like it
    should refuse, because filebox refuses every `.env` it sees — stops working, and the only
    way anyone finds out is a confusing failure much later. The decision here was to keep
    filebox's rule out of Athena: an installed app legitimately owns a `.env`, and "credential"
    is a question about what a path *is* (a store's directory, a private key, Hermes' own
    config), not about what it is *called*. `t_security.py` asserts both halves with the same
    care, including that an ordinary file in the person's own folder is neither called a
    credential nor refused — because a rule nobody checks the edges of is a rule that will be
    "fixed" into either failure by the next person who has a bad afternoon.

131. **A security test that has never seen its own bug is a test of nothing.** Every refusal in
    `t_security.py` was observed failing against a deliberately weakened guard before it was
    trusted: with `_in_roots` reduced to a string-prefix check (no realpath) and
    `_secret_reason` stubbed to return "", twelve checks went red — the `..` walk, the linked
    folder, the write through a link, `~/.ssh`, Athena's own token directory, the credential-store
    link, the app folder reached through a link, and Hermes' own config. Restored, 63/63. This is
    the same discipline as the lease's control case: without it, "it was refused" is a claim about
    a code path that may not have run. The mutation is cheap (two edits, one run, revert) and it
    is the only evidence that separates a guard from a comment.

132. **A rectangle is not a change.** The parked mouse-lag item had been unanswerable for two
    reasons — no target, and no way to tell a fix from a mood — and the mechanism turned out to
    be one line. `_read_update` treated "pixels arrived" as "the picture is new" and threw away
    the encoded frame for every rectangle a guest sent, so a pointer moving across a desktop the
    server re-sends as identical pixels cost a full re-encode each time: **8.4 ms and four
    megabytes per arriving rectangle at 1280×800, measured**, and 200 of them is 1.7 seconds of
    encoding for a screen that never moved. The fix compares each row against what is already on
    the canvas (a slice comparison, not an encode) and adds a fourth answer to the reader's
    vocabulary: `"same"` — pixels arrived and they were the pixels already here. `"idle"` means
    nothing came; `"same"` means something came and nothing changed; conflating them either
    re-encodes a still desktop or under-reports a guest that is talking. Two checks in
    `t_screen.py` and the table in [`docs/performance.md`](docs/performance.md).

133. **A number without a method is a rumour, and a missing number is not a zero.** The
    performance table is written by `tests/live/t_perf.py`, one row per measurement with the
    command that produced it in the last column. The rows that need a live guest, a cold engine
    or half an hour of running print `SKIPPED` with the exact command that would fill them, and
    the summary **counts them separately from the passes** — because a suite that reports
    "10 passed" when four of them were never attempted is the same lie as a green check on a
    code path that did not run. This is the reason the file exists at all: "the screen feels
    laggy" cannot be closed, and "8.4 ms per arriving rectangle, 1.7 s removed from 200 of them"
    can.

134. **A colour with no theme behind it is a colour nobody sees.** Three surfaces were written
    for a dark app — the screen viewer's letterbox, the "no screen yet" placeholder, the video
    backdrop — so a light-theme user got a black rectangle where a picture was arriving. The
    audit that followed found something worse: **45 uses of twelve variable names that nothing
    defines.** They were the retired standalone filebox and sysmon plugins' theme names, inherited
    into the merged panel where the host injects `--card`/`--foreground`/`--accent`/`--border`/
    `--muted-foreground` and nothing else. An undefined `var()` is not an error in CSS: `color`
    inherits by accident, `background` disappears, and `--ui-red` on the memory meter's "hot" fill
    meant the hottest state had no colour at all. Every one is now the app's variable with the old
    name as a fallback, the three surfaces are on `--card`, and `t_ui_theme.py` (10 checks, all
    mutation-verified) refuses both a bare literal and a name with nothing behind it. The lesson
    generalises past colour: **a name that resolves to nothing fails silently in whichever
    environment the author was not using.**

135. **200 is not success, and the panel has to know it.** The backend refuses with `ok: false` and
    a reason at status 200 — the honest taxonomy, since "the port is taken" and "the request was
    malformed" are different things a status code cannot both say — which means a panel that reads
    only the status code sees *success*: the button flashes, the card returns to "stopped", and
    nothing explains. An audit found **nine** places that did exactly that, and one of them was the
    screen's own input route, where `ok: false` is the control lease refusing a keystroke — so a
    person typed into a machine and the panel said nothing at all. The rule is now mechanical: no
    mutating call site may skip the check (a declared exception carries a reason), and the checker
    itself is *run* by Node against five shapes of answer, so what is asserted is the sentence a
    person sees rather than the presence of a helper. **A refusal nobody can read is the same as no
    refusal — it is an action that silently did not happen.**

136. **A 9,400-line module is a module nobody can hold in their head.** `dashboard/plugin_api.py`
    had become routes interleaved with the machinery behind them: the settings store, Node
    discovery, the engine supervisor, the credential stores, the capability runner — one file, in
    the order they were written. R9 takes them out one group at a time, and the first is the
    credential stores (165 lines) into `dashboard/credentials.py`. Two rules came out of doing it:

    * **Sibling modules are imported by name, never package-relative.** `plugin_api.py` is itself
      loaded by path (`spec_from_file_location`), so `from .credentials import x` resolves at
      import time and then fails at *call* time inside the plugin host — `attempted relative import
      with no known parent package`, found live on 2026-10-14. This is why the bare-name import is
      wrapped in a `try` here as it is everywhere else in `dashboard/`.
    * **The moved module takes what it needs as a parameter.** The extracted code used the
      module-level `engine = EngineManager()` singleton; it now receives the engine. A module that
      reaches for a global cannot be loaded, tested, or read without constructing one first, and
      the callers keep their old shapes through thin wrappers in `plugin_api.py` rather than a
      rename of every call site.
    * And because the import is guarded, the split is verified rather than assumed: loading the API
      and reading `athena_credentials is not None` is what proves the group moved instead of
      silently degrading to "credentials unavailable".

137. **A total that counts installed apps is a number about the machine, not about the code.**
    `t_agent_tools.py` and `t_names.py` each reported a total — and both totals included one check
    per app installed *in Athena on that machine*. Installing or removing an app silently moved a
    number the README quotes, and one fewer app read as a regression. Both harnesses now make one
    aggregated check per group (`every app row is complete (7 app(s))`) that still names the
    offending app in its detail. This is the same failure as the R7 numbers and the R8 scan: a
    count has to be about what the code does, or it stops meaning anything the moment it moves.

138. **An alias is a name for a function, not a seam.** When `plugin_api.py` handed its Node lookup to
    `node_runtime.py`, the old private names stayed as re-exports, so no call site changed. But
    `t_runtime.py` patched `api._find_node_uncached` to count how often the search behind a cached
    lookup actually runs — and `find_node` calls *its own module's* search. The patch landed on the
    alias, the real search ran unwatched, and the count read zero. A test patches the module that
    owns the function. The memo has the same shape of trap in reverse: `_NODE_MEMO` is re-exported as
    **the same dict object**, because a test that clears it has to clear the one the lookup consults.

139. **A probe that writes is a probe that edits the machine, so point it at a copy first.** While
    proving that the settings store kept its seam, I called `save_settings` against the *live*
    settings file for one line before redirecting the path — and that one line dropped a key from
    Zack's real `settings.json`, because a merge only keeps what it reads. The seam worked exactly
    as designed; the probe was the bug. Two rules follow. Redirect the path **before** the first
    mutating call, never after. And a probe whose paragraph is about reading should not save at all:
    if it must save, `tempfile.mkdtemp()` is one line away and costs nothing.

140. **Moving a module moves its seams, and a moved seam is what a harness fails on.** The engine
    supervisor (`engine.py`, 561 lines) left `plugin_api.py` and **five harnesses failed — none of them
    on behaviour**. `t_hardening.py` patched `api.subprocess` and `_reap_port` ran `engine.py`'s
    `subprocess`, so the taskkill capture stayed empty; `t_idle_users.py` and `t_runtime.py` grepped
    `plugin_api.py` for source that now lives next door; `t_runtime.py` built `api.EngineManager()`
    three times, and the class now takes its directories and settings from the module that owns them.
    Every one is rule 152 wearing different clothes. So when a class moves: **re-export its constants as
    the same objects** (`_PROBE_AT`, `_PROBE_LOCK`, `_STARTING_PROBE`), give callers a factory
    (`_new_engine()`) rather than making them spell out the new arguments, and grep the tests for every
    name the moved code used to share with the module it left.

141. **The engine supervisor could be lifted out because it was a class; the runner could not,
    because it was not.** After the credential stores and the supervisor came out of
    `plugin_api.py`, the next group was the capability runner — the routing question, the
    hand-over, and the chain that feeds one app's output to the next app's input. It is twelve
    functions rather than one class, and the only seam it had to the module it left was the set of
    globals it called: `_bring_up`, `_appcall_module`, `_idle_module`, `_job_add`, `_app_folder`,
    `_app_url`, `_runtime`, `seed_credentials`. There is nothing to re-export when the moved thing
    is a function and not a class — so instead of a factory the API hands the runner each of those
    names as an argument, resolved at call time. A patch on `plugin_api` is a patch the runner still
    reads, because the runner never reaches for the global; the API reaches for it and passes it in.
    The rule underneath is the one the settings paths already taught: **the module that owns a seam
    is the module a test patches, so keep the seam where the test expects to find it** — the split
    changed where the *code* lives, not where the *seam* lives. `dashboard/runner.py` came out at
    346 lines; `plugin_api.py` went from 8,504 to 8,291. The suite was green on the first run after
    the move, which is the whole point of the injection: a split that needs its harnesses rewritten
    to keep passing was a split that moved the seam.

142. **The working arc: two halves that never call each other agree on a name, and both are written
    for a failure that is silent.** Hermes Desktop draws a travelling arc around a working session's
    sidebar row; the panel now wears the same arc on the card an agent is using, so you can watch an
    agent work without reading a log. The agent side maps a tool to a `kind:name` key and broadcasts
    `plugin.athena.spot` **before** the tool runs — the arc is up while the work happens, not after it
    — and does it by wrapping the one `_tool()` registration site, so a new tool with an entry in
    `_SPOT_SUBJECT` is spotlighted with no second edit. The panel side puts `data-athena-spot` on each
    card root: a **space-separated list** of every name that card answers to, matched by
    `[data-athena-spot~="…"]`, so a card that answers to both an id and a name is one attribute and not
    two. Everything about it is shaped by the fact that **an arc that never draws is indistinguishable
    from an agent doing nothing**, so it cannot be allowed to fail loudly: `_spot_key` is total (it runs
    before *every* tool, so a key it cannot build is `""`, never an exception — a cosmetic feature must
    not break the tool it decorates), `_spot` swallows a broken broadcast, and "nothing named" selects
    nothing rather than the first card carrying the attribute. And because only one tab draws each kind
    of card, the card an agent named is usually **not mounted at all** — so the panel follows the agent
    to the tab that draws it, which is exactly what `screen.wanted` already does for a machine's
    console, and the wait for that tab is bounded so a card that never appears gives up quietly instead
    of leaving a timer running. `t_spot.py` (20 checks) holds both ends to that contract — the three
    card roots, the subscription, and the follow — because the pair has no type checker, and the drift
    would be invisible. A wrapper in the call path of all 24 tools needed its own proof too:
    `t_agent_tools.py` now calls a tool *through* that wrapper, since every previous check only
    established that a handler was callable and a wrapper that mangled its arguments would have taken
    the whole toolset down while the suite stayed green.

143. **A backtick in the panel's CSS ends the template literal it lives in.** `plugin.js.pre-merge`
    keeps its whole stylesheet inside a JS template literal, so naming things in a CSS comment —
    `arc-border`, `transform`, `rgb(0 0 0)` — terminates the string early. It did not present as
    "unbalanced backtick"; it appeared one step downstream as `uitest/athena.mjs: SyntaxError:
    Unexpected identifier 'arc'`, in a file nobody had edited. The general form is worth more than the
    instance: **when a generated file fails to parse, the bug is in the generator's input** — go and
    read the source the generator reads. A companion trap lives in the same file and the same guard:
    `t_ui_theme` refuses bare hex literals, so the arc's mask stencil is written `rgb(0 0 0)`.

## Credentials

Athena never asks for a key Hermes already has. The rule that governs what it may then *say*
about one is in [`docs/threat-model.md`](docs/threat-model.md) §2 — a scan lists the key names it
finds and where they are, and never a value — and `t_security.py` plants a real token, scans it,
and then searches its own output for the value, for any sixteen-character fragment of it, and for
any token-shaped run that survived.

| Source | Reaches Athena as |
| --- | --- |
| `gh` CLI / `auth.json` | `credentials/github.json`, `gitconfig` |
| Any existing local install | `cache/HF_AUTH/token` |
| Hermes provider config | provider API keys |

Verified on this machine: GitHub reports connected out of the box, and the HuggingFace token
is inherited with no second login.

**The engine's own flag asks a different question.** `GET /connect/status/huggingface` needs more
than a token on disk: the provider shells out to the `hf` CLI and looks for it inside *its own*
Miniforge (`<home>/bin/miniforge/Scripts/hf.exe`), which is where it keeps every tool it runs.
A home whose Miniforge was built without that package answers `false` while apps keep working —
they read the token from `HF_TOKEN_PATH` regardless. Installed once here:

    <home>/bin/miniforge/python.exe -m pip install "huggingface_hub[cli]"

after which the engine's real check agrees (`{"provider":"huggingface","connected":true}`).
If Miniforge is ever rebuilt, re-run that line; Athena's `/credentials` route is the source of
truth either way.

## Status

**1.0.1.** Athena is a plugin, so "1.0" is a promise about a surface, not about a
program: the engine, the panel, the CLI and the 24 agent tools are frozen, and the
module layout underneath them may move without changing a caller. What is stable,
what is explicitly out of scope, and what is *not yet proven* are each written
down — the definition of done is a table in `docs/release.md`, and the version
history is in `CHANGELOG.md`.

**Stable:** the 24 `athena_*` tools and the plugin's own routes; a settings file
written by an older version is still read, key by key; the catalogue is treated
strictly as a data source and re-drawn here.

**Out of scope for 1.0:** a sandbox (apps run as the user — stated, not
mitigated); macOS; other languages (English-only, dictionaries ready); the
legacy-launcher rename (the compatibility shim stays, reasoned in
`docs/NAMING.md`); telemetry of any kind.

**Not yet proven:** the store rehearsal (publish → install from the real store →
unpublish) and the Windows-and-Linux CI run — both need something this machine
cannot provide alone, and both are named with their reason in `docs/release.md`.

Everything below is verified end to end on this machine.

- **A watched machine's picture follows the guest, at the guest's own ratio.** A session opened
  while a guest was still at its firmware's screen used to stay that size for as long as the tab
  was open — measured: 640x480 for 150 seconds, one connection, `alive=True`, while a fresh read of
  the same machine said 1280x800. The panel was showing a squash of the first second of a boot and
  no control in it could correct that, because nothing in the panel was broken. A session now asks
  to be told when the guest resizes (`DesktopSize`, encoding `-223`), asks over the guest's whole
  possible screen instead of over its own picture of it, and makes a new canvas when the answer
  says the guest grew. Verified against a real guest: the same probe that reproduced the stale
  session now follows `640x480 -> 720x400 -> 1280x800` on one connection, with zero disagreement
  against a fresh read — and both ways a guest can grow (an announcement, or a wider picture) are
  unit tests against `t_screen.py`'s own VNC server.
- `hermes plugins doctor athena` reports OK — 24 tools registered, no warnings (its four
  dependency warnings are answered by upper bounds in `python_dependencies`) — and
  `hermes plugins validate` passes: all 24 tools,
  discovery/manifest/import/registration OK. (The validator is a step in `tests/live/run_all.sh`
  now — the day a tool was registered without being declared, every other check stayed green.)
- **The promises the plugin makes about itself are a test now, not a habit.** `t_release.py`
  (32 checks) re-derives the version from every file that carries one, the 24-tool surface from
  the manifest, the catalogue entry and the plugin's own `_tool(...)` calls, the release files,
  and the Hermes window: `requires_hermes` is `>=0.21.0,<0.23.0` in both files, and the suite
  fails the day the Hermes running here steps outside it. The third-party inventory is checked
  against a fresh walk of `engine/node_modules` — 501 packages, the licence table, the full list,
  the single package with no licence field, and that none of them is copyleft.
- **An upgrade keeps what the old version wrote, and the two resolvers agree about it.** The
  settings file moved out of the plugin folder into Athena's home and both are read; `t_upgrade.py`
  (13 checks) proves the old file is still read, that the new one wins *key by key* rather than
  wholesale, and that a save carries every key the old file held instead of writing only what its
  caller was holding. It also caught the drift: `plugin_api.py` read the first readable file whole,
  so a new file holding only a port hid the old file's `iso_dir` and made the engine answer 42001
  while `system.py` — which resolves the same two files per key, because it cannot import the other
  module — answered 42700. Both now test each candidate value and fall through, and the test asks
  for the same number back from both.
- **The screen now says who is driving it, and refuses the second hand.** `dashboard/lease.py`
  records who holds a guest's keyboard — a person at the panel, or an agent acting for one —
  and `t_lease.py` (65 checks) proves both halves: the rules against an explicit clock, and the
  wiring against a fake screen that counts what it was given. An agent's keystroke into a screen
  somebody is using is refused with `human_has_control` and **never reaches the guest**; the
  control in the same test shows that with no lease consulted the same keystroke *does* reach it,
  so that claim is measured rather than assumed. The dock draws the backend's answer under the
  keyboard line, with a `Take it back` button that posts `/machines/{name}/lease/release`, and an
  agent can ask (`op: "lease"`) before typing rather than discovering the refusal. The driver
  asserts the row, its button, the route it posts, and that a screen nobody holds draws no row.
- Engine boots headless, serves HTTP 200 on port 42001, home inside Hermes.
- Isolation holds: the real user-level engine config is byte-identical after a full run.
- Catalogue live: 4,289 apps indexed; browse, search, and repo resolution all return records.
- **A real store app installed and built.** `chatbot-ollama` resolved from a bare name to its
  repo, cloned into the app folder, ran its own `install.js`, and completed `npm ci` +
  `npm run build` — a full Next.js build with a `BUILD_ID`. That run is what proved the
  toolchain prerequisite: before Miniforge existed the same install cloned and built nothing.
- All 80 URLs the UI calls exist on the backend (113 routes declared, 35 reached through a
  computed path), including launch, stop, and the engine surviving a stop.
- **Start, actually starts.** A stopped machine's verb button was tried in the wrong order — a
  stop first, a start only if that failed — so pressing Start on a stopped machine stopped
  nothing, called it done, and left the machine exactly where it was, with a success line over
  it. The action now comes from the machine's state before the click. A machine that *can't*
  start says what is missing on its card, with the button that fixes it.
- **A machine you can watch, and use, in a tab of its own.** A guest's screen is *in* the panel,
  and at the size of the pane: pressing Start opens it as a tab named after the machine
  (`watchMachine`, `shellTabs`, `MachineScreenTab`), the same bar an app's output lives in, and the
  card's button opens the same tab rather than laying a 640x480 installer out in a card. The
  plugin reads QEMU's VNC framebuffer server-side (`dashboard/screen.py`) — over **one held
  connection per machine** (`ScreenSession`), asking for only what changed and handing back the
  same bytes when nothing did — and the tab draws a picture of it, refreshed while it is open
  (`GET /machines/{name}/screen`) at the pace of the guest —
  1.2 s while it changes, 2.5 s once two frames are byte-identical. Clicking the picture clicks the
  guest, typing reaches it, the wheel scrolls — pointer and key events go back over the same
  protocol (`POST /machines/{name}/screen/input`, `athena_machines op: screen|type`) — **with no
  click in front of every letter**: the picture claims the `[data-terminal]` keyboard scope Hermes
  itself uses for remote screens, so the app's type-to-focus reflex stops pulling the caret back
  into the chat, and opening the tab focuses the picture so typing begins without a click —
  while a field of this app keeps its own keys and `Ctrl`-shortcuts stay the app's. One keypress
  is one character, even though React's handler and the tab's own listener both see it. The same
  tab says where the machine starts and offers the other way (`POST /machines/{name}/boot`),
  because a finished install that reboots into its own installer reads as an install that was
  lost: the disk comes first and the medium is the fallback. And it is not a setting anyone has
  to find: the first start after an install finishes reads the disk file, adopts what is on it
  (`boot: system`, install queue emptied by dropping `-cdrom`), and says so in a sentence — with
  "Attach the installer again" one click away. Typing is also read back at the speed of the
  person doing it rather than a fixed 1.2/2.5 s habit: 1313 ms before, 197 ms after, measured on
  the same machine. "Open in a
  new window" is a restart into QEMU's SDL display and it comes back the same way; the reply names
  which happened, and a fallback is stated rather than silent. The guest's serial console
  (`GET /machines/{name}/console`) is one button away and polls every 2 s, so a boot looks like a
  boot: the loader, the installer, a panic, all inside the panel.
- **The screen stopped being a photograph.** From the same report ("the input in the virtual
  machine was still a little laggy. & just laggy in general"): the picture used to cost a full
  handshake per frame *and per keystroke*, a whole-screen transfer of mostly unchanged pixels, and
  a PNG re-encode on every tick. Now one connection is held per machine, later requests are
  incremental, a frame nobody changed is returned as the same bytes (exactly the signal the tab
  already used to choose 1.2 s over 2.5 s), and input travels down the socket that is already
  open. Held connections are let go when idle, replaced when dead, and a guest that changes
  resolution ends its session and says so instead of writing outside its buffer. `t_screen.py`
  checks all of that against a fake VNC server — 77 checks, in milliseconds, without booting
  anything.
- **A pointer that moves in the guest.** A machine's command line now carries an absolute pointing
  device (`-usb -device usb-tablet`), because VNC sends a position and a PS/2 emulation can only
  turn that into relative motion — which a Wayland desktop with no hardware cursor cannot follow.
  Reported from omarchy ("i have no mouse passthrough so i cant move the mouse on the vm
  desktop"), fixed as a flag, and checked as a flag QEMU accepts: the live check in
  `t_machines.py` starts the real command line, waits, and fails with QEMU's own words if it
  refuses. It caught exactly this — the tablet was in the argv, the string checks were green, and
  QEMU would have refused to start the machine at all.

- **A restart that used to look like a crash.** Moving a screen out to a window and back is a stop
  and a start, and the start raced the previous QEMU's hold on the guest's console file — QEMU
  exited with `could not connect serial device to character backend`, which is a machine that
  appears to have crashed. Measured live: the move out died on its first try and the fallback boot
  1.5 s later succeeded with the same console line. `stop()` now waits for the process to go,
  `boot()` waits on the console file itself (`wait_for_console`), and one more attempt is made
  only when QEMU says the file was held (`console_was_held`). Verified live both ways: out to a
  window (`display: "window"`, `display_fallback: null`, a real 656x519 SDL window owned by the
  QEMU process) and back into the panel (`state: "running"`, its screen read again).
- **A machine that actually executes.** `-cpu host` under WHPX froze the vCPU while QEMU stayed
  alive — a running process, a guest that never booted, and QEMU's "guest has not initialized the
  display (yet)" placeholder forever. `qemu_cpu_args()` now pairs accelerator and CPU model by
  measurement, and a real ISO boot reaches the installer's own keyboard prompt (1280x800) in the
  panel.
- **An image list you can open.** Images are grouped by folder with the top folder open and
  subfolders shut, one click each, and a button for all of them — nothing held back inside a
  folder you opened yourself.
- **The image folder survives everything else.** `t_agent_tools.py` (172 checks) now proves a save
  by somebody holding one key keeps every other key, and that a "go back to the inbox" is said as
  a removal rather than as silence. This one was caught live: a real settings file had lost the
  folder with eight images in it.
- **A weight pulled by name into a folder an app reads.** `hf_remote("hf-internal-testing/tiny-random-gpt2")`
  described the repository (file list, exact bytes, weights told apart from configs), then a pull of
  443 KB landed in a real library's `checkpoints/`; progress was counted from the folder, and a second
  pull into the same folder wrote **0 bytes** because the file was already there.
- **A crash read back to its cause, from the strings this machine really prints.** The triage
  reader names what a stopped app's log says: a CUDA OOM with the numbers torch printed
  (111.76 GiB asked of a 31.82 GiB card), a Windows port clash (`WinError 10048`), ComfyUI's own
  missing-weight sentence with the file and folder parsed out of it, a gated repository's 401, a
  missing package. Checked against real captured output, and against the one bug that mattered:
  PyTorch's `OutOfMemoryError` contains `MemoryError`, so a full card used to be read as a full
  RAM until the pattern was word-bounded — the test exists to keep it that way.
- **A machine, made and used for real.** Not a mock: `create` fetched the Ubuntu WSL root
  filesystem (340 MB), `wsl --import` created the distro, `start` booted it, and a command run
  inside it came back with the machine's own `uname` and core count — then a failed command was
  reported as a failure carrying its exit code, and a flood of output was cut in the middle
  rather than swallowed. The provisioning run is `tests/live/machine_provision_check.py`, which
  is deliberately **not** in the suite: a test that downloads 340 MB and creates a distro on
  whoever runs it is not a test, it is an install. What the suite does check is the part that
  can be proved without booting anything: the `wsl -l -v` parser against captured UTF-16LE
  output (a parser that reads it as text matches nothing, silently), the registry against a
  temporary home, and the three setup paths against the platform's real commands.
- **A guest's screen is a socket, not a port.** A QEMU guest's display used to be
  `-vnc 127.0.0.1:<port>`, described in the code as "loopback only — a guest display is not for
  the LAN". That is true and it guards the wrong door: what a loopback port is reachable by is
  every other program running as this user, and a guest's screen is a live feed of what that guest
  is doing, passwords typed into it included. It is a unix socket inside the machine's own folder
  now, given mode `0600` as soon as QEMU has created it, and the screen reader dials whichever
  spelling the machine was launched with — because a guest started before this change still has a
    port and still has a screen worth showing. QEMU 11.1 makes the socket on Windows as well as
    Linux; that was measured, not assumed from the platform. **The measurement was of the wrong
    half.** QEMU *creating* a socket says nothing about anything being able to *open* one, and this
    box's CPython 3.11 reports `hasattr(socket, "AF_UNIX") == False` — so Windows Python cannot
    connect to a unix socket at all. A guest started here under the socket spelling came up running,
    healthy, and with a screen the panel could not read: `nothing is listening on …vnc.sock — module
    'socket' has no attribute 'AF_UNIX'`. Which spelling is used is therefore a **feature test on the
    running interpreter** (`machines.unix_sockets_available()`), not a platform name: Linux and macOS
    get the socket, Windows keeps the loopback port, and a screen that cannot be dialled is not a more
    private screen, it is a broken one. The port is what remains in the threat model's accepted
    weaknesses, and it is named there rather than implied away.
- **A memory floor under a start, and an idle clock under a guest an agent started.** Two
  different failures with one shape: something heavy that arrives without anybody asking. `start`
  is refused, not warned, when the host has less free memory than `machine_min_free_mb` (1536 MB,
  `0` turns it off, and it is an **integer** because "1536.0 MB" invites an argument about
  rounding at the moment the answer has to be yes or no) — a half-started guest is worse than a
  start that never happened. And a machine the *agent* starts goes on the idle clock under a
  `machine:<name>` key, reaped by the same window, the same thread and the same `due()` as the
  apps are; a guest a person pressed Start on is never armed, which is the whole point of the
  distinction. Asking for a guest's screen counts as using it, so a machine being watched is a
  machine that is not idle.
- **A paste that arrives as a paste, and a screen that comes to you.** Pasting into a guest was
  keystrokes — fine for a word, hopeless for a URL or a block of config, and every character of it
  landed in whatever was listening. `screen.clipboard_text` and `_client_cut_text` send the one
  RFB message for it (`ClientCutText`, type 6, declared in this plugin as unused since it was
  written), encoded ISO 8859-1 because that is the only encoding RFB's clipboard defines, with
  anything outside it *named* rather than sent as mojibake — the same treatment a key with no
  keysym gets. And a guest an agent starts now broadcasts `screen.wanted`, which brings the
  Machines tab forward and opens that machine's screen in every open window: somebody who asked an
  agent to do something to a computer should get to watch it happen rather than be told afterwards
  that it did.
- **An acceptance run against a real install, not a fixture.** `tests/acceptance/run.sh` enables
  the plugin, starts the engine, builds and installs a real app from the catalogue over the
  plugin's own HTTP API, proves its URL answers, stops it, uninstalls it, stops the engine,
  disables the plugin, and asserts the machine is as it was found — no listener on the engine
  port, no node process holding the engine directory, and the app's folder actually gone. Eight
  steps, and it **restores the plugin's own enable state on the way out**, including after a
  failure partway: a harness that turned Athena off and walked away would be the bug it exists to
  catch.
- **The image folder, the menu, and the fit — on a computer that need not support VMs.**
  `t_machines.py` now runs 217 checks: that accelerator/CPU pairing as a pure function of the
  accelerator, the VNC port QEMU's own rule gives (`5900+N`) and the display number picked around
  whatever is already bound, and a panel machine's reach line saying where its screen is rather
  than naming a port to go and find. Seven more are about the question the panel asks every
  few seconds: an answer about WSL costs two `wsl.exe` launches, so it is kept for a short
  window, a fresh answer really asks again, an operation that changes a distribution drops the
  cached one (including a failed import, which may still have landed), and "there is no WSL on
  this machine" is kept like any other answer rather than discovered twice a poll. Ten more are about a `machine.json` somebody opened in an editor: a value that
  is not a number names the field and the range it may be, a cleared name is refused as a
  name, a moved disk image is named by where it should be, and a machine that simply has no
  disk, display or boot order is not a fault — a WSL machine has none of the three. `t_screen.py` runs 77 checks against a VNC server written in
  the test itself: the handshake, the pixel format asked for, a rectangle landing at its own
  coordinates, the byte order proved with colour (a monochrome screenshot cannot reveal a channel
  swap), the keysym a shifted character becomes, and the exact bytes a click and a keystroke put on
  the wire. The rest: the fit as a pure function of a host's numbers (a big
  host gives half its cores, capped at eight, and a quarter of its RAM; a 1 GB host says it is
  too small instead of promising; a nearly full disk sizes the guest down rather than filling
  the disk), the folder listing against a real temporary folder (newest first, a subfolder
  walked into and each file saying which folder it came from, a depth beyond the limit recorded
  as not walked rather than omitted, more files than the limit reported as such, a missing
  folder a fact and not an error), and the rule that ties them together — a file chosen from the menu
  is a valid machine *because* the kind travelled with it, while the same file with no kind is
  still refused. The UI driver checks the panel's own reading of it: the menu's labels, the
  `not there yet` / `no images yet` split, a Windows name *suggesting* a Windows guest, and a
  fit that was never read saying nothing rather than inventing numbers. Eleven of those checks are
  about the screen: that a display nobody chose is answered by the host and written onto the
  machine, that a window machine is a window machine wherever it is read from, that VNC binds to
  loopback, and that a guest's console is read from the end — a tail that starts mid-line drops
  the severed half-line, an empty console is empty rather than an error, and a catch-up read of
  the whole log still contains its first line. Eleven more are about the ports a machine forwards:
  that the list the command line opens is the same list a failure is diagnosed against, that ssh is an
  ordinary forwarded port to the guest's own port 22, that a half-declared pair is neither forwarded nor
  claimed, and that a port something else is holding is named in the failure along with whether to close
  what holds it or change it in the machine's settings — while a failure that is not about a port keeps
  its own words.
- **The capability runner, out of the middle.** `dashboard/runner.py` (346 lines) now owns what happens
  after a file is routed: bringing an app up, running one of its capabilities, and handing what it made
  to the next app in a chain. Unlike the supervisor it is not a class, so nothing could be re-exported —
  every seam a harness patches (`_bring_up`, `_appcall_module`, `_idle_module`, `_job_add`, `_app_folder`,
  `_app_url`, `_runtime`, `seed_credentials`) is passed to the runner as a callable and resolved at call
  time. That is why `t_accepts`, `t_pipeline`, `t_schedule`, `t_convert` and `t_idle_users` still fill in the fakes a
  split could have quietly bypassed, and the suite was green the first run after the move.
- **The supervisor, out of the middle.** `dashboard/engine.py` (613 lines) now owns the engine's child
  process — start, stop, readiness, the port override, the read log, the `pids_from_*` parsers, and the
  probe memo.
  It takes `engine_dir`/`plugin_dir` and the two settings callables from the API module, because the
  settings *paths* stay where the harnesses patch them; everything else it owns outright. The singleton
  is still `api.engine`, built by `_new_engine()` so a harness can build a second one the same way. The
  readiness memo also moved up: it was defined four thousand lines *below* its only reader.
- **Settings, out of the middle.** `dashboard/settings_store.py` (159 lines) holds the defaults, the
  merge-on-save, and the two file resolvers. The *paths* deliberately stayed in `plugin_api.py`:
  three harnesses redirect a test run by patching `api.SETTINGS_FILE`, so the store takes the path as
  a parameter and the API's wrappers hand over whatever its global holds at call time. A store that
  owned the path itself would have those tests writing to the real file — the same trap as the node
  memo, one module over.
- **Where Node is, on its own.** `dashboard/node_runtime.py` (130 lines) now holds the lookup — the
  tool roots, the bundled copies, the 60-second memo and the free-port search — out of the middle of
  nine thousand lines of routes. It takes no engine and no settings, so nothing has to be passed in;
  `plugin_api.py` re-exports the old names unchanged. Its import is deliberately **not** wrapped in a
  try: a swallowed failure there would answer "no Node on this machine", which is a lie about the
  machine and would have the engine refuse to start for the wrong reason.
- **A folder click is a selection, not an action.** In the Files tab a single click on a folder no
  longer opens the bottom preview panel — the folder is selected, and the panel stays out of the
  way of the double click that walks into it. Files keep the one-click preview, where the click
  *is* the action. Fixed in the filebox donor and rebuilt into the panel.
- **One group out of the monolith.** The credential stores — what is on this machine and how it
  is copied to a guest — now live in `dashboard/credentials.py`, imported by name and handed the
  engine rather than reaching for the singleton. Verified by loading the API and confirming the
  module is really there, not by a suite that would pass either way.
- **Failures you can read.** Nine mutating calls in the panel believed a 200 without reading it;
  the worst was the screen's input route, where the control lease's refusal (`human_has_control`)
  vanished. All nine now surface the backend's own sentence, and `t_ui_js.py` keeps the rule
  mechanical — including running `actionFailure` in Node against a refusal, a detail, silence, a
  success and a `null`. The driver then presses a real button in two regions with a backend that
  refuses, and reads the host notification a person would see.
- **The panel on somebody else's theme.** `t_ui_theme.py` holds the rule that a colour in the
  panel is the app's variable, with literals allowed only for *status* — green healthy, red broken,
  amber careful, meanings a theme must not re-tint — and it caught what an eyeball had not: 45 uses
  of twelve variables that resolved to nothing after the standalone plugins were merged in. The
  memory meter's "hot" fill was literally uncoloured. All mapped onto the variables the app
  injects, the three dark-only surfaces now on `--card`, and the gate locks both in; every check
  was watched failing against the old code before it counted.
- **Numbers instead of impressions.** `tests/live/t_perf.py` measures what this machine can
  measure and writes the table into [`docs/performance.md`](docs/performance.md), one row per
  number with the method in the last column: one 1280×800 frame encodes in **8.4 ms**, 200 polls
  of a still screen cost **one** encode, 200 rectangles that arrive with pixels identical to what
  is already on screen also cost **one** encode — where the old behaviour paid ~**1.7 s** of
  encoding for the same traffic, which is the parked mouse-lag item closed rather than felt — and
  a pointer event down the session's own connection costs microseconds against a whole handshake.
  The rows that need a live guest, a cold engine or half an hour of running print `SKIPPED` with
  the exact command that would fill them, and the summary counts them **separately** from the
  passes: a suite that reports a pass for a measurement it never attempted is the same lie as a
  green check on a code path that did not run.
- **What it trusts, written down, and proved where it can be.** [`docs/threat-model.md`](docs/threat-model.md)
  names an installed app as running with your rights and refuses to call that a sandbox, lists
  the accepted weaknesses in one table, and points each refusal at the check that holds it —
  `t_security.py`, 63 checks, mutation-verified. [`docs/supply-chain.md`](docs/supply-chain.md)
  accounts for what Athena is made of: four Python packages and a vendored engine, with the
  procedure for changing either. A promise nothing executes is a promise that has already broken,
  so the two documents and the two harnesses ship together or not at all.
- **Getting back out.** The dock names who holds the keyboard and the one click that changes it,
  in both states (`dockNotes`, `keyboardButton`, `keyboardNote`), and the driver reads both
  sentences. `keyOwnerStep` is checked as a value across the whole gesture: pressing the pair is
  not the release, `Ctrl+Alt+F2` cancels it so the combination still reaches the guest, letting go
  of one modifier while the other is still down is not the release either, and a lone Ctrl is not
  one. The wiring is checked as wiring — the dock draws a button whose label is the function's
  answer and whose `onClick` runs without throwing, and the picture attaches the `onKeyUp` the
  gesture ends on; a stub that cannot re-render cannot prove more than that, and this says so
  rather than implying otherwise. The stuck-button case is driven for real: a mousedown in the
  guest, then a `mouseup` on the window, and the event that comes back is
  `{kind: 'pointer', buttons: [], x: 10, y: 10}`. Three positive controls — freezing the button's
  label, moving the gesture to the way down, and dropping the window's `mouseup` listener — each
  failed exactly one check, and restoring went green.
- **How long the wait has been.** The engine's cold boot is 30 seconds on this machine's home and
  there is nothing in the panel that could shorten it, so the panel counts it instead: `/status`
  carries `starting_ms`, and the header reads `starting… 14s` while the engine comes up.
  `t_runtime.py` (49 checks) drives the silent-port rig it already had — a port that accepts and
  says nothing — and checks that the number is an integer, that it counts up between two polls
  rather than answering zero, that an engine this process **spawned** reports from its spawn time
  (seven seconds of it planted on `_started_at`, and the answer lands in that window), and that an
  engine which is simply not up reports `null`. The UI driver checks the header's side of it:
  `starting… 14s`, `starting… 2s` for a running engine with 2200 ms on the clock, no number
  below a second, no number when none was measured, and `waited(null)`/`waited(NaN)`/`waited('14000')`
  all empty. Positive control: crippling `waited()` to return a constant failed four checks, and
  restoring it went green — so the checks read the function and not the layout around it.
- **The folder you walk to, the subfolders inside it, and the bytes on the bar.** Three
  halves, one per layer. The installer plan is driven against the **real `winget`** on this
  box: the QEMU installer's URL and its published sha256 are parsed out of its own output
  (a pure function over captured text, so the rule is proved with no network at all), and the
  plan falls back to the command when winget will not name a file instead of inventing one.
  The downloader is driven against an in-process HTTP server: progress callbacks arrive in
  order and add up to the byte count, a truncated file and a wrong sha256 are both refused,
  and the `.part` file is renamed only after they agree. The scan is driven against a real
  folder tree (`windows/`, `linux/arch/`, `mac/`, and one path too deep) — three levels are
  read, the fourth is reported as left out. `t_agent_tools.py` (172 checks) proves the agent
  half on the same rules: `isos` lists an image inside a subfolder *with the folder it came
  from*, `folders` answers with what is inside one folder (folders, drives, file count) and
  reads nothing else. The panel half is in the UI driver: `isoGroups` turns that scan into
  `windows (2)`, `linux/arch (1)` and the top folder as three lists, the menu labels a file
  with its folder (`windows/win11.iso — installer, 6.0 GB`), the folder line says
  `4 images in 3 folders`, and `JobProgress` draws `48 MB / 197 MB` for a download while
  refusing to divide by a total it never learned.
- **The half of a QEMU machine that can be proved without QEMU.** `t_machines.py`
  pins the command line as a value rather than a habit, on a computer where no QEMU exists:
  an ISO is a CD-ROM and the boot order puts it first, the image is the file and never the
  URL it was fetched from, the accelerator is one this host reports (WHPX here) instead of
  one that would be refused, a Windows guest gets `if=ide` and an `e1000` with no virtio
  anywhere in its argv while a Linux guest gets the fast ones, `ssh.port` and each `ports`
  entry become loopback-only forwards (`hostfwd=tcp:127.0.0.1:2222-:22`) and a machine with
  no ports forwards nothing, the display is a real choice (`sdl` / `none` / loopback VNC), a
  machine pointed at its own disk image gets exactly one drive while one whose disk does not
  exist yet gets none, and every malformed port is refused at creation with the port numbers
  in the sentence (a word, a zero, 99999, a forward with no host side) while a number written
  as a string is normalised instead. State is checked against the process table, not the
  pidfile: a pid left by a crash is not a running machine, stopping an already-dead one is
  `stopped` rather than a failure, and a snapshot of a disk being written to is refused with
  `stop it first`. The live half is `machine_provision_check.py --qemu`, and it was run here:
  Alpine virt ISO (61 MB fetched) → create → boot → **the guest's own serial console** ("Welcome
  to Alpine Linux 3.20", "Kernel 6.6.49-0-virt on an x86_64") → **its screen read the way the
  panel reads it** (1280x800, 19 KB PNG, saved beside the machine — not QEMU's 640x480
  placeholder, which is the difference between a guest that is executing and one that is not) →
  **<kbd>Enter</kbd> sent into that screen** (2 key messages, the guest answered) → a command
  refused with the way in (the forwarded port) → **out to a window of its own and back into the
  panel** (both moves a restart, machine still running, no `display_fallback`) → stop → snapshot
  (a real 197 KB file) → destroy
  (`gone: True`). It runs under WHPX as well as TCG, which is the pairing `qemu_cpu_args()`
  exists to get right. An earlier round of this suite *could not* run it — QEMU was not installed
  then, and it said so and changed nothing.
- **A queued launch, proved without a GPU.** The wait queue's own decisions are checked with
  a stubbed gate and a stubbed engine: a card that is full means no launch, a card that frees
  means exactly one launch, a launch the engine refuses comes back as a failed job quoting
  the refusal, and taking an entry back ends its job without starting anything. Threads and
  the clock are stubbed too, so the whole file runs in a second.
- **The report answered on the real box, in one call.** `report_view()` reads this machine's
  seven app folders in ~2 s and says `6 apps that cannot generate yet` — with the note that
  none of them has ever had an update check, so none is called up to date. Nothing was
  fetched: the stub that raises on `_update_check` is what the test uses to hold that line.
  It is drawn at the top of the System window (the `NeedsYou` block), which is the window
  someone opens when something is not working.
- **What each app will ask you for, read off its own scripts.** `keyscan` walked all seven real
  app folders in 23 s and answered honestly in both directions: `RMBG-2-Studio` (11 files),
  `Hunyuan3d-2-lowvram` (57) and `datasette` (238) read as *runs here* with nothing wanted;
  `Maestro` names two keys (one of them answered by Hermes) *and* a cloud host; `wan` names four
  keys but stopped at the file cap, so it is reported as *partly read* and explicitly **not** as
  running locally. Nothing was fetched and no value left the walk: `t_keyscan.py` checks the
  restraint (a bare `PORT=`, a README mention, `MONKEY`, a hardcoded value are all left alone),
  `t_keys.py` checks the wiring (`local: None` never reads as `local: False`, the app list never
  walks a folder, no value is in the payload), and the driver checks that the badge and the list
  filter cannot disagree about the same app.
- **A finish notice with the artifact attached.** `t_appcall.py` (90 checks) proves the payload
  carries the file and the stamp, that a deleted event bus and a listener that throws both leave
  the caller's answer untouched, and that a missing or backwards clock yields `None` rather than a
  zero-second run. The driver holds the wording: it names `out.png` and not "finished", counts a
  second file instead of hiding it, reports `2h 2m` rather than 122 minutes, says "no file came out"
  when that is the truth, takes only the first line of an app's own error, and marks a 3-second run
  quiet while an unknown duration never does.
- **What a running job says about itself.** `t_pull.py` reads the ages off a *live* queued
  download, so `elapsed`, `since_output` and `since_move` are proven mid-run and not just on a
  fixture, and that a `done` job's age stops counting. `t_updates.py` proves the two states the
  header exists for: a job one moment old already has both ages (`working…` over a hung job was
  the bug), and a job that only moved its bar is not read as silent. Deleting the stamps leaves
  the ages null rather than zero. The driver holds the wording — the three tones, a status that
  is never `warn` for a job that is printing, the kind hint, the `just started` and `gave up
  after` cases, that no sentence anywhere promises an expected duration, and that an unknown kind
  still reports the silence without guessing at its cause.
- **The rig mark, counted over the real catalogue.** Sampled live: of the 300 most popular
  apps, 241 have a Windows + NVIDIA check-in — so on the popular page the chip is barely a
  filter and the honest thing is that it says so. The long tail is where it earns its place:
  the 100 newest apps are 76% never-run and page 60 of the catalogue is 95% never-run with only
  5 Windows + NVIDIA check-ins. `t_store.py` holds the weaker half of this (a row keeps the two
  maps through `_enrich`), and the driver holds the wording: no badge may claim the app
  *requires* anything, and the chip must keep exactly the rows the badge marks.
- **Putting an app back after a bad update.** `t_updates.py` (78 checks) builds a real remote, a
  real clone and a real pull, then puts it back: the files really go back — the hand edit made
  first is gone, exactly as the warning said — HEAD is the commit that was asked for, and the
  reset is itself in the history, so "where it has been" stays honest with no file of Athena's
  holding it. All four refusals are exercised, including a commit fetched into the object store
  but never checked out. The driver holds the wording: a folder that is not a repository is told
  rather than offered a button, an app with one commit in its whole history gets no pointless
  button, the row it is on now is labelled instead of offered, the changes that would be lost are
  counted before anything is agreed to, and the button hands back the commit it was drawn beside
  — never the newest.
- **What the card has cost, counted in the one place the reading exists.** `t_cost.py` (53 checks)
  folds real samples in and holds every line the feature may not cross: a 3-second sample at 300 W
  is 0.00075 kWh and not 0.75 (the two divisors that both look right are checked apart), a 54-second
  gap credits no time and no energy while the next sample resumes rather than re-anchoring, a
  backwards clock contributes nothing, a step of exactly the longest allowed is credited and one
  just past it is a gap, a card that reports no wattage gets time counted and energy left null, and
  energy with no price yields no money. It also proves there is one record, not two: the loader
  hands back the same module, the monitor is subscribed exactly once — a second subscription would
  fold every sample in twice, which no reader could see — and one sample taken by the monitor is one
  sample in the record. The driver (22 checks) holds the wording in every state the panel can be in,
  including the two that would otherwise read as a claim: nothing sampled yet is said rather than
  shown as a zero, and 9 seconds of work is 0.00075 kWh rather than `0.00`.
- **A file in hand, and the apps that would take it.** `t_accepts.py` (56 checks) asks the routing
  question of the manifests and holds the line on what may be claimed: a typed `Image` input takes
  an image and a `.mp4` is refused with the sentence that says which is which, an app that takes any
  file still ranks below one whose input wanted that kind, a capability that wants a *link* is not a
  candidate for a file on this machine however well its intent matches, two equal fits come back in
  the same order whichever way the apps arrive, and an app that has never been probed is reported as
  unprobed rather than assumed able. It also proves the routing question starts nothing — the runtime
  it is asked through raises if `start` is so much as called — and that a path which is not there,
  or a directory, is refused as such. The hand-over refuses everything it can before a job exists:
  an unknown app is a 404, a capability that is not in the manifest comes back with the intents that
  are, and a capability whose required input was not passed lists what it needs — with no call made
  and no job created in any of those cases. A real call then runs as a job that ends on its own with
  the files the app produced, keeps the app's own output in the log, names the artifact as it lands,
  and re-probes when the url moved. The driver (17 checks) holds the wording of every panel state,
  including the two that would otherwise read as an answer: nothing chosen yet says how to choose,
  and nothing fits is said with the kind of file it is rather than shown as an empty list.
- **A chain: one app's output into the next one's input.** `t_pipeline.py` (33 checks) holds the
  handover rule from both sides. `appcall.links()` is pure and reads the two manifests only: an
  image out and an image in is a certain handover, an app that takes any file takes it too, an image
  out and a video in is refused with a sentence naming both sides, one matching output among several
  is enough, an app that takes no file at all cannot follow one that makes them, and an app that does
  not type what it produces may run — but the handover is reported as *not promised*. The route
  refuses a chain whole before a job exists: a stage naming an app that is not installed, one that
  does not name a capability, one whose capability is not in the manifest (with the intents that are
  listed), a handover the manifests do not support, and a stage needing more inputs than the files
  can fill (with `{files}` named as the way to give them) — each a 400 with no call made and no job
  created. A chain that holds runs as one job: the files the first app made are the inputs the second
  was given, the second ran its own capability, each stage is reported in order with what it made,
  the log names the stage as it starts and how many files were handed over, and every artifact is
  still named as it lands. A three-stage chain carries the files through so the third gets the
  second's output and not the first's, a stage may name its other arguments with `{file}` where the
  handover goes, a stage that makes nothing stops the chain with that said and the next app never
  called, and a stage that fails names itself and still reports what did come out. The driver (11
  checks) holds the bar's wording: while it runs it says it will report, a finished run names what it
  made, a chain says how many stages and what came out the far end, a stop names the stage and the
  app's own reason with both stages still listed, and a chain that ended with nothing says the last
  app made nothing to hand on.
- **A repeat: an app that runs again on its own.** `t_schedule.py` (63 checks) holds the
  whole path from "run this every morning" to a job that actually fires. A schedule naming an
  app that is not installed, a capability that is not in the manifest (with the intents it does
  have named), required inputs that were not given, or a time Hermes cannot read is refused
  before anything exists — no job, no script, no row — and a build with no cron store answers
  503 instead of storing a job nobody will run. The job that is created is a `no_agent` script
  job: no prompt, so starting it costs no model call; the script imports the plugin's own API
  and calls `run_one()` with the app, the capability and the inputs; it compiles, it explains
  in its own docstring why it is not an agent turn, and it is given the interpreter that can
  import the plugin. `run_one()` is the job-less runner: it takes the same bring-up, calls the
  same capability and reports the same files, makes no job, fails as a sentence rather than a
  traceback when the app is missing or throws, and a run counted by `_schedule_runs()` comes
  from the same `recipe.json` the Runs tab reads. Reading schedules back is where the honesty
  is: a waiting job shows its next fire and how often it has really run, a job removed from the
  store is reported `gone` with what that means, a missing script says it cannot fire, a paused
  job is not reported as enabled, filtering by app drops the rest, and stopping one removes the
  cron job, the script and the row and says plainly that it will not run again. The last block
  runs against the real store this build ships — the three schedule forms the route offers as
  examples parse, nonsense is rejected, and `create_job` still takes `no_agent`, `script` and
  `interpreter`. The driver (10 checks) holds the repeats block: nothing scheduled says so in
  words, a build with no cron store says that is why, a row reads as its time, its next fire and
  its real run count with the `.git` off the app name, a missing job reads "will not run" with
  its own reason carried through, a paused one does not look like it will fire, every row can be
  stopped, and the Runs tab is where the block is asked for. The last checks drive the agent
  tools against the same instance: `athena_schedule` refuses a repeat with no app and capability,
  an app that is not installed, an unknown action, a stop with no id, and a time Hermes cannot
  read (leaving the store untouched); one it does create is in the index the panel lists, in the
  cron store, with its script on disk, lists back with the same answer the route gives, and stops
  with the route's own DELETE. And an agent asking for a chain gets it refused before anything
  starts when a stage does not fit, or run stage by stage through `run_one()` when it does.
- **Startup, measured instead of felt.** The report was "Athena takes 10-30 seconds to start"; the
  stopwatch said the time was not the engine's boot but the *asking*. On this machine a port with
  nothing behind it drops the connection instead of refusing it, so every look at a stopped engine
  spent its whole timeout: **2.08s per `/status`** on a 5s clock, **1.57s on `/apps`** (it asks
  whether the engine is up before it can draw a launch button), and **2.4s inside every app health
  check** for the same reason. Now the cheap question comes first and is bounded
  (`EngineManager.listening()`), so the same calls measure **0.41s** worst-case `/status` (1-20ms
  once settled), **0.06s** for `/apps`, and **0.37s** per health check, with the seven of them
  checked six at a time instead of one after another. First panel call to a ready panel: **4.4s**,
  which is the engine's own boot — and the panel now asks for the engine as it loads instead of
  when a button is clicked, so that boot happens while the window is still being read. The Node
  search behind every status answer runs **once, not once per look** (a 60s memo keyed on
  `ATHENA_NODE`; an override in the environment is still picked up immediately, which is the whole
  reason the key is the override and not "have we looked yet"). `t_runtime.py` (49 checks, 21 of
  them new) pins all of it against real sockets: a port nobody holds answers *nothing* and does so
  inside the budget, a probe of a stopped engine does not spend its timeout, a port that accepts
  and stays silent reads as an engine **starting** (a field of its own, with its holder named)
  rather than as a spinner forever, a repeated look while it stays down is free, the Node search
  runs once, and one app's check blowing up comes back as that app's reason instead of the whole
  report failing.
- **A live probe that has nothing to watch says so instead of failing.** `t_idle_live.py` answers a
  question about a *running* app; with no engine listening there is no answer to give, and asking
  anyway raised `int(None)` out of `Runtime`'s own constructor — a missing engine reading as a
  broken plugin in the suite. It now reports `SKIPPED — no engine is listening` and exits 0, the
  same policy it already had for "nothing is running", and `run_all.sh` counts it as a reported
  skip (`SUITE GREEN (1 live probe(s) skipped — nothing running to watch)`) rather than either a
  pass or a failure. `_port_open()` refuses a URL whose port was never resolved the same way: the
  gate that skips a request must not be the thing that throws.
- **A text file that is not UTF-8 is still a text file.** The preview decided text from bytes by
  decoding UTF-8 and calling anything else binary, so a Windows notes file in UTF-16 was "binary" —
  and, worse, a packed binary whose every other byte is NUL decoded cleanly as a 16-bit string and
  was drawn as a screen of control characters. Now a BOM is believed; NULs in every other byte mean
  16-bit, with the side given by which bytes are the NULs; a NUL with no such pattern means binary;
  clean UTF-8 is text, then latin-1; and because conversion is not evidence — `bytes([0, 1]) * 200`
  is a valid UTF-16BE string of one control character — a decoded result must also *look* like text
  (`_looks_like_text`). A BOM is stripped after decoding, since decoded with an explicit codec it
  would sit at the top of every preview as an invisible zero-width character. `t_files.py` (27
  checks) covers all six cases against real bytes.
- **One port, one answer.** The System tab read a hardcoded `http://127.0.0.1:42001` while the rest
  of the plugin resolved the port from `ATHENA_PORT`, then the settings file, then the default — so a
  second Athena on another port drew its app rows as though the engine were down. It now resolves the
  same three places in the same order on every call (memoised for 5s, with the kept-alive socket
  dropped when the engine moves), and cannot import `plugin_api.py` to reuse that answer because that
  module imports *this* one to mount the router, so `t_system.py` (22 checks) asks **both** modules
  the same question in six configurations and requires the same number back. The typo is gone too:
  `int()` ran at settings load, so `ATHENA_PORT=4200o` took every route down and showed an engine
  "down" for a reason written nowhere; a value that is not a port now falls through to the file, then
  to the default, and the plugin is still there to say so.
- **A job that went away with the engine says so.** Jobs are a dict in the engine's process: a
  restart takes them all, and a panel polling `GET /jobs/<id>` every 1.5s mid-install was told
  nothing and reported a network fault. The 404 body now carries `gone_with_restart: true` alongside
  `no such job`, with when this run of the engine started, and the panel says the restart and points
  at the app's own log instead of inventing a connection problem. `t_updates.py` section 11 (71 → 78
  checks) holds the body, and the driver drives the panel's wording (`jobGoneWords`).
- **The words the folded-in tabs ask for by name.** The Files and System tabs carry their own
  dictionaries; a key the code asks for and the dictionary lost draws the *key* on screen, which
  reads like English and looks like a styling bug — one real instance (`t('modified')` in the file
  properties panel, with no `modified` in the dictionary) had been shipping that way. `t_i18n.py` (9
  checks) reads the **built** panel, because the fold is what produces the failure: 111 words in the
  Files dictionary against the 97 asked for by name, 13 against 11 in the System one; nothing blank;
  no dictionary mostly unused; and where the two share a name (`up`, `backendDown`) they must mean
  the same thing, since the registration spreads one over the other and would otherwise let one tab
  quietly say the other's words.
- **A shelf-wide update ends on the shelf.** Update all takes minutes, so the panel moves to the
  terminal tab while it runs — and then stayed there, on a tab that only exists while a job does:
  the bar had no such tab and the screen showed one nobody could click. It now lands back on Apps and
  re-asks both whether anything is still behind (`/apps/updates`) and for the shelf itself, so the
  bar can go away when it is empty. The one sentence that accounts for the run — how many moved, and
  when some did not, where the reasons are — is `updateAllWords()`, a pure function precisely so it
  can be asserted rather than left inside a handler whose state the harness cannot see; the driver
  clicks Update all, watches the job run and finish, and checks the tab it comes back to, plus four
  cases of the sentence.
- **The folder picker says what it is looking at.** `folderWords()` is read off whatever the backend
  answered, including the answers that are not a listing, and it is now driven in the driver: waiting
  before anything is read, the folder's name with the reason it could not be read, "cannot be read"
  when no reason came back, "1 folder, 1 file" counted as one, and nothing about files at all when
  none were found.
- **A screen check that failed once, and why it could.** `t_screen.py` proved "a rectangle that
  arrives changes the picture" by reading a reference frame and then polling for a different one —
  but the fake guest was free to send the rectangle *between* those two moments, and then the
  reference already was the changed picture: every later poll re-encoded the same pixels, the two
  were equal for ever, and a guest that had changed was reported as unchanged. It went unnoticed for
  as long as it did because the race needs a loaded machine to lose, and it did lose once in a full
  suite run (`changed=False encoded=61`). The change is now held back until the test has taken its
  reference (`FakeVnc(arm=…)`), which lets the check assert both halves instead of one: **nothing
  moves while the guest has not moved** — a session that repaints an unchanged screen as a different
  picture is its own bug — and the picture does move, with a new encode, once the rectangle is sent.
  Twelve consecutive runs green; a control that arms the guest *before* the reference is read fails
  the first of those two checks, which is the mechanism, made deterministic. `t_screen.py` is 77
  checks.
- **The gate that checks the counts this file quotes was reading the wrong number.** It takes each
  test's own summary line out of a transcript and compares the first number in it with the claim here,
  and a summary line begins with the test's *name*: `t_i18n: 9 passed` was read as **18**, so `t_i18n`
  appeared to disagree with this file while this file was right. Every test whose name contains a digit
  was exposed to the same misreading (`t_http2` would have "run 2 checks"), and it fails silently,
  because the wrong number is still a number. The reader now drops a leading `name:` (or `name/`) first
  and is self-checked against four known summary lines — including `217/217 checks passed`, where the
  leading digits *are* the count — plus a bare name with no count at all. `check_doc_counts.py` (24
  claims) passes, and it now compares 13 documented per-file counts.
- `desktop/plugin.js` is syntax-checked and carries no upstream branding; the desktop loads a
  copy from `desktop-plugins/athena/`, and the suite reports when that loaded copy had gone stale
  and re-syncs it.
