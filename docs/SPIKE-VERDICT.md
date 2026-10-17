# Athena Phase 0 — Spike Verdicts

Date: 2026-10-11. Engine probed: `pinokiod@8.2.0` (npm), stand-alone install at `C:/tmp/rhe`.

## Task 1 — Headless boot: **PASS**

`pinokiod` boots as a plain Node process. No Electron anywhere in the path.

- Entry: `new Server({...config}).start({debug:true})` (pattern from `pinokiod/script/index.js`).
- Requires a store shim with a **property** named `store` (not a method) — `kernel/index.js:82`
  calls `this.store.clone(store.store)` and throws `ERR_INVALID_ARG_TYPE` if `store.store` is undefined.
- Serves the complete Pinokio UI: `GET /` → **200**, `<title>Pinokio</title>`, 4815 bytes.
- Boot log on success: `Server listening on http://localhost:43100`.

### Port
`server/index.js:165` sets `this.port = DEFAULT_PORT` (42000) in the constructor and
**ignores `config.port`** (commented out at line 166). `start()` only assigns DEFAULT_PORT
`if (!this.port)`. Therefore:
**Override before start: `server.port = N; server.start(...)` — honored, no patch required.**
Verified listening on 43100 while another instance held 42000.

### Home resolution (`kernel/index.js:1094`)
`let home = this.store.get("home") || process.env.PINOKIO_HOME`
The kernel's `Store` root is hardcoded: `path.resolve(os.homedir(), ".pinokio")` (`kernel/store.js`).

**Isolation recipe (verified): spawn the engine with**
- `USERPROFILE=<plugin>/engine/profile`
- `PINOKIO_HOME=<athena home>`
- `HOME` unset

`os.homedir()` honors `USERPROFILE` on Windows (verified) and ignores `HOME`.
Result: store lands at `<profile>/.pinokio/config.json`, home resolves to PINOKIO_HOME,
and the real Pinokio config (`~/.pinokio/config.json`) is never read or written.

## Task 2 — Electron / native audit: **PASS**

- **Zero** `require('electron')` and zero `electron-store` references anywhere in `pinokiod`.
  (`electron-store` lives only in the Pinokio *Electron app's* own `config.js`, not the kernel package.)
- Native addons present and loading for win32-x64:
  `@homebridge/node-pty-prebuilt-multiarch` (`conpty.node`, `pty.node`, `winpty-agent.exe`),
  `better-sqlite3` (`better_sqlite3.node`), `@parcel/watcher-win32-x64` (`watcher.node`), `koffi`.

### CRITICAL: Node version
Node **26.7.0** (ABI 147) **FAILS** — no `node-pty-prebuilt-multiarch` prebuild exists for that ABI,
and the source fallback dies with `spawn EINVAL`.
**Pin the engine to Node 22.21.1 (ABI 127)** — installs clean, all natives present.
Node 24.10.0 (ABI 137) is untested-but-plausible fallback.

## Task 3 — Desktop embedding: **PASS (iframe)**

`curl -D -` against the engine shows **no `Content-Security-Policy` and no `X-Frame-Options`**,
and `Access-Control-Allow-Origin: *`. The Pinokio UI can be iframed directly.

## Task 4 — Agent tools: **CONFIRMED**

Hermes plugins register real agent tools from `~/.hermes/plugins/<id>/__init__.py`:

```python
def register(ctx):
    ctx.register_tool(name=..., toolset=..., schema={...}, handler=...)
```

Unified package layout therefore supports all three halves at once:
`plugin.yaml` + `__init__.py` (agent tools) + `dashboard/plugin_api.py` (HTTP) + `desktop/plugin.js` (UI).

## Credential inheritance (Zack's requirement)

- pinokiod ships connect providers **github / huggingface / x**; OAuth tokens persist to
  `<home>/connect/<name>/auth.json`. HuggingFace additionally uses an `hf` CLI token at
  `<home>/cache/HF_AUTH/token` and reads `HF_TOKEN_PATH`.
- Git auth: Pinokio writes `<home>/gitconfig` using git-credential-manager
  (`namespace = pinokio`, `gitHubAuthModes = oauth`).
- Hermes side available to inherit from:
  - GitHub: the `gh` CLI's own stored token (keyring), scopes gist/read:org/repo/workflow.
  - HuggingFace: `hf` CLI in the Hermes venv (`hermes-agent/venv/Scripts/hf`).
  - Model/provider keys: `~/.hermes/auth.json` → `credential_pool.<provider>[0]`
    (nous, openrouter, deepseek, meta, copilot, muse, nvidia, …).
- Existing Pinokio install already holds a live HF token at `C:\pinokio\cache\HF_AUTH\token`.

**Injection plan:** on first start, seed `<home>/connect/huggingface/auth.json` +
`<home>/cache/HF_AUTH/token` and `<home>/gitconfig` from those Hermes sources; export app env
vars from the same pool. No re-login, no re-entering keys.

## Discovered: the real Pinokio install

- Home: **`C:\pinokio`** (from `~/.pinokio/config.json` → `"home"`). Not `~/pinokio`.
- 8 installed apps under `C:\pinokio\api\`: Hunyuan3d-2-lowvram, Maestro, remove-video-bg,
  RMBG-2-Studio, stabledaw, stable-diffusion-webui-forge, wan, yue2-groove-pinokio.

## Side effect to disclose

The very first spike boot ran without `USERPROFILE`/`PINOKIO_HOME` set and therefore resolved to the
**real** home. It rewrote `~/.pinokio/config.json`, adding/refreshing the `access` block
(protocol/host/port/candidates + `updated_at`) and `version`. It did **not** change `home` or `theme`.
Real Pinokio rewrites that block on its own boot, so this is benign — but it was real state, and every
subsequent probe ran fully isolated.
