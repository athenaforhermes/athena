# What Athena trusts, and what it refuses

Written because the alternative is a plugin that runs other people's code and never says so.
Every claim here is either a rule the code enforces or a weakness named out loud; the enforced
ones have tests, and the tests are named next to them.

The short version: **Athena has one privilege boundary — the person in front of it — and no
sandbox.** Everything else in this file is about being honest at the edges of that.

---

## 1. What an installed app is

An installed app is a folder of scripts from a git repository you named, run on this machine by
this machine's own Node and Python. It runs **as you**.

That is the design. An app that generates images needs your GPU, your model cache, your disk,
and a port to serve a UI on; a sandbox that denied all four would be a sandbox that runs no
apps. So:

| An installed app can | Because |
| --- | --- |
| read anything you can read | it is your user |
| write anywhere you can write | same |
| open network connections | model downloads, package installs, its own API calls |
| start processes, and keep them running | that is what a "running app" is |
| read Athena's own settings file | it lives inside Athena's home |

**What follows from that:** install apps from repositories you would run yourself. Athena
reports what an app asks for — `athena_app_health` lists a missing interpreter or model before
you launch it — but it does not audit an app's code, and it cannot.

**Not defended, on purpose:** running two apps as different OS users, per-app network policy,
seccomp/AppContainer-style confinement, signed manifests. Any of those would be a real boundary
and none is in 1.0. Athena refuses to *implement* what it cannot *test*, and a sandbox nobody
tests is a claim, not a wall.

## 2. Where credentials live

| Credential | Where | Who may read it |
| --- | --- | --- |
| Hugging Face token | `<athena home>/cache/HF_AUTH/token` | the app that asked for it, through its own environment; the panel to *name* it, never to print it |
| Publish token | the environment, supplied by the store's own tooling | the publish flow, for one push |

Athena's own token file is refused to the file browser even though it sits inside Athena's home
— scope says yes, the credential rule says no, and the rule is the one that answers
(`tests/live/t_security.py`).

**The rule, verbatim, because it is the one that is easiest to break while looking helpful:**

> 扫描只列『被命名但未持有』的键名与位置，**绝不输出键值**；任何 token/密码/连接串写 [REDACTED]

A scan of an app's folder reports **names and locations** and nothing else. The test plants a
real token in a real folder, scans it, then searches the entire serialized result for the token,
for any sixteen-character fragment of it, and for any surviving token-shaped run. A scan that
leaks is worse than a scan that finds nothing, so the leak is the thing under test.

## 3. What Athena refuses

These are the places where Athena constrains *itself*. Each is a rule in `dashboard/files.py`,
`dashboard/keyscan.py` or the app-id resolvers, and each has a check in
`tests/live/t_security.py` (63 checks).

### The file browser's scope

| | Reaches | In code |
| --- | --- | --- |
| **Reads** | Athena's home, plus every fixed disk | `_read_roots()` |
| **Writes** | Athena's home, plus your own user folder | `_write_roots()` |
| **Neither** | credential stores, wherever they are | `_secret_reason()` |

Reads are deliberately wide: a file browser that cannot show you your own photo library is a
feature nobody uses. Writes are narrow, because a write is the one that cannot be taken back.

Everything is decided on the **resolved** path. A link inside an allowed folder whose target is
outside it is outside it — which is the case a string-prefix check gets wrong, so the test
plants a real link (a junction on Windows, where a directory symlink needs a privilege this
plugin should not require) and asks through the real resolver.

### What the second question catches

Scope alone would hand over a private key, because `~/.ssh` is inside your own folder. So a path
also has to pass "what is this":

* `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube` — credential stores, whatever the scope says;
* Athena's own `cache/HF_AUTH`;
* `config.yaml` and its relatives — Hermes' own settings file;
* a *link* to any of the above, which is why the check resolves first.

**And the non-refusals matter just as much**: an app's own `.env` inside Athena's home is
allowed. The rule deliberately did not inherit filebox's "any file named `.env`" clause — an
installed app legitimately owns one, and a scope rule that refuses the app's own config is a bug
wearing a security costume. A guard that refuses everything is not a guard, it is a broken
feature, so both halves are tested.

### An app id is a name, not a path

`/apps/..` is reachable from a URL, `..` is a directory, and it resolves to the Athena home —
every app, every model cache, `settings.json`. The uninstall route deletes what it resolves, so
an id passes a name check (no separators, not `.` or `..`) **and** the folder has to be a direct
child of `api/` by resolved path. A link in `api/` pointing elsewhere is refused as "not
installed" rather than followed.

### The screen is a work surface, not a security boundary

Stated plainly, because it is the easiest thing here to over-claim. The guest runs in a VM on
this machine and the panel streams it. The **control lease** (`dashboard/lease.py`) — one
keyboard, a person always wins, an agent refused while a person holds it — is a *tool-level*
fence:

* it stops two things that are both cooperating with it, which is the actual failure mode: an
  agent typing an installer's answers while a person is also clicking;
* it is enforced **before** anything is sent to the guest, so a refused keystroke never leaves —
  and the test asserts that against a screen that counts what it was given, with a control case
  proving the same keystroke *does* arrive when the lease is not consulted;
* it is **not** an OS boundary. Anything on this machine that can reach the engine's port can
  drive the guest without asking the lease.

The engine port binds **loopback only**. Any process running as any local user on this machine
can reach it. That is an accepted weakness, and it is the same shape as the browser's DevTools
port: loopback is not a wall between users on one box, it is a wall between this box and the
network.

A **guest's screen is not a loopback port**. It used to be — `-vnc 127.0.0.1:<port>`, reasoned as
"a guest display is not for the LAN" — and that reasoning was true and pointed at the wrong
attacker. The thing a loopback port is reachable by is not the network, it is every other program
running as this user, and a guest's screen is a live feed of whatever that guest is doing,
including the passwords someone types into it. It is a unix socket inside the machine's own folder
now (`machines.vnc_bind`), given mode `0600` the moment QEMU has made it, and the screen reader
dials the socket with the address it was launched with (`screen._open` handles both spellings,
because a machine started before this change still has a port and still has a screen worth
showing).

What that does and does not buy, precisely: on Linux and macOS the mode is the guarantee — another
account on the box cannot open the socket. On Windows a mode is advisory and the **ACL** is what
decides access, so the socket's location inside the machine's own folder is the half that holds
there, and the mode is the half that a future reader should not mistake for the whole thing.

**And on Windows the socket is not used at all.** This paragraph used to claim the socket for both
platforms on the strength of a measurement that QEMU 11.1 creates one on Windows — which it does,
and which is only the giving-away half. The taking-away half is Python, and the CPython 3.11 on
this box has no `socket.AF_UNIX`: the guest starts, runs, and serves a socket that **nothing here
can open**, so the panel shows an empty screen and it reads as a broken guest. A display nobody can
dial is not a more private display. So the choice is a feature test on the running interpreter
(`machines.unix_sockets_available()`, read from `socket` itself rather than from `sys.platform`):
Linux and macOS get the socket, **Windows keeps the loopback port** — which is therefore still a
live accepted weakness on Windows, named as such below rather than implied away — and
`machines.vnc_bind`'s `unix=True` branch is what the tests pin the socket's shape with.

A guest started before the socket change is still on its loopback port until it is next stopped,
which is why the old spelling is still reachable behind `unix=False`: refusing to read a running
machine's screen would have broken the feature this change was made to protect.

### The publish flow

Publishing pushes a store entry to a repository you name, with a token you supply. The flow
**refuses to push with no credential** (the route answers rather than letting git prompt into a
log nobody reads), the token travels as an environment variable that a shell-level credential
helper reads — never as `git config` on the command line, which lands in shell history — and the
job log the panel shows is tested for the token, for any fragment of it, and the checkout's
`.git/config` is tested for it too, because that file outlives the push.

## 4. Accepted weaknesses, in one place

| Weakness | Why it is accepted | What it would take |
| --- | --- | --- |
| An installed app can do anything you can | that is what running an app means | per-app OS user or VM, per-app network policy |
| Loopback ports are reachable by any local process | the engine is a local service | per-request authentication between panel and engine |
| The lease is a tool-level fence | it fixes the real failure (two cooperators) | OS-level input ownership |
| A non-US keymap lands US characters | keysyms are sent as US layout names | a per-machine keymap the lease would have to own |
| `api/` folders are trusted by name, not by signature | installs are from repos you name | signing and verification |
| macOS is untested | nobody has run the suite there | a macOS run in CI, which R3 covers for Linux only |
| A shared memory number decides whether a guest may start | `available` is the kernel's estimate, and something else can take the memory between the check and the boot | cgroups or a job object, which is a resource manager rather than a plugin |
| A Windows guest's screen is a loopback port | Windows CPython here has no `socket.AF_UNIX`, so the socket spelling gives a display nothing can open — a broken screen, not a private one | a reader that can dial a socket on Windows, at which point the mode and ACL work applies to it |
| A guest's socket mode is advisory where it *is* used | the ACL decides, and the socket's folder is the part that holds | per-boot ACLs set at creation |
| What a *guest* copies is read and discarded | there is nowhere in the panel to put it, and dropping the message is worse than holding it — an unread message stalls the stream | a clipboard pane, and the RFB encoding question that comes with it |
| A first start that has to provision is not put on the idle clock | the job outlives the call, and arming a guest that does not exist yet is a watch on nothing | the job recording who asked for it, so the arming survives the call that asked |
| The panel's job log is readable by whoever can read Athena's home | it is your machine, and the log's value is in being readable | per-user log permissions |

## 5. How these claims are kept true

* `tests/live/t_security.py` — 63 checks: the refusals, the non-refusals, the redaction, the
  publish credential.
* Each refusal check was **observed failing against a deliberately weakened guard** before it
  was trusted: with `_in_roots` reduced to a string prefix and `_secret_reason` stubbed to "" ,
  twelve checks went red, including the `..` walk, the linked folder, the symlinked app folder,
  `~/.ssh`, and the credential-store link. Restored, 63/63. A security test that has never seen
  the bug it is supposed to catch is a test of nothing.
* `tests/live/t_keys.py`, `t_keyscan.py` — the scan's own behaviour, including that it never
  prints a value.
* `tests/live/t_lease.py` — the lease, its control case, and that a refusal happens before the
  wire.
* `tests/live/t_release.py` — the licence inventory and the declared dependency list.
