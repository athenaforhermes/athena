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

## 3. Node: a vendored engine

The engine — the thing that actually runs an app — is `pinokiod` **8.2.0**, vendored with its
`node_modules` under `engine/`, pinned in `engine/package.json` and locked by
`engine/package-lock.json` (lockfileVersion 3).

Vendored on purpose:

* an install that needs `npm` to work is an install that fails on a machine with no network, no
  npm, or a proxy in the way;
* the tree is reproducible from the lockfile, and the plugin ships what it tested;
* 501 packages, all under permissive licences (MIT 397, ISC 48, BSD-3-Clause 17, and the rest),
  inventoried in `THIRD-PARTY.md`.

`THIRD-PARTY.md` is **generated from each package's own `package.json` on disk** — not from a
guess about what a name usually is. To regenerate after an engine change:

```bash
python tests/live/t_release.py --dump-third-party > THIRD-PARTY.md
python tests/live/t_release.py
```

The second command is the gate: it re-reads the inventory and fails if a licence group, a
package name or the declared Python list disagrees with what is on disk. Comparing the licence
lines exactly is not possible (npm's metadata moves), so the comparison is per licence group,
name-sorted — a change that matters shows up, a reordering does not.

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
a scan and never print its value. Nothing downloads at install time.

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
