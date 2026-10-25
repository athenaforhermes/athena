# What Athena ships, where it came from, and how to re-verify it

A plugin that installs things should say what *it* is made of. This is that list, with the reason
each item is here and the procedure for changing it. Two tests keep it honest:
`tests/live/t_release.py` (the licence inventory and the declared dependency list, both read from
the files themselves) and `tests/live/t_security.py` (the publish credential).

---

## 1. Athena's own code

MIT, © Zack Horton. `LICENSE` is the licence; `NOTICE` carries what MIT asks to be carried.

## 2. Python: four declared packages

Declared in `plugin.yaml` as `python_dependencies`, pinned to a range, and installed into
**Hermes' own interpreter** — Athena does not create a virtualenv of its own, because a second
interpreter on a user's machine is a second thing to explain.

| Package | Range | Why it is needed | Why not something else |
| --- | --- | --- | --- |
| `send2trash` | `>=1.8,<2` | deleting an app or a file moves it to the OS recycle bin instead of unlinking it | `os.remove` is not recoverable, and "Athena deleted my model cache" is not a sentence to earn |
| `psutil` | `>=5.9,<9` | which processes are running, how much memory is free, what the machine is — `athena_report`, headroom refusals, machine ids | the stdlib has no portable process or memory API; shelling out to `wmic`/`free` is slower and lands differently per platform |
| `Pillow` | `>=9,<14` | thumbnails, the screen stream's encoders, image metadata | it is the only image library that is already everywhere |
| `numpy` | `>=1.24,<3` | the framebuffer and screenshot maths (diffing, scaling, the changed-rectangle path) | pure-Python loops over a 1280×800 frame are the difference between a live screen and a slideshow |

`psutil` is marked `sys_platform != 'android'` — the one platform where it is not available, and
the marker is the honest way to say so rather than an import guard that fails late.

**Changing this list:** edit `plugin.yaml`, then run `python tests/live/t_release.py`. It reads
the declaration, compares it against the packages the code actually imports, and fails on a
drift in either direction — an import that was never declared, or a declaration nothing uses.
That is the whole gate; there is no second list to forget to update.

## 3. Node: an engine that is pinned, not vendored

The engine — the thing that actually runs an app — is `pinokiod` at **one exact version**, the
version written in `engine/package.json` and locked by `engine/package-lock.json` (lockfileVersion
3), which pins that version *and* every transitive dependency of it by version and by integrity
hash. There is no range and no floating tag.

The pin is the manifest and the lockfile. **The engine is not vendored**, and a published
repository carries no engine code and no `node_modules` — setup installs the engine from npm, from
that lockfile, at that version, and **only behind a consent step that names the version**. Nothing
is fetched before you agree. Until that install has run once, an app cannot start; the panel says
so instead of working around it.

Why it is provisioned rather than shipped, stated as a trade rather than a win:

* an install now needs the network and a working registry once, where a vendored tree needed
  neither;
* what that buys is a published repository that holds only Athena's own code, and an engine whose
  every entry is checked against npm's own integrity hashes on the way in;
* on a machine with no network, browsing the catalogue still works and running an app does not.

The 501 packages are the same tree either way, all under permissive licences (MIT 397, ISC 48,
BSD-3-Clause 17, and the rest). `THIRD-PARTY.md` is their inventory, **generated from each
package's own `package.json` on disk** — from a local install of the lockfile, not from a guess
about what a name usually is. To regenerate after an engine change:

```bash
cd engine && npm ci              # the lockfile's tree, on disk — the consent step does this
python tests/live/t_release.py --dump-third-party > THIRD-PARTY.md
python tests/live/t_release.py
```

The second `python` command is the gate: it re-reads the inventory and fails if a licence group, a
package name or the declared Python list disagrees with what is on disk. Comparing the licence
lines exactly is not possible (npm's metadata moves), so the comparison is per licence group,
name-sorted — a change that matters shows up, a reordering does not. It reads the tree, so it
needs the install to have happened: a checkout with no `engine/node_modules` (which is what a
clone of the repository is) cannot run this gate until it does.

**Node itself is not vendored.** Athena finds the Node Hermes already downloaded
(`_find_node`), or one you point it at with `ATHENA_NODE`. Supported: 22–24. 25+ is refused on
purpose: the engine's native addons (`node-pty`, `better-sqlite3`) ship prebuilt binaries only up
to Node 24, and Node 26 fails both at install and at boot — a refusal with a reason beats a
stack trace.

**Updating the engine:** bump `pinokiod` in `engine/package.json`, `npm install` in `engine/`,
regenerate `THIRD-PARTY.md`, run the full suite. The suite's `build committed` step is the
tripwire for the panel half; `t_release.py` is the tripwire for this half.

## 4. Models and other downloads

Model weights are not shipped. If an app needs one, the app downloads it from Hugging Face under
your own token (`<athena home>/cache/HF_AUTH/token`), and Athena's role is to *name* the token in
a scan and never print its value. Installing an app downloads that app's repository — that is what
a clone is — and nothing else. The one download *setup* performs is the engine itself, once, from
npm, at the pinned version and behind a consent step (§3).

## 5. What Athena installs on your behalf

**Athena installs from repositories you name; nothing is fetched from a list Athena controls.**

There is no curated feed, no auto-update of installed apps, and no telemetry. The catalog the
panel can show is a file with entries in it — a URL and a description — and installing is a
`git clone` of that URL, run as you. This is the custom-node policy and it is deliberate: the
moment Athena maintains a list it also owns the trust decision that comes with it, and a plugin
that runs other people's code as the user should not also be the thing choosing which code.

What that means for you: the same judgement you use before running a repository's install script
is the judgement this needs. `docs/threat-model.md` §1 says the same thing from the other side.

## 6. The publish flow's credential

Publishing pushes to a repository you name, using a token you supply at publish time. The route
refuses without one; the token is passed to git as an environment variable read by a shell-level
credential helper rather than as command-line config (which lands in shell history); it is not
written into the published checkout's `.git/config`, which outlives the push. All four of those
are checks in `t_security.py`.
