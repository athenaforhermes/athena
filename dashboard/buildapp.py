"""Turn a repository into an Athena app.

Athena does not call a model to build apps. The Hermes agent is the generator:
it reads `docs/build-an-app.md`, reads the repository, and writes the launcher.
This module is the half that needs no judgement — it fetches the repository,
reports what it found, lays down a launcher skeleton with the mechanical parts
already correct (clone step, venv wiring, update/reset, metadata, gitignore),
and validates the result. The agent fills in the parts that need reading.

Every function here is called by both the HTTP route and the agent tool, so the
two can never drift.

Layout of a scaffolded app (`<home>/api/<name>/`):

    athena.js  athena_meta.json  install.js  start.js  update.js  reset.js
                 (+ a one-line pinokio.js shim: the engine resolves only that
                  legacy name, so the app carries both)
    README.md   .gitignore         SPEC.md     icon.png
    app/        <- created by install.js, never by this module
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import naming as athena_naming

GUIDE_REL = "docs/build-an-app.md"
EXAMPLES_REL = "docs/examples"

AI_HINTS = (
    "torch", "torchvision", "torchaudio", "diffusers", "transformers", "accelerate",
    "xformers", "triton", "sentencepiece", "bitsandbytes", "vllm", "llama-cpp",
    "onnxruntime-gpu", "cuda", "tensorflow", "jax", "flax", "safetensors",
)
SERVER_HINTS = {
    "gradio": "gradio", "streamlit": "streamlit", "fastapi": "fastapi",
    "uvicorn": "uvicorn", "flask": "flask", "django": "django",
    "express": "express", "next": "next", "vite": "vite", "http-server": "http-server",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next"}


def _run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 300,
         env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return 127, f"{e}"


def node_exe() -> Optional[str]:
    """The node that pinokiod runs on, for syntax-checking generated scripts.

    Delegates to the backend when it can: `plugin_api` is the one place that knows
    where Hermes keeps its toolchain and which versions pinokiod's native addons
    accept, and a second copy of that search here is a second copy to get wrong.
    (This file is imported by it, so the import is inside the function — by the
    time anything calls this, both modules are loaded.)
    """
    for cand in (os.environ.get("ATHENA_NODE"), shutil.which("node")):
        if cand:
            return cand
    try:
        import plugin_api as _api  # noqa: PLC0415 — same directory, deferred on purpose
        return _api._find_node()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- repo identity

def repo_parts(url: str) -> Dict[str, str]:
    """Split a repo reference into host/owner/repo. Accepts a clone URL, an
    `owner/repo` slug, or a pasted `.../tree/<branch>` URL (branch dropped)."""
    raw = (url or "").strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    raw = re.sub(r"^https?://", "", raw)
    raw = re.sub(r"^www\.", "", raw)
    parts = [p for p in raw.split("/") if p]
    if not parts:
        return {}
    if "." in parts[0] and len(parts) >= 3:          # github.com/owner/repo
        host, owner, repo = parts[0], parts[1], parts[2]
    elif len(parts) >= 2:                            # owner/repo
        host, owner, repo = "github.com", parts[0], parts[1]
    else:
        return {}
    repo = re.sub(r"\.git$", "", repo)
    return {"host": host, "owner": owner, "repo": repo,
            "clone_url": f"https://{host}/{owner}/{repo}.git",
            "slug": slug_for(f"{owner}/{repo}")}


def slug_for(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-.")
    return s or "app"


def _gh_token() -> str:
    """Hermes's existing GitHub login — never ask the user for one."""
    for cmd in (["gh", "auth", "token"],):
        rc, out = _run(cmd, timeout=30)
        tok = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if rc == 0 and tok and " " not in tok:
            return tok
    return ""


def _auth_env() -> Dict[str, str]:
    env = dict(os.environ)
    tok = _gh_token()
    if tok:
        env["GH_TOKEN"] = tok
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _fetch_json(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    tok = _gh_token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


# ---------------------------------------------------------------------- recon

def _read(path: Path, limit: int = 6000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception:
        return ""


# A manifest is JSON, and half a JSON file is not JSON — it is a syntax error. So the cap
# for the machine-readable ones is not a reading budget that can be trimmed for tidiness:
# a `package.json` that runs past it must come back whole or not be parsed at all.
JSON_MANIFEST_LIMIT = 512 * 1024
JSON_MANIFESTS = ("package.json",)


def _parse_json(text: str, where: str) -> Tuple[Dict[str, Any], List[str]]:
    """Parse a manifest, and never let one bad byte end the build.

    A 9 KB `package.json` used to raise out of `json.loads` — the file was read through an
    8 KB window, and the truncated text is a `JSONDecodeError`, not a smaller manifest. The
    whole build answered with a traceback: no scaffold, no launcher, no explanation a person
    could act on. Nothing else in this module trusts its own input (every read is wrapped,
    every subprocess is wrapped); this is the same treatment for the one input that was
    parsed with faith in it.
    """
    text = (text or "").lstrip("\ufeff").strip()
    if not text:
        return {}, []
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001 — the wording goes into the report, nothing else
        return {}, [f"{where} could not be read as JSON ({e.__class__.__name__}: {e}); "
                    f"the scanner continued without it"]
    if not isinstance(data, dict):
        return {}, [f"{where} is not a JSON object; the scanner continued without it"]
    return data, []


def _tree(root: Path) -> List[str]:
    rc, out = _run(["git", "-C", str(root), "ls-files"], timeout=120)
    if rc == 0 and out.strip():
        return [l.strip().replace("\\", "/") for l in out.splitlines() if l.strip()]
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            files.append(str(Path(dirpath, f).relative_to(root)).replace("\\", "/"))
        if len(files) > 4000:
            break
    return files


def classify(tree: List[str], readme: str, manifests: Dict[str, str]) -> Dict[str, Any]:
    """What kind of thing is this repository? Facts only — no guesses invented."""
    files = set(tree)
    lower = {f.lower() for f in tree}
    py = [f for f in tree if f.endswith(".py")]
    js = [f for f in tree if f.endswith((".js", ".mjs", ".cjs", ".ts"))]
    pkg, manifest_notes = _parse_json(manifests.get("package.json") or "", "package.json")
    scripts = (pkg.get("scripts") or {}) if isinstance(pkg, dict) else {}
    deps = ({k.lower() for k in (pkg.get("dependencies") or {})} if isinstance(pkg, dict) else set()) | \
           ({k.lower() for k in (pkg.get("devDependencies") or {})} if isinstance(pkg, dict) else set())
    req = (manifests.get("requirements.txt") or "") + "\n" + (manifests.get("pyproject.toml") or "") \
        + "\n" + (manifests.get("setup.py") or "") + "\n" + (manifests.get("environment.yml") or "")
    req_l = req.lower()

    stack = []
    if py or req.strip() or "pyproject.toml" in lower or "setup.py" in lower:
        stack.append("python")
    if "package.json" in lower or js:
        stack.append("node")
    if "index.html" in lower and not stack:
        stack.append("static")

    # The AI bundle installs machine-level python/CUDA prerequisites. A Node
    # package named "transformers" is a JS library, not a torch install — so
    # only python manifests can ask for the bundle.
    py_manifests = " ".join(manifests.get(k, "") for k in
                            ("requirements.txt", "pyproject.toml", "setup.py", "environment.yml")).lower()
    ai = sorted({h for h in AI_HINTS if h in py_manifests})
    servers = sorted({name for key, name in SERVER_HINTS.items()
                      if key in req_l or key in " ".join(deps) or key in readme.lower()})

    # entry point, in order of confidence
    entry, kind = "", "unknown"
    for cand in ("app.py", "gradio_app.py", "webui.py", "main.py", "server.py", "run.py",
                 "app/main.py", "src/main.py", "demo.py"):
        if cand in files:
            entry, kind = cand, "server"
            break
    if not entry:
        for cand in ("server.js", "index.js", "app.js", "src/index.js", "main.js"):
            if cand in files:
                entry, kind = cand, "server"
                break
    if kind == "unknown":
        if "index.html" in lower and "static" in stack:
            kind = "static"
        elif py or js:
            kind = "cli"

    install: List[str] = []
    if "python" in stack:
        if "requirements.txt" in lower:
            install.append("uv pip install -r requirements.txt")
        elif "pyproject.toml" in lower or "setup.py" in lower:
            install.append("uv pip install -e .")
    if "node" in stack and ("python" not in stack or "package.json" in lower and not py):
        if "bun.lock" in lower or "bun.lockb" in lower:
            install.append("bun install")
        else:
            install.append("npm i --no-audit --no-fund")

    launch = ""
    # `-u` matters: the engine watches stdout for the URL, and python block-buffers
    # stdout when it is not a terminal. A server whose banner is print()ed then
    # never announces its port — it runs perfectly and looks dead to the engine.
    # Seen with `python -m http.server`, which serves fine but says nothing.
    if "python" in stack and entry:
        launch = f"python -u {entry}"
    elif kind == "static":
        # A folder of html needs no entry point and no dependencies: the stock
        # http.server is the whole app. Without this the scanner called every
        # static site undecidable and escalated it to the agent, which is the
        # most common shape on GitHub and the cheapest one to run.
        # --bind 127.0.0.1 keeps the announced URL connectable: left to
        # itself http.server binds [::] and prints http://[::]:8080/.
        launch = "python -u -m http.server 8080 --bind 127.0.0.1"
    elif "node" in stack:
        start_script = scripts.get("start") or scripts.get("dev") or scripts.get("serve")
        if start_script:
            launch = "npm start" if scripts.get("start") else "npm run dev"
        elif entry:
            launch = f"node {entry}"
    port_hint = ""
    m = re.search(r"(?:localhost|127\.0\.0\.1)[:/](\d{2,5})", readme)
    if m:
        port_hint = m.group(1)

    return {
        "stack": stack, "kind": kind, "entry": entry, "launch": launch,
        "install_commands": install, "needs_ai_bundle": bool(ai), "ai_markers": ai,
        "servers": servers, "port_mentioned_in_readme": port_hint,
        "python_files": len(py), "js_files": len(js), "total_files": len(tree),
        "package_scripts": scripts,
        # Said in the report when something was skipped, rather than silently thinner facts.
        "manifest_notes": manifest_notes,
    }


def recon(url: str, cache_dir: Path, timeout: int = 600) -> Dict[str, Any]:
    """Shallow-clone the repo into the analysis cache and report what is there.

    The clone is for READING only. install.js does its own full clone into
    `app/` at install time, so nothing here becomes the app's runtime copy.
    """
    parts = repo_parts(url)
    if not parts:
        return {"ok": False, "error": f"cannot parse a repository from {url!r}"}
    dest = cache_dir / "scaffold" / parts["slug"]
    if dest.exists():
        rc, out = _run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin"], timeout=timeout)
        _run(["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"], timeout=60)
        if rc != 0:
            shutil.rmtree(dest, ignore_errors=True)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Public first: a stale credential must not turn a readable repo into a
        # 404. The token is only for the private case, where it is required.
        rc, out = _run(["git", "clone", "--depth", "1", parts["clone_url"], str(dest)], timeout=timeout)
        if rc != 0:
            shutil.rmtree(dest, ignore_errors=True)
            rc, out2 = _run(["git", "clone", "--depth", "1", parts["clone_url"], str(dest)],
                            timeout=timeout, env=_auth_env())
            if rc != 0:
                return {"ok": False, "error": f"git clone failed: {out2.strip()[-800:]}", "repo": parts}
    rc, branch = _run(["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"], timeout=60)
    rc, head = _run(["git", "-C", str(dest), "log", "-1", "--format=%H %s"], timeout=60)

    tree = _tree(dest)
    manifests: Dict[str, str] = {}
    for name in ("requirements.txt", "pyproject.toml", "setup.py", "environment.yml",
                 "package.json", "Dockerfile", "Makefile"):
        p = dest / name
        if p.is_file():
            manifests[name] = _read(p, JSON_MANIFEST_LIMIT if name in JSON_MANIFESTS else 8000)
    readme = ""
    for name in ("README.md", "readme.md", "README.rst", "README.txt", "README"):
        p = dest / name
        if p.is_file():
            readme = _read(p, 8000)
            break
    info = classify(tree, readme, manifests)
    meta = _fetch_json(f"https://api.github.com/repos/{parts['owner']}/{parts['repo']}") or {}
    return {
        "ok": True, "repo": parts, "branch": branch.strip() or "HEAD",
        "head": head.strip()[:200], "analysis_dir": str(dest),
        "readme_head": readme[:3000], "tree_top": tree[:200],
        "manifests_present": sorted(manifests), "github": {
            "description": (meta.get("description") or "")[:300],
            "stars": meta.get("stargazers_count"), "license": ((meta.get("license") or {}).get("spdx_id") or ""),
            "avatar": ((meta.get("owner") or {}).get("avatar_url") or ""),
            "default_branch": meta.get("default_branch") or "",
            "private": meta.get("private"),
        },
        **info,
    }


# ------------------------------------------------------------------ scaffolding

T_ATHENA_HEADER = ("// Athena launcher — Athena reads this file. The legacy-named shim\n"
                   "// beside it re-exports it, because the engine resolves only that filename.\n")
T_ATHENA = T_ATHENA_HEADER + """module.exports = {
  version: "7.0",
  title: "@@TITLE@@",
  description: "@@DESC@@",
  icon: "icon.png",
  menu: async (kernel, info) => {
    let installed = info.exists("app")
    let running = {
      install: info.running("install.js"),
      start: info.running("start.js"),
      update: info.running("update.js"),
      reset: info.running("reset.js")
    }
    if (running.install) {
      return [{ default: true, icon: "fa-solid fa-plug", text: "Installing", href: "install.js" }]
    } else if (installed) {
      if (running.start) {
        let local = info.local("start.js")
        if (local && local.url) {
          return [
            { default: true, icon: "fa-solid fa-rocket", text: "Open Web UI", href: local.url },
            { icon: "fa-solid fa-terminal", text: "Terminal", href: "start.js" }
          ]
        }
        return [{ default: true, icon: "fa-solid fa-terminal", text: "Terminal", href: "start.js" }]
      } else if (running.update) {
        return [{ default: true, icon: "fa-solid fa-terminal", text: "Updating", href: "update.js" }]
      } else if (running.reset) {
        return [{ default: true, icon: "fa-solid fa-terminal", text: "Resetting", href: "reset.js" }]
      }
      return [
        { default: true, icon: "fa-solid fa-power-off", text: "Start", href: "start.js" },
        { icon: "fa-solid fa-rotate", text: "Update", href: "update.js" },
        { icon: "fa-solid fa-eraser", text: "Reset", href: "reset.js" }
      ]
    }
    return [{ default: true, icon: "fa-solid fa-download", text: "Install", href: "install.js" }]
  }
}
"""

T_INSTALL = """module.exports = {
@@REQUIRES@@  run: [
    {
      method: "shell.run",
      params: {
        message: [ "git clone @@CLONE_URL@@ app" ],
        // Guard on what the clone produces, not on the folder: the app's
        // virtual environment lives inside app/ too, so a bare exists('app')
        // check is defeated the moment anything else has run.
        "when": "{{!exists('app/.git')}}"
      }
    },
@@INSTALL_STEPS@@    {
      method: "notify",
      params: { html: "Installed. Click Start to launch it." }
    }
  ]
}
"""

T_START = """module.exports = {
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
@@VENV@@        path: "app",
        message: [
          "@@LAUNCH@@"
        ],
        on: [{
          "event": "@@EVENT@@",
          "done": true
        }]
      }
    },
    {
      method: "local.set",
      params: { url: "{{input.event[1]}}" }
    }
  ]
}
"""

T_UPDATE = """module.exports = {
  run: [
    { method: "shell.run", params: { message: [ "git pull" ] } },
    { method: "shell.run", params: { path: "app", message: [ "git pull" ] } }
  ]
}
"""

T_RESET = """module.exports = {
  run: [
@@RESET_STEPS@@    { method: "fs.rm", params: { path: "app" } }
  ]
}
"""

T_GITIGNORE = """app/
env/
venv/
node_modules/
logs/
cache/
*.db
*.sqlite
__pycache__/
"""

T_README = """# @@TITLE@@

@@DESC@@

## Install and run

Use the Install button, then Start. Athena handles the environment: the app's
own code lives in `app/`, its dependencies in a virtual environment beside it.

## Programmatic access

_Not documented yet._ If this app exposes an HTTP API, document it here: the
endpoint, an example `curl`, and an example in JavaScript and Python.
"""


def _install_steps(info: Dict[str, Any]) -> str:
    steps = []
    cmds = info.get("install_commands") or []
    if cmds:
        stack = info.get("stack") or []
        venv = '        venv: "env",\n' if "python" in stack else ""
        steps.append(
            "    {\n"
            "      method: \"shell.run\",\n"
            "      params: {\n"
            f"{venv}        path: \"app\",\n"
            "        message: [\n"
            + "".join(f"          \"{c}\",\n" for c in cmds)
            + "        ]\n"
            "      }\n"
            "    },\n")
    if info.get("needs_ai_bundle"):
        steps.append(
            "    {\n"
            "      method: \"script.start\",\n"
            "      params: {\n"
            "        uri: \"torch.js\",\n"
            "        params: { path: \"app\", venv: \"env\" }\n"
            "      }\n"
            "    },\n")
    if not steps:
        steps.append("    // TODO(agent): no dependency manifest was found — add the install steps this project needs.\n")
    return "".join(steps)


def _reset_steps(info: Dict[str, Any]) -> str:
    # The virtual environment lives inside app/ (that is the convention the
    # kernel resolves), so removing app/ removes it — no separate step needed.
    out = ""
    if "node" in (info.get("stack") or []):
        out += "    { method: \"fs.rm\", params: { path: \"app/node_modules\" } },\n"
    return out


_TEMPLATE_RE: Optional[re.Pattern] = None


def is_ours(p: Path) -> bool:
    """True when Athena wrote this launcher — so it may be migrated.

    A launcher that belongs to the project is never moved: it is the upstream
    repo's file, and renaming it would diverge from every future update.
    """
    p = Path(p)
    if athena_naming.tracked_by_git(p):
        return False
    try:
        txt = p.read_text(encoding="utf-8")
    except OSError:
        return False
    if txt.startswith("// Athena launcher"):
        return True
    global _TEMPLATE_RE
    if _TEMPLATE_RE is None:
        # The header is optional: launchers written before it existed are still
        # Athena's, and they are exactly the ones that need migrating.
        body = (re.escape(T_ATHENA[len(T_ATHENA_HEADER):])
                .replace(re.escape("@@TITLE@@"), ".*?").replace(re.escape("@@DESC@@"), ".*?"))
        _TEMPLATE_RE = re.compile("(?:" + re.escape(T_ATHENA_HEADER) + ")?" + body, re.S)
    return bool(_TEMPLATE_RE.fullmatch(txt))


def migrate_launcher(app_dir: Path) -> str:
    """Put an Athena-authored launcher on Athena's name, and leave the shim.

    Apps built before the rename still carry a launcher Athena generated. Moving
    it is safe — the file is ours, and nothing upstream tracks it. Returns what
    happened so the caller can say it out loud.
    """
    app_dir = Path(app_dir)
    if (app_dir / athena_naming.LAUNCHER).is_file():
        return "already"
    legacy = app_dir / athena_naming.LEGACY_LAUNCHER
    if not legacy.is_file():
        return "no-launcher"
    if not is_ours(legacy):
        return "upstream"
    legacy.rename(app_dir / athena_naming.LAUNCHER)
    athena_naming.write_shim(app_dir)
    athena_naming.ensure_meta(app_dir)
    return "migrated"


def _spec_md(info: Dict[str, Any], url: str, name: str) -> str:
    g = info.get("github") or {}
    lines = [
        f"# SPEC — {name}",
        "",
        f"Repository: {url}  ",
        f"Branch: {info.get('branch')}  Head: {info.get('head')}  ",
        f"Analysis copy (read freely, never edit): `{info.get('analysis_dir')}`",
        "",
        "## What the scanner found",
        "",
        f"- stack: `{', '.join(info.get('stack') or []) or 'unknown'}`",
        f"- kind: `{info.get('kind')}`",
        f"- entry point guess: `{info.get('entry') or 'none'}`",
        f"- launch guess: `{info.get('launch') or 'none'}`",
        f"- install commands: `{'; '.join(info.get('install_commands') or []) or 'none'}`",
        f"- server frameworks seen: `{', '.join(info.get('servers') or []) or 'none'}`",
        f"- AI stack markers: `{', '.join(info.get('ai_markers') or []) or 'none'}`",
        f"- needs the AI bundle: `{info.get('needs_ai_bundle')}`",
        f"- port mentioned in README: `{info.get('port_mentioned_in_readme') or 'none'}`",
        f"- files: {info.get('total_files')} ({info.get('python_files')} python, {info.get('js_files')} js)",
        f"- upstream description: {g.get('description') or '(none)'}",
        f"- license: {g.get('license') or 'unknown'}",
        "",
        "## What you must decide (the scanner cannot)",
        "",
        "1. **The launch command.** `start.js` currently says "
        f"`{info.get('launch') or 'TODO'}` — confirm it against the README and the "
        "code, and fix the working directory if it is not the repo root.",
        "2. **The URL event.** The `on.event` regex must match what the server "
        "actually prints. If it prints nothing useful, set "
        "`url: \"http://127.0.0.1:{{port}}\"` instead and pass the port to the app.",
        "3. **Extra install steps.** System binaries (ffmpeg, cmake…), model "
        "downloads (`hf.download`), config files, `conda` packages.",
        "4. **Anything the scanner got wrong.** It reads manifests and filenames; "
        "you can read the code.",
        "",
        "## Then",
        "",
        "Follow `docs/build-an-app.md` — validate with `node --check`, install, "
        "launch, fetch the URL, and only then report it as working.",
        "",
    ]
    return "\n".join(lines)


def _download_icon(url: str, dest: Path) -> bool:
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "athena"})
        tok = _gh_token()
        if tok and "github" in url:
            req.add_header("Authorization", f"Bearer {tok}")
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        if data:
            dest.write_bytes(data)
            return True
    except Exception:
        return False
    return False


def scaffold(home: Path, url: str, name: Optional[str] = None,
             force: bool = False) -> Dict[str, Any]:
    """Lay down a launcher skeleton for `url` at `<home>/api/<name>/`.

    Never overwrites an existing launcher unless `force` — an app that already
    works must not be clobbered by a rebuild.
    """
    home = Path(home)
    parts = repo_parts(url)
    if not parts:
        return {"ok": False, "error": f"cannot parse a repository from {url!r}"}
    app_name = slug_for(name or parts["slug"])
    app_dir = home / "api" / app_name
    existing = athena_naming.has_launcher(app_dir)
    if existing and not force:
        return {"ok": False, "already_an_app": True, "app": app_name, "path": str(app_dir),
                "error": f"{app_name} already has a launcher — pass force to rebuild it"}

    info = recon(url, home / "cache")
    if not info.get("ok"):
        return info

    g = info.get("github") or {}
    title = name or parts["repo"]
    desc = (g.get("description") or "").strip() or f"{title} — installed from {parts['owner']}/{parts['repo']}."
    desc_js = desc.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:300]

    app_dir.mkdir(parents=True, exist_ok=True)
    files: Dict[str, str] = {
        athena_naming.LAUNCHER: T_ATHENA.replace("@@TITLE@@", title).replace("@@DESC@@", desc_js),
        "install.js": T_INSTALL.replace("@@CLONE_URL@@", parts["clone_url"])
                               .replace("@@REQUIRES@@", '  requires: {\n    bundle: "ai"\n  },\n' if info.get("needs_ai_bundle") else "")
                               .replace("@@INSTALL_STEPS@@", _install_steps(info)),
        "start.js": T_START.replace("@@VENV@@", '        venv: "env",\n' if "python" in (info.get("stack") or []) else "")
                           .replace("@@LAUNCH@@", info.get("launch") or "python app.py  // TODO(agent): confirm the launch command")
                           .replace("@@EVENT@@", "/(http:\\/\\/[^\\s)]+)/"),
        "update.js": T_UPDATE,
        "reset.js": T_RESET.replace("@@RESET_STEPS@@", _reset_steps(info)),
        ".gitignore": T_GITIGNORE,
        "README.md": T_README.replace("@@TITLE@@", title).replace("@@DESC@@", desc),
        "SPEC.md": _spec_md(info, url, app_name),
        athena_naming.META: json.dumps({
            "title": title, "description": desc, "icon": "icon.png", "version": "2.0",
        }, indent=2) + "\n",
    }
    for rel, text in files.items():
        (app_dir / rel).write_text(text, encoding="utf-8", newline="\n")
    # The engine resolves the launcher by its literal legacy filename, so the
    # app carries the shim as well. Unconverted apps keep working either way.
    athena_naming.write_shim(app_dir)
    if info.get("needs_ai_bundle"):
        src = Path(__file__).resolve().parent.parent / EXAMPLES_REL / "python-gradio-server" / "torch.js"
        if src.is_file():
            shutil.copy2(src, app_dir / "torch.js")
    icon_ok = _download_icon(g.get("avatar") or "", app_dir / "icon.png")

    guide = Path(__file__).resolve().parent.parent / GUIDE_REL
    return {
        "ok": True, "app": app_name, "path": str(app_dir), "url": url,
        "analysis_dir": info.get("analysis_dir"), "guide": str(guide),
        "icon": icon_ok,
        "files": sorted(files) + [f"{athena_naming.LEGACY_LAUNCHER} (shim)"]
                 + (["torch.js"] if info.get("needs_ai_bundle") else []),
        "recon": {k: v for k, v in info.items() if k not in ("readme_head", "tree_top", "github")},
        "github": g, "readme_head": info.get("readme_head", ""), "tree_top": info.get("tree_top", [])[:80],
    }


# ------------------------------------------------------------------ validation

def validate(home: Path, name: str) -> Dict[str, Any]:
    """Check a launcher the way the kernel will: it must load and parse."""
    app_dir = Path(home) / "api" / name
    errors: List[str] = []
    notes: List[str] = []
    if not app_dir.is_dir():
        return {"ok": False, "errors": [f"no such app folder: {app_dir}"]}
    node = node_exe()
    for js in sorted(app_dir.glob("*.js")):
        if node:
            rc, out = _run([node, "--check", str(js)], timeout=60)
            if rc != 0:
                errors.append(f"{js.name}: syntax error — {out.strip().splitlines()[-1] if out.strip() else 'unknown'}")
        else:
            notes.append("node not found — syntax checks skipped")
    pj = athena_naming.launcher_of(app_dir)
    if pj is None:
        errors.append(f"{athena_naming.LAUNCHER} is missing — this folder is not an app")
    elif node:
        rc, out = _run([node, "-e",
                        f"const m=require({json.dumps(str(pj))});"
                        "if(!m||typeof m!=='object'){console.log('NOT_OBJECT');process.exit(3)}"
                        "if(typeof m.menu!=='function'){console.log('NO_MENU')}"], timeout=60)
        if rc == 3:
            errors.append(f"{pj.name} does not export an object")
        if "NO_MENU" in out:
            errors.append(f"{pj.name} exports no menu function")
    for meta in athena_naming.META_NAMES:
        p = app_dir / meta
        if p.is_file():
            try:
                val = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(val, dict):
                    errors.append(f"{meta} must contain an object")
                declared = str(val.get("path") or "").strip()
                if declared and declared != "api":
                    errors.append(f'{meta}: path must be "api" or omitted (found {declared!r})')
                if "plugins" in val and not isinstance(val["plugins"], list):
                    errors.append(f"{meta}: plugins must be an array")
            except Exception as e:
                errors.append(f"{meta}: {e}")
    if athena_naming.meta_of(app_dir) is None:
        errors.append(f"no metadata file ({athena_naming.META}) — the app will show as untitled")
    return {"ok": not errors, "errors": errors, "notes": notes, "path": str(app_dir)}


# ----------------------------------------------------------------- the handoff

def build_prompt(home: Path, url: str, name: Optional[str] = None) -> Dict[str, Any]:
    """Scaffold, then hand the work to the agent as a ready-to-submit prompt."""
    home = Path(home)
    res = scaffold(home, url, name=name)
    if not res.get("ok"):
        return res
    guide = res["guide"]
    prompt = (
        f"Build an Athena app from {url} (app name: {res['app']}).\n\n"
        f"A launcher skeleton is already at {res['path']} — read its SPEC.md first.\n"
        f"Write the launcher as athena.js. The legacy-named file beside it is a "
        f"one-line compat shim — the engine resolves that filename and nothing else, "
        f"so leave it in place and never edit it.\n"
        f"Read {guide} and follow it; adopt the syntax from the examples in "
        f"{Path(guide).parent / 'examples'}.\n"
        f"The repository is checked out read-only for you at {res['analysis_dir']}.\n\n"
        f"Then: fix install.js and start.js for this project, validate every script with node --check, "
        f"install it, launch it, fetch the URL to prove it answers, stop it, and tell me what works. "
        f"If the repo turns out not to be a runnable app, say so instead of inventing one."
    )
    res["prompt"] = prompt
    return res
