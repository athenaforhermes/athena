# Machines

A machine is a real OS that Athena can start, stop, hand a command to, and show you — usable
by you in the panel and by the agent through tools, both driving the same engine routes.

It exists because half of what people want to try is not native to the machine Hermes runs on:
an app that only installs on Ubuntu, a build that needs Arch, a "does this work on Windows"
question that should not be answered by guesswork. A machine answers those without changing
your host.

## Forgetting a machine and destroying one

Two buttons, and they are not two words for one thing:

| | what happens to the folder | what happens to the distro | undo |
| --- | --- | --- | --- |
| **Forget it (keeps the distro)** | moves to `forgotten/<name>/` | for QEMU it *is* `disk.qcow2` in that folder, so it stays; for WSL it stays registered with WSL | "Bring it back" moves the folder back, disk and all |
| **Destroy it and its disk** | deleted (`unregister=1` is the only path that removes files) | gone; a WSL distro is `wsl --unregister`d | nothing to undo |

`GET /machines/forgotten` lists what was put aside, with how much is still on each disk; a
forgotten machine is out of the machines list but not hidden, and the panel draws a section for
them with the button that brings one back. The listing is read from the folders, not remembered,
so it cannot outlive what it describes. Note the route order: FastAPI matches in declaration
order, so `/machines/forgotten` is declared before `/machines/{name}` — written after it, the
literal path would never be reached and the list would answer "no machine called 'forgotten'".

## Where the names live

The tab owns the read loop, so it owns the loop's state: `lastInput`, `timerRef`, `liveRef`,
`tickRef` and the `kick` that moves them are declared in `MachineScreenTab`. The picture
(`GuestScreen`) sends input and draws the frame it is handed, and it takes `kick` as a prop. That
split is the fix for a workspace-wide crash — with the names in one function and the loop in
another, mounting the tab threw `liveRef is not defined` and took the whole pane down with it,
which reads as "Athena stopped working" rather than as a broken tab.

The lesson is in the testing, not the code: source-level checks all passed while the panel could
not render, because they asked whether a string was present and every string was. `probe_tabs.mjs`
resolves each `X.current` against its enclosing scopes, and `uitest/driver.mjs` mounts the tab and
runs its effects — the only check in the suite that can fail the way the panel failed.

## Naming a machine

The first row of the make-a-machine form is `newMachineRow(o)` — pure, handed its callbacks, and
registered in `mk_ui_test.py` so the driver can draw it. It labels the field and the kind, hints in
instructions rather than in something that looks like a value, draws an empty field as a dashed
slot, starts the cursor in it, submits on Enter, and tells a disabled `Make it` why it is disabled.

## Two verbs, two routes, and the method the caller sends

`POST /machines/{name}/forget` and `POST /machines/{name}/destroy` are separate routes rather than
one `DELETE` with a query string, because the panel's `act()` helper sends POST — and
`DELETE /machines/{name}` answered `405 Method Not Allowed` to it, so destroying a machine never
worked and the panel showed an error while the machine stayed where it was. `unregister=1` on the
DELETE is kept for callers that speak HTTP verbs.

This is the failure mode worth keeping in mind: the routes *existed* and the paths matched, so a
path-only test passed while the button was dead. `t_move.py` therefore checks `(method, path)`
pairs for every literal call in the built panel.

## How fast the picture follows the person

Input is cheap and the guest is not the problem: measured here 2026-10-14 on a WHPX machine,
a keystroke leaves the panel in **20 ms**, the guest has drawn the answer in **139 ms**, and a
frame costs **30 ms** to read. What was slow was the panel asking on a fixed schedule of its own
(1.2 s while the screen moved, 2.5 s once it had gone quiet), so a letter could take a second and
a half to appear on a machine that had drawn it immediately.

`frameDelay(frame, still, sinceInput)` now answers in that order — what the person just did, then
whether the screen is moving, then whether it has gone quiet:

| since the last input | delay | why |
| --- | --- | --- |
| under 1.6 s | 180 ms | just above the guest's own echo: the answer is read back when it exists, and one character is not read twice |
| nothing recent, screen changing | 1200 ms | something is happening that nobody asked for |
| nothing recent, two identical frames | 2500 ms | a still screen does not need two pictures a second |

Every event (key, click, drag, wheel) goes through `send()`, which kicks the reader: the pending
look is cancelled and a fresh one is taken 80 ms later, then the fast cadence carries it. The
burst is a window, not a mode — it has to end, or an idle machine would be read five times a
second for as long as the tab was open.

### The cadence was only the half of it

Fixing *when* the panel looked did not make the machine feel quick: "the input in the virtual
machine was still a little laggy. & just laggy in general." The other half was *how* it looked.
Every frame and every keystroke opened its own RFB connection — a handshake, a security exchange
and a pixel-format negotiation per letter — then asked for the **whole** screen
(`incremental=0`), copied every row out of a Python bytearray, and re-encoded the lot as a PNG at
`compress_level=6` on each tick, most of it pixels that had not changed. And because QEMU's VNC
server runs `share=off` by default, the frame reader and the input sender were racing the single
connection slot.

`ScreenSession` is the same architecture every real remote console uses. One connection per
machine, held open; the first request is the whole screen and every later one is incremental;
input goes down the socket that is already open, so a letter costs a few bytes rather than a
session; the canvas is only re-encoded when a rectangle actually arrived, and `frame()` hands back
the *same* PNG bytes when none did — which is what the tab's still-frame detection already reads.
`share=force-shared` is asked for on the way in, so a second reader never has to wait for a slot.

The registry sits in `screen.py` beside the class (`session_for`, `reap_sessions`,
`close_session`), not in the route file, because a held connection is a resource with a lifetime:
it must be reaped when idle, replaced when dead, and released when the machine is forgotten or
stopped. `_held_session()` in `plugin_api.py` is only the name the routes call.

All of it is checkable without a guest, so `t_screen.py` checks it against the fake RFB server:
one connection across many frames and six keystrokes (one handshake, one `security-choice`), one
re-encode across three polls of a screen nobody changed, a rectangle that does arrive changing the
picture, a guest that changes resolution ending the session instead of writing outside its buffer,
and the registry answering the same session twice, releasing it on request, and letting go of one
nobody has looked at.

### The pointer

"Once i got into omarchy i have no mouse passthrough so i cant move the mouse on the vm desktop."
VNC delivers the pointer's *absolute position*. With no pointing device declared, QEMU emulates a
PS/2 mouse and converts that position into relative motion, which only works while the guest draws
a cursor the emulation can push around. A Wayland desktop draws no hardware cursor, so the pointer
was read, translated, and never moved anything. `-usb -device usb-tablet` attaches an absolute
pointing device and libinput reads it 1:1.

`-usb` is part of the fix, not decoration: the tablet hangs off a USB bus and `-machine q35` has
no USB controller until one is asked for. Without it QEMU refuses the entire command line —
`No 'usb-bus' bus found for device 'usb-tablet'` — and exits before booting. That is why the check
for it is not a string check: `t_machines.py` starts the real command line, waits two and a half
seconds, and fails with QEMU's own words if it will not run. The string check (`usb-tablet` is in
the argv) stayed green through the whole bug, which is the lesson: a command line is a request.

## An installer becomes a machine by itself

A machine made from an installer ISO is not meant to stay one. The first start after the install
finishes asks the disk file what the machine is now (`is_installed()` → `disk_written_bytes()` ≥
`INSTALLED_BYTES`, 256 MiB) and, if there is a system on it and nobody has said otherwise, writes
`boot: "system"` and drops `-cdrom` from the command line. From then on the machine boots what its
disk holds and there is no installer in it to boot by accident. Measurements that set the
threshold, 2026-10-14: never started 193 KiB, booted live session a few MiB, finished omarchy
install 5.89 GiB.

Deliberately not stored: `installed` is read from the file each time, so wiping the disk returns
the machine to being an empty one with a medium, and an explicit `boot: "medium"` is never
overruled — a live or rescue image keeps starting on its medium however much is on the disk. The
row carries `installed` and `medium_attached`; the tab says which of the three states the machine
is in and offers the other direction ("Start on the installer" / "Start on its own system" /
"Attach the installer again").

## What a machine with a medium attached starts on

An installer ISO is a CD-ROM, and where the firmware looks first is a decision that has to be
somewhere. It is `boot` in the spec, resolved by `boot_preference()`:

| `boot` | `-boot` | means |
| --- | --- | --- |
| `""` (nobody said) or `system` | `order=cd` | the disk first, and the CD-ROM when nothing on it boots |
| `medium` | `order=dc` | the CD-ROM first, every time — a live or rescue medium |

The default is the disk first because of a report that is worth remembering: omarchy's installer
finished, said "reboot now", and the guest came back to the installer menu. The install was
never lost — `qemu-img info` showed 5.89 GiB on the disk — but the machine was `order=d`, so the
medium won every time. With `order=cd` an empty machine still lands in its installer (nothing
bootable on the disk, so the firmware falls through) and an installed one starts itself. Measured
here, 2026-10-14: the letters concatenate and **`order=c,d` is a syntax error** — QEMU answers
"Expected '=', after parameter 'd'" and the machine does not start at all, which is why the
argument comes from one function that tests can read.

`POST /machines/{name}/boot` (`{"boot": "system" | "medium"}`) changes it. Where a machine starts
is decided on QEMU's command line, so on a running machine it is a stop and a start — the same
restart, with the same race guards (`_job_running`), as moving a screen. The machine tab offers
the other way as one button in the words the setting means ("Start on the installer" / "Start on
its own system") and states which way it is currently, so nothing here is implicit.

## What is decided, and why

**Two providers, one interface.** `wsl` is the default on Windows: it is a real Linux kernel,
it is already installed on most of these machines, it costs a fraction of a full VM, and — the
decisive property — **the guest shares your localhost**, so a service started inside it is
reachable from Hermes at `127.0.0.1`, which is exactly what the app runner already does. `qemu`
is the escape hatch for anything bootable: an Omarchy or Bazzite ISO, a Windows installer, a
`.qcow2` you already have. A `docker` provider is cheap to add later; Docker Desktop is present
on a lot of machines already.

**Machines are claimable, not shared.** A machine is created Athena-owned (its own distro or
its own disk in `<athena home>/machines/<name>/`) so the agent can be given a shell without
that shell being your development environment. Pointing a machine at an **existing** distro is
supported and is an explicit, deliberate choice: it is the one configuration where an agent
command can touch work you care about, so it is never the default and the panel says so.

**The first thing a machine does is ask.** `wsl --install` needs elevation and often a reboot,
and QEMU is a 1 GB system package. So neither is ever installed as a side effect of something
else: a machine action that needs a missing prerequisite returns `needs_setup` with the exact
command and the question, the panel renders that as a Setup card with an Install button, and
the agent is told to ask the person before running it.

**Three ways to answer a missing prerequisite, kept distinct.** The host returns one of
three shapes, because they are three different social situations and collapsing them is how a
tool ends up silently asking for a password:

- `elevated` — on Windows, `wsl.exe --install` starts UAC, and *the consent prompt is the
  answer*. One button, no typed command.
- `job` — a long install worth watching (QEMU on Windows). It becomes a job with a progress
  line, like every other slow thing Athena does.
- `manual` — a command that needs `sudo` on Linux. It is **shown and never run**: a plugin
  that cannot talk to you must not pretend to have your password.

**A machine that cannot start is refused when it is made.** A QEMU machine with no image can
never boot, so `MachineSpec.validate()` raises at creation rather than at `start`, where the
failure would arrive minutes later as an opaque QEMU error. A QEMU spec must carry
`image.path` or `image.url` *and* say `kind: iso|disk`. This is the rule that makes "give
Athena an ISO instead of WSL" a field rather than a promise.

**The QEMU command line is a pure function, because it is the part that can be proved.**
`qemu_argv(spec, dir)` takes a spec and a folder and returns an argv — no subprocess, no QEMU
needed on the machine running the tests. That is not tidiness: it is the only part of a QEMU
provider that can be verified on a computer without QEMU, and it is where the mistakes live
that produce a guest which boots and then does nothing.

- **The accelerator is discovered, not assumed.** `whpx` when Windows reports Hyper-V, `kvm`
  when `/dev/kvm` is readable, `tcg` otherwise. TCG is slow, and being slow is the correct
  failure: a machine that boots slowly is a machine that boots.
- **A Windows guest is given IDE disks and an e1000 NIC.** An installer has no virtio driver,
  so a Windows ISO shown a virtio disk reports a machine with no hard drive and stops. Linux
  guests get virtio, which is the fast one. This is decided from `os`, so testing on Windows
  is a field rather than a combination somebody has to know.
- **The image is a file, never a URL.** By the time a command line exists the image has been
  fetched; handing QEMU an `https://` path produces a guest that boots nothing at all.
- **The console is `-serial file:…/console.log`, not a pipe.** A pipe with no reader fills up
  and stalls the guest; a file cannot.
- **`hostfwd` is explicit and loopback-only.** A QEMU guest does not share your localhost —
  that is the difference from WSL and the reason a service inside it is unreachable until a
  port is listed. `ssh: {port: 2222}` becomes `hostfwd=tcp:127.0.0.1:2222-:22`; each entry in
  `ports` becomes its own forward; a machine with no ports forwards nothing rather than
  inventing one.
- **A machine that boots its own disk gets no second one.** `kind: disk` means that image *is*
  the machine, so no empty `disk/` is created beside it.
- **Before its disk exists, a machine's command line has no drive in it at all** — the argv is
  built from what is there, not from what is planned.

**State is the process table, not the pidfile.** `qemu.pid` is a claim; `qemu_running()` asks
the process table whether that pid is alive and is running QEMU. A pidfile left by a crash
reads as "not running", because the alternative is a panel offering to stop a machine that
died last week. `stop()` is a SIGTERM then a SIGKILL, and reports `state: stopped` when the
machine was already gone — stopping something that is already stopped is not a failure.

**Ports are data, so they are validated where a person can still see the mistake.** Every
entry must be `{host: <number>, guest: <number>}` in 1..65535, or `SpecError` at creation. A
malformed pair silently drops its `hostfwd`, and a machine with a dropped forward is a machine
that looks dead from the outside. Numbers arriving as strings are normalised rather than
refused: that is what JSON from a form looks like.

**A snapshot of a running machine is refused, not attempted.** Copying a disk that is being
written to is not a backup, it is a coin toss. `stop` it first — the error says so.

## The architecture constraint that shaped the console

Dashboard plugin routes are served one request at a time through `httpx.ASGITransport`
(`hermes_cli/plugin_host_child.py::op_asgi`): a route gets a request, returns a complete
response, and the bytes are shipped back. **A plugin route cannot host a websocket, a stream,
or a long poll.** It is the same reason the app engine is its own process speaking WebSocket
rather than a set of routes.

So a console cannot be a plugin route, and neither can a picture. The guest's **serial console**
is read from the end and served as text (`GET /machines/{name}/console` — an install's log is
megabytes), and the guest's **screen** is read as a framebuffer and served as a PNG
(`GET /machines/{name}/screen`). Both are polled rather than streamed, and both close after each
read: a client that is never held open cannot go stale behind a machine that was stopped or killed.

**The plugin is the VNC client** (`dashboard/screen.py`). The panel cannot open a socket to the
guest's VNC server, so the plugin does it server-side: RFB 3.8 handshake, the pixel format *we*
choose (32-bit, blue in the low byte — the order Pillow decodes with no shuffling, and the order a
real QEMU frame was verified against QEMU's own `screendump` for), one raw rectangle per update,
decoded into a canvas and handed to the panel as a PNG. No vendored noVNC, no websockify, no extra
dependency: about 280 lines of stdlib, because the one thing that had to be true — a raw socket we
control — is exactly what a plugin route cannot have.

**Where that picture is drawn is a tab of its own, not a card.** Pressing Start opens the machine's
screen as a tab named after it (`watchMachine`, `shellTabs`, `MachineScreenTab`), the same bar an
app's output lives in, and the card's button opens the same tab. It was first drawn inside the
machine's card and that was wrong for a measured reason: a 640x480 installer in a card is a
*picture of* a computer — the report of it was "a tiny little screen" — while a guest you are
installing on is a computer you use. The picture keeps its own aspect and scales to the pane; the
input half below is unchanged by any of it.

**The picture owns the keyboard, in the way this app expects.** `GuestScreen`'s host and image
carry `data-terminal` and `data-remote-screen` — the same markers the app's own remote-screen pane
uses — because Hermes' chat composer has a type-to-focus reflex that otherwise focuses the chat and
types there on the first printable key, which read to the user as "it resets into our session for me
to type over here". Opening the tab focuses the picture (only when nothing is being typed), so
typing begins without a click, and the tab's window listener acts only on keys that are this
picture's business. The driver presses keys at it to check the halves: one aimed at the picture
reaches the guest **once**, one that is not is left alone. Once matters — plugin panes render in
the app's React singleton, so the picture's handler is given a synthetic event while the tab's
window listener sees the native one, and a marker set on only one of them sent every key twice
(`zack` arrived as `zzaacckk`).

**Clicking the picture is still how a combination is sent.** A click is
combination (`Ctrl+Alt+F2`) is sent, but the tab itself listens on the window while it is open, so
an installer that asks for a hostname, a password and a username is typed into like any other text
box. A key aimed at a field of this app stays in that field, and `Ctrl`/`Alt`/`Meta` shortcuts still
belong to the app rather than the guest — both are guards in `onWindowKey`, and both are checked by
the driver by pressing keys at it rather than by reading the source.

**Input goes back the same way** (`POST /machines/{name}/screen/input`): a click is a press and a
release at the same point, a wheel notch is a press and a release with the wheel's own mask bit,
typing is a keysym with shift held around it when the character needs one. The panel maps a click
on the scaled picture to the guest's own coordinates (`guestPoint`), so a click lands where it looks
like it lands — at the tab's size or at a window's, the arithmetic is the same.

```
display: ""          # nobody said — answered by default_display(), which answers "panel"
display: panel       # the default: QEMU writes no window, the screen is read over VNC and shown here
display: window      # QEMU's own SDL window (SDL, not GTK: no GTK runtime needed)
display: none        # headless and unwatched, for a machine that only ever answers on a port
```

**A restart is two chances to fail, and both are handled.** Moving a screen is a stop and a start,
and the start arrives while the previous QEMU is still letting go of the guest's console file —
QEMU exits with `could not connect serial device to character backend 'file:console.log'` and the
machine is gone, which reads as a crash. `stop()` waits for the process to be gone, `boot()` waits
on the console file itself (`wait_for_console`), and one more attempt happens only when QEMU's own
words say the file was held (`console_was_held`). The other failure is social, not technical: a
machine that is still being *made* has a job that starts and stops it, so `set_display` refuses
while that job is running rather than putting two actors on one machine (measured: the job's boot
died with "Ignoring request for interrupt vector 0" and the machine was left stopped). A refusal
writes nothing, so no machine is ever left asking for a display it is not using.

Where a screen lives is decided on QEMU's command line, which is decided at start. That is why
**"Pop out into a window" is a restart** — `POST /machines/{name}/display` stops the machine,
rewrites the spec, starts it again, and replies with a sentence saying so rather than a button that
looks like it missed. A stopped machine just records the choice ("it will show its screen in the
panel the next time it starts").

Two further things the command line had to get right, both found by measurement rather than by
reading:

- **A VNC display number is picked from the ones nothing else holds** (`pick_vnc_display`), because
  two machines quietly sharing display `:0` is one machine drawing into a screen nobody is watching.
- **The accelerator and the CPU model are one decision.** `-accel whpx -cpu host` on Windows leaves
  the vCPU frozen at its reset vector while QEMU itself is alive: a running process, a guest that
  never booted, and QEMU's "guest has not initialized the display (yet)" placeholder forever. It
  looks exactly like a machine that started and has nothing to show, which is what it was reported
  as. `qemu_cpu_args()` gives WHPX no `-cpu` at all (QEMU picks a model that works, still
  accelerated), `-cpu host` only to KVM, and `-cpu max` to TCG.

## Layout and spec

```
<athena home>/machines/<name>/
  machine.json      the spec — everything needed to rebuild this machine
  disk/             qcow2 image, or the distro's vhdx
  exports/          snapshots (wsl --export tarballs, qemu qcow2 copies)
  console.log       serial / first-boot output
```

```yaml
name: ubuntu-dev
provider: wsl                  # wsl | qemu
os: ubuntu-24.04               # a preset, or a custom image below
image:                         # optional — the "different OS files" field
  kind: rootfs                 # rootfs | iso | disk
  path: E:/isos/omarchy.iso    # a local file
  url: https://…               # or something to fetch once
distro: athena-ubuntu-dev      # wsl: which distro to drive
attach: false                  # true = use an existing distro, never the default
cpus: 4
memory_mb: 4096
disk_gb: 40
ssh: {port: 2222}              # qemu: hostfwd so a headless guest is reachable
ports:                         # qemu: everything else you want to reach from here
  - {host: 8080, guest: 80}    #   host side first — the one you will open
vnc_port: 3                    # qemu: display number (not a port) for the guest's screen
display: panel                 # panel (the default: the screen is read here) | window | none
```

Note what is *not* in that file: which accelerator will be used, whether virtio or IDE disks
will be attached, whether the image has been fetched yet. Those are derived at start time from
the spec and this computer, so the same `machine.json` moves to another machine and still
means the same thing.


## Sizing, and the folder of images

Two things nobody should have to know before their first machine.

**The fit.** `host_facts()` reads this computer — cores, RAM, free space on the disk the Athena
home really sits on — and `fit_spec()` turns those numbers into a recommendation: half the
cores capped at eight, a quarter of the RAM, 40 GB of disk, clamped by what is free. They are
separate functions because the second is a claim that can be checked anywhere: `t_machines.py`
proves what a 32-core host is offered, what a 4-core/8 GB host is offered, and that a host with
1 GB is told it is too small rather than promised a guest it cannot hold. When the host could
not be read at all, every number is a default and its `why` line says exactly that.

Three doors to the same answer: `GET /machines/fit`; `MachineIn` with `cpus`/`memory_mb`/`disk_gb`
defaulting to `0` (*size it for this computer*); and the panel's three empty fields, whose
placeholders *are* the fit. `athena_machines op=fit` is the agent's door, so "make me a machine"
makes one this computer can hold.

**The folder.** `iso_dir(home, settings)` — `settings.iso_dir`, else `ATHENA_ISO_DIR`, else
`<home>/isos` — names the one folder Athena reads. `scan_isos(dir)` returns
`{dir, exists, isos, skipped, limit}`: files only (a subfolder is counted, not walked into),
newest first, sizes in MB, each with the kind its name *suggests* (`image_kind_for` — extension
only, never a guess). `exists: false` is a fact, not an error, and the panel says `not there
yet` rather than pretending the folder is empty.

The menu is drawn from that list — every file, `kind not obvious from the name` when the name
says nothing, and `somewhere else on this computer…` for a path typed by hand. Picking a file
fills the path, the kind and a suggested guest OS: three fields, all visible, all changeable,
because a name is evidence and not an answer.

`POST /machines/isos {dir, create}` is deliberately two decisions: a path that does not exist
comes back as a question (`"needs_folder": true`, "…does not exist yet"), and `create: true`
makes it. `athena_machines op=isos` / `op=set_iso_dir` are the agent's halves of the same.

## Walking to the folder, and reading inside it

A path box is a folder somebody has to remember. `Change folder…` opens a browser instead:
`/machines/folders?path=<folder>` lists the **folders inside one folder** — never files, never a
drive sweep, never a walk of its own — and the panel draws one level per click with `Up` (absent
at a root), the drives as a way in, and `Use this folder` meaning *the folder you are standing in*,
so nothing depends on what a text box says. Where the desktop app exposes a native folder dialog
(`window.hermesDesktop.selectPaths`) it is offered as an extra door, but the browser is the path
that always exists: a plugin cannot count on the host's picker. The typed path stays available
(`Type a path`) for a path already on the clipboard.

Once a folder is set, the images inside it are read **recursively and shallowly**: `ISO_DEPTH = 3`
means `windows/win11.iso` and `linux/arch/arch.iso` are found, while a fourth level down is
recorded in `skipped` as *not walked* rather than silently left out. Every file carries `rel` — the
folder it came from, relative to the image folder — so:

* the folder line can say `E:/isos — 6 images in 3 folders` (a nested folder is not an empty one);
* the menu can label entries `windows/win11.iso — installer, 6.0 GB`, because two files named
  `install.iso` in `windows/` and `linux/` are two different files;
* the panel's list groups them (`windows (3)`, `linux/arch (1)`, top folder first, unlabelled) and
  each row's *New machine* button fills exactly what the menu fills — one rule, two doors.

`folders` counts the *subfolders* an image was found in (the top folder itself is not one), and the
agent sees the same shape: `athena_machines op=isos` lists the subfolder files with their `rel`,
`op=folders path=…` answers with a folder's contents.

## Installing QEMU: a plan, and a bar with bytes in it

`qemu_needs_setup()` asks `winget show --id SoftwareFreedomConservancy.QEMU` and parses the
installer's URL and published sha256 out of the output (`parse_installer_fields()`, a pure function —
so the rule is tested on captured text with no network and no winget at all). When winget names a
file the plan's method is `download`, and the fetch is Athena's own:

* `download_with_progress(url, dest, on_progress, expect_sha256=…)` reports `done`/`total` as bytes
  arrive, writes `<name>.part`, and renames it only after the size **and** the sha256 agree;
* the existing job poll draws it, so the panel keeps one progress path (the card grows
  `downloading qemu-w64-setup-20260811.exe · 48 MB / 197 MB`);
* when winget will not name a file, the plan falls back to the command (`job`/`manual`) and says so
  rather than inventing an URL. `winget download`'s own progress was measured on this machine and
  cannot be split into a percentage reliably, which is why the downloader is ours.

## What the two halves show

Same host, same routes, two callers:

| Panel (Machines tab) | Agent (`athena_machines`) |
| --- | --- |
| A card per machine: chip, OS line, one verb (`Start`/`Stop`), `Run a command`, and a `⋯` menu holding snapshot / forget / destroy behind confirms | `op: list\|setup\|create\|start\|stop\|exec\|snapshot\|delete` |
| A Setup card per missing prerequisite, carrying the command, a **Copy** button and **Set it up** | `needs_setup` in the same shape, and the tool's description tells the agent to **ask you** |
| Unclaimed distros listed below, each an offer to attach — never automatic | `unclaimed` in `list`, so the agent can offer the same choice |

The console is the panel's `Run a command` field plus an output panel; a WSL guest shares
localhost, so a service started in it is reachable from Hermes exactly as if it were local.

## Verified facts this is built on

Checked on this machine rather than assumed:

- WSL 2.7.14 — `--install [--from-file <Path>] [--name] [--location] [--fixed-vhd]
  [--vhd-size] [--web-download] [--no-distribution]`, `--import`, `--export <Distro> <File>`,
  `--manage --resize/--set-sparse/--set-default-user/--move`, `--mount --vhd`, `--terminate`,
  `--unregister`, `--shutdown`, `--status`. `--install --from-file` is the clean way to make a
  machine from a rootfs tarball; `--import` is the fallback for older WSL.
- `wsl.exe -l -v` writes **UTF-16LE with no BOM**, `*` marks the default, columns are padded,
  lines end `\r\n`. Decode it or the distro names arrive as `U\0b\0u\0n\0t\0u\0`.
- `SoftwareFreedomConservancy.QEMU` exists in winget (11.1.0). QEMU is **not** installed here.
- On QEMU 11.1.0 at `C:\Program Files\qemu`: `-device usb-tablet` is **refused** on `-machine q35`
  (`No 'usb-bus' bus found for device 'usb-tablet'`) and accepted once `-usb` is added — checked by
  starting the line and watching it stay up, not by reading it.
- Ubuntu ships a WSL rootfs at
  `https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz`
  (~340 MiB, HTTP 200).

## Slices

1. **WSL** — **done (0.23.0).** Registry and spec, detection, create from a rootfs URL or a
   local file, attach to an existing distro, start/stop, exec, snapshot, forget and destroy;
   `needs_setup` with the three setup methods; the **Machines** panel tab; the `athena_machines`
   agent tool; the parser, registry and setup checks in `t_machines.py` and the panel's driver
   assertions.
   Proved end to end on this machine, not mocked: `tests/live/machine_provision_check.py`
   fetched Ubuntu 24.04's WSL root filesystem (340 MB), created `athena-ubuntu-dev` with
   `wsl --install --from-file`, booted it (`Linux 6.18.33.2-microsoft-standard-WSL2 x86_64`),
   ran a command inside it as root (24 cores), reported a failing command as a failure with its
   exit code, and truncated a flood of output instead of swallowing it. That probe is
   deliberately **not** in `run_all.sh`: a test that downloads 340 MB and creates a distro on
   whoever runs the suite is an install, not a test.
2. **QEMU** — **done (0.24.0), installer and image folder added (0.26.0).** Detection with the install prompt; disks; boot an ISO or an
   existing `.qcow2`/raw disk; accelerator discovery (WHPX / KVM / TCG); Windows guests given
   IDE + e1000 automatically; explicit loopback-only `hostfwd` forwards from `ssh.port` and
   `ports`; `display: panel|window|none` with VNC on loopback; serial output captured to
   `console.log`; process-table state with stale-pidfile handling; snapshots refused while
   running; the image fetched into the machine's own folder as a job with a progress line.
   Verified by `t_machines.py` (**162 checks**, 4 skipped where this computer cannot answer) on a computer with **no QEMU installed** — which
   is the point of the pure command line: the argv for both guest OSes, both image kinds, the
   forwarded ports, the display, the disk membership and every port-shape refusal are all
   checked here, and the network paths that need QEMU itself are checked by asserting what it
   says when QEMU is absent (`needs_setup`, "half installed" for `qemu-img`).

   The live half is `tests/live/machine_provision_check.py --qemu`: it fetches Alpine's virt
   ISO (61 MB), boots it in `PANEL` display, waits for the guest's own boot output to reach
   `console.log`, reads its screen the way the panel reads it at 1280x800, sends it a keystroke,
   **moves the screen out to a window and back** (both a restart, and the step that catches the
   two failures above), asserts a shell command is refused *with the forwarded port named*, stops
   it, snapshots the disk, and destroys it. It also caught the one that mattered: a fetched
   image's path was never saved, so every later start re-ran the provisioning job. Run here on 2026-10-14 against QEMU 10 at `C:\Program
   Files\qemu`; it says SKIPPED, without changing anything, on a computer with no QEMU.
3. **The screen, in a tab of its own — done (0.30.0).** The plugin reads QEMU's VNC framebuffer
   server-side and the machine's **own tab** draws it (`MachineScreenTab`), filling the pane,
   refreshed on a timer that follows the guest (1.2 s while it changes, 2.5 s once two frames are
   byte-identical). Start opens that tab; the card's button opens the same tab; "Open in a new
   window" is a restart into QEMU's SDL display and the same button brings it back, saying which
   happened. Input travels back over RFB, so an installer's questions can be answered in the
   panel: verified end to end against a real ISO, which reached its installer's keyboard prompt
   and responded to a keystroke, and against the omarchy ISO (a real 640x480 text-mode installer
   screen — small because the guest is small, which is exactly why the picture belongs in a pane
   rather than a card).
4. Docs, constraints, skill.
