# Building an Athena app from a repository

You are the generator. Athena does not call a model to build apps — the Hermes
agent reads this guide, reads the repository, and writes the launcher. Athena
supplies the folder, the runtime, and the installer that runs the result.

Read this whole file before writing anything, then **adopt an example** from
`docs/examples/` instead of inventing structure. The examples are real launchers
and their exact syntax is the contract.

---

## 1. What an "app" is

An Athena app is a folder in `<ATHENA_HOME>/api/<name>/` whose root contains a
launcher: `athena.js` plus the scripts it references. The app's *own code* is
not part of the launcher — `install.js` clones it into `app/` inside the launcher
folder. That separation is the whole design: the launcher is yours and
reproducible; `app/` is the upstream project, untouched.

```
<ATHENA_HOME>/api/<name>/
├── athena.js           # the menu: ties every script together, decides defaults
├── <legacy name>       # a one-line shim: the engine resolves only this filename
                        #   (the literal string is in docs/NAMING.md)
├── athena_meta.json    # metadata: title, description, icon, version
├── install.js          # clone the app repo + install every dependency
├── start.js            # launch it; capture the URL the server prints
├── update.js           # git pull the launcher and the app
├── reset.js            # delete what install.js created, back to zero
├── README.md           # what it is, how to use it, how to call it
├── .gitignore          # app/, venv/, node_modules/, logs/, cache/, *.db …
├── icon.png            # optional but wanted: a square icon
└── app/                # ← created by install.js, not by you
```

If `athena.js` is missing the folder is **not an app** — Athena will show it as
uninitialized and refuse to run it. A folder containing only a cloned repository
is exactly that state; giving it a launcher is what turns it into an app.

`athena_meta.json` is the metadata file Athena reads. An app that predates the
rename may carry the older names instead; Athena reads those too, preferring its
own (the mapping table is `docs/NAMING.md`). Write `athena_meta.json`.

---

## 2. Decide which kind of launcher you are writing

| Kind | Needs | Example |
|---|---|---|
| **Server with a web UI** (gradio, streamlit, uvicorn, node/express, comfyui…) | `install.js` + `start.js` + `athena.js` + `athena_meta.json` | `docs/examples/python-gradio-server/`, `docs/examples/node-server/` |
| **Static web app** (pure HTML/JS, no backend) | `index.html` at the launcher root + `athena_meta.json`. No scripts at all. | `docs/examples/static-site/` |
| **CLI / no web UI** | `athena.js` + `athena_meta.json` (+ whatever scripts the menu points at) | `docs/examples/cli-app/` |
| **Local AI model runner** (torch, diffusers, cuda) | as server, **plus** `requires: { bundle: "ai" }` in `install.js` and the bundled `torch.js` step | `docs/examples/ai-python-server/` |

Look at the repository's README first. If it says "open http://localhost:…",
it is a server. If it says "run `python main.py --input …`", it is a CLI. If it
is a library or a model repo with no runnable entry point, **stop and say so** —
do not invent a UI for something that has none.

---

## 3. The canonical `start.js` (server)

This is the pattern. Use it or adapt it; do not invent a different shape without
a reason you can state.

```javascript
module.exports = {
  // Keeps the shell alive after the run array finishes. MANDATORY for servers:
  // without it the server process is killed the moment the script ends.
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        venv: "env",              // creates/reuses <launcher>/env — python apps only
        env: { },                 // extra environment variables
        path: "app",              // RELATIVE path to run from. Never absolute.
        message: [
          "python app.py",
        ],
        on: [{
          // Watch the terminal for the line that announces the server.
          // The parenthesised group becomes input.event[1] in the next step.
          "event": "/(http:\\/\\/\\S+)/",
          "done": true            // move on, keep the shell alive
        }]
      }
    },
    {
      // Publish the captured URL as a local variable named `url`.
      // athena.js reads it via info.local("start.js").url to show "Open Web UI".
      method: "local.set",
      params: { url: "{{input.event[1]}}" }
    }
  ]
}
```

**Capturing the URL is not optional.** Without it there is no way to open the
app. If the server prints nothing usable, set it explicitly after the port is
known:

```javascript
{ method: "local.set", params: { url: "http://127.0.0.1:{{port}}" } }
```

## 4. The canonical `install.js`

```javascript
module.exports = {
  // Only when the app installs torch/cuda: declares the machine-level AI
  // prerequisites (CUDA, HuggingFace CLI) so they are installed first.
  requires: { bundle: "ai" },
  run: [
    {
      method: "shell.run",
      params: {
        message: [ "git clone https://github.com/OWNER/REPO app" ],
        "when": "{{!exists('app')}}"     // idempotent: never re-clone
      }
    },
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [ "uv pip install -r requirements.txt" ]
      }
    }
  ]
}
```

Rules that make installs actually work:

- **`uv` over `pip`**, always, even when the README says pip: `uv pip install -r requirements.txt`.
- **`conda` for binaries** (ffmpeg, cmake, sox, git-lfs…), `npm` for node, `bun` if the project wants bun. All are preinstalled.
- **Every step must be idempotent** — guard clones and downloads with `"when": "{{!exists('…')}}"`. Users re-run installs.
- **Guard a clone on `app/.git`, never on `app/`.** The app's virtual environment
  lives *inside* `app/` (that is how the kernel resolves `venv` against `path`),
  so anything that touches the environment first makes a bare `exists('app')`
  check pass and the clone is silently skipped — you end up installing a
  dependency tree into a folder with no code in it.
- **Pin the Python version when the project needs a newer one:**
  `venv: { path: "env", python: "3.12" }`. The machine's default interpreter may
  be older than the project requires, and `uv` fails the resolve rather than
  telling you why in plain words.
- **No Docker.** Ignore Docker instructions in the README; write per-platform steps instead.
- **Download weights with `hf.download`** (HuggingFace CLI) rather than a raw `curl`, and put the destination under `cache/` or `app/`, listed in `.gitignore`.

## 5. The canonical `athena.js`

`athena.js` builds the menu from the current state, so the right thing happens
without the user choosing: install if not installed, start if not running, open
the UI once it is up.

```javascript
module.exports = {
  version: "7.0",                       // schema version — leave as-is
  menu: async (kernel, info) => {
    let installed = info.exists("app")
    let running = info.running("start.js")
    let local = info.local("start.js")
    if (local && local.url) {
      return [
        { default: true, icon: "fa-solid fa-rocket", text: "Open Web UI", href: local.url },
        { icon: "fa-solid fa-terminal", text: "Terminal", href: "start.js" }
      ]
    }
    if (installed) {
      return [
        { default: !running, icon: "fa-solid fa-power-off", text: "Start", href: "start.js" },
        { icon: "fa-solid fa-rotate", text: "Update", href: "update.js" },
        { icon: "fa-solid fa-eraser", text: "Reset", href: "reset.js" }
      ]
    }
    return [
      { default: true, icon: "fa-solid fa-download", text: "Install", href: "install.js" }
    ]
  }
}
```

- `default: true` on exactly one item, on the step the user should take next.
- `info.exists(path)` / `info.running(script)` / `info.local(script)` drive it.
- No stop script is needed — anything startable is stoppable from the UI.

## 6. `update.js` / `reset.js`

```javascript
// update.js — pull the launcher, then the app
module.exports = { run: [
  { method: "shell.run", params: { message: [ "git pull" ] } },
  { method: "shell.run", params: { path: "app", message: [ "git pull" ] } }
]}
```

```javascript
// reset.js — delete what install.js created; the user is back to a clean slate
module.exports = { run: [
  { method: "fs.rm", params: { path: "app" } },
  { method: "fs.rm", params: { path: "env" } }
]}
```

## 7. `athena_meta.json`

```json
{
  "title": "Human Readable Name",
  "description": "One sentence on what it does, in the user's words.",
  "icon": "icon.png",
  "version": "2.0"
}
```

- **Do not change `version`** — it is the launcher schema version, not the app's.
- `icon`: download a square image to `icon.png` in the launcher root. The repo
  owner's GitHub avatar (`https://api.github.com/users/<owner>` → `avatar_url`)
  is a good default, and `icon.jpg`/`icon.png` both work.
- If the app only runs on one platform, declare it here (`"platform": ["win32"]`,
  `"arch"`, `"gpu"`) instead of silently failing on the others.

---

## 8. Template expressions (use these, don't hardcode)

| Expression | Meaning |
|---|---|
| `{{port}}` | the next free port — **never hardcode a port from a README** |
| `{{which('bash')}}` | absolute path of a command, resolved at runtime |
| `{{exists('app')}}` | does a relative path exist (use with `"when"`) |
| `{{running('start.js')}}` | is that script running |
| `{{local.url}}` | a local variable set earlier in this script |
| `{{input.event[1]}}` | capture group from the previous step's `on.event` |
| `{{args.x}}` | parameter passed in from `athena.js` `params` |
| `{{cwd}}` `{{uri}}` `{{self.daemon}}` | current folder, script uri, own fields |
| `{{platform}}` `{{arch}}` `{{gpu}}` | `win32`/`darwin`/`linux`, arch, primary GPU vendor |
| `{{os.*}}` `{{path.*}}` | Node's `os` and `path` modules |

## 9. Script API

`shell.run` (venv, env, path, message, on, sudo, shell, input, chain) ·
`local.set` · `input` · `filepicker` · `fs.write` / `fs.read` / `fs.copy` /
`fs.download` / `fs.link` / `fs.open` / `fs.cat` / `fs.rm` · `json.set` /
`json.get` / `json.rm` · `jump` · `log` · `net` · `notify` · `script.start` /
`script.stop` / `script.download` / `script.return` · `web.open` · `hf.download`

A leading-underscore endpoint or an `on_*` handler is widget plumbing, not a
capability — Athena filters those out when it probes an app for its agent
manifest, so don't build the app's value on one.

---

## 10. Rules that decide whether it works

1. **Bind to `127.0.0.1` / `localhost`.** If the app defaults to `0.0.0.0`, add the flag that pins it locally.
2. **Minimise launch flags.** `python app.py` beats `python app.py --port 7860 --host 127.0.0.1 --verbose`. Only add what is genuinely required.
3. **Ports:** gradio/streamlit pick a free port themselves — pass nothing. If a port is required, `--port {{port}}`. If you need the same port later, `{{port}}` once into `local.set` and reuse `{{local.x}}`.
4. **Relative paths only** in `shell.run.path`.
5. **Never modify `app/`** to make the launcher work. Put helper scripts in the launcher root and point at them. (Exception: the user explicitly asks you to patch the project.)
6. **Self-contained:** everything the app needs must be installed by `install.js` — a fresh machine must be able to run it from your launcher alone.
7. **Cross-platform** where possible; declare limitations in `athena_meta.json` when not.
8. **Write `README.md`**: what it does, how to use it, and how to call its main features programmatically (JavaScript, Python, and curl examples).
9. **Don't guess the API.** The examples in `docs/examples/` are the reference. If a step's syntax isn't in there, don't invent it.
10. **`.gitignore` the generated stuff:** `app/`, `env/`, `venv/`, `node_modules/`, `logs/`, `cache/`, `*.db`, downloaded models.

---

## 11. Write it, then prove it

Before claiming an app works:

1. **Syntax-check every script**: `node --check <file>.js`. A launcher with a syntax error fails silently in the UI.
2. **JSON-parse `athena_meta.json`.**
3. **Install it**: run the app's install action and read the log to the end. A green run is not a working app — check the last lines for errors. If Athena scaffolded this app for you, `athena_build_app(action="auto", url=…)` already did scaffold → install → prove as one job; read its verdict instead.
4. **Prove it runs. This is the last step and it is not optional:** `athena_build_app(action="smoke", app="<name>")` launches the app, waits for its port and fetches its URL. `ok: true` with an `http` code is the only evidence that counts.
5. **`ok: false` means you are not finished.** The verdict carries the log tail and the URL the app claimed; fix the launcher and smoke it again. Never report a URL you did not fetch.
6. Stop it once proved (`stop_after`), so it does not sit on the port holding VRAM.

Athena's agent tools do all of this: install, launch, read logs, probe
capabilities. Use them rather than shelling out, so the app lands in the same
state the UI would put it in.

## 12. Troubleshooting

- The app's install/start logs are the first place to look — read them, in full, before changing anything.
- `No module named X` → the step that installs X isn't using the same `venv`/`path` as the step that runs it.
- The server starts but no URL appears → the `on.event` regex doesn't match what it actually prints. Run it once by hand, copy the exact line. Two silent causes: a banner printed with `print()` is *buffered* when python is not attached to a terminal, so launch it `python -u …`; or the engine itself is down, in which case nothing can appear — check that `GET /info/scripts` answers before blaming the app.
- The URL captured is `http://0.0.0.0:…` or `http://[::]:…` → that is a bind address, not somewhere to connect. Bind explicitly (`--bind 127.0.0.1`) so the URL you announce is the URL that answers.
- The server starts and immediately dies → `daemon: true` is missing, or the command runs from the wrong `path`.
