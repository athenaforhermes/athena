"""Athena backend — the local AI app store for Hermes.

Supervises a headless `pinokiod` engine (Node, vendored under ../engine) and
exposes it to the Hermes desktop UI and to agents.

Design notes (see ../docs/SPIKE-VERDICT.md for the evidence):
  * pinokiod boots as a plain Node process — zero Electron in its graph.
  * Isolation: we spawn it with USERPROFILE pointing at our private profile dir,
    so the kernel's own ~/.pinokio/config.json lands there, never the real one.
  * Port: pinokiod hardcodes 42000; `server.port` set before start() overrides it.
  * Readiness is an HTTP probe, NOT stdout parsing — a forked node-pty helper
    can crash and take the parent's stdout with it while the server keeps serving.
"""
from __future__ import annotations

import base64
import concurrent.futures
import importlib.util
import json
import datetime
import traceback
import os
import re
import shutil
import socket
import stat
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, Request as FastAPIRequest
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ENGINE_DIR = PLUGIN_DIR / "engine"

# Athena's own view of the public app store. Loaded by path because this module
# is itself loaded by path (see ../__init__.py), so package-relative imports
# would not resolve.
_DASH_DIR = Path(__file__).resolve().parent
if str(_DASH_DIR) not in sys.path:
    sys.path.insert(0, str(_DASH_DIR))
try:
    import catalog as athena_catalog
except Exception:  # noqa: BLE001 - store stays empty if the client is missing
    athena_catalog = None

# Athena's control channel to the engine. The engine's own UI drives installs
# over a WebSocket, so that is the protocol Athena speaks to run apps.
try:
    import runtime as athena_runtime
except Exception:  # noqa: BLE001
    athena_runtime = None

# The store publisher lives under tools/ so it can also be run from a shell. It
# is loaded by path here for the same reason everything else in this plugin is:
# `dashboard/` is not a package on sys.path, and a second copy of the publisher
# would be a second set of rules about what a branch contains.
def _publish_module():
    global _PUBLISH_MOD
    if _PUBLISH_MOD is not None:
        return _PUBLISH_MOD
    path = _DASH_DIR.parent / "tools" / "publish_store.py"
    spec = importlib.util.spec_from_file_location("athena_publish_store", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["athena_publish_store"] = mod
    spec.loader.exec_module(mod)
    _PUBLISH_MOD = mod
    return mod


_PUBLISH_MOD = None

# Is there room? Disk before an install, VRAM before a launch, memory before a guest.
# Numbers and the user's own thresholds, never a prediction of what an app will need.
#
# Reloaded, not just imported: this module is re-executed on a plugin reload but the
# modules it imports are not — `import` finds them in `sys.modules` and hands back the
# copy already there. An edit to `headroom.py` therefore lands on disk and does not take
# effect, and the symptom is not an error but a *wrong answer*: the first real
# `machine_gate` call arrived as `AttributeError` against a copy predating the function.
# Safe here because nothing does `from headroom import ...`.
try:
    import headroom as athena_headroom
except Exception:  # noqa: BLE001 - without it, no gates, only launches
    athena_headroom = None

# The plugin as its own VNC client: a guest's screen arrives as a framebuffer we decode, a
# PNG the panel can draw, and input that travels back the same way. Loaded by name like the
# rest of dashboard/ — this module is itself loaded by path, so a package-relative import
# resolves at import time and then fails at call time inside a plugin host (`attempted
# relative import with no known parent package`, found live, 2026-10-14).
try:
    import screen as athena_screen
except Exception:  # noqa: BLE001 - without it a machine has a console and no picture
    athena_screen = None

def _held_session(name: str, host: str, port: int, drop: bool = False) -> Any:
    """The screen connection held open for a machine, if this install has a screen reader.

    The holding and the reaping live in dashboard/screen.py, beside the session they manage,
    where the suite can drive them against a fake VNC server. This is only the name the
    routes here call, and it exists so a plugin without dashboard/screen.py still answers.
    """
    if athena_screen is None:
        return None
    return athena_screen.session_for(name, host, port, drop=drop)

# What Athena calls its own files. The vendor-named launcher is a legacy input
# Athena reads and converts — never a name it writes into a new app. Ask here,
# or a second answer drifts in and an app that runs in one path 404s in another.
import naming as athena_naming

# Building an app from a repository: fetch the repo, report what is there, lay
# down the launcher skeleton. The judgement half is the agent's (see docs/
# build-an-app.md) — this module is the part that needs none.
try:
    import buildapp as athena_buildapp
except Exception:  # noqa: BLE001
    athena_buildapp = None

# The app-folder browser. Ported from the standalone file browser and re-scoped:
# every path it accepts must resolve inside Athena's home. Mounted under /files,
# so its own /list, /read, /write ... become /files/list, /files/read, ...
try:
    import files as athena_files

    # The sandbox rides on the router, not on individual routes. Per-route it was
    # enough to leave the whole disk readable once; here a new route inherits it.
    router.include_router(
        athena_files.router,
        prefix="/files",
        dependencies=[Depends(athena_files.sandbox_dep)],
    )
except Exception:  # noqa: BLE001 - browser is optional; the store still works
    athena_files = None

# The machine's own stats, and per-app RAM/VRAM proxied from the engine.
try:
    import system as athena_system
    router.include_router(athena_system.router, prefix="/sys")
except Exception:  # noqa: BLE001 - monitor is optional
    athena_system = None

# ------------------------------------------------------------------- settings
# The store is `settings_store.py`. The three names below stay here on purpose: the file
# paths are how a test run redirects settings I/O (`api.SETTINGS_FILE = tmp`), so the
# wrappers read these globals at call time rather than capturing them at import.
import settings_store as athena_settings

def _settings_home() -> Path:
    return athena_settings.settings_home(PLUGIN_DIR)
SETTINGS_FILE = _settings_home() / "settings.json"
LEGACY_SETTINGS_FILE = PLUGIN_DIR / "settings.json"
DEFAULTS: Dict[str, Any] = athena_settings.defaults(PLUGIN_DIR)
_as_port = athena_settings.as_port


def _settings_on_disk() -> Dict[str, Any]:
    return athena_settings.settings_on_disk(LEGACY_SETTINGS_FILE, SETTINGS_FILE)


def _port_from_files() -> Optional[int]:
    return athena_settings.port_from_files(SETTINGS_FILE, LEGACY_SETTINGS_FILE)


def _load_settings() -> Dict[str, Any]:
    return athena_settings.load_settings(SETTINGS_FILE, LEGACY_SETTINGS_FILE, DEFAULTS)


def _save_settings(cfg: Dict[str, Any]) -> None:
    athena_settings.save_settings(cfg, SETTINGS_FILE, LEGACY_SETTINGS_FILE)


# ---------------------------------------------------------------- node runtime
# Where Node is, and which port is free: `node_runtime.py`, 130 lines that need nothing
# from this module. Re-exported under the old private names so no call site changes.
#
# Unlike the credential shim below, this import is NOT wrapped in a try: a swallowed
# failure here would answer "no Node on this machine", which is a lie about the machine
# and would have the engine refuse to start for the wrong reason. If the module cannot be
# found, the API should not load at all.
import node_runtime as athena_node

_find_node = athena_node.find_node
_find_node_uncached = athena_node.find_node_uncached
_find_free_port = athena_node.find_free_port
_tool_roots = athena_node.tool_roots
_bundled_nodes = athena_node.bundled_nodes
_valid_node = athena_node.valid_node

# The 60-second memo, by the same name and the same object: `t_runtime.py` clears it between
# cases, and an alias to a copy would have the test clearing a dict nobody consults.
_NODE_MEMO = athena_node._NODE_MEMO


# ---------------------------------------------------------------- engine manager
# The supervisor is `engine.py`: the child process, the port, the log, the readiness question.
# It takes `engine_dir`/`plugin_dir` and the two settings callables from here, because the
# settings *paths* are this module's globals and the harnesses patch them.
import engine as athena_engine

EngineManager = athena_engine.EngineManager
# Re-exported as the same objects, not copies: `_PROBE_AT`/`_PROBE_LOCK` are the memo the manager
# writes to, and `_STARTING_PROBE` is the timeout every route quotes.
_STARTING_PROBE = athena_engine._STARTING_PROBE
_PROBE_LOCK = athena_engine._PROBE_LOCK
_PROBE_AT = athena_engine._PROBE_AT
_pids_from_lsof = athena_engine._pids_from_lsof
_pids_from_ss = athena_engine._pids_from_ss

# The singleton keeps its name: every route reaches `engine`, and so do the harnesses that
# drive `api.engine.start()` / `.probe()` / `.home`.
def _new_engine() -> EngineManager:
    """A manager wired to this module's directories and settings -- what the singleton is.

    Public because a caller that wants a second manager (the runtime harness builds one to
    watch a start it never launches) has to wire it the same way, and the arguments are this
    module's, not the manager's.
    """
    return EngineManager(
        engine_dir=ENGINE_DIR,
        plugin_dir=PLUGIN_DIR,
        load_settings=_load_settings,
        save_settings=_save_settings,
    )


engine = _new_engine()


# ---------------------------------------------------------------- credentials
# The credential stores live in credentials.py: 165 lines of reading what is on this
# machine and copying it to a guest. They came out of this file for the reason the whole
# R9 rung exists -- routes and the machinery behind them in one 9,400-line module.
#
# The wrappers below are deliberate rather than tidy-up: everything in here calls these
# by their old names and shapes, and a rename would be a rename of every call site. The
# engine is passed in from this module's own singleton, which is the one thing the moved
# file refuses to reach for itself.
try:
    import credentials as athena_credentials
except Exception:  # noqa: BLE001 - without it the credentials rows read as unavailable
    athena_credentials = None


def _athena_home() -> Path:
    return athena_credentials.athena_home(engine)


def pinokio_home() -> Optional[Path]:
    return athena_credentials.pinokio_home()


def collect_credentials() -> Dict[str, Any]:
    return athena_credentials.collect_credentials(engine)


def seed_credentials(target_home: Path) -> Dict[str, Any]:
    return athena_credentials.seed_credentials(engine, target_home)


def _app_credential_env(app_id: str) -> Dict[str, str]:
    """What this app has been given: asked for once, remembered, empty when nobody was
    asked. Fail-closed lives in `engine.credentials_for_app`; this is only the manager."""
    return athena_engine.credentials_for_app(app_id, engine)


# ---------------------------------------------------------------- models


class InstallIn(BaseModel):
    url: str


class BuildIn(BaseModel):
    url: str
    name: Optional[str] = None
    force: bool = False


class SettingIn(BaseModel):
    home: Optional[str] = None
    port: Optional[int] = None
    autostart: Optional[bool] = None
    share_caches: Optional[bool] = None
    idle_stop_seconds: Optional[int] = None
    store_index: Optional[str] = None
    # How the Apps tab draws each app: `cards` (the default) or `rows`, which fits
    # far more apps on a screen but clips the description to one line. Two words
    # is the whole vocabulary, and anything else is stored as the default rather
    # than kept as a third state nothing can render.
    apps_layout: Optional[str] = None
    disk_warn_free_gb: Optional[float] = None
    vram_warn_free_gb: Optional[float] = None
    # MB, an int not a float: this one refuses rather than warns, and "1536.0 MB
    # free" invites somebody to try 1536.5 and then argue about rounding.
    machine_min_free_mb: Optional[int] = None


class ImportIn(BaseModel):
    source: str
    apps: List[str] = []
    mode: str = "copy"  # copy | adopt


# ---------------------------------------------------------------- engine routes


@router.get("/status")
def status() -> JSONResponse:
    """Engine liveness, and nothing else.

    Nothing here spawns a CLI: a route the status chip polls every 8 seconds must not
    pay ~700ms to answer. Credentials have their own route, under Settings.
    """
    _maybe_autostart()
    payload = engine.status()
    # So the panel can say "bringing the engine up" rather than "stopped" while
    # the engine it is starting is still on its way.
    payload["autostart"] = dict(_AUTOSTART)
    return JSONResponse(payload)


@router.post("/engine/start")
def engine_start() -> JSONResponse:
    return JSONResponse(engine.start())


@router.post("/engine/stop")
def engine_stop() -> JSONResponse:
    return JSONResponse(engine.stop())


@router.post("/engine/restart")
def engine_restart() -> JSONResponse:
    engine.stop()
    return JSONResponse(engine.start())


@router.post("/engine/setup")
def engine_setup(consent: bool = False) -> JSONResponse:
    """What the engine needs — and, with `?consent=true`, fetch it from npm.

    `setup_state` names the facts; the fetch runs in the background. With no consent
    `engine.setup()` downloads nothing at all, so that is a property of the call.
    """
    out = engine.setup(consent=consent)
    return JSONResponse(out, status_code=200 if out.get("ok") else 409)


# ---------------------------------------------------------------- apps


@router.get("/apps")
def apps() -> JSONResponse:
    """Installed apps, read from disk.

    This used to ask the engine, which only answers while the engine is running
    — so with the engine stopped the list came back empty and apps that were
    plainly sitting on disk could not be seen, launched or deleted. The folders
    under the app home are the truth; the engine is only asked about liveness.
    """
    api_root = engine.home / "api"
    folders: List[Path] = []
    if api_root.is_dir():
        try:
            folders = sorted((d for d in api_root.iterdir() if d.is_dir()),
                             key=lambda p: p.name.lower())
        except OSError:
            folders = []

    # Pinokio clones an app into a folder named after its repo with a ".git"
    # suffix, while a folder created here has no suffix, so both spellings have
    # to be matched against the catalogue or half the apps lose their icon.
    store_by_repo: Dict[str, Dict[str, Any]] = {}
    if athena_catalog is not None:
        try:
            for rec in athena_catalog.index(engine.home)["apps"]:
                repo = (rec.get("repoUrl") or "").rstrip("/").split("/")[-1]
                if repo:
                    store_by_repo.setdefault(repo.lower(), rec)
        except Exception:  # noqa: BLE001
            store_by_repo = {}

    rt = None
    if engine.probe() and athena_runtime is not None:
        try:
            rt = athena_runtime.Runtime(engine.port, engine.home)
        except Exception:  # noqa: BLE001
            rt = None

    # One bulk read of what the engine is running, rather than a status call per
    # app: the app's live URL comes back with it, which is what a card needs to
    # offer "open" instead of a Launch button that appears to do nothing.
    live: Dict[str, Dict[str, Any]] = {}
    if rt is not None:
        try:
            for folder, rows in rt.running().items():
                url = next((r["url"] for r in rows if r.get("url")), None)
                live[folder] = {"url": url}
                base_f = folder[:-4] if folder.lower().endswith(".git") else folder
                live.setdefault(base_f, live[folder])
        except Exception:  # noqa: BLE001
            live = {}

    items = []
    for d in folders:
        name = d.name
        base = name[:-4] if name.lower().endswith(".git") else name
        rec = store_by_repo.get(base.lower()) or store_by_repo.get(name.lower()) or {}
        info = live.get(name) or live.get(base) or {}

        items.append({
            "id": name,
            "name": rec.get("title") or base,
            "description": rec.get("description") or "",
            "icon": rec.get("iconUrl") or "",
            "tags": _tag_list(rec),
            "repo": rec.get("repoUrl") or "",
            "in_store": bool(rec),
            "manifest": athena_naming.has_launcher(d),
            "linked": _is_link_dir(d),
            "running": bool(info),
            "url": info.get("url"),
            "path": str(d),
            # Read from the scan cache, never a walk: this route is polled, and a
            # directory walk per poll is exactly what the cache exists to avoid.
            # `scanned: False` is the honest answer for an app nobody has read.
            "keys": _keys_cached(name),
        })

    return JSONResponse({
        "apps": items,
        "home": str(engine.home),
        "engine": engine.status(),
        "engine_up": rt is not None,
    })


def _tag_list(rec: Dict[str, Any]) -> List[str]:
    out = []
    for t in rec.get("tags") or []:
        name = t.get("tag") if isinstance(t, dict) else t
        if name:
            out.append(str(name))
    return out


def _runtime() -> "athena_runtime.Runtime":
    return athena_runtime.Runtime(engine.port, engine.home)


# ---- handing a file to an app ------------------------------------------------
# The other half of "ask the app what it can do": the answer is only useful if a
# person can then give the app the file they are looking at. These two routes are
# the routing question and the hand-over. The routing question is answered from
# the manifests already on disk — no app is started to ask it — and the hand-over
# runs as a job, because a generation takes as long as it takes.
#
# Everything behind these two routes — bringing an app up, running one of its
# capabilities, chaining one app's output into the next one's input — is
# `runner.py`. It was this file's own machinery until it was lifted out (rule 136)
# so that the routes and the work they do stopped sharing a page. What is left here
# are the routes, and the thin wrappers they call.
#
# Every wrapper hands the runner the seams *this* module owns — `engine`,
# `_appcall_module`, `_idle_module`, `_job_add`, `_app_folder`, `_app_url`,
# `_runtime`, `seed_credentials` — one call at a time. That injection is the whole
# point: a harness that patches one of them on this module is still patching the
# code that runs (rule 138), and a name that moved is a name that was re-exported
# so the harness did not have to move with it (rule 140).

import runner as athena_runner

# The chain vocabulary, re-exported under the names this module already used.
_FILES_SENTINEL = athena_runner.FILES_SENTINEL
_FILE_SENTINEL = athena_runner.FILE_SENTINEL


class AcceptsBody(BaseModel):
    """The file to route."""
    path: str = ""


class CallBody(BaseModel):
    """A capability to run, and what to give it.

    `inputs` is positional, in the order the capability declared them — that is
    Gradio's contract, and the same list `athena_generate` passes.

    `then` continues the chain: each entry is `{app, capability}` and runs in turn
    on what the stage before it produced. An entry may also carry `inputs` — the
    capability's other arguments, in order, with `{files}` (all of them) or
    `{file}` (the first) where the handover goes.
    """
    capability: str = ""
    inputs: Optional[List[Any]] = None
    timeout: Optional[float] = None
    then: Optional[List[Dict[str, Any]]] = None


def _app_frames() -> List[Dict[str, Any]]:
    """Installed apps with their folder and whether each is serving.

    The answer is built by `runner.app_frames`; this is the seam, so it is the name
    this module resolves (and a harness patches).
    """
    return athena_runner.app_frames(engine, _runtime, athena_runtime)

@router.post("/accepts")
def accepts(body: AcceptsBody) -> JSONResponse:
    """Which installed apps would take this file — and why the others would not.

    Nothing is started to answer this. The capabilities came from each app's own
    `agent.json`, so an app that has never been probed is reported as such rather
    than assumed to be able to do anything, and a capability that wants a link
    rather than a path is kept out of the list with its reason attached.
    """
    ac = _appcall_module()
    if ac is None:
        return JSONResponse({"ok": False, "error": "the capability probe is unavailable in this build"})
    raw = str(body.path or "").strip()
    if not raw:
        return JSONResponse({"ok": False, "error": "no file was named"})
    p = Path(os.path.expanduser(raw))
    if not p.is_file():
        # A path that is not there is not an app's problem, and saying so here is
        # more useful than a list of candidates for a file nobody can open.
        return JSONResponse({"ok": False, "error": f"there is no file at {raw}"})
    kind = ac.file_kind(p.name)
    frames = _app_frames()
    rows: List[Dict[str, Any]] = []
    for f in frames:
        man = ac.load(engine.home, f["folder"])
        rows.append(dict(f, manifest=man))
    view = ac.candidates(kind, rows)
    try:
        size = p.stat().st_size
    except OSError:
        size = None
    return JSONResponse(dict(view, ok=True, path=str(p), name=p.name, size=size,
                             apps=len(frames)))


# ---- the capability runner's seams -------------------------------------------
# Five wrappers, one per function the routes above and the direct callers further
# down still call by name. Each resolves this module's globals at call time and
# hands them to `runner`, which is what keeps a monkeypatch on this module
# meaningful: the runner never reaches across files for a name it could be handed.


def _bring_up(app_id: str, seconds: float = 180.0) -> Dict[str, Any]:
    """Make sure an app is serving, starting it if it is not — `runner.bring_up`."""
    return athena_runner.bring_up(app_id, seconds, engine=engine, app_url=_app_url,
                                  runtime=_runtime, seed_credentials=seed_credentials)


def _then_stages(rows: Optional[List[Any]]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """The stages that follow the first call, normalised — `runner.then_stages`."""
    return athena_runner.then_stages(rows)


def _chain_check(ac: Any, first_cap: Dict[str, Any],
                 stages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Whether every stage can run, asked before any of it starts — `runner.chain_check`."""
    return athena_runner.chain_check(first_cap, stages, ac=ac, engine=engine,
                                     app_folder=_app_folder)


def _run_capability(jid: str, app_id: str, folder: Path, capability: str,
                    inputs: List[Any], timeout: float = 900.0) -> Dict[str, Any]:
    """Start the app if needed, run one capability on it, and report what came back.

    The work is `runner.run_capability`; the seams it needs — the app-call probe,
    the idle module, the job log, and `_bring_up` above — are looked up here at call
    time so a patch on this module reaches it.
    """
    return athena_runner.run_capability(jid, app_id, folder, capability, inputs,
                                        timeout, engine=engine,
                                        ac_loader=_appcall_module,
                                        idle_loader=_idle_module, job_add=_job_add,
                                        bring_up=_bring_up)


def _run_pipeline(jid: str, app_id: str, folder: Path, capability: str, inputs: List[Any],
                  stages: List[Dict[str, Any]], timeout: float = 900.0) -> Dict[str, Any]:
    """Run one capability and hand what it makes to the next one, in order.

    One job, several stages — `runner.run_pipeline`. It is handed the same seams
    `_run_capability` is, plus `_app_folder`, so the chain resolves every stage the
    same way a single call does.
    """
    return athena_runner.run_pipeline(jid, app_id, folder, capability, inputs, stages,
                                      timeout, engine=engine,
                                      ac_loader=_appcall_module,
                                      idle_loader=_idle_module, job_add=_job_add,
                                      bring_up=_bring_up, app_folder=_app_folder)

@router.post("/apps/{app_id}/call")
def app_call(app_id: str, body: Optional[CallBody] = None) -> JSONResponse:
    """Hand one file (or a few) to one of the app's own capabilities.

    A job, because the panel's contract for anything slow is a job id it can
    watch. Everything that can be refused is refused before the job exists: an
    app that was never probed has nothing to call, a capability that is not in
    the manifest is not in the manifest, and a capability that needs inputs
    cannot be run without them.
    """
    folder = _app_folder(app_id)
    if folder is None:
        return JSONResponse({"ok": False, "error": f"no installed app called {app_id}"},
                            status_code=404)
    ac = _appcall_module()
    if ac is None:
        return JSONResponse({"ok": False, "error": "the capability probe is unavailable in this build"})
    wanted = str((body.capability if body else "") or "").strip()
    if not wanted:
        return JSONResponse({"ok": False, "error": "name the capability to run"})
    man = ac.load(engine.home, folder)
    if not man or not (man.get("capabilities") or []):
        return JSONResponse({"ok": False, "app": app_id,
                             "error": ("athena has no list of what this app can do yet — "
                                       "it is written the first time the app is asked, "
                                       "and the app has to be running for that")})
    cap = next((c for c in man["capabilities"]
                if wanted in (c.get("name"), c.get("intent"))), None)
    if cap is None:
        return JSONResponse({"ok": False, "app": app_id,
                             "error": f"{app_id} has no capability called {wanted!r}",
                             "available": [c.get("intent") for c in man["capabilities"]]})
    raw_inputs = body.inputs if body and body.inputs is not None else []
    if not isinstance(raw_inputs, list):
        raw_inputs = [raw_inputs]
    required = [i for i in (cap.get("inputs") or []) if i.get("required")]
    if len(raw_inputs) < len(required):
        return JSONResponse({"ok": False, "app": app_id,
                             "capability": cap.get("name"), "intent": cap.get("intent"),
                             "error": (f"{cap.get('name')} needs {len(required)} input(s) — "
                                       "pass them in order, local file paths for the "
                                       "ones that take a file"),
                             "inputs": [{"name": i.get("name"), "component": i.get("component"),
                                         "takes_a_local_file": i.get("accepts_local_path")}
                                        for i in required]})
    timeout = float((body.timeout if body and body.timeout else 900) or 900)
    # A chain is checked whole before any of it starts, so a pipeline that cannot
    # work is refused rather than discovered halfway through with a job already
    # running: every stage installed, every capability in its manifest, and every
    # handover matching what the stage before it says it produces.
    stages, stage_err = _then_stages(body.then if body else None)
    if stage_err:
        return JSONResponse({"ok": False, "app": app_id, "error": stage_err},
                            status_code=400)
    if stages:
        chain_err = _chain_check(ac, cap, stages)
        if chain_err:
            return JSONResponse(chain_err, status_code=400)
    head = cap.get("intent") or cap.get("name")
    title = f"{app_id}: {head}" + (f" + {len(stages)} more" if stages else "")
    work = (lambda j: _run_pipeline(j, app_id, folder, str(cap.get("name")), raw_inputs,
                                    stages, timeout=timeout)) if stages else \
           (lambda j: _run_capability(j, app_id, folder, str(cap.get("name")),
                                      raw_inputs, timeout=timeout))
    jid = _job_spawn("call", title, work, app=app_id)
    return JSONResponse({"ok": True, "job": jid, "app": app_id,
                         "capability": cap.get("name"), "intent": cap.get("intent"),
                         "stages": [stage["app"] for stage in stages]})


@router.get("/environment")
def environment() -> JSONResponse:
    """Whether Athena's engine has its own toolchain.

    Apps run their commands through the engine, which prefixes every one of them
    with a conda activation. Until Miniforge is provisioned that activation fails
    and the chained command after it never runs — an app's install silently
    clones its repo and builds nothing. So this is not cosmetic: it is the
    difference between an app being installed and merely downloaded.
    """
    if not engine.probe():
        return JSONResponse({"ready": False, "engine_running": False,
                             "home": str(engine.home)})
    try:
        facts = _runtime().environment()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ready": False, "error": str(e)}, status_code=502)
    facts["engine_running"] = True
    return JSONResponse(facts)


@router.post("/environment/provision")
def environment_provision() -> JSONResponse:
    """Install the engine's toolchain (Miniforge and friends).

    Runs through the engine's own kernel API, and keeps running in the engine
    whether or not anyone stays connected, so this returns as soon as the work
    is underway.
    """
    if not engine.probe():
        started = engine.start(wait=90)
        if not started.get("ok"):
            return JSONResponse({"ok": False, "error": "engine did not start",
                                 "detail": started}, status_code=502)
    try:
        out = _runtime().call_kernel("kernel.bin.mod.conda.install", {}, wait=25.0)
        return JSONResponse(out)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


# Athena's own name for each thing you can do to an app. Each maps to a script
# the app itself ships; None means "whatever the app marks as its default",
# which is the install script for a fresh clone and start for an installed one.
APP_SCRIPTS = {
    "launch": None,
    "install": "install.js",
    "update": "update.js",
    "reset": "reset.js",
}

# The engine sets these on every git command it runs, so a repository that needs
# credentials fails instead of hanging on a prompt. Same values, same intent.
NON_INTERACTIVE_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
}

# How long an install may produce no output before the panel stops watching.
# Downloading a multi-gigabyte model is legitimately silent for minutes, so this
# is generous: the cost of waiting is a slow panel update, the cost of guessing
# wrong is calling a healthy install dead.
IDLE_INSTALL = 300.0


# "Has this app's repository moved?" is a network question, so it is never
# folded into anything the UI polls — the user asks, and the answer is held for a
# few minutes so pressing the button repeatedly cannot hammer the remote.
UPDATE_CHECK_TTL = 300.0
_update_cache: Dict[str, "tuple[float, Dict[str, Any]]"] = {}


def _git_at(folder: Path, args: List[str], timeout: float = 90.0):
    """Run git inside an app, on the engine's own allowlist — a repository is code from
    somewhere else, and `git fetch` in it runs hooks and filters that repository ships."""
    return subprocess.run(
        ["git", "-C", folder.as_posix(), *args],
        capture_output=True, text=True, timeout=timeout,
        env=athena_engine.child_env(NON_INTERACTIVE_GIT_ENV),
    )


def _update_check(app_id: str, refresh: bool = False) -> Dict[str, Any]:
    """Whether the app's repository has commits the installed copy lacks.

    Read-only apart from a `git fetch`, which only writes inside the app's own
    `.git`. It never builds anything: the update itself is the app's own
    update.js, run through the normal action, so the answer here is only "is
    there something to update to".
    """
    now = time.time()
    if not refresh:
        hit = _update_cache.get(app_id)
        if hit and now - hit[0] < UPDATE_CHECK_TTL:
            return {**hit[1], "cached": True}

    folder = _app_folder(app_id)
    if folder is None or not (folder / ".git").exists():
        out: Dict[str, Any] = {
            "ok": True, "is_repo": False,
            "reason": "this app was not installed from a git repository"}
    elif not shutil.which("git"):
        out = {"ok": False, "error": "git is not on PATH"}
    else:
        try:
            fetched = _git_at(folder, ["fetch", "--quiet", "--no-tags", "--prune",
                                       "origin"], timeout=120)
        except subprocess.TimeoutExpired:
            out = {"ok": False, "error": "the remote did not answer in time"}
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": str(e)}
        else:
            if fetched.returncode != 0:
                tail = (fetched.stderr or fetched.stdout or "").strip().splitlines()
                out = {"ok": False, "offline": True,
                       "error": (tail[-1] if tail else "git fetch failed")[:200]}
            else:
                head = _git_at(folder, ["rev-parse", "HEAD"], timeout=30).stdout.strip()
                up = _git_at(folder, ["rev-parse", "--abbrev-ref",
                                      "--symbolic-full-name", "@{u}"],
                             timeout=30).stdout.strip()
                if not up:
                    out = {"ok": True, "is_repo": True, "up_to_date": None,
                           "current": head[:12],
                           "reason": "this copy has no upstream branch to "
                                     "compare against"}
                else:
                    # `--left-right --count HEAD...up` prints "<ahead> <behind>":
                    # left is HEAD-only, right is upstream-only.
                    counts = _git_at(
                        folder, ["rev-list", "--left-right", "--count",
                                 f"HEAD...{up}"], timeout=60).stdout.split()
                    ahead = int(counts[0]) if len(counts) == 2 and counts[0].isdigit() else None
                    behind = int(counts[1]) if len(counts) == 2 and counts[1].isdigit() else None
                    remote = _git_at(folder, ["rev-parse", up], timeout=30).stdout.strip()
                    out = {"ok": True, "is_repo": True, "branch": up,
                           "current": head[:12], "remote": remote[:12],
                           "ahead": ahead, "behind": behind,
                           "up_to_date": (behind == 0) if behind is not None else None}

    out["checked_at"] = now
    if out.get("ok"):
        # Cache successes only: a failure is usually "offline right now", and
        # remembering that for five minutes would hide a working network.
        _update_cache[app_id] = (now, out)
    return {**out, "cached": False}


@router.get("/apps/{app_id}/update-check")
def app_update_check(app_id: str, refresh: bool = False) -> JSONResponse:
    """One app's answer to "has its repository moved?" — the route a card asks."""
    return JSONResponse(_update_check(app_id, refresh))


# ---- the same question, asked once for the whole shelf ---------------------
def _installed_app_ids() -> List[str]:
    """Every app folder, by the name the routes call an app by.

    Read from disk rather than from the engine: the engine's app list is a
    view of the same folders, and an app whose engine entry is stale still has
    an update to take.
    """
    root = _athena_home() / "api"
    try:
        return sorted((d.name for d in root.iterdir() if d.is_dir()), key=str.lower)
    except OSError:
        return []


@router.get("/apps/updates")
def apps_updates(refresh: bool = False) -> JSONResponse:
    """Which of the installed apps have repositories that have moved on.

    One card at a time is a button; the question a person actually has is
    whether there is anything to update at all, and answering it per app means
    opening every card. Each check is a network round trip, so they run
    together — and an app that cannot be checked reports why instead of
    failing the whole answer.
    """
    ids = _installed_app_ids()
    results: Dict[str, Any] = {}
    if ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for app_id, res in zip(ids, pool.map(lambda a: _update_check(a, refresh), ids)):
                results[app_id] = res
    behind = {a: int(r["behind"]) for a, r in results.items()
              if r.get("ok") and r.get("behind")}
    return JSONResponse({
        "ok": True,
        "checked": len(ids),
        "any": bool(behind),
        "updates": sorted(behind, key=str.lower),
        "behind": behind,
        "apps": results,
    })


def _wait_for_app_to_finish(jid: str, app_id: str, what: str,
                            timeout: float = 1800.0, quiet: float = 60.0) -> bool:
    """Wait for an app's script to actually end, not merely to have started.

    The engine answers `start` the moment the process exists, so a caller that
    acts on that answer is acting on a script that is still running. Harmless
    for a launch; wrong for an update, because the tree is about to be replaced
    by whatever the pull brings back. So this waits on the same thing the panel
    watches — is any of this app still running — and says which happened.
    """
    began = time.time()
    last_note = began
    while time.time() - began < timeout:
        try:
            running = _runtime().running(app_id) or {}
        except Exception:  # noqa: BLE001
            running = {}
        live = [s for entries in running.values() for s in (entries or [])]
        if not live:
            return True
        if time.time() - last_note > quiet:
            last_note = time.time()
            try:
                tail = (_runtime().logs(app_id) or "").strip().splitlines()[-1:]
            except Exception:  # noqa: BLE001
                tail = []
            _job_add(jid, f"… still {what}" + (f" — {tail[0][:160]}" if tail else ""))
        time.sleep(2.5)
    _job_add(jid, f"!! {app_id}: {what} did not finish within "
                  f"{int(timeout // 60)} minutes — stopping here rather than "
                  f"converting a tree that may still be changing")
    return False


def _update_one(jid: str, app_id: str) -> Dict[str, Any]:
    """One app: run its own update, wait for it, then read it as Athena's again.

    The update is the app's — its update.js, started through the same action
    route the per-card button uses. The conversion afterwards is Athena's half:
    a `git pull` restores whatever the repository ships, including a
    legacy-named launcher, and the app is only an Athena app again once those
    have been converted. Skipping this is how an app quietly reverts to the
    naming it was imported with, one update at a time.
    """
    _job_add(jid, f"--- {app_id}")
    resp = app_action(app_id, "update")
    try:
        out = json.loads(resp.body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        out = {"ok": False, "error": "the update returned something unreadable"}
    if not out.get("ok"):
        why = out.get("port_failure") or out.get("error") or "the update did not start"
        _job_add(jid, f"!! {app_id}: {why}")
        return {"ok": False, "error": why}
    if out.get("disk_warning"):
        _job_add(jid, out["disk_warning"])
    finished = _wait_for_app_to_finish(jid, app_id, "updating")
    try:
        conv = _convert_app(app_id, jid, provision=False)
    except Exception as e:  # noqa: BLE001
        _job_add(jid, f"!! {app_id}: the update ran, but re-reading it failed: {e}")
        return {"ok": False, "finished": finished, "error": str(e)}
    if not conv.get("ok"):
        _job_add(jid, f"!! {app_id}: {conv.get('error')}")
    else:
        _job_add(jid, f"ok  {app_id}: updated and re-read as an Athena app")
    return {"ok": bool(conv.get("ok")), "finished": finished,
            "converted": bool(conv.get("ok")), "error": conv.get("error")}


def _update_all_work(jid: str, ids: List[str]) -> Dict[str, Any]:
    """Update every app whose repository has moved on, one at a time."""
    _job_add(jid, f"asking {len(ids)} app(s) whether their repository has moved…")
    behind: Dict[str, int] = {}
    for app_id in ids:
        res = _update_check(app_id, refresh=True)
        if res.get("ok") and res.get("behind"):
            behind[app_id] = int(res["behind"])
    if not behind:
        _job_add(jid, "every app is already at the latest commit its repository has")
        return {"updated": [], "failed": [], "behind": {}}
    _job_add(jid, f"{len(behind)} app(s) have updates: "
                  + ", ".join(f"{a} ({n})" for a, n in sorted(behind.items())))
    done: List[str] = []
    failed: List[str] = []
    for app_id in sorted(behind, key=str.lower):
        out = _update_one(jid, app_id)
        (done if out.get("ok") else failed).append(app_id)
    _job_add(jid, f"updated {len(done)} app(s)"
                  + (f", {len(failed)} did not: {', '.join(failed)}" if failed else ""))
    return {"updated": done, "failed": failed, "behind": behind}


@router.post("/apps/update-all")
def apps_update_all() -> JSONResponse:
    """Update every app that has something to update to, then re-read each one.

    A job rather than a request: pulls take as long as the network takes, and
    the panel's contract for anything slow is a job id it can watch.
    """
    ids = _installed_app_ids()
    if not ids:
        return JSONResponse({"ok": False, "error": "no apps installed"})
    jid = _job_spawn("update-all", f"updating {len(ids)} app(s)",
                     lambda j: _update_all_work(j, ids))
    return JSONResponse({"ok": True, "job": jid, "apps": ids})


# NOTE: this route has the same shape as `/apps/{app_id}/{action}` below it, so
# it must stay above it — FastAPI answers with the first route that matches, and
# a declaration after the catch-all is never reached. The catch-all would run the
# app's update.js and call that done; this one waits for the pull to finish and
# then re-reads the app, which is the whole point of asking for an update here.
@router.post("/apps/{app_id}/update")
def app_update(app_id: str) -> JSONResponse:
    """Update one app, and stay with it until it is an Athena app again."""
    if _app_folder(app_id) is None:
        return JSONResponse({"ok": False, "error": "app not found"}, status_code=404)
    jid = _job_spawn("update", f"updating {app_id}",
                     lambda j: _update_one(j, app_id))
    return JSONResponse({"ok": True, "job": jid, "app": app_id})


@router.get("/apps/{app_id}/status")
def app_status(app_id: str) -> JSONResponse:
    """Whether an app is running, where its UI is, and what it last printed.

    `running` comes from the engine's own book of running scripts rather than
    from asking "is this app up", because an app can be mid-launch (a script
    running, no URL yet) and the viewer needs to tell that apart from idle.
    """
    if not engine.probe():
        return JSONResponse({"running": False, "port": None, "url": None,
                             "scripts": [], "engine": engine.status()})
    try:
        rt = _runtime()
        rows = rt.running(app_id)
        flat = [r for v in rows.values() for r in v]
        url = next((r["url"] for r in flat if r.get("url")), None)
        return JSONResponse({
            "running": bool(flat),
            "url": url,
            "port": _port_of(url),
            "scripts": flat,
            "log_tail": rt.logs(app_id)[-6000:],
        })
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"running": False, "url": None, "error": str(e)},
                            status_code=502)


def _port_of(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    try:
        return int(url.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return None


# ------------------------------------------------------------------- ports
# Two apps, one socket — and only one half of that is knowable.
#
# The half that is NOT: what port an app is about to want. Nothing in the seven
# apps installed here names one. A recursive grep for "7860" finds it in an
# app's own docs, in a model's tokenizer and in `site-packages`, and not once in
# a launcher — these apps let Gradio choose implicitly, so there is no declaration
# to read and no pre-launch check to run. (An earlier version of this comment
# claimed four of seven apps named 7860. That was the recursive grep talking.)
#
# The half that IS: when a launch dies on a socket, the app's own output says so
# — `EADDRINUSE`, "address already in use" — and Athena knows which of its apps
# is serving on that port at this moment. That turns an opaque failure into a
# sentence naming the app to stop, in the card message and in the smoke verdict,
# instead of leaving the answer at the bottom of a terminal.

def _running_ports() -> Dict[int, str]:
    """`{port: app}` for every app the engine says is serving right now.

    One engine call, not one per app: `/info/scripts` lists every running script
    with the URL it opened, which is the only honest source for a port.
    """
    out: Dict[int, str] = {}
    try:
        rows = _runtime().running()
        for folder, entries in (rows or {}).items():
            for e in (entries or []):
                port = _port_of((e or {}).get("url"))
                if port:
                    out.setdefault(port, folder)
    except Exception:  # noqa: BLE001
        # The whole read is inside the guard, not just the call: this feeds a
        # warning, and a warning path must never be the reason a launch fails.
        # A runtime that answers something other than the shape above — an old
        # engine, a half-written response — means no conflicts to report.
        return out
    return out


def port_failure_note(text: str) -> Optional[str]:
    """A failed bind read out of the app's own output, named where it can be.

    Returns None unless the output really says the socket was taken, so a
    missing module or a bad argument is never reported as a port clash.
    """
    low = (text or "").lower()
    if not any(s in low for s in ("eaddrinuse", "address already in use",
                                  "only one usage of each socket address",
                                  "port is already in use")):
        return None
    live = _running_ports()
    for raw in re.findall(r":(\d{4,5})\b", text or ""):
        owner = live.get(int(raw))
        if owner:
            return (f"port {raw} is {owner}, which is running right now — stop it, "
                    f"or start this app on another port")
    return ("another app is already on the port this one tried to bind"
            if live else "the port this app tried to bind was already taken")


def smoke_app(app_id: str, wait: float = 75.0, stop_after: bool = False,
              href: Optional[str] = None) -> Dict[str, Any]:
    """Launch an app and prove it actually serves something.

    The build ladder needs a verdict a script can reach on its own: "it is up
    and answering" is the only evidence that a generated launcher works, and it
    is the difference between automating a repository and guessing at it. A
    launcher finishing is not proof — an app can print its URL and die a second
    later, which is exactly what a missing file or a wrong port looks like.

    `ok` is true only when an HTTP request to the app's own URL answered below
    400. Everything observed is returned, so a failure carries its own evidence
    to whoever has to finish the job by hand.
    """
    if not engine.probe():
        # A dead engine looks exactly like a broken app from here: no port, no
        # URL, nothing in the log. Three "the app never bound a port" verdicts in
        # one afternoon were all an engine killed by a fatal in its own socket
        # layer. Try once to bring it back before blaming the app.
        try:
            engine.start(wait=120)
        except Exception:  # noqa: BLE001
            pass
    if not engine.probe():
        return {"ok": False, "app": app_id, "reason": "engine_down",
                "error": "the engine is not running and would not start — nothing "
                         "can be built or launched until it does"}
    try:
        rt = _runtime()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "app": app_id, "error": str(e)}

    url = _app_url(app_id) or ""
    started = False
    if not url:
        # Ask the app what it offers *now* and launch that by name. Letting the
        # engine pick from its menu cache gives you the action from before the
        # app changed: right after an install, the cache still says Install, so
        # the "launch" re-runs install.js and the app never binds a port.
        script = href
        if not script:
            try:
                script = rt.open_default(app_id)
            except Exception:  # noqa: BLE001
                script = None
        try:
            res = rt.start(app_id, script=script, wait=1.0)
            if isinstance(res, dict) and res.get("ok"):
                # Started by Athena to prove it works, so it is on the window like
                # any other agent launch: a smoke test that leaves VRAM held is
                # not a favour. (`stop_after` stops it outright.)
                _idle_note(app_id, "a smoke launch", watch=True)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "app": app_id, "error": f"launch failed: {e}"}
        if isinstance(res, dict) and not res.get("ok"):
            if res.get("reason") == "needs_choice":
                return {"ok": False, "app": app_id, "reason": "needs_choice",
                        "error": "this app has no default action — pick one of "
                                 "its menu entries and launch that",
                        "choices": res.get("choices") or res.get("leaves") or []}
            return {"ok": False, "app": app_id,
                    "error": str(res.get("error") or res.get("reason") or res)}
        started = True

    deadline = time.time() + max(5.0, wait)
    while time.time() < deadline:
        url = _app_url(app_id) or url
        if url:
            break
        time.sleep(2.0)

    http: Optional[int] = None
    note = ""
    if url:
        try:
            # `python -m http.server` and plenty of others announce 0.0.0.0,
            # which is a bind address, not somewhere to connect. Probing it
            # verbatim would fail a server that is answering perfectly well.
            probe = re.sub(r"^(https?://)(0\.0\.0\.0|\[::\]|\*)", r"\g<1>127.0.0.1", url)
            probe = probe.rstrip("),.")
            req = Request(probe, headers={"User-Agent": "athena"})
            with urlopen(req, timeout=10) as r:
                http = int(getattr(r, "status", None) or r.getcode() or 0)
                r.read(4000)
        except HTTPError as e:
            http = int(e.code)
        except URLError as e:
            note = f"bound {url} but nothing answered: {e.reason}"
        except Exception as e:  # noqa: BLE001
            note = str(e)
    else:
        note = "no URL appeared — the app never bound a port"

    logs = ""
    try:
        logs = rt.logs(app_id)[-2500:]
    except Exception:  # noqa: BLE001
        pass

    ok = bool(url) and http is not None and http < 400
    if stop_after and (started or url):
        try:
            rt.stop(app_id)
            stopped = True
        except Exception:  # noqa: BLE001
            stopped = False
    else:
        stopped = False
    out: Dict[str, Any] = {"ok": ok, "app": app_id, "url": url or None,
                           "http": http, "launched": started,
                           "stopped_after_proving": stopped,
                           "seconds_waited": round(max(0.0, wait - (deadline - time.time())), 1),
                           "logs": logs}
    if note:
        out["note"] = note
    if not ok:
        # "The app never bound a port" is a verdict, not a reason. The reason is
        # in the app's own output, and if it is a taken socket Athena can name
        # whoever holds it.
        port_note = port_failure_note(f"{note}\n{logs}")
        if port_note:
            out["port_note"] = port_note
    return out


@router.get("/apps/{app_id}/terminal")
def app_terminal(app_id: str) -> JSONResponse:
    """What the app is printing right now, with where it is serving.

    Athena calls this the terminal because that is what it is for: watching an
    install clone or a build download. The text itself is whatever the app has
    written to its own output, which is a log only in the sense that a terminal
    scrollback is one.
    """
    try:
        output = _runtime().logs(app_id)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"output": "", "error": str(e)}, status_code=502)
    # The engine reports the URL a running app opened, when it has one. This is
    # read from its list of running scripts, not from its list of installed
    # apps — the latter carries no port, so asking it always came back empty.
    return JSONResponse({"app": app_id, "output": output,
                         "port": _app_port(app_id), "url": _app_url(app_id)})


class ActionBody(BaseModel):
    """Optional body for an app action.

    `href` names which entry of the app's menu to run and `input` carries the
    values that entry supplies. Hunyuan3D-2-LowVRAM needs both: its start.js
    builds `--profile {{args.profile}}` with no fallback, so the app cannot be
    launched without first picking one of its 33 menu entries.
    """
    href: Optional[str] = None
    input: Optional[Dict[str, Any]] = None


class AutoIn(BaseModel):
    """A repository to turn into a running app without a human in the loop."""
    url: str
    name: Optional[str] = None
    force: bool = False
    wait: float = 90.0


def _ladder_verdict(jid: str, app: str, url: str, *, verified: bool, stage: str,
                    reason: str, detail=None, logs: str = "") -> Dict[str, Any]:
    """End a build job with a verdict, and make a failure actionable.

    "Failed" on its own is useless to the agent that has to finish the job. A
    refusal carries the stage it died at, where the specification it has to
    satisfy lives, and the output that explains it — so the next step is a
    message, not an investigation.
    """
    app_dir = engine.home / "api" / app
    builder = athena_buildapp
    broot = Path(builder.__file__).resolve().parent.parent if builder else Path(__file__).parent
    out: Dict[str, Any] = {"ok": True, "app": app, "url": url,
                           "verified": bool(verified), "stage": stage, "reason": reason}
    if not verified:
        out["escalate"] = True
        out["spec"] = str(app_dir / "SPEC.md")
        out["guide"] = str(broot / getattr(builder, "GUIDE_REL", "docs/build-an-app.md"))
        out["ask"] = ("The scanner could not finish this one. Read SPEC.md for the "
                      "repository, make the launcher do the right thing, then prove it "
                      f"with athena_build_app(action='smoke', app='{app}').")
        if detail is not None:
            out["detail"] = detail
        if logs:
            out["logs"] = logs[-2500:]
    _job_end(jid, "done", result=out)
    return out


def _ladder_worker(jid: str, url: str, name: Optional[str], force: bool,
                   wait: float) -> Dict[str, Any]:
    """Turn a repository into a running app, unattended.

    The ladder is: read the repo (the scanner, mechanical), decide whether it is
    runnable *without judgement*, install it, and prove it answers. Only the two
    things a script cannot do — invent a launch command, and repair something
    that was wrong — come back to the agent, and they come back with evidence.
    """
    builder = athena_buildapp
    if builder is None:
        _job_end(jid, "failed", error="the builder module is unavailable")
        return {"ok": False}
    home = engine.home
    if not engine.probe():
        _job_add(jid, "Starting the engine…")
        started = engine.start(wait=120)
        if not started.get("ok"):
            _job_end(jid, "failed", error="the engine did not start, so nothing can be built")
            return {"ok": False}

    _job_progress(jid, 0, 5, "reading the repository")
    _job_add(jid, f"Reading {url}")
    try:
        res = builder.scaffold(home, url, name=name, force=force)
    except Exception as e:  # noqa: BLE001
        err = f"the repository could not be read: {e}"
        _job_add(jid, err)
        _job_end(jid, "failed", error=err)
        return {"ok": False}

    if not res.get("ok"):
        if res.get("already_an_app"):
            app = res["app"]
            _job_add(jid, f"{app} already has a launcher — proving it rather than rebuilding it.")
            return _ladder_finish(jid, app, url, wait, jid_prefix="")
        err = res.get("error") or "the repository could not be read"
        _job_add(jid, err)
        return _ladder_verdict(jid, name or url, url, verified=False,
                               stage="recon", reason=err)

    app = res["app"]
    recon = res.get("recon") or {}
    _job_add(jid, "Stack: " + (", ".join(recon.get("stack") or []) or "unknown")
             + f"  ·  shape: {recon.get('kind')}  ·  {recon.get('total_files')} files")
    if recon.get("servers"):
        _job_add(jid, "Serves with: " + ", ".join(recon["servers"]))

    # The one thing a script cannot invent. If the scanner found no launch
    # command it wrote a marker instead of a guess, and no amount of installing
    # will fix an unknown entry point — so stop here, before spending minutes.
    start_js = home / "api" / app / "start.js"
    text = start_js.read_text(encoding="utf-8", errors="replace") if start_js.is_file() else ""
    if "TODO(agent)" in text:
        why = ("the scanner found no launch command for this repository, so the app "
               "cannot be started unattended")
        _job_add(jid, f"Needs a human decision: {why}")
        return _ladder_verdict(jid, app, url, verified=False, stage="launcher",
                               reason=why, detail={"spec": str(home / "api" / app / "SPEC.md"),
                                                   "kind": recon.get("kind"),
                                                   "entry": recon.get("entry")})

    _job_progress(jid, 1, 5, "checking the launcher")
    try:
        val = builder.validate(home, app)
    except Exception as e:  # noqa: BLE001
        val = {"ok": False, "errors": [str(e)]}
    if not val.get("ok"):
        errs = val.get("errors") or ["the launcher did not validate"]
        _job_add(jid, "Launcher check failed: " + "; ".join(str(e) for e in errs)[:400])
        return _ladder_verdict(jid, app, url, verified=False, stage="launcher",
                               reason="the generated launcher did not pass validation",
                               detail=errs)

    try:
        rt = _runtime()
    except Exception as e:  # noqa: BLE001
        _job_end(jid, "failed", error=f"the engine runtime is unavailable: {e}")
        return {"ok": False}

    _job_progress(jid, 2, 5, "installing")
    try:
        script = rt.open_default(app)
    except Exception as e:  # noqa: BLE001
        script = None
        _job_add(jid, f"Could not read the app's menu: {e}")
    if not script:
        return _ladder_verdict(jid, app, url, verified=False, stage="launcher",
                               reason="the launcher offers no default action to run")
    _job_add(jid, f"Running {os.path.basename(script)}")
    try:
        ran = rt.run(app, uri=script, idle_timeout=IDLE_INSTALL,
                     on_step=lambda cur, total, method: _job_progress(jid, 2, 5, f"installing ({cur}/{total})"),
                     on_line=lambda line: _job_add(jid, line))
    except Exception as e:  # noqa: BLE001
        return _ladder_verdict(jid, app, url, verified=False, stage="install",
                               reason=f"the install could not be started: {e}")
    if ran.get("state") == "error":
        why = ran.get("error") or "the install script reported an error"
        _job_add(jid, why)
        return _ladder_verdict(jid, app, url, verified=False, stage="install",
                               reason=why, logs=_job_log(jid), detail={"script": os.path.basename(script)})

    return _ladder_finish(jid, app, url, wait, jid_prefix="install ")


def _ladder_finish(jid: str, app: str, url: str, wait: float, jid_prefix: str = "") -> Dict[str, Any]:
    """The whole point: launch it and see it answer. Anything less is a guess."""
    _job_progress(jid, 4, 5, "proving it runs")
    _job_add(jid, f"Launching {app} to prove it serves…")
    # Stop it once answered: the ladder's job is a verdict, not a service. A
    # daemon left behind holds the app folder open, and a leftover server on the
    # port makes the NEXT build look broken (seen: a leaked http.server blocked
    # its own rebuild's cleanup).
    sm = smoke_app(app, wait=wait, stop_after=True)
    if sm.get("ok"):
        _job_add(jid, f"Verified: {sm.get('url')} answered HTTP {sm.get('http')} "
                      f"after {sm.get('seconds_waited')}s.")
        _job_end(jid, "done", result={"ok": True, "app": app, "url": url, "verified": True,
                                      "proved": sm.get("url"), "http": sm.get("http"),
                                      "seconds": sm.get("seconds_waited")})
        return {"ok": True, "app": app, "verified": True}
    why = sm.get("error") or sm.get("note") or "the app did not come up"
    _job_add(jid, f"Not verified: {why}")
    return _ladder_verdict(jid, app, url, verified=False, stage="smoke", reason=why,
                           logs=sm.get("logs") or _job_log(jid),
                           detail={"claimed_url": sm.get("url"), "http": sm.get("http")})


def _job_log(jid: str) -> str:
    with _JOBS_LOCK:
        j = _JOBS.get(jid) or {}
        return "\n".join(j.get("log") or [])


def auto_build(url: str, name: Optional[str] = None, force: bool = False,
               wait: float = 90.0) -> Dict[str, Any]:
    """Start the ladder as a job. Both the panel and the agent come through here."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "no repository given"}
    if _job_running("build"):
        return {"ok": False, "error": "a build is already running — wait for it or "
                                      "check the runs panel"}
    jid = _job_spawn("build", f"Building {name or url}",
                     lambda j: _ladder_worker(j, url, name, force, wait))
    return {"ok": True, "job": jid, "app": name or "", "url": url}


@router.post("/catalog/auto")
def catalog_auto(body: AutoIn) -> JSONResponse:
    """Turn a repository into a running app without a human in the loop.

    Answers with a job, not a verdict: a build clones, installs and launches,
    which is minutes. The result carries `verified: true` when the app was seen
    answering on a port, and `escalate: true` with the evidence when the scanner
    ran out of what it can know.
    """
    out = auto_build(body.url, body.name, body.force, body.wait)
    return JSONResponse(out, status_code=200 if out.get("ok") else 409)


# ---- publishing to the Athena Store ---------------------------------------
# The store is one repository, one branch per app. It used to be rebuilt by
# hand from a shell; this is the same publisher, reachable from an app's card,
# using the GitHub login Hermes already holds — so publishing never asks the
# user to sign in twice.


def _store_info(home: Optional[Path] = None) -> Dict[str, Any]:
    """Where the store is, and what it takes to write to it.

    Read from the same setting the Store tab browses (`store_index`), because a
    store you can read but not write is a browse, and a store you can write but
    not read is a mystery. No setting means Athena's own default.
    """
    home = home or _athena_home()
    info: Dict[str, Any] = {"configured": False, "index": "", "repo": "", "clone": "",
                            "token": "", "checkout": str(Path(home) / "store")}
    if athena_catalog is None:
        return info
    try:
        index = athena_catalog.source_url(home)
    except Exception:  # noqa: BLE001
        index = ""
    info["index"] = index or ""
    root = ""
    try:
        root = athena_catalog._raw_root(index) or ""
    except Exception:  # noqa: BLE001
        root = ""
    if not root:
        return info
    owner_repo = root.split("raw.githubusercontent.com/", 1)[-1].strip("/")
    if owner_repo.count("/") != 1:
        return info
    info["repo"] = owner_repo
    info["clone"] = f"https://github.com/{owner_repo}.git"
    try:
        info["token"] = athena_catalog._gh_token() or ""
    except Exception:  # noqa: BLE001
        info["token"] = ""
    info["configured"] = True
    return info


def _model_library_candidates(home: Path) -> List[Dict[str, Any]]:
    """Folders on this machine that already hold ComfyUI weights.

    Evidence, not a guess: folders an installed app is *already* pointed at
    through its own extra-paths file (read with the same parser the probe uses,
    so "what an app can reach" and "what we offer" cannot drift), plus any path
    a person named in `model_roots` in settings. Nothing is scanned drive-wide —
    a folder listed here is one some app could generate from today, which is the
    only kind worth offering as "your weights already live here".
    """
    ac = _appcall_module()
    if ac is None:
        return []
    roots: Dict[str, Dict[str, Any]] = {}

    def remember(raw: Any, who: str) -> None:
        try:
            path = Path(str(raw))
            key = str(path.resolve()).lower()
        except Exception:  # noqa: BLE001
            return
        rec = roots.setdefault(key, {"path": path, "apps": []})
        if who and who not in rec["apps"]:
            rec["apps"].append(who)

    api = home / "api"
    if api.is_dir():
        for d in sorted(api.iterdir()):
            if not d.is_dir():
                continue
            name = d.name[:-4] if d.name.lower().endswith(".git") else d.name
            try:
                # The parser answers with the *mapped* folder — pointing at
                # `.../models` maps `checkpoints: checkpoints`, so it returns
                # `.../models/checkpoints`, a leaf. What can be offered as a
                # library is the folder that holds such leaves, and asking the
                # leaf for its own `checkpoints/` is how this found nothing on a
                # machine whose weights ComfyUI could already reach.
                for p in ac._comfy_extra_roots(d):
                    remember(ac._comfy_library_root(p), name)
            except Exception:  # noqa: BLE001
                continue
    for raw in _load_settings().get("model_roots") or []:
        remember(raw, "")

    out: List[Dict[str, Any]] = []
    for rec in roots.values():
        counts = ac.comfy_weight_counts(rec["path"])
        if not any(counts.values()):
            continue
        out.append({"folder": rec["path"].as_posix(), "counts": counts,
                    "total": sum(counts.values()), "apps": sorted(rec["apps"])})
    out.sort(key=lambda r: (-r["total"], r["folder"]))
    return out


def _dir_size_str(n: int) -> str:
    """Bytes as a person says them. These numbers are all about disk, so the
    unit a caller would have used anyway is the one to answer in."""
    for unit, div in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            text = f"{n / div:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{n} B"


def _hf_cache(home: Path) -> Dict[str, Any]:
    """What HuggingFace has already downloaded, by repository.

    The hub cache is where a download lands by default, and it is the one weight
    store that is not a ComfyUI library: code asks for a repository by name and
    the cache answers. So a repository sitting here is a download already paid
    for — worth naming twice over, as disk nothing reads and as the answer to
    "do we have this already?" before anything is fetched.

    Folders are named `models--<owner>--<name>` by HF's own layout, which is the
    only mapping back from a folder to the id anyone would search by.
    """
    hub = home / "cache" / "HF_HOME" / "hub"
    repos: List[Dict[str, Any]] = []
    total = 0
    complete = True
    try:
        entries = sorted(p for p in hub.iterdir() if p.is_dir()) if hub.is_dir() else []
    except OSError:
        entries = []
    for d in entries:
        if not d.name.startswith("models--"):
            continue
        # Blobs live inside the repository folder and the snapshots only link to
        # them, so the walk skips the links and each byte is counted once.
        size, done = _dir_size(d, budget=2.0)
        total += size
        complete = complete and done
        repos.append({"repo": "/".join(d.name[len("models--"):].split("--")),
                      "folder": d.as_posix(), "bytes": size, "bytes_complete": done})
    repos.sort(key=lambda r: (-r["bytes"], r["repo"]))
    return {"folder": hub.as_posix(), "repos": repos, "count": len(repos),
            "bytes": total, "bytes_complete": complete}


def _weights_inventory(home: Path, sizes: bool = True) -> Dict[str, Any]:
    """Who needs which weights, and what is on this disk that nothing reads.

    Three questions that are really one: what each app can generate with right
    now, which library each app is reading, and what a folder is costing. The
    last two are what make the write half — pulling a repository into a folder
    an app actually reads — possible without asking anyone for a path, and an
    unused library is only worth naming with its size attached.

    Nothing here downloads, starts, or changes anything: every answer is a
    directory walk, the same reason the health route can be polled.
    """
    ac = _appcall_module()
    apps: List[Dict[str, Any]] = []
    libs: Dict[str, Dict[str, Any]] = {}

    def remember(raw: Any, who: str) -> None:
        try:
            path = Path(str(raw))
            key = str(path.resolve()).lower()
        except Exception:  # noqa: BLE001
            return
        rec = libs.setdefault(key, {"folder": path.as_posix(), "apps": [],
                                    "counts": {}, "total": 0, "empty": False})
        if who and who not in rec["apps"]:
            rec["apps"].append(who)

    api = home / "api"
    if ac is not None and api.is_dir():
        for d in sorted(api.iterdir()):
            if not d.is_dir():
                continue
            name = d.name[:-4] if d.name.lower().endswith(".git") else d.name
            try:
                verdict = ac._comfy_models(d)
            except Exception:  # noqa: BLE001
                continue
            # Every folder the verdict looked at counts as a library, whether or
            # not it holds anything: an app pointed at an empty folder is wired
            # up wrong, which is a different fact from an app with no mapping.
            # The verdict names its folders as strings (it is JSON-bound), and the
            # library-root helper works on paths — hence the cast rather than
            # a second spelling of the same rule.
            for f in verdict.get("folders") or []:
                try:
                    p = Path(str(f.get("folder")))
                    # An app's own three models/<key> folders are leaves of one
                    # library, and `_comfy_library_root` (rightly) leaves them
                    # alone while they are empty — so they are folded up here.
                    # Without this an app with nothing installed reports three
                    # libraries instead of one, and the shelf reads as noise.
                    if f.get("source") == "app" and p.parent.name.lower() == "models":
                        p = p.parent
                    else:
                        p = ac._comfy_library_root(p)
                    remember(p, name)
                except Exception:  # noqa: BLE001
                    continue
            apps.append({"app": name, "ready": bool(verdict.get("ready")),
                         "kind": verdict.get("kind") or "",
                         "weighable": bool(verdict.get("weighable", True)),
                         "count": int(verdict.get("count") or 0),
                         "folders": [str(f.get("folder")) for f in verdict.get("folders") or []],
                         "next_step": verdict.get("next_step")})
    for raw in _load_settings().get("model_roots") or []:
        remember(raw, "")

    for rec in libs.values():
        counts = ac.comfy_weight_counts(Path(rec["folder"])) if ac is not None else {}
        rec["counts"] = counts
        rec["total"] = sum(counts.values())
        rec["empty"] = rec["total"] == 0
        if sizes:
            rec["bytes"], rec["bytes_complete"] = _dir_size(Path(rec["folder"]), budget=2.0)
        # A folder can hold hundreds of megabytes and still have nothing to
        # generate with — LoRAs, a VAE, embeddings. Saying "empty" next to a
        # size would read as a bug, so the two facts are stated together.
        if rec["empty"] and rec.get("bytes"):
            rec["note"] = ("no checkpoints, unet or diffusion models here — what it "
                           "holds cannot generate on its own")

    hf = _hf_cache(home)
    libraries = sorted(libs.values(), key=lambda r: (-r.get("bytes", 0), r["folder"]))
    # "Unreferenced" means no app mapping leads here. The hub cache is *not*
    # folded into this: an app that asks HuggingFace for a repository gets it
    # from there, so those bytes are read — they are cached, which is a
    # different fact and a useful one, since a cached repository never needs
    # downloading again.
    unused = [r for r in libraries if not r["apps"] and not r["empty"]]
    unused_bytes = sum(int(r.get("bytes", 0)) for r in unused)
    ready = sum(1 for a in apps if a["ready"])
    # Only an app this question can actually judge. A gradio app whose weights
    # live somewhere ComfyUI would never look is not an app that "cannot
    # generate" — reporting it as one is precisely the false verdict that sent
    # an agent after models a machine already had.
    need = [a["app"] for a in apps if not a["ready"] and a.get("weighable", True)]
    summary = (f"{ready}/{len(apps)} apps can generate; {len(libraries)} weight "
               f"librar{'y' if len(libraries) == 1 else 'ies'} holding "
               f"{_dir_size_str(sum(int(r.get('bytes', 0)) for r in libraries))}")
    if unused_bytes:
        summary += f"; nothing reads {_dir_size_str(unused_bytes)} of it"
    if hf["count"]:
        summary += (f"; {hf['count']} repositories cached "
                    f"({_dir_size_str(int(hf['bytes']))})")
    return {
        "apps": apps,
        "need_weights": need,
        "libraries": libraries,
        "hf_cache": hf,
        "unreferenced": [
            {"folder": r["folder"], "bytes": int(r.get("bytes", 0)), "total": r["total"],
             "kind": "library"} for r in unused],
        "cached": {
            "folder": hf["folder"], "count": hf["count"], "bytes": int(hf["bytes"]),
            "repos": [{"repo": x["repo"], "bytes": x["bytes"]} for x in hf["repos"]],
            "detail": "a repository already downloaded: asking HuggingFace for it "
                      "again is answered from here, so it is never fetched twice",
        },
        "totals": {"apps": len(apps), "ready": ready, "need_weights": len(need),
                   "libraries": len(libraries),
                   "weights_bytes": sum(int(r.get("bytes", 0)) for r in libraries),
                   "unreferenced_bytes": unused_bytes, "cached_repos": hf["count"],
                   "cached_bytes": int(hf["bytes"])},
        "summary": summary,
    }


# ------------------------------------------------------- pulling weights in

HF_API = "https://huggingface.co/api"


def _hf_cli(home: Path) -> Optional[Path]:
    """The `hf` command Athena already has, if it is there.

    Hermes ships a Miniforge inside Athena's home and the HuggingFace CLI lives
    in it, so pulling a model needs no second Python environment and no
    hand-rolled HTTP: `hf download` already resumes a partial file, checks every
    hash against the hub's metadata, and honours the token apps inherit.
    """
    for rel in ("bin/miniforge/Scripts/hf.exe", "bin/miniforge/bin/hf",
                "bin/miniforge/hf.exe", "bin/miniforge/hf"):
        cand = home / rel
        if cand.exists():
            return cand
    found = shutil.which("hf") or shutil.which("huggingface-cli")
    return Path(found) if found else None


def _hf_token_value(app_id: str = "") -> str:
    """The HuggingFace token a pull may use, or "".

    A secret handed only to a child process: never logged, never in a job result,
    never returned by a route, and never read from `cache/HF_AUTH/token` — nothing
    writes that file any more. With an app named it is the one *that app* was given,
    asked for once through Hermes' gate; an app nobody has said yes for — or one with
    no gate to ask — gets "" and downloads what it can anonymously.
    """
    if app_id:
        return _app_credential_env(app_id).get("HF_TOKEN", "")
    try:
        return str((collect_credentials().get("huggingface") or {}).get("token") or "")
    except Exception:  # noqa: BLE001
        return ""


def _hf_http_json(url: str, token: str = "") -> Dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "athena"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=25) as r:
            return {"ok": True, "data": json.loads(r.read().decode("utf-8"))}
    except HTTPError as e:
        if e.code == 404:
            return {"ok": False, "error": "no such repository on HuggingFace"}
        if e.code in (401, 403):
            return {"ok": False, "error": (f"HuggingFace refused this repository ({e.code}) — "
                                           "it is private or gated, and the token Athena "
                                           "holds is not accepted for it")}
        return {"ok": False, "error": f"HuggingFace answered HTTP {e.code}"}
    except (URLError, OSError, ValueError) as e:
        return {"ok": False, "error": f"could not reach HuggingFace: {e}"}


def _hf_cached_repo(home: Path, repo: str) -> Optional[Path]:
    """The cache folder a repository would already be in, if it is there."""
    d = home / "cache" / "HF_HOME" / "hub" / ("models--" + repo.strip("/").replace("/", "--"))
    return d if d.is_dir() else None


def hf_remote(repo: str, home: Optional[Path] = None) -> Dict[str, Any]:
    """What a repository holds and what it costs, before anything is fetched.

    Asked of HuggingFace rather than of this disk, because "should this be
    downloaded" is answered by a size and a file list. The exact byte counts come
    from the API's own `blobs=true`: the CLI's dry run prints rounded sizes, and
    "about 4 gigabytes" is not a number to plan a disk around. The three files
    that matter most are named separately — an app can only generate from a
    weight, so a repository of configs and tokenizers is not a model to download
    no matter how large it is.

    The cached copy on this disk is checked in the same breath, because a
    repository already here should never be fetched twice.
    """
    repo = repo.strip().strip("/")
    if "/" not in repo:
        return {"ok": False, "error": "a repository is named `owner/name`, "
                                      f"and `{repo}` is not"}
    got = _hf_http_json(f"{HF_API}/models/{repo}?blobs=true", _hf_token_value())
    if got.get("ok") is False:
        return {**got, "repo": repo}
    data = got["data"] or {}
    files = [{"file": str(s.get("rfilename")), "bytes": int(s.get("size") or 0)}
             for s in (data.get("siblings") or []) if s.get("rfilename")]
    files.sort(key=lambda f: (-f["bytes"], f["file"]))
    ac = _appcall_module()
    suffixes = tuple(getattr(ac, "_COMFY_WEIGHT_SUFFIXES",
                             (".safetensors", ".ckpt", ".pt", ".gguf", ".sft")))
    weights = [f for f in files if f["file"].lower().endswith(suffixes)]
    total = sum(f["bytes"] for f in files)
    cached = _hf_cached_repo(home or _athena_home(), data.get("id") or repo)
    present: List[str] = []
    if cached is not None:
        snaps = cached / "snapshots"
        try:
            present = sorted({p.name for rev in snaps.iterdir() if rev.is_dir()
                              for p in rev.rglob("*") if p.is_file()})
        except OSError:
            present = []
    return {
        "ok": True,
        "repo": data.get("id") or repo,
        "private": bool(data.get("private")),
        "gated": bool(data.get("gated")),
        "files": files,
        "count": len(files),
        "bytes": total,
        "weights": weights,
        "weights_bytes": sum(f["bytes"] for f in weights),
        "has_generating_weights": bool(weights),
        "already_cached": cached is not None,
        "cached_folder": cached.as_posix() if cached is not None else None,
        "cached_files": present,
        "cached_note": ("a copy of this repository is already on this disk, so "
                        "pulling it again copies from the local cache rather "
                        "than from the network") if cached is not None else None,
        "summary": (f"{data.get('id') or repo}: {len(files)} files, "
                    f"{_dir_size_str(total)}"
                    + (f", of which {len(weights)} are weights "
                       f"({_dir_size_str(sum(f['bytes'] for f in weights))})" if weights
                       else " — no weights in it")
                    + ("; already cached here" if cached is not None else "")),
    }


def _pull_dest(home: Path, body: "PullBody") -> Dict[str, Any]:
    """Where a pulled repository should land, and why.

    A weight an app cannot reach is not a download, it is storage — so the
    destination defaults to the library the named app already reads, filed under
    the key that makes it usable (`checkpoints/`, unless told otherwise). An app
    with no mapping of its own is pointed at its own `models/`, which is the
    folder ComfyUI reads by default. With neither an app nor a folder, the pull
    is a *cache* entry: the repository becomes available to anything that asks
    for it by name, and nothing on this disk pretends to read it yet.
    """
    ac = _appcall_module()
    key = (body.key or "checkpoints").strip().lower()
    known = tuple(getattr(ac, "_COMFY_KNOWN_KEYS", ("checkpoints",)))
    if key not in known:
        return {"ok": False, "error": (f"`{key}` is not a folder ComfyUI reads — it looks in "
                                       + ", ".join(known))}
    if body.app:
        folder = _app_folder_for(body.app)
        if folder is None:
            return {"ok": False, "error": f"no app called {body.app} is installed"}
        extra = ac._comfy_extra_roots(folder) if ac is not None else []
        lib = ac._comfy_library_root(extra[0]) if extra else (folder / "app" / "models")
        return {"ok": True, "dest": lib / key, "library": lib, "key": key,
                "because": (f"{body.app} is already pointed at {lib.as_posix()}"
                            if extra else f"{body.app} reads its own models/ folder")}
    if body.folder:
        lib = Path(body.folder).expanduser()
        if ac is not None:
            lib = ac._comfy_library_root(lib)
        return {"ok": True, "dest": lib / key, "library": lib, "key": key,
                "because": "the folder named in the request"}
    return {"ok": True, "dest": None, "cache_only": True,
            "because": "no app or folder was named, so this is a cache entry"}


def _pull_matches(files: List[Dict[str, Any]], include: Optional[str]) -> List[Dict[str, Any]]:
    """The files an `--include` pattern actually selects, for a real size.

    The pattern is a glob and the CLI is the thing that applies it, so the
    matching here has to agree with `fnmatch` on the repository's paths — this is
    only a size, but a size that is wrong by the whole repository is worse than
    no size at all.
    """
    if not include:
        return files
    import fnmatch
    pats = [p.strip() for p in str(include).split(",") if p.strip()]
    return [f for f in files
            if any(fnmatch.fnmatch(f["file"], p) or fnmatch.fnmatch(f["file"], p.rstrip("/") + "/*")
                   or fnmatch.fnmatch(f["file"], "*/" + p) for p in pats)]


def _pull_run(job: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch one repository and stream its progress into the job it belongs to.

    The `hf` CLI does the two parts worth trusting someone else with: a partial
    file resumes where it stopped, and every file is hashed against the hub's own
    metadata. What this wrapper adds is everything Athena-shaped — the token, the
    shared cache, a destination an app can actually read, and a byte count that
    comes from watching the folder rather than from a progress bar.
    """
    home = _athena_home()
    cli = _hf_cli(home)
    if cli is None:
        return {"ok": False, "error": "no `hf` command found — Athena's Miniforge is "
                                      "where it is expected; reinstall the engine's "
                                      "HuggingFace CLI to pull models here"}
    jid = job["jid"]
    repo, include, revision = job["repo"], job.get("include"), job.get("revision")
    dest: Optional[Path] = job.get("dest")
    # This child gets *this app's* granted token (below) and nothing else that is
    # credential-shaped: an ambient HF_TOKEN in Athena's own process is not this
    # app's to spend, and an app nobody has said yes for downloads anonymously.
    # The CLI draws its bar with carriage returns, which arrive as one enormous line
    # and say nothing a job log can show; the bytes on disk are the progress instead.
    env = {k: v for k, v in os.environ.items()
           if k not in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")}
    env["HF_HOME"] = str(home / "cache" / "HF_HOME")
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    if job.get("token"):
        env["HF_TOKEN"] = env["HUGGING_FACE_HUB_TOKEN"] = str(job["token"])
    cmd = [str(cli), "download", repo]
    if include:
        cmd += ["--include", include]
    if revision:
        cmd += ["--revision", revision]
    if dest is not None:
        dest.mkdir(parents=True, exist_ok=True)
        cmd += ["--local-dir", dest.as_posix()]
    _job_add(jid, f"Downloading {repo}" + (f" ({include})" if include else "")
                  + (f" into {dest.as_posix()}" if dest is not None
                     else " into the shared cache"))
    # Growth is watched in the folder this pull writes into, so a resumed file
    # counts from where it stopped instead of starting the bar over.
    probe = dest if dest is not None else (home / "cache" / "HF_HOME" / "hub" /
                                           ("models--" + repo.replace("/", "--")))
    before = _dir_size(probe, budget=2.0)[0]
    total = int(job.get("total") or 0)

    logf = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False,
                                       encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                text=True, env=env, cwd=str(home))
        while proc.poll() is None:
            time.sleep(1.5)
            got = _dir_size(probe, budget=1.0)[0]
            _job_progress(jid, got, max(total, got), _dir_size_str(got)
                          + (f" of {_dir_size_str(total)}" if total else ""))
        logf.flush()
        logf.seek(0)
        said = [ln.strip() for ln in logf.read().splitlines() if ln.strip()]
    finally:
        try:
            logf.close()
            os.unlink(logf.name)
        except OSError:
            pass
    for line in said[-6:]:
        _job_add(jid, line[:300])

    wrote = _dir_size(probe, budget=3.0)[0]
    landed = [p for p in dest.rglob("*") if p.is_file()] if dest is not None else []
    # `hf` keeps its own record of what it has already copied in a dot-folder
    # inside the destination. That bookkeeping is not what anyone asked for, so
    # it is counted separately instead of being passed off as downloaded files.
    bookkeeping = [p for p in landed if ".cache" in p.parts]
    landed = [p for p in landed if ".cache" not in p.parts]
    ac = _appcall_module()
    suffixes = tuple(getattr(ac, "_COMFY_WEIGHT_SUFFIXES",
                             (".safetensors", ".ckpt", ".pt", ".gguf", ".sft")))
    weights = [p for p in landed if p.suffix.lower() in suffixes]
    ok = proc.returncode == 0 and (dest is None or bool(landed))
    res: Dict[str, Any] = {
        "ok": ok,
        "repo": repo,
        "dest": dest.as_posix() if dest is not None else None,
        "cache_only": dest is None,
        "cache_folder": (home / "cache" / "HF_HOME" / "hub" /
                         ("models--" + repo.replace("/", "--"))).as_posix(),
        "bytes_on_disk": wrote,
        "bytes_written": max(0, wrote - before),
        "resumed_from": before,
        "files": len(landed),
        "weights": len(weights),
        "exit_code": proc.returncode,
    }
    if bookkeeping:
        res["local_metadata"] = (
            (dest / ".cache" / "huggingface").as_posix() + " holds hf's record of what "
            "it has already copied here — it is what makes a re-pull skip the files "
            "that are already in place, and nothing else reads it")
    if not ok:
        res["error"] = (f"`hf download` exited {proc.returncode}"
                        + ("" if not said else ": " + said[-1][:200]))
        res["hint"] = ("a gated or private repository needs a token that is accepted for "
                       "it; an interrupted pull resumes if it is started again")
    _job_progress(jid, wrote, wrote, _dir_size_str(wrote))
    return res


# The queue is one slot wide on purpose: a disk and a link are both shared, and
# two multi-gigabyte pulls at once make neither one faster — the second is only
# harder to explain. A job exists the moment it is asked for (its id is the
# answer the caller gets) and starts when the slot frees.
_PULL_LOCK = threading.Lock()
_PULL_QUEUE: List[Dict[str, Any]] = []
_PULL_PUMP: List[Optional[threading.Thread]] = [None]
# The pull in the slot right now, if any. Waiting behind the *running* download
# is the ordinary case, and a count that only looked at the queue would call it
# zero ahead — which is how "it ran two at once" would read from the outside.
_PULL_ACTIVE: List[Optional[str]] = [None]


def _pull_pump() -> None:
    """Run queued downloads, one at a time, oldest first."""
    while True:
        with _PULL_LOCK:
            if not _PULL_QUEUE:
                _PULL_PUMP[0] = None
                _PULL_ACTIVE[0] = None
                return
            job = _PULL_QUEUE.pop(0)
            waiting = len(_PULL_QUEUE)
            _PULL_ACTIVE[0] = job["jid"]
        if waiting:
            _job_progress(job["jid"], 0, 0, f"downloading — {waiting} more queued")
        try:
            res = _pull_run(job)
        except Exception as e:  # noqa: BLE001
            _job_add(job["jid"], f"!! {e}")
            _job_end(job["jid"], "failed", error=str(e))
            continue
        _job_end(job["jid"], "done" if res.get("ok") else "failed", result=res,
                 error=None if res.get("ok") else res.get("error"))


def _pull_enqueue(job: Dict[str, Any]) -> None:
    with _PULL_LOCK:
        _PULL_QUEUE.append(job)
        ahead = len(_PULL_QUEUE) - 1 + (1 if _PULL_ACTIVE[0] else 0)
        alive = _PULL_PUMP[0] is not None and _PULL_PUMP[0].is_alive()
        if not alive:
            _PULL_PUMP[0] = threading.Thread(target=_pull_pump, name="athena-pull",
                                             daemon=True)
            _PULL_PUMP[0].start()
    if ahead:
        _job_progress(job["jid"], 0, 0, f"queued — {ahead} download(s) ahead of it")
        _job_add(job["jid"], f"Queued behind {ahead} download(s); they run one at a "
                             "time so no two of them split the link.")


def _app_folder_for(app_id: str) -> Optional[Path]:
    """The folder an app id names, or None. One resolver, under its older name."""
    return _app_folder(app_id)


def _publish_worker(jid: str, app_id: str, store: Dict[str, Any],
                    approve: bool = False) -> Dict[str, Any]:
    """Clone or fetch the store, rebuild every branch from this machine, push."""
    mod = _publish_module()
    checkout = Path(store["checkout"])
    line = lambda text: _job_add(jid, str(text))  # noqa: E731
    _job_add(jid, f"Store: {store['repo']}")
    _job_add(jid, f"Checkout: {checkout}")
    # Approved to force-push the store? The caller's yes, or the person's, right here.
    asked = {"approved": True, "message": ""} if approve else athena_engine.ask_approval(
        "athena_publish", f"force-push every branch of {store['repo']} for {app_id} "
        "(publishing rebuilds each branch from scratch)", "athena:publish")
    try:
        mod.ensure_checkout(checkout, store["clone"], store["token"], progress=line)
        res = mod.publish(home=_athena_home(), out=checkout, repo=store["repo"],
                          push=asked["approved"], token=store["token"], progress=line)
    except Exception as e:  # noqa: BLE001
        _job_add(jid, f"!! {e}")
        return {"ok": False, "error": str(e), "app": app_id}
    branch = ""
    for name in res.get("branches") or []:
        if name.endswith(app_id) or app_id.startswith(name):
            branch = name
    if asked["approved"]:
        _job_add(jid, f"Pushed {len(res.get('branches') or [])} branches to {store['repo']}")
    else:
        _job_add(jid, "Nothing was sent: publishing rewrites every branch of the store, "
                      f"so the push is a force-push. {asked['message']}".strip())
    return {**res, "app": app_id, "branch": branch or "", "pushed": bool(asked["approved"]),
            "store": f"https://github.com/{store['repo']}"}


def publish_app(app_id: str, approve: bool = False) -> Dict[str, Any]:
    """Publish this machine's apps to the store, from one app's card.

    Answers with a job, not a result: a publish clones or fetches, rewrites every
    branch, and pushes. Pushing *rewrites* those branches, so it is a force-push and
    needs approval for this call — `approve=True` from a caller that has just asked,
    or the human gate `engine.ask_approval` asks inside the job.
    """
    folder = _app_folder_for(app_id)
    if folder is None:
        return {"ok": False, "error": f"no app named {app_id}"}
    store = _store_info()
    if not store["configured"]:
        return {"ok": False, "app": app_id,
                "error": "the store is not configured — set a store index in Settings, "
                         "or point Athena at a repository you can write to"}
    if not store["token"]:
        return {"ok": False, "app": app_id,
                "error": "no GitHub credential is available — connect GitHub in Hermes "
                         "and the store will use that login"}
    if _job_running("publish"):
        return {"ok": False, "app": app_id, "error": "a publish is already running"}
    jid = _job_spawn("publish", f"Publishing {app_id} to {store['repo']}",
                     lambda j: _publish_worker(j, app_id, store, approve))
    return {"ok": True, "app": app_id, "job": jid, "repo": store["repo"],
            "store": f"https://github.com/{store['repo']}",
            "branch": _publish_module().slug_for(folder.name),
            "note": "every app is republished, so the store stays a mirror of this "
                    "machine rather than a pile of stale branches"}


@router.post("/apps/{app_id}/publish")
def app_publish(app_id: str, approve: bool = False) -> JSONResponse:
    """Put this app (and the store's index) on GitHub, from the app's card.

    `?approve=true` says the person has just approved this push; without it the job
    still builds the store and asks the human gate before sending anything.
    """
    out = publish_app(app_id, approve=approve)
    return JSONResponse(out, status_code=200 if out.get("ok") else 409)

@router.post("/apps/{app_id}/smoke")
def app_smoke(app_id: str, body: Optional[ActionBody] = None) -> JSONResponse:
    """Launch an app and prove it serves — the gate a built app has to pass.

    Separate from /health (structure) and the capability probe (what it can do):
    this answers "does it actually come up and answer on a port", which is the
    only thing that makes a generated launcher trustworthy.
    """
    body = body or ActionBody()
    return JSONResponse(smoke_app(app_id, href=body.href))


@router.get("/apps/{app_id}/menu")
def app_menu(app_id: str) -> JSONResponse:
    """The app's own list of runnable entries, including nested submenus."""
    return JSONResponse(_app_menu(app_id))


@router.get("/apps/{app_id}/health")
def app_health(app_id: str) -> JSONResponse:
    """What is broken about this app, and whether a fix exists.

    Deliberately shallow: the UI polls it, so it must not start interpreters.
    """
    return JSONResponse(_app_health(app_id))


class FixBody(BaseModel):
    """`provision` lets a repair escalate into the app's own install.

    Default true, because a button labelled Fix is expected to fix, and a failed
    launch is nearly always an app that was never installed here — which is
    exactly the case that used to send the user back to the store. The cost of
    asking is low: these install scripts re-sync dependencies rather than rebuild
    from nothing, so on an app that is already provisioned they resolve and
    return. Set it false to verify and repair without running the app's install.
    """
    provision: bool = True


CONVERSION_KEY = "athena"


def _conversion(folder: Path) -> Optional[Dict[str, Any]]:
    """What conversion recorded for this app, if it has been converted."""
    p = folder / "agent.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    v = data.get(CONVERSION_KEY)
    return v if isinstance(v, dict) else None


def _convert_app(app_id: str, jid: str, provision: bool = True) -> Dict[str, Any]:
    """Make an app Athena's own the moment it lands.

    An app arrives as a folder copied from another install, or cloned from a
    repository, and in that state it is not runnable here: its virtualenvs name
    the install they came from, so activation quietly puts the base interpreter
    first and the first launch dies on a missing module (`No module named
    'torch'` while the app's own venv imported torch 2.10.0 fine). Nothing about
    that is visible until someone asks the app to do something — which is the
    worst possible time to find out.

    So conversion is: check it, apply every repair with a deterministic answer,
    write the guide and manifest that let an agent run it, and record that this
    happened. Only an app whose environment is genuinely unprovisioned, or whose
    install has never run in this copy, gets its own install script run.
    """
    folder = _app_folder(app_id)
    if folder is None:
        return {"ok": False, "error": "app not found"}
    _job_add(jid, f"Converting {app_id} to an Athena app")
    # A launcher Athena generated before the rename is still on the legacy
    # filename. Move it now — the file is ours, so nothing upstream tracks it.
    # A launcher that belongs to the project is left exactly where it is.
    if athena_buildapp is not None:
        try:
            how = athena_buildapp.migrate_launcher(folder)
            if how == "migrated":
                _job_add(jid, f"  launcher moved to {athena_naming.LAUNCHER} "
                              f"(the legacy-named shim stays for the engine)")
            meta_how = athena_naming.ensure_meta(folder)
            if meta_how == "renamed":
                _job_add(jid, f"  metadata moved to {athena_naming.META}")
        except Exception as e:  # noqa: BLE001
            _job_add(jid, f"  !! the launcher could not be moved: {e}")
    # What was already known about the app, kept across conversion: writing the
    # guide rewrites the manifest from scratch, and an app that has already told
    # us what it can do must not be made to forget it.
    known: Dict[str, Any] = {}
    try:
        p = folder / "agent.json"
        if p.is_file():
            known = json.loads(p.read_text(encoding="utf-8", errors="replace")) or {}
    except (OSError, ValueError):
        known = {}
    res = _fix_app(app_id, jid, provision)

    # What the app is and how to run it, in the agent's own copy.
    guide = ""
    ac = _appcall_module()
    if ac is not None:
        try:
            man = ac.static_manifest(engine.home, folder)
            guide = str(man.get("guide_path") or "")
            _job_add(jid, f"  guide written: {guide}")
        except Exception as e:  # noqa: BLE001
            _job_add(jid, f"  !! guide could not be written: {e}")

    health = res.get("health") or {}
    checks = list(health.get("checks") or [])
    # Every failed check is reported, including the informational ones — but
    # they are not the same thing. A blocking failure means the app will not run;
    # an informational one ("its install has never run in this copy") is a
    # standing note, and an app carrying it may be perfectly runnable. Recording
    # only the blocking ones would lose the note that explains a later launch
    # failing on a missing module.
    failed = [str(c["name"]) for c in checks if not c.get("ok")]
    blocking = [str(c["name"]) for c in checks if not c.get("ok")
                and not c.get("informational")]
    stamp: Dict[str, Any] = {
        "converted_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "repairs": res.get("fixed") or [],
        "health_ok": bool(health.get("ok")),
        "install_started": bool(res.get("install_started")),
        "guide": guide,
    }
    if failed:
        stamp["outstanding"] = failed
    if blocking:
        stamp["blocking"] = blocking
    try:
        p = folder / "agent.json"
        data: Dict[str, Any] = {}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except ValueError:
                data = {}
        for k in ("capabilities", "probed"):
            if known.get(k) and not data.get(k):
                data[k] = known[k]
        data[CONVERSION_KEY] = stamp
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str),
                     encoding="utf-8")
    except OSError as e:
        _job_add(jid, f"  !! conversion could not be recorded: {e}")

    if blocking:
        _job_add(jid, "Converted, with something still outstanding: "
                      + ", ".join(blocking))
    elif stamp["install_started"]:
        _job_add(jid, "Converted — installing now; launch it when the install ends.")
    else:
        _job_add(jid, "Converted — this app is ready to run.")
    if "Install history" in failed:
        # Reported, not run: an app whose install ran in the install it came
        # from looks exactly like one that was never provisioned, and rebuilding
        # over a working environment is how repair breaks things.
        _job_add(jid, "  note: its install has never run in this copy — press "
                      "Install on the app if a launch fails on a missing module")
    return {"ok": True, "app": app_id, "converted": True, "guide": guide, **stamp}


def _start_sweep() -> Optional[str]:
    """Convert whatever is already installed and has not been converted.

    Called once, on the first backend request of a session: an app installed
    before conversion existed, or dropped into `api/` by hand, is otherwise left
    in exactly the state conversion prevents. Silent when there is nothing to do.
    """
    try:
        if not _pending_conversions():
            return None
    except Exception:  # noqa: BLE001
        return None
    return _job_spawn("convert", "Converting installed apps",
                      lambda jid: _convert_pending(jid=jid))


def _pending_conversions() -> List[str]:
    """Installed apps that have not been through conversion yet."""
    home = Path(engine.home) / "api"
    if not home.is_dir():
        return []
    out: List[str] = []
    for d in sorted(home.iterdir()):
        if not d.is_dir() or not athena_naming.has_launcher(d):
            continue
        if _conversion(d) is None:
            out.append(d.name)
    return out


def _convert_pending(jid: str = "", max_apps: int = 12) -> Dict[str, Any]:
    """Convert everything already on disk that has not been converted.

    Apps installed before conversion existed, or dropped into `api/` by hand,
    would otherwise sit there in exactly the state this prevents. One job, one
    app at a time: provisioning an app can mean a multi-gigabyte download, and
    six of those at once is not a favour.
    """
    pending = _pending_conversions()[:max_apps]
    out: List[Dict[str, Any]] = []
    for app_id in pending:
        try:
            out.append(_convert_app(app_id, jid, provision=False))
        except Exception as e:  # noqa: BLE001
            out.append({"app": app_id, "ok": False, "error": str(e)})
    return {"checked": len(pending), "apps": out}


def _fix_app(app_id: str, jid: str, provision: bool = True) -> Dict[str, Any]:
    """Verify an app and repair everything that has a deterministic repair.

    Shared by the Fix button and by conversion on install/import, so an app that
    arrives is checked by the same code that checks one the user is worried about.
    """
    _job_add(jid, f"Verifying {app_id}")
    before = _app_health(app_id, deep=True)
    for c in before["checks"]:
        _job_add(jid, f"  {'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")

    folder = _app_folder(app_id)
    fixed: List[str] = []
    assert folder is not None

    # 1. Environments that still belong to the install they were copied from.
    try:
        touched = _retarget_venv(folder, engine.home)
        if touched:
            fixed.append(f"repointed {len(touched)} virtualenv file(s)")
            _job_add(jid, f"FIXED {len(touched)} virtualenv file(s): {', '.join(touched[:4])}")
    except Exception as e:  # noqa: BLE001
        _job_add(jid, f"!! virtualenv repair failed: {e}")

    # 2. Submodules that were cloned but never checked out.
    code = folder / "app"
    if (code / ".gitmodules").is_file():
        st = _git_out(code, "submodule", "status") or ""
        if any(l.startswith("-") for l in st.splitlines()):
            _job_add(jid, "FIXING uninitialized submodules")
            if _git_out(code, "submodule", "update", "--init", "--recursive") is not None:
                fixed.append("initialized submodules")
                _job_add(jid, "FIXED submodules initialized")
            else:
                _job_add(jid, "!! submodule init failed")

    # 3. Cache settings from before the shared store existed.
    if _load_settings().get("share_caches", True):
        try:
            envf = _write_shared_env(folder, engine.home)
            if envf.get("changed"):
                fixed.append(f"retargeted {envf['changed']} cache setting(s)")
                _job_add(jid, f"FIXED retargeted {envf['changed']} cache setting(s) at the shared store")
        except Exception as e:  # noqa: BLE001
            _job_add(jid, f"!! cache env repair failed: {e}")

    after = _app_health(app_id, deep=True)
    for c in after["checks"]:
        _job_add(jid, f"  {'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")

    # Fix is expected to *fix*. Everything with a deterministic repair is
    # repaired above; what is left is an app nothing has been installed into,
    # or one whose install has never run in this copy — and in both cases the
    # app's own install script is the repair. Reporting a defect and declining
    # to act on it is the behaviour this button exists to replace.
    install_started = False
    never = next((c for c in after["checks"] if c["name"] == "Install history"), None)
    want_install = provision and (
        bool(after.get("needs_install")) or (never is not None and not never["ok"]))
    if not provision and (after.get("needs_install") or (never and not never["ok"])):
        _job_add(jid, "  provisioning was not requested — reporting without running install")
    if want_install:
        why = ("nothing is installed in its environment"
               if after.get("needs_install")
               else "its install has never run in this copy")
        _job_add(jid, f"Provisioning — {why}; running the app's own install")
        try:
            r = _runtime().start(app_id, "install.js")
            install_started = bool(r.get("ok"))
            _job_add(jid, f"  install {r.get('state') or r.get('error') or 'requested'}")
        except Exception as e:  # noqa: BLE001
            _job_add(jid, f"!! install could not start: {e}")

    if after.get("ok"):
        _job_add(jid, "Everything checks out.")
    out: Dict[str, Any] = {"app": app_id, "fixed": fixed, "health": after,
                           "install_started": install_started}
    return out


@router.post("/apps/{app_id}/fix")
def app_fix(app_id: str, body: Optional[FixBody] = None) -> JSONResponse:
    """Verify this app and repair it where it stands.

    Repairs everything with a deterministic repair — environments bound to
    another install, uninitialized submodules, cache settings written before the
    shared store existed — re-checks with the environments actually started, and
    then finishes the job: an app with nothing installed in its environment, or
    whose install has never run in this copy, gets its own install script run.
    Diagnosing a defect and declining to act on it is the behaviour this replaces.
    """
    if not _app_folder(app_id):
        return JSONResponse({"ok": False, "error": "app not found"}, status_code=404)
    provision = body.provision if body else True

    def work(jid: str) -> Dict[str, Any]:
        return _fix_app(app_id, jid, provision)
    return JSONResponse({"ok": True, "job": _job_spawn("fix", f"Fixing {app_id}", work)})


class ModelsBody(BaseModel):
    """The step the verdict asked for, applied to this app.

    `folder` is only meaningful for `point`, which is the one action that
    exists — an action this does not know is an error rather than a silent
    success, because a caller that guessed the wrong verb should be told.
    """

    action: str = "point"
    folder: Optional[str] = None
    force: bool = False


class PullBody(BaseModel):
    """A repository to pull, and where it should land.

    `repo` is the hub's own id (`owner/name`) — the one name an app and a person
    both use, so nothing has to be memorised about where a file came from. The
    rest of the body decides only *where it lands*: an app's library, a folder
    named directly, or nowhere in particular, which makes it a cache entry.
    """

    repo: str
    app: Optional[str] = None
    folder: Optional[str] = None
    key: str = "checkpoints"
    include: Optional[str] = None
    revision: Optional[str] = None


# Registered above `/apps/{app_id}/{action}`: FastAPI answers with the first
# route that matches, and the catch-all matches this path too. `t_ui_routes.py`
# guards the order, the same way it does for the heartbeat route below.
@router.get("/apps/{app_id}/models")
def app_models(app_id: str) -> JSONResponse:
    """What this app can generate with, and where weights could come from.

    The verdict is the manifest's own question asked freshly, and the candidates
    are the folders on this machine that already hold generating weights — so
    acting on "cannot generate" does not require anyone to remember a path.
    """
    folder = _app_folder_for(app_id)
    if folder is None:
        return JSONResponse({"ok": False, "error": "app not found"}, status_code=404)
    ac = _appcall_module()
    if ac is None:
        return JSONResponse({"ok": False, "error": "the capability module is unavailable"},
                            status_code=503)
    return JSONResponse({"ok": True, "app": app_id, "models": ac._comfy_models(folder),
                         "candidates": _model_library_candidates(_athena_home())})


@router.post("/apps/{app_id}/models")
def app_models_apply(app_id: str, body: Optional[ModelsBody] = None) -> JSONResponse:
    """Point ComfyUI at a weights folder, and answer with the re-checked verdict.

    This is the action behind "cannot generate": a small write outside the
    checkout, whose result is a verdict rather than a promise — the caller does
    not have to ask again to find out whether the app can generate now.
    """
    body = body or ModelsBody()
    folder = _app_folder_for(app_id)
    if folder is None:
        return JSONResponse({"ok": False, "error": "app not found"}, status_code=404)
    ac = _appcall_module()
    if ac is None:
        return JSONResponse({"ok": False, "error": "the capability module is unavailable"},
                            status_code=503)
    if body.action != "point":
        return JSONResponse({"ok": False, "error": f"unknown action {body.action}"},
                            status_code=400)
    if not body.folder:
        return JSONResponse({"ok": False, "error": "point needs a folder",
                             "candidates": _model_library_candidates(_athena_home())},
                            status_code=400)
    out = ac.comfy_point_models(folder, body.folder, force=bool(body.force))
    return JSONResponse(out, status_code=200 if out.get("ok") else 409)


# ------------------------------------------------------------------- triage
#
# The reader itself lives next door in `triage.py`, because it is one pure
# function over text and that is the easiest kind of thing to test against the
# real strings this machine produced. Loading it by path keeps the same shape as
# the capability module: a plugin that imports a sibling at module scope breaks
# the whole dashboard when that sibling is missing.
_TRIAGE: Any = None


# ------------------------------------------------- what an app will ask for
#
# Read out of the app's own scripts by `keyscan.py`: the environment names it
# reads and the literal cloud hosts it calls. Names and lines only — no value is
# ever read, held, cached or returned, so this feature cannot leak a secret that
# it never looks at.
#
# Two deliberate limits. The scan is a directory walk, so it is *cached* beside the
# plugin's home and the app list only ever reads the cache — a badge is not worth
# a walk per poll. And "runs on this box" is a claim about a complete read: a
# capped scan says it was capped and does not make it.

_KEYSCAN = None


def _keyscan_module():
    global _KEYSCAN
    if _KEYSCAN is None:
        path = Path(__file__).with_name("keyscan.py")
        if not path.exists():
            return None
        try:
            spec = importlib.util.spec_from_file_location("athena_keyscan", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _KEYSCAN = mod
        except Exception:  # noqa: BLE001
            return None
    return _KEYSCAN


# The environment variables a provider key is conventionally kept in. Named here
# rather than discovered, because "Hermes holds an OpenAI key" has to be a fact
# about a slow-changing list, not a guess at a name pattern.
_PROVIDER_ENV = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "GEMINI_API_KEY": "google",
    "GOOGLE_API_KEY": "google",
    "REPLICATE_API_TOKEN": "replicate",
    "GROQ_API_KEY": "groq",
    "MISTRAL_API_KEY": "mistral",
    "COHERE_API_KEY": "cohere",
    "TOGETHER_API_KEY": "together",
    "OPENROUTER_API_KEY": "openrouter",
    "DEEPSEEK_API_KEY": "deepseek",
    "PERPLEXITY_API_KEY": "perplexity",
    "ELEVENLABS_API_KEY": "elevenlabs",
    "STABILITY_API_KEY": "stability",
    "XAI_API_KEY": "xai",
    "RUNPOD_API_KEY": "runpod",
    "FAL_KEY": "fal",
    "GITHUB_TOKEN": "github",
    "GH_TOKEN": "github",
}


def _held_providers() -> List[str]:
    """Providers Hermes already has a credential for — from files and env only.

    No subprocesses on purpose: this runs beside a scan the UI asked for, and
    spending twenty seconds on `gh auth token` to colour a badge is how a route
    becomes one nobody calls. Everything here is a file read or an environment
    lookup, and the answer it gives is deliberately the pessimistic one — a
    provider we cannot confirm is reported as *not* held, so a card only ever
    claims a credential we can point at.

    Hermes' own credentials come from `collect_credentials`, not from opening
    `auth.json`; `cache/HF_AUTH/token` is gone from here too — nothing writes it.
    """
    held: List[str] = []
    try:
        held.extend(str(prov).lower() for prov in collect_credentials())
    except Exception:  # noqa: BLE001
        pass
    try:
        if (Path.home() / ".cache" / "huggingface" / "token").is_file() \
                or (Path.home() / ".huggingface" / "token").is_file():
            held.append("huggingface")
    except Exception:  # noqa: BLE001
        pass
    try:
        if (Path.home() / ".config" / "gh" / "hosts.yml").is_file():
            held.append("github")
    except Exception:  # noqa: BLE001
        pass
    for var, prov in _PROVIDER_ENV.items():
        if os.environ.get(var):
            held.append(prov)
    return sorted(set(held))


def _keys_cached(folder_name: str) -> Dict[str, Any]:
    """The cached answer for one app, in the shape a card uses. Never a walk."""
    ks = _keyscan_module()
    if ks is None:
        return {"scanned": False, "local": None, "keys": [], "keys_total": 0,
                "held_count": 0, "cloud_total": 0, "complete": False}
    return ks.summarise(ks.load(_athena_home(), folder_name))


def keys_of(app_id: str, refresh: bool = False) -> Dict[str, Any]:
    """Read an app's scripts now, or hand back what the last read said."""
    ks = _keyscan_module()
    folder = _app_folder_for(app_id)
    if ks is None or folder is None:
        return {"ok": False, "error": "app not found" if folder is None
                else "the scanner is missing from this install"}
    if not refresh:
        cached = ks.load(_athena_home(), folder.name)
        if cached is not None:
            return cached
    return ks.scan_and_save(_athena_home(), folder, held=_held_providers(),
                            folder_name=folder.name)


@router.get("/apps/{app_id}/keys")
def app_keys(app_id: str, refresh: int = 0) -> JSONResponse:
    """What this app's scripts ask for, and whether Hermes already answers it.

    `refresh=1` reads the folder again; anything else is the cached answer, which
    is what makes this safe to call from a card on every render.
    """
    return JSONResponse(keys_of(app_id, refresh=bool(refresh)))


def _access_line(access: Dict[str, Any]) -> str:
    """One sentence for the health panel: what this app wants from you.

    Kept in the backend rather than the UI because the agent tool and the card
    then say the same words — two spellings of one fact is how a panel starts
    disagreeing with the thing it describes.
    """
    if not access.get("scanned"):
        return "its scripts have not been read yet — press Read to see what it asks for"
    keys = access.get("keys") or []
    held = [k["name"] for k in keys if k.get("held")]
    missing = [k["name"] for k in keys if not k.get("held")]
    cloud = int(access.get("cloud_total") or 0)
    if not keys and not cloud:
        return ("no key or cloud endpoint in its scripts — it runs on this box"
                if access.get("complete")
                else "nothing found, but some scripts were too big to read — not a clean answer")
    parts = []
    if missing:
        parts.append(f"wants {', '.join(missing)}")
    if held:
        parts.append(f"Hermes already answers {', '.join(held)}")
    if not keys and cloud:
        parts.append(f"posts to a cloud endpoint ({cloud} found) without a key in its scripts")
    if cloud and keys:
        parts.append(f"and calls {cloud} cloud endpoint(s)")
    return "; ".join(parts) or "its scripts name no key"


def _triage_module():
    global _TRIAGE
    if _TRIAGE is None:
        path = Path(__file__).with_name("triage.py")
        if not path.exists():
            return None
        try:
            spec = importlib.util.spec_from_file_location("athena_triage", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _TRIAGE = mod
        except Exception:  # noqa: BLE001
            return None
    return _TRIAGE


def app_log_text(app_id: str, limit: int = 300_000) -> str:
    """An app's own output, read from the engine's log files on disk.

    Deliberately not through the engine's API: a stopped engine is exactly when
    someone wants to know why something died, and the log is a file either way.
    """
    if athena_runtime is None:
        return ""
    try:
        text = athena_runtime.Runtime(engine.port, engine.home).logs(app_id, limit=limit)
    except Exception:  # noqa: BLE001
        return ""
    return text if isinstance(text, str) else ""


def last_failure(app_id: str, limit: int = 120_000) -> Optional[Dict[str, Any]]:
    """What the app's log says about why it stopped, if it says anything.

    The machine facts go in with the text, because they change the answer: an
    out-of-memory sentence with a neighbour named is a card being held, and the
    same sentence alone is a request that was too big for the card.
    """
    tri = _triage_module()
    if tri is None:
        return None
    text = app_log_text(app_id, limit=limit)
    if not text or text.startswith("(no "):
        return None
    running: List[str] = []
    try:
        # The engine keys a running entry by the app *folder*, which is not what a
        # person calls the app; the folder's name is.
        for folder in (_runtime().running() or {}):
            running.append(Path(str(folder)).name)
    except Exception:  # noqa: BLE001
        running = []
    try:
        report = tri.triage(text, {"app": app_id, "running": running})
    except Exception:  # noqa: BLE001
        return None
    return report if report.get("found") else None


# Registered above `/apps/{app_id}/{action}` like the routes around it: FastAPI
# answers with the first route that matches, and the catch-all matches this path
# too. `t_ui_routes.py` guards the order.
@router.get("/apps/{app_id}/triage")
def app_triage(app_id: str, lines: int = 0) -> JSONResponse:
    """Why this app last stopped, named — with the action that answers it.

    `lines=1` also returns the log tail that goes with the reading, for a caller
    that wants to show the evidence rather than only the verdict.
    """
    if _triage_module() is None:
        return JSONResponse({"ok": False, "error": "the triage reader is unavailable"},
                            status_code=503)
    if _app_folder(app_id) is None:
        return JSONResponse({"ok": False, "error": "app not found"}, status_code=404)
    report = last_failure(app_id, limit=300_000)
    if report is None:
        text = app_log_text(app_id, limit=300_000)
        tri = _triage_module()
        report = (tri.triage(text, {"app": app_id}) if text and not text.startswith("(no ")
                  else {"found": False, "cause": None, "title": None, "fix": None,
                        "why": "this app has produced no output yet, so there is nothing "
                               "to read.", "evidence": None, "also": [], "tail": []})
    out: Dict[str, Any] = {"ok": True, "app": app_id, **report}
    if not lines:
        out.pop("tail", None)
    return JSONResponse(out)



@router.get("/models")
def models_shelf(sizes: bool = True) -> JSONResponse:
    """The weights on this machine, from the shelf's end rather than one app's.

    The per-app card answers "can this app generate". The question a person
    actually asks next — what is this disk full of, what does this box still
    lack, what can I delete — is about all of it at once, and it is the same
    data walked from the other direction: each app's verdict, each library an
    app reads, and everything present that nothing reads. `sizes=0` answers
    without weighing folders, for callers that only want the layout.
    """
    if _appcall_module() is None:
        return JSONResponse({"ok": False, "error": "the capability module is unavailable"},
                            status_code=503)
    return JSONResponse({"ok": True, **_weights_inventory(_athena_home(), sizes=bool(sizes))})


@router.get("/models/remote")
def models_remote(repo: str) -> JSONResponse:
    """What a repository on HuggingFace holds, before a byte of it is fetched.

    Asked before pulling rather than after: the answer is a size, a file list and
    whether a copy is already on this disk, and all three are reasons not to
    start a download at all. A repository with no weights in it is named as such
    — plenty of large repositories are configs and tokenizers.
    """
    return JSONResponse(hf_remote(repo))


@router.post("/models/download")
def models_download(body: PullBody) -> JSONResponse:
    """Start a pull as a job, and answer with the job rather than with the model.

    A large pull outlives the request by minutes, so the reply is the id to
    watch — plus what it will cost, which is known before it starts. Downloads
    run one at a time (a disk and a link are both shared), so a second request
    queues behind the first instead of competing with it.
    """
    home = _athena_home()
    if _hf_cli(home) is None:
        return JSONResponse({"ok": False, "error": "no `hf` command found in Athena's "
                                                   "Miniforge, so nothing can pull"},
                            status_code=503)
    info = hf_remote(body.repo, home)
    if info.get("ok") is False:
        return JSONResponse(info, status_code=404)
    dest = _pull_dest(home, body)
    if dest.get("ok") is False:
        return JSONResponse(dest, status_code=400)
    take = _pull_matches(info["files"], body.include)
    if body.include and not take:
        return JSONResponse({"ok": False, "error": f"nothing in {info['repo']} matches "
                                                   f"`{body.include}`"}, status_code=400)
    jid = _job_new("download", f"Downloading {info['repo']}")
    _pull_enqueue({"jid": jid, "repo": info["repo"], "include": body.include,
                   "revision": body.revision, "dest": dest.get("dest"),
                   "app": body.app or "",
                   "token": _hf_token_value(body.app or ""),
                   "total": sum(f["bytes"] for f in take)})
    return JSONResponse({
        "ok": True, "job": jid, "repo": info["repo"], "dest": dest.get("dest").as_posix()
        if dest.get("dest") is not None else None,
        "cache_only": bool(dest.get("cache_only")), "landing_because": dest.get("because"),
        "files": len(take), "bytes": sum(f["bytes"] for f in take),
        "weights": len([f for f in take if f in info["weights"]]),
        "already_cached": info.get("already_cached"),
        "watch": f"/jobs/{jid}",
    })


# A heartbeat route, deliberately registered above the catch-all below: FastAPI
# answers with the first route that matches, and `/apps/{app_id}/{action}` also
# matches this path. While this route sat at the bottom of the file, every
# heartbeat was dispatched as the action "activity" and the engine went looking
# for an activity.js that does not exist. `t_ui_routes.py` now guards the order.
@router.post("/apps/{app_id}/activity")
def app_activity(app_id: str) -> JSONResponse:
    """A heartbeat: something is still using this app, so leave it running.

    Athena's own UI sends this while an app's viewer is open. It cannot see
    clicks inside the app itself — the viewer loads the app's own URL, not
    something Athena proxies — so this is how "the person still has it open"
    reaches the reaper.
    """
    _idle_note(app_id, "viewer open")
    return JSONResponse({"ok": True, "app_id": app_id})


# ------------------------------------------------------------- waiting for card
#
# The VRAM gate warns and lets a launch through, because no app here declares what
# it needs and a refusal would be a guess. That leaves exactly one case with no
# answer: the card is full *now* and the work is wanted *later* — a render that can
# happily start in twenty minutes. So a launch can be queued instead of retried by
# hand.
#
# In memory on purpose. A queue that survived a restart would start an app nobody
# is watching, on a machine whose whole premise is that the user decides — and a
# stale intent (this afternoon's render, remembered) is worse than asking again.
# The job it spawns is the ordinary kind, so the panel already shows it in the
# Terminal, with what it is waiting for and how long it has been waiting.
_WAIT_QUEUE: List[Dict[str, Any]] = []
_WAIT_LOCK = threading.Lock()
_WAIT_POLL = 20                     # seconds between looks at the card
_WAIT_MAX = 2 * 3600                # a two-hour-old intent is no longer an intent
_WAIT_GIVE_UP_TEXT = ("given up after {mins:.0f} minutes — the card never freed "
                      "enough for this; nothing was started")


class WaitIn(BaseModel):
    action: str = "launch"
    max_seconds: Optional[int] = None


def _wait_gate() -> Optional[Dict[str, Any]]:
    """The card's numbers, and whether they are low enough to be worth waiting for."""
    if athena_headroom is None:
        return None
    try:
        return athena_headroom.gpu_gate(_load_settings(), running=_running_ports().values())
    except Exception:  # noqa: BLE001
        return None


def waiting_view() -> Dict[str, Any]:
    with _WAIT_LOCK:
        queue = [dict(w) for w in _WAIT_QUEUE]
    g = _wait_gate()
    return {"ok": True, "queue": queue, "count": len(queue),
            "poll_seconds": _WAIT_POLL, "max_seconds": _WAIT_MAX,
            "card": g or None,
            # Said here rather than only in a docstring, because a person reading
            # the panel is the one who needs to know the queue is not durable.
            "note": "queued launches live in this dashboard's memory and are dropped "
                    "when it stops"}


def _wait_launch(entry: Dict[str, Any], jid: str) -> None:
    """Run the queued action now, and report what the app itself said about it."""
    app_id = entry["app"]
    action = (entry.get("action") or "launch").strip() or "launch"
    script = APP_SCRIPTS.get(action, APP_SCRIPTS["launch"])
    # Off the queue before it starts, not after: an entry that is still listed while
    # the launch is in flight reads as "waiting" to anything that looks — and the
    # pump would come round and start it a second time.
    with _WAIT_LOCK:
        _WAIT_QUEUE[:] = [w for w in _WAIT_QUEUE if w["jid"] != jid]
    _job_add(jid, f"card free — starting {app_id} ({action})")
    try:
        out = _runtime().start(app_id, script)
    except Exception as e:  # noqa: BLE001
        _job_end(jid, "failed", error=str(e))
        return
    if out.get("ok"):
        _idle_note(app_id, f"the queued {action}", watch=False)
        _job_add(jid, f"{app_id} started")
        _job_end(jid, "done", result={"app": app_id, "action": action,
                                      "url": _app_url(app_id) or None})
    else:
        _job_add(jid, f"the launch was refused: {out.get('error') or 'no reason given'}")
        _job_end(jid, "failed", error=out.get("error") or "launch failed", result=out)


def _wait_pump(entry: Dict[str, Any]) -> None:
    """The thread behind one queued launch: watch the card, start it when it frees.

    The card is asked the same question the gate asks (one sampler, one answer), and
    the wait ends the moment the person removes the entry — the queue is a list of
    intentions, and an intention can be withdrawn.
    """
    app_id = entry["app"]
    jid = entry["jid"]
    started = time.time()
    deadline = started + float(entry.get("max_seconds") or _WAIT_MAX)
    while True:
        with _WAIT_LOCK:
            if not any(w["jid"] == jid for w in _WAIT_QUEUE):
                _job_add(jid, "taken off the queue — nothing was started")
                _job_end(jid, "done", result={"app": app_id, "cancelled": True})
                return
        g = _wait_gate()
        now = time.time()
        if g is None:
            # Nothing to wait for: no card, no numbers, or the user turned the
            # warning off. Waiting would be theatre, so the work runs.
            _wait_launch(entry, jid)
            return
        free = float(g.get("free_gb") or 0)
        want = float(g.get("need_gb") or g.get("warn_gb") or 0)
        if free >= want or not want:
            _wait_launch(entry, jid)
            return
        if now >= deadline:
            with _WAIT_LOCK:
                _WAIT_QUEUE[:] = [w for w in _WAIT_QUEUE if w["jid"] != jid]
            _job_add(jid, _WAIT_GIVE_UP_TEXT.format(mins=(now - started) / 60.0))
            _job_end(jid, "failed", error="the card never freed enough")
            return
        _job_progress(jid, free, want, label=f"{free:g} GB free · {want:g} GB wanted")
        # The gate's own sentence, verbatim: it is the one that names which apps are
        # holding the card, and two spellings of the same numbers is how a panel
        # starts disagreeing with itself.
        _job_add(jid, f"{now - started:5.0f}s  {g.get('message') or f'{free:g} GB free'}")
        time.sleep(_WAIT_POLL)


# ------------------------------------------------------------------ what needs me
#
# One answer to the question a person actually has when they open this at night:
# what is asking something of me? Each of the four facts below is already known by
# a different part of this plugin — a health check, the update cache, the weight
# shelf, the wait queue — and the point of folding them together is that nobody
# should have to open four tabs to find out there is one thing to do.
#
# Everything here is a read: directory walks, cache reads, log tails. The update
# checks are the *cached* answers, never a fresh `git fetch`, because a report that
# reaches the network on every call is a report that cannot be asked cheaply — and
# the count of apps never checked is reported instead of guessed at.
_REPORT_PRIORITY = {"broken": 0, "stopped": 1, "update": 2, "weights": 3, "disk": 4,
                    "waiting": 5}


def _health_sweep(ids: List[str], workers: int = 6) -> Dict[str, Any]:
    """`{app: health}` for every app, checked at the same time.

    Returns the error text, not a raise, when one app's checks blow up: the
    sweep is a report about apps, and one broken folder must not stop it from
    reporting on the others.
    """
    out: Dict[str, Any] = {}

    def one(app_id: str) -> Any:
        try:
            return _app_health(app_id)
        except Exception as e:  # noqa: BLE001
            return str(e)

    if not ids:
        return out
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(workers, len(ids)))) as pool:
        for app_id, res in zip(ids, pool.map(one, ids)):
            out[app_id] = res
    return out


def report_view(app_limit: int = 200) -> Dict[str, Any]:
    """What is broken, behind, wasteful and queued, in the order it matters."""
    home = _athena_home()
    ids = _installed_app_ids()[: max(1, int(app_limit or 200))]

    broken: List[Dict[str, Any]] = []
    stopped: List[Dict[str, Any]] = []
    healthy = 0
    # The checks are independent reads of different folders, and run one after
    # another they cost their sum: seven apps at ~350ms each is 2.5s every time
    # the report tab is drawn. Run them together and classify below, in the same
    # order the list always came in. Failures travel as values so a check that
    # blows up stays a finding about one app instead of a 500 for the sweep.
    health: Dict[str, Any] = _health_sweep(list(ids))
    for app_id in ids:
        h = health.get(app_id)
        if not isinstance(h, dict):
            # A string is the error that check raised; None cannot happen, but a
            # report route must not be the thing that dies on the way to a report.
            broken.append({"app": app_id, "title": "its checks could not run",
                           "detail": str(h or "no result")[:200]})
            continue
        failing = [c for c in (h.get("checks") or [])
                   if not c.get("ok") and not c.get("informational")]
        if failing:
            first = failing[0]
            broken.append({
                "app": app_id,
                "title": first.get("name") or "a check failed",
                "detail": first.get("detail") or "",
                "checks": len(failing),
                "needs_install": bool(h.get("needs_install")),
                "fixable": bool(h.get("fixable")),
                "next_step": first.get("next_step"),
            })
        else:
            healthy += 1
        # A log that names why the app last stopped is history, so it is never
        # "broken" — but an app that is not running and whose log was written in the
        # last half-day is the thing someone most wants to be told about.
        f = h.get("failure") or None
        if f and not _app_is_running(app_id) and _log_is_recent(home, app_id, hours=12):
            stopped.append({"app": app_id, "title": f.get("title"),
                            "detail": f.get("why"), "evidence": f.get("evidence"),
                            "fix": f.get("fix")})

    # The updates already known. An app whose check has never run is not assumed to
    # be up to date — it is counted as unchecked, which is a different statement.
    behind: List[Dict[str, Any]] = []
    unchecked: List[str] = []
    for app_id in ids:
        hit = _update_cache.get(app_id)
        if not hit:
            unchecked.append(app_id)
            continue
        res = hit[1] or {}
        if res.get("ok") and res.get("behind"):
            behind.append({"app": app_id, "behind": int(res["behind"]),
                           "next_step": {"action": "update", "label": "Update it",
                                         "detail": "the repository has commits the "
                                                   "installed copy does not"}})

    inv = _weights_inventory(home)
    disk = []
    for r in (inv.get("unreferenced") or []):
        disk.append({"kind": "library", "folder": r.get("folder"),
                     "bytes": int(r.get("bytes") or 0),
                     "size": _dir_size_str(int(r.get("bytes") or 0)),
                     "title": "weights nothing here reads",
                     "detail": "no app on this machine is pointed at this folder, so "
                               "these bytes are only cost",
                     "next_step": {"action": "shelf", "label": "Show what is filling "
                                                               "the disk",
                                   "detail": "the shelf weighs every library and names "
                                             "the ones nothing reads"}})
    # Apps that cannot generate, as one item rather than one per app: they share a
    # single answer (the machine's weights are somewhere these apps were never
    # pointed at), and seven copies of that answer is not more information.
    weights = []
    need_weights = [a for a in (inv.get("need_weights") or [])]
    biggest = [r for r in (inv.get("libraries") or []) if int(r.get("bytes") or 0)]
    if need_weights and biggest:
        top = biggest[0]
        weights.append({
            "kind": "weights", "apps": need_weights,
            "title": f"{len(need_weights)} app{'' if len(need_weights) == 1 else 's'} "
                     "cannot generate right now",
            "detail": f"no weight library is mapped for "
                      f"{'it' if len(need_weights) == 1 else 'them'}, and this disk "
                      f"already holds {len(biggest)} librar"
                      f"{'y' if len(biggest) == 1 else 'ies'} — the largest is "
                      f"{top.get('folder')} ({_dir_size_str(int(top.get('bytes') or 0))})",
            "next_step": {"action": "point_models", "folder": top.get("folder"),
                          "label": "Point them at the weights on this disk",
                          "detail": "the library is already here, so this is a mapping "
                                    "rather than a download"},
        })

    with _WAIT_LOCK:
        queued = [dict(w) for w in _WAIT_QUEUE]

    needs = (broken + stopped + behind + weights + disk)
    for n in needs:
        n["kind"] = ("broken" if n in broken else "stopped" if n in stopped
                     else "update" if n in behind else "weights" if n in weights
                     else "disk")
    needs.sort(key=lambda n: _REPORT_PRIORITY.get(n["kind"], 9))

    bits = []
    if broken:
        bits.append(f"{len(broken)} app{'' if len(broken) == 1 else 's'} that cannot run")
    if stopped:
        bits.append(f"{len(stopped)} app{'' if len(stopped) == 1 else 's'} that stopped "
                    "and has not been restarted")
    if behind:
        bits.append(f"{len(behind)} update{'' if len(behind) == 1 else 's'} waiting")
    if weights:
        bits.append(f"{len(need_weights)} app{'' if len(need_weights) == 1 else 's'} "
                    "that cannot generate yet")
    if disk:
        bits.append(f"{_dir_size_str(sum(int(d['bytes']) for d in disk))} nothing reads")
    if queued:
        bits.append(f"{len(queued)} launch{'es' if len(queued) != 1 else ''} queued for "
                    "the card")
    headline = ("Nothing needs you — every app here runs, and nothing is queued."
                if not bits else " · ".join(bits))
    return {
        "ok": True,
        "when": time.time(),
        "headline": headline,
        "needs": needs,
        "waiting": queued,
        "counts": {"apps": len(ids), "healthy": healthy, "broken": len(broken),
                   "stopped": len(stopped), "updates": len(behind),
                   "unchecked_updates": len(unchecked),
                   "unused_libraries": len(disk),
                   "unused_bytes": sum(int(d["bytes"]) for d in disk),
                   "waiting": len(queued)},
        "weights": {"summary": inv.get("summary"), "totals": inv.get("totals"),
                    "unreferenced": inv.get("unreferenced"),
                    "cached": inv.get("cached")},
        # Said out loud, because a report that quietly leaves things out is worse
        # than one that says what it looked at.
        "notes": [
            f"health and weights were read from disk for {len(ids)} app folders",
            (f"{len(unchecked)} app{'' if len(unchecked) == 1 else 's'} ha"
             f"{'s' if len(unchecked) == 1 else 've'} never had an update check, so "
             "they are not counted as up to date — ask for updates to find out")
            if unchecked else "every app has had an update check",
            "the update answers here are the cached ones: this report never reaches "
            "the network",
        ],
    }


def _app_is_running(app_id: str) -> bool:
    """Whether the engine says any of this app is live. Never raises, never blocks long."""
    try:
        return bool(_runtime().running(app_id))
    except Exception:  # noqa: BLE001
        return False


def _log_dirs(home: Path, app_id: str) -> List[Path]:
    """Every place the engine keeps this app's output.

    The same three the runtime reads, spelled out again on purpose: that reader
    *concatenates* the text and this one only needs timestamps, so asking it would
    mean reading megabytes to learn a date. Kept in step by the test that writes a
    log in the first location and asserts the report notices it.
    """
    return [home / "logs" / "shell" / "api" / app_id,
            home / "logs" / "shell" / "cleaned" / "api" / app_id,
            home / "api" / app_id / "logs"]


def _log_is_recent(home: Path, app_id: str, hours: float = 12.0) -> bool:
    """Whether this app's most recent log was written recently.

    The log's own mtime, not the app's: an app installed a year ago whose log was
    written twenty minutes ago is exactly what "it stopped recently" means.
    """
    newest = 0.0
    for base in _log_dirs(home, app_id):
        if not base.is_dir():
            continue
        try:
            for p in base.rglob("*.txt"):
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            continue
    return bool(newest) and (time.time() - newest) < hours * 3600.0


@router.get("/report")
def report(limit: int = 200) -> JSONResponse:
    """Everything that is asking something of you, in one answer."""
    try:
        return JSONResponse(report_view(limit))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)


@router.post("/apps/{app_id}/wait")
def app_wait(app_id: str, body: Optional[WaitIn] = None) -> JSONResponse:
    """Queue this app's launch for when the card frees, instead of retrying by hand.

    A queue of one per app: asking twice is the same intention twice, and the answer
    is where it already is in line.
    """
    if _app_folder(app_id) is None:
        return JSONResponse({"ok": False, "error": "app not found"}, status_code=404)
    action = ((body.action if body else "launch") or "launch").strip() or "launch"
    if action not in APP_SCRIPTS:
        return JSONResponse({"ok": False, "error": f"unknown action {action}"},
                            status_code=400)
    with _WAIT_LOCK:
        # The index is taken here rather than through `_wait_position`, which locks
        # the same lock: that is a deadlock, not a helper call, and it hung this
        # route until the test that calls it twice in a row caught it.
        for i, w in enumerate(_WAIT_QUEUE):
            if w["app"] == app_id:
                return JSONResponse({"ok": True, "app": app_id, "job": w["jid"],
                                     "position": i + 1, "already": True})
        # One job per queued launch, named for the thing being waited for rather
        # than for the waiting: the Terminal reads "comfy.git", which is what the
        # person is watching.
        jid = _job_new("wait", f"{app_id} — waiting for the card")
        _job_add(jid, f"queued: start {app_id} ({action}) when the card has room")
        entry: Dict[str, Any] = {"app": app_id, "action": action, "jid": jid,
                                 "queued_at": time.time(),
                                 "max_seconds": (body.max_seconds if body else None)}
        _WAIT_QUEUE.append(entry)
        position = len(_WAIT_QUEUE)
    threading.Thread(target=_wait_pump, args=(entry,), daemon=True).start()
    return JSONResponse({"ok": True, "app": app_id, "job": jid, "position": position})


@router.delete("/apps/{app_id}/wait")
def app_unwait(app_id: str) -> JSONResponse:
    """Take a queued launch off the queue. The pump notices and ends its job."""
    with _WAIT_LOCK:
        before = len(_WAIT_QUEUE)
        _WAIT_QUEUE[:] = [w for w in _WAIT_QUEUE if w["app"] != app_id]
    return JSONResponse({"ok": True, "app": app_id, "removed": before - len(_WAIT_QUEUE)})


@router.get("/waiting")
def waiting() -> JSONResponse:
    """What is queued for the card, and what the card looks like right now."""
    return JSONResponse(waiting_view())




def _force_rmtree(path: Path) -> None:
    """Delete a tree, clearing read-only attributes on the way through.

    Git marks the files under .git/objects read-only, and Python's rmtree will
    not delete a read-only file — it raises instead. Every app that arrived by
    being cloned therefore failed to delete, at the first pack file, leaving the
    folder in place. The attribute has to be cleared before the unlink.
    """
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:  # noqa: BLE001
            raise

    shutil.rmtree(path, onerror=onerror)


@router.delete("/apps/{app_id}")
def app_uninstall(app_id: str) -> JSONResponse:
    """Remove an installed app and everything it put on disk.

    An app is just a directory under the engine's home, so uninstalling means
    deleting it — but a running app holds its files open, and on Windows that
    makes the delete fail halfway. So stop it first, then report honestly
    whether the folder actually went, rather than claiming success because
    rmtree was called.
    """
    # The name is checked before it is ever a path. This route deletes whatever it
    # resolves, so an id of `..` — which `/apps/..` reaches, and which used to
    # resolve to the Athena home — was a recursive delete of the whole install.
    base = _app_dir(app_id)
    if base is None:
        return JSONResponse({"ok": False, "error": f"{app_id!r} is not an app name"},
                            status_code=400)
    # Apps live under their Pinokio name, which carries a `.git` suffix, but an
    # id may arrive with or without it — `/apps` reports the suffixed form while
    # anything hand-made or half-imported may not. Resolve both, the same way
    # import does, instead of failing on a name that is only a suffix away.
    folder = base
    if not _present(folder):
        # One level up is checked too, so an unlucky symlink reported as absent is still
        # found by name -- and the same candidate list as every other resolver.
        for cand in _app_folder_candidates(base):
            if cand != base and _present(cand):
                folder = cand
                break
    if not _present(folder) or not _direct_child(folder, base.parent):
        return JSONResponse({"ok": False, "error": f"{app_id} is not installed"},
                            status_code=404)

    # Deleting waits on the engine to stop the app, then deletes a folder that
    # may hold tens of thousands of files. Both are slow, and doing them inside
    # the request is what made Hermes appear to hang, so it runs as a job.
    def work(jid: str) -> Dict[str, Any]:
        name = folder.name        # the real on-disk name, `.git` and all
        result: Dict[str, Any] = {"app": app_id, "name": name, "path": str(folder)}

        _job_add(jid, f"Stopping {name}…")
        try:
            _runtime().stop(name)
            result["stopped"] = True
            time.sleep(1.2)          # let the shell release its handles
            _job_add(jid, "  stopped")
        except Exception as e:  # noqa: BLE001 - not running is the common case
            result["stopped"] = False
            result["stop_note"] = str(e)
            _job_add(jid, f"  was not running ({str(e)[:80]})")

        # An imported app may be a junction into the user's original install
        # rather than a copy, and those must not be treated alike. rmtree refuses
        # a link outright (so a linked app could never be deleted at all), and
        # following it would destroy the original install — hundreds of
        # gigabytes Athena never owned. Unlink the link; leave the data.
        _job_add(jid, "Removing files…")
        try:
            if _is_link_dir(folder):
                _unlink_dir(folder)
                result["unlinked"] = True
                result["note"] = ("Removed Athena's link. The original app folder "
                                  "was left untouched.")
                _job_add(jid, "  link removed; original left alone")
            else:
                _force_rmtree(folder)
                _job_add(jid, "  folder removed")
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)
            _job_add(jid, f"  !! {e}")

        # App logs and the engine's per-shell history belong to the app too;
        # leaving them makes a reinstall look like it inherited old output.
        try:
            logs = engine.home / "logs" / "shell" / "cleaned" / "api" / name
            if logs.exists():
                _force_rmtree(logs)
        except Exception:  # noqa: BLE001
            pass

        # The engine caches what it knows about an app, keyed by its folder, so
        # a stale entry can outlive the files and re-list an app that is gone.
        for stale in (engine.home / "kv" / f"api-{name}.json",
                      engine.home / "kv" / f"{name}.json"):
            try:
                if stale.is_file():
                    stale.unlink()
                    _job_add(jid, f"  cleared {stale.name}")
            except Exception:  # noqa: BLE001
                pass

        result["removed"] = not _present(folder)
        result["ok"] = result["removed"]
        if not result["ok"]:
            result.setdefault("error", "the app folder is still on disk — something "
                                       "is holding it open; stop the app and retry")
        return result

    jid = _job_spawn("delete", f"Deleting {app_id}", work)
    return JSONResponse({"ok": True, "job": jid, "app": app_id})


# ---------------------------------------------------------------- history

_REVERT_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _reflog_entries(folder: Path, limit: int = 12) -> List[Dict[str, Any]]:
    """Where this app's HEAD has been, newest first — read from git, not kept by us.

    The app's own repository is the only record a revert needs, and the only one
    that cannot go missing: git writes a reflog entry for every move, including
    the resets this route performs, so there is no bookkeeping file of Athena's to
    go stale, be deleted with Athena, or drift out of step with the repository.

    The stamp is pulled out of the ref selector (`%gd`) rather than read from
    `%cd`: in reflog output `%cd` is the *commit's* own date, so a three-year-old
    commit says nothing about when this app moved onto it. Checked on a real app
    here, where the two dates are seven months apart.
    """
    out = _git_at(folder, ["reflog", "--date=iso", f"-n{int(limit)}",
                           "--format=%H\t%gd\t%gs"], timeout=30)
    if out.returncode != 0:
        return []
    rows: List[Dict[str, Any]] = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        sel = parts[1]
        when = sel.split("@{", 1)[1][:-1] if "@{" in sel and sel.endswith("}") else ""
        rows.append({"sha": parts[0].strip(), "short": parts[0].strip()[:12],
                     "when": when, "what": parts[2].strip()})
    return rows


def _app_history(app_id: str, limit: int = 12) -> Dict[str, Any]:
    """One app's answer to "where has this been?", and whether it can go back.

    Read-only: a reflog read, a HEAD read and a working-tree read. It says nothing
    about updates being available — that is `_update_check`'s question — and it
    never fetches, so it answers the same way offline.
    """
    folder = _app_folder_for(app_id)
    if folder is None:
        return {"ok": False, "error": f"there is no app called {app_id}"}
    if not (folder / ".git").exists():
        return {"ok": True, "is_repo": False,
                "reason": "this app was not installed from a git repository, so it "
                          "has no commits to go back to"}
    if not shutil.which("git"):
        return {"ok": False, "error": "git is not on PATH"}
    try:
        entries = _reflog_entries(folder, limit=limit)
        head = _git_at(folder, ["rev-parse", "HEAD"], timeout=30).stdout.strip()
        subject = _git_at(folder, ["log", "-1", "--format=%s", "HEAD"],
                          timeout=30).stdout.strip()
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "the app's repository did not answer in time"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if not entries:
        return {"ok": True, "is_repo": True, "commits": [], "can_revert": False,
                "reason": "this app's repository keeps no record of where it has "
                          "been, so there is nothing to put back"}
    # What going back would actually cost, counted before anyone is asked to
    # agree to it: the tracked files with changes that are not in any commit.
    # Untracked files are not counted because `reset --hard` does not touch them
    # — and a local `.env` sitting on top of the clone is a normal way to run an
    # app, not something to frighten anyone about.
    dirty = 0
    try:
        st = _git_at(folder, ["status", "--porcelain", "--untracked-files=no"],
                     timeout=60).stdout or ""
        dirty = len([ln for ln in st.splitlines() if ln.strip()])
    except Exception:  # noqa: BLE001
        pass
    # Commits of its own that the reset would leave behind, which is the other way
    # a revert loses work.
    ahead = 0
    try:
        up = _git_at(folder, ["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                              "@{u}"], timeout=30).stdout.strip()
        if up:
            log = _git_at(folder, ["log", "--oneline", f"{up}..HEAD"],
                          timeout=30).stdout or ""
            ahead = len([ln for ln in log.splitlines() if ln.strip()])
    except Exception:  # noqa: BLE001
        pass
    for e in entries:
        e["current"] = e["sha"] == head
    # Only a commit this folder has actually stood on is offered, and only one
    # that is not where it already is.
    back = [e for e in entries if e["sha"] != head]
    return {"ok": True, "is_repo": True, "branch": _branch_of(folder),
            "head": {"sha": head, "short": head[:12], "subject": subject},
            "commits": entries, "dirty": dirty, "ahead": ahead,
            "can_revert": bool(back),
            "reason": ("" if back else
                       "this app has only ever been on one commit, so there is "
                       "nothing to put back to")}


def _branch_of(folder: Path) -> str:
    try:
        return _git_at(folder, ["rev-parse", "--abbrev-ref", "HEAD"],
                       timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


@router.get("/apps/{app_id}/history")
def app_history(app_id: str, limit: int = 12) -> JSONResponse:
    """Where an app has been — the route its card asks before offering to go back."""
    return JSONResponse(_app_history(app_id, limit=limit))


class RevertBody(BaseModel):
    sha: str


@router.post("/apps/{app_id}/revert")
def app_revert(app_id: str, body: RevertBody) -> JSONResponse:
    """Put an app back on a commit it has actually been on, then rebuild its view.

    Four refusals, all of them things a person would want refused rather than
    done quietly: a folder that is not a repository, a sha that is not a commit
    here, a commit this app has never stood on (which is how a typo or a
    fetched-but-never-checked-out branch would otherwise get installed), and the
    commit it is already on.

    What it does *not* do is run the app's own update afterwards: an update
    script's first act is normally `git pull`, which would fast-forward straight
    back to the commit the app is being rescued from. The files go back and
    Athena re-reads the app; if its environment then needs rebuilding, that is the
    app's own Install — one visible click, not something done behind the log.
    """
    folder = _app_folder_for(app_id)
    if folder is None or not (folder / ".git").exists():
        return JSONResponse({"ok": False,
                             "error": f"{app_id} was not installed from a git repository"},
                            status_code=400)
    want = (body.sha or "").strip()
    if not _REVERT_SHA_RE.match(want):
        return JSONResponse({"ok": False, "error": "that is not a commit id"},
                            status_code=400)
    if not shutil.which("git"):
        return JSONResponse({"ok": False, "error": "git is not on PATH"}, status_code=400)
    try:
        resolved = _git_at(folder, ["rev-parse", "--verify", f"{want}^{{commit}}"],
                           timeout=30)
        full = (resolved.stdout or "").strip()
        if resolved.returncode != 0 or not full:
            return JSONResponse({"ok": False,
                                 "error": "no commit with that id in this app"},
                                status_code=400)
        head = _git_at(folder, ["rev-parse", "HEAD"], timeout=30).stdout.strip()
        been = {e["sha"] for e in _reflog_entries(folder, limit=500)}
        dirty = [ln for ln in (_git_at(folder, ["status", "--porcelain",
                                                "--untracked-files=no"],
                                       timeout=60).stdout or "").splitlines() if ln.strip()]
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "the app's repository did not answer "
                                                   "in time"}, status_code=504)
    if full == head:
        return JSONResponse({"ok": False, "error": "this app is already on that commit"},
                            status_code=400)
    if full not in been:
        return JSONResponse({"ok": False,
                             "error": "this app has never been on that commit, so there "
                                      "is nothing of it to go back to"}, status_code=400)

    what = ""
    try:
        what = _git_at(folder, ["log", "-1", "--format=%s", full],
                       timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    short = full[:12]

    def work(jid: str) -> Dict[str, Any]:
        _job_add(jid, f"putting {app_id} back to {short}"
                      + (f" — {what}" if what else ""))
        if dirty:
            _job_add(jid, f"  {len(dirty)} file(s) with changes that are not in a "
                          f"commit go back to what the commit says")
        r = _git_at(folder, ["reset", "--hard", full], timeout=180)
        if r.returncode != 0:
            why = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1]
            _job_add(jid, f"!! the reset failed: {why[:200]}")
            return {"ok": False, "error": why[:300]}
        _job_add(jid, f"  files are back at {short}")
        note = []
        try:
            conv = _convert_app(app_id, jid, provision=False)
            if conv.get("ok"):
                _job_add(jid, f"ok  {app_id} re-read as an Athena app at {short}")
            else:
                note.append(conv.get("error") or "re-reading the app failed")
                _job_add(jid, f"!! {app_id}: {note[-1]}")
        except Exception as e:  # noqa: BLE001
            note.append(str(e))
            _job_add(jid, f"!! {app_id}: re-reading it failed: {e}")
        # Said out loud because it is the one thing about a revert that surprises
        # people: the app is behind again, and its own Update is what moves it on.
        _job_add(jid, "  updating this app again will move it forward to the newest "
                      "commit; if its environment needs rebuilding, its Install does that")
        return {"ok": True, "from": head[:12], "to": short, "subject": what,
                "dirty": len(dirty), "note": "; ".join(note)}

    jid = _job_spawn("revert", f"Putting {app_id} back to {short}", work)
    return JSONResponse({"ok": True, "job": jid, "watch": f"/jobs/{jid}",
                         "app": app_id, "from": head[:12], "to": short,
                         "subject": what, "dirty": len(dirty)})

class CostPriceBody(BaseModel):
    price: float


@router.get("/cost")
def cost_summary() -> JSONResponse:
    """What this machine has spent on its card, while Hermes was watching it.

    Not a bill and not a meter: a running total folded together from the samples
    the monitor takes — the time the card was working, and the energy it drew
    while Hermes was open. A card that does not report power draw gets its time
    counted and its energy left null, which the UI says out loud rather than
    showing a zero.
    """
    mod = _cost_module()
    if mod is None:
        return JSONResponse({"ok": False, "error": "the cost record is not available"})
    home = _athena_home()
    return JSONResponse(mod.summary(home, price=mod.get_price(home)))


@router.post("/cost/price")
def cost_set_price(body: CostPriceBody) -> JSONResponse:
    """The price per kWh, so the energy can be answered in money as well.

    Stored beside the record rather than in settings: it is a figure about this
    machine's running total, and the summary is the only thing that needs both.
    """
    mod = _cost_module()
    if mod is None:
        return JSONResponse({"ok": False, "error": "the cost record is not available"})
    try:
        price = float(body.price)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "the price must be a number"},
                            status_code=400)
    if price < 0 or price > 1000:
        # A price is a sanity-checked input, not a free number: a 100x typo here
        # would read as a real cost in the panel.
        return JSONResponse({"ok": False,
                             "error": "the price must be between 0 and 1000 per kWh"},
                            status_code=400)
    return JSONResponse(mod.set_price(_athena_home(), round(price, 4)))


@router.post("/cost/reset")
def cost_reset() -> JSONResponse:
    """Start the record over, keeping the price — a total nobody can clear is a
    total nobody can act on."""
    mod = _cost_module()
    if mod is None:
        return JSONResponse({"ok": False, "error": "the cost record is not available"})
    return JSONResponse(mod.reset(_athena_home()))

# ---------------------------------------------------------------- discover


@router.get("/catalog")
def catalog_browse(q: str = "", offset: int = 0, limit: int = 60,
                  tag: str = "", sort: str = "trending") -> JSONResponse:
    """Athena's own store listing.

    The store answers these directly — search, tag filtering and ranking are
    its own — and the crawl on disk only steps in when it cannot be reached, so
    a network problem degrades the grid instead of emptying it.
    """
    if athena_catalog is None:
        return JSONResponse({"apps": [], "error": "catalog client unavailable"},
                            status_code=503)
    try:
        res = athena_catalog.browse(engine.home, offset=offset, limit=limit,
                                    q=q, tag=tag, sort=sort)
        return JSONResponse(res)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"apps": [], "error": str(e)}, status_code=502)


@router.get("/catalog/tags")
def catalog_tags(limit: int = 300) -> JSONResponse:
    """The store's own filter list, with its own counts."""
    if athena_catalog is None:
        return JSONResponse({"tags": [], "error": "catalog client unavailable"},
                            status_code=503)
    try:
        return JSONResponse(athena_catalog.tags(engine.home, limit=limit))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"tags": [], "error": str(e)}, status_code=502)


@router.get("/catalog/detail")
def catalog_detail(ref: str) -> JSONResponse:
    """One app's full store record, live, cached as a fallback."""
    if athena_catalog is None:
        return JSONResponse({"error": "catalog client unavailable"}, status_code=503)
    try:
        return JSONResponse(athena_catalog.detail(engine.home, ref))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=404)


@router.get("/catalog/resolve")
def catalog_resolve(ref: str) -> JSONResponse:
    """Turn a catalogue slug, page URL, or owner/repo into an installable repo."""
    if athena_catalog is None:
        return JSONResponse({"error": "catalog client unavailable"}, status_code=503)
    try:
        return JSONResponse(athena_catalog.resolve(engine.home, ref))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=502)


def app_folder_name(repo_url: str) -> str:
    """The folder an app occupies, named the way the engine names it.

    The engine force-suffixes the URL with `.git` and then takes the basename,
    so an app installed from `github.com/owner/repo` lives in `api/repo.git`.
    Athena used to strip that suffix: its own installs were then named
    differently from the engine's, an app that had been imported read as not
    imported, and importing it again reported "already imported". Both spellings
    are matched on read; this is the authoritative one on write.
    """
    url = repo_url.rstrip("/")
    if not url.endswith(".git"):
        url += ".git"
    return url.split("/")[-1] or "app.git"


def clone_app(repo_url: str, home: Path, timeout: int = 1800,
              ref: Optional[str] = None, name: Optional[str] = None) -> Dict[str, Any]:
    """Bring a repository into Athena's own app folder, the engine's way.

    Installing an app is, at bottom, putting its repository where the engine looks
    for apps: `<home>/api/<name>.git/` containing a launcher. We clone ourselves so the
    credentials Hermes already holds reach private and gated repositories too, and so a
    failure is reported as a failed clone rather than as a mysterious engine error.

    No `--depth`: several apps' update scripts walk tags and history, and a shallow
    clone installs fine and then breaks on the first update.
    """
    name = name or app_folder_name(repo_url)
    dest = Path(home) / "api" / name
    if athena_naming.has_launcher(dest):
        return {"ok": True, "name": name, "path": str(dest), "already": True,
                "ref": ref}

    dest.parent.mkdir(parents=True, exist_ok=True)
    # git's environment, from an allowlist rather than copied from this process. A
    # clone carries a repository's hooks and a checkout of an installed app runs them,
    # so the child gets the machine's settings (PATH, temp, proxy, CA bundle) and the
    # credentials *this app* was given — asked for once, here at install, and
    # remembered per app. An app nobody has said yes for gets none, and clones what
    # is public. The engine pins these so a private repo or a credential helper
    # fails outright instead of blocking on a password prompt nobody can see.
    env = athena_engine.child_env({**NON_INTERACTIVE_GIT_ENV,
                                   **_app_credential_env(name)})

    url = repo_url.rstrip("/")
    if not url.endswith(".git"):
        url += ".git"

    # An app of ours is one branch of the store repository, and the rest of that
    # repository is other apps with their own history — a plain clone would pull
    # every one of them to install one. `--single-branch` takes just this app's
    # line of history: still a full clone of that branch, so the app's own
    # `git pull` still works, but nothing else comes along.
    #
    # `--` before the URL because the URL is data: one that starts with a dash is an
    # option to git otherwise (`--upload-pack=...` reads a URL as a command to run),
    # and this URL comes from a repository's own metadata.
    args = ["git", "clone"]
    if ref:
        args += ["--branch", ref, "--single-branch"]
    args += ["--", url, name]

    # Relative destination, run from the api folder — the engine's own command.
    proc = subprocess.run(
        args,
        cwd=str(dest.parent),
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    log = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return {"ok": False, "name": name, "path": str(dest),
                "error": log[-1500:] or f"git clone exited {proc.returncode}"}
    if not athena_naming.has_launcher(dest):
        return {"ok": False, "name": name, "path": str(dest),
                "error": f"this repository has no {athena_naming.LAUNCHER} launcher, "
                         "so it is not an app"}
    return {"ok": True, "name": name, "path": str(dest), "already": False, "log": log[-600:]}


@router.post("/catalog/scaffold")
def catalog_scaffold(body: BuildIn) -> JSONResponse:
    """Turn a repository into a launcher skeleton.

    This is the mechanical half of "install from a repo that has no launcher":
    clone the repository somewhere Athena can read it, work out what kind of
    project it is, and write the launcher files with the parts that need no
    judgement already correct. The agent fills in the rest — Athena has no model
    of its own, and the agent is the one that reads code.
    """
    if athena_buildapp is None:
        return JSONResponse({"error": "builder unavailable"}, status_code=503)
    if not (body.url or "").strip():
        return JSONResponse({"ok": False, "error": "no repository given"}, status_code=400)
    try:
        return JSONResponse(athena_buildapp.scaffold(engine.home, body.url,
                                                     name=body.name, force=body.force))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@router.post("/catalog/build-prompt")
def catalog_build_prompt(body: BuildIn) -> JSONResponse:
    """Scaffold, and hand back the prompt that starts the agent on the rest.

    The panel submits this into the composer — the same door the user types
    through — so the work happens in a real chat, with the agent's own tools,
    rather than behind a button that pretends to be a model.
    """
    if athena_buildapp is None:
        return JSONResponse({"error": "builder unavailable"}, status_code=503)
    if not (body.url or "").strip():
        return JSONResponse({"ok": False, "error": "no repository given"}, status_code=400)
    try:
        return JSONResponse(athena_buildapp.build_prompt(engine.home, body.url,
                                                         name=body.name))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@router.get("/catalog/guide")
def catalog_guide() -> JSONResponse:
    """The authoring guide, so the panel can show what the agent will read."""
    path = PLUGIN_DIR / athena_buildapp.GUIDE_REL if athena_buildapp else None
    if not path or not path.is_file():
        return JSONResponse({"error": "guide not found"}, status_code=404)
    return JSONResponse({"path": str(path), "text": path.read_text(encoding="utf-8")})


@router.post("/catalog/install")
def catalog_install(body: InstallIn) -> JSONResponse:
    """Install from the store: resolve the reference, clone it, then run the
    app's own default action — the same script the engine itself would run.

    Deliberately a job. An install clones a repository and then builds it, which
    takes minutes; answering inline is what made the panel say "Installed" the
    moment the clone landed, for an app that had not been built at all.
    """
    ref = (body.url or "").strip()
    if not ref:
        return JSONResponse({"ok": False, "error": "no app given"}, status_code=400)
    if _job_running("install"):
        return JSONResponse({"ok": False, "error": "an install is already running — "
                             "wait for it to finish"}, status_code=409)

    resolved: Dict[str, Any] = {}
    # Resolve anything that is not already a plain clone URL: a store slug, an
    # app name, or a store URL carrying a branch (`…/athena-store#athena-wan2gp`,
    # `…/tree/athena-wan2gp`). A bare repo URL needs no resolving.
    plain_url = re.match(r"^(?:https?://|git@)\S+\.git/?$", ref) is not None
    if athena_catalog is not None and not plain_url:
        try:
            resolved = athena_catalog.resolve(engine.home, ref)
        except Exception as e:  # noqa: BLE001
            resolved = {"error": str(e)}

    target = resolved.get("git")
    if not target:
        # Never fall back to cloning the bare name: git would fail with a
        # confusing "repository does not exist" instead of the real reason.
        return JSONResponse({
            "ok": False, "app": ref,
            "error": resolved.get("error") or f"could not find an app called '{ref}'",
            "searched": resolved.get("slug") or "",
        }, status_code=404)

    # A store install is the biggest thing Athena starts: a clone, then an
    # environment, then usually a model. Ask about the disk before the job
    # exists, so a nearly-full drive is a sentence rather than a half-built app.
    heads: Dict[str, Any] = {}
    if athena_headroom is not None:
        gate = athena_headroom.install_gate(_athena_home(), _load_settings())
        if not gate.get("ok"):
            gate.update({"app": ref, "repo": target})
            return JSONResponse(gate)
        if gate.get("message"):
            heads["disk_warning"] = gate["message"]

    label = resolved.get("name") or ref
    jid = _job_spawn("install", f"Installing {label}",
                     lambda j: _install_worker(j, target, label,
                                               ref=resolved.get("ref"),
                                               folder=resolved.get("folder")))
    return JSONResponse({"ok": True, "app": ref, "name": label,
                         "repo": target, "job": jid, **heads})


def _install_worker(jid: str, target: str, label: str, ref: Optional[str] = None,
                    folder: Optional[str] = None) -> Dict[str, Any]:
    """Clone, then run the app's own install action, streaming all the way."""
    folder = folder or app_folder_name(target)
    _job_add(jid, f"Repository: {target}")
    _job_add(jid, f"Branch: {ref}" if ref else "Branch: default")
    _job_add(jid, f"Destination: api/{folder}")

    try:
        got = clone_app(target, engine.home, ref=ref, name=folder)
    except Exception as e:  # noqa: BLE001
        _job_add(jid, f"clone failed: {e}")
        _job_end(jid, "failed", error=f"clone failed: {e}")
        return {"ok": False}

    for line in (got.get("log") or "").splitlines():
        if line.strip():
            _job_add(jid, line)
    if not got.get("ok"):
        err = got.get("error") or "clone failed"
        _job_add(jid, err)
        _job_end(jid, "failed", error=err)
        return {"ok": False}

    name = got["name"]
    _job_add(jid, "Already cloned — reusing it." if got.get("already") else "Cloned.")

    # Point the app's caches at the shared store *before* its install script runs,
    # so a fresh install writes each model and wheel straight into the one copy
    # rather than downloading its own and needing a move afterwards. This runs
    # after the clone because the app's ENVIRONMENT travels with the repo.
    if _load_settings().get("share_caches", True):
        try:
            app_dir = engine.home / "api" / folder
            targets = _shared_targets(engine.home)
            folded = 0
            for key, src in _app_private_dirs(engine.home, app_dir).items():
                folded += _merge_into(src, Path(targets[key]))["bytes"]
            env = _write_shared_env(app_dir, engine.home)
            if env["changed"]:
                _job_add(jid, f"Sharing caches with every other app "
                              f"({env['changed']} setting(s) → cache/)")
            if folded:
                _job_add(jid, f"Folded in {_fmt_bytes(folded)} that was already "
                              "sitting in the app's own cache")
        except Exception as e:  # noqa: BLE001 - never block an install on sharing
            _job_add(jid, f"Could not point caches at the shared store: {e}")

    if not engine.probe():
        _job_add(jid, "Starting the engine…")
        started = engine.start(wait=120)
        if not started.get("ok"):
            msg = "the engine did not start, so nothing can be built"
            _job_add(jid, msg)
            _job_end(jid, "failed", error=msg)
            return {"ok": False}

    try:
        seed_credentials(engine.home)
    except Exception:  # noqa: BLE001 - never block an install on credential sharing
        pass

    try:
        rt = _runtime()
    except Exception as e:  # noqa: BLE001
        msg = f"the engine runtime is unavailable: {e}"
        _job_add(jid, msg)
        _job_end(jid, "failed", error=msg)
        return {"ok": False}

    # The engine runs the entry the app marks default; many Pinokio apps mark
    # none, so fall back to the app's own first runnable choice.
    script = rt.open_default_or_first(name, _app_menu)
    if not script:
        msg = f"the app offered no runnable action — its {athena_naming.LAUNCHER} has no menu entry marked default"
        _job_add(jid, msg)
        _job_end(jid, "failed", error=msg)
        return {"ok": False}

    _job_add(jid, f"Running {os.path.basename(script)}")

    def _step(cur: int, total: int, method: str) -> None:
        _job_add(jid, f"— step {cur} of {total}"
                      + (f" ({method})" if method else ""))

    try:
        res = rt.run(name, uri=script, idle_timeout=IDLE_INSTALL,
                     on_step=_step,
                     on_line=lambda line: _job_add(jid, line))
    except Exception as e:  # noqa: BLE001
        msg = f"the install could not be started: {e}"
        _job_add(jid, msg)
        _job_end(jid, "failed", error=msg)
        return {"ok": False}

    state = res.get("state")
    if state == "error":
        err = res.get("error") or "the install script reported an error"
        _job_add(jid, err)
        _job_end(jid, "failed", error=err, result={"app": name, "state": state})
        return {"ok": False}

    # Ask the app what it offers now. An install that ran to completion moves the
    # app on — Forge only offers start.js once app/venv exists — so a changed
    # answer is the app's own word that it is built. That beats guessing at a
    # marker file, which differs per app.
    built = False
    try:
        after = rt.open_default(name)
        if after and os.path.basename(after) != os.path.basename(script):
            _job_add(jid, f"The app now offers {os.path.basename(after)}.")
            built = True
    except Exception:  # noqa: BLE001
        pass

    if state == "done":
        # An app has just landed and must be made Athena's own: checked, repaired
        # where the repair is certain, given its guide, and installed if its
        # environment is genuinely empty. Its own job, so the install is done
        # when it says it is done and the conversion is visible on its own.
        _job_spawn("convert", f"Converting {name} to an Athena app",
                   lambda cjid, a=name: _convert_app(a, cjid, provision=False))
        _job_add(jid, "Done." if built else
                 "The script finished but the app did not change its default "
                 "action — read the output above before trusting this install.")
        info = {"app": name, "state": state,
                "script": os.path.basename(script), "built": built}
        _job_end(jid, "done", result=info)
        return info

    # Still running after the idle window: a long download emits nothing for
    # minutes, and that is not a failure.
    _job_add(jid, "No output for a while — the install is still running in the "
                  "engine. Launch the app once it settles.")
    info = {"app": name, "state": state, "built": built}
    _job_end(jid, "done", result=info)
    return info


# ---------------------------------------------------------------- credentials


@router.get("/credentials")
def credentials() -> JSONResponse:
    c = collect_credentials()
    for k, v in c.items():
        if isinstance(v, dict) and "token" in v:
            v.pop("token", None)
    return JSONResponse(c)


@router.post("/credentials/seed")
def credentials_seed() -> JSONResponse:
    return JSONResponse(seed_credentials(engine.home))


# ---------------------------------------------------------------- import


IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003   # a junction
IO_REPARSE_TAG_SYMLINK = 0xA000000C       # a real symlink


def _present(path: Path) -> bool:
    """True if something occupies this name, even a broken link.

    `Path.exists()` resolves the link first, so a junction pointing at a folder
    that has since moved or been deleted reports as absent — while still holding
    the name, which makes creating it again fail with "file already exists".
    """
    return os.path.lexists(path)


def _is_link_dir(path: Path) -> bool:
    """True if `path` is a link rather than a real folder.

    `os.path.islink` is not enough: it only recognises true symlinks, and a
    Windows junction carries a different reparse tag, so a junction reports as
    an ordinary directory. That matters because shutil.rmtree *refuses* a
    junction (it notices the target's stat differs) while this check being wrong
    means we never call the function that removes it.
    """
    try:
        if os.path.islink(path):
            return True
        tag = getattr(os.lstat(path), "st_reparse_tag", 0)
        return tag in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK)
    except OSError:
        return False


def _unlink_dir(path: Path) -> None:
    """Remove a link itself, leaving whatever it points at alone."""
    try:
        os.rmdir(path)          # the junction case
    except OSError:
        os.unlink(path)         # the symlink case


def _link_dir(src: Path, dst: Path) -> None:
    """Point `dst` at `src` without copying it.

    Importing by copy duplicates whatever the app downloaded, and an app with
    models is routinely tens or hundreds of gigabytes — far more than is worth
    spending to reuse an install that is already sitting on the same disk. A
    directory junction on Windows (and a plain symlink elsewhere) reuses it in
    place; neither needs administrator rights the way a symlink does on Windows.
    """
    if os.name == "nt":
        p = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "mklink failed").strip())
    else:
        os.symlink(src, dst, target_is_directory=True)


# ---------------------------------------------------------------- long jobs

# Some of this work takes minutes, not milliseconds: sizing an app means walking
# every file in it, and a real install runs to hundreds of gigabytes. Done inside
# the request it pins a worker for the duration and the panel looks frozen, so
# anything open-ended runs on a thread and the panel polls it.
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 6 * 3600

# When this process started, which is the whole life of the jobs above it.
#
# Jobs are a dictionary in the engine's memory: they exist for as long as the
# Python process that made them, and a restart takes every one of them away. That
# is a reasonable design — a job is a thread and a log tail, and neither survives
# a restart in any honest form — but the panel had no way to tell "this job is
# gone because the engine came back" from "this request failed", so a restart
# mid-install read as a network fault. One stamp makes the difference sayable.
SESSION_STARTED = time.time()

# Liveness is asked for by nearly every route. Uncached, a busy engine made each
# of them pay its own blocking round trip, and a handful of concurrent polls
# each stalled for the full timeout. A one-second-old liveness answer is still a
# liveness answer, so it is cached briefly and single-flighted.
# `_PROBE_LOCK` and `_PROBE_AT` now live in `engine.py`, beside their only reader.
# (They were defined here, four thousand lines below the class that used them.)


# ---------------------------------------------------------------- auto-start
# "The engine is up whenever Hermes is" has to hold without a Start press and
# without a background service. The trigger is the first request that arrives:
# the desktop's status chip polls /status the moment its window opens, so in
# practice the engine is up seconds after Hermes is. Doing it at import time
# instead would fire from every host that imports this module just to read its
# routes — CLI tooling does exactly that — and a check must not spawn an engine
# as a side effect.
_AUTOSTART_LOCK = threading.Lock()
_AUTOSTART: Dict[str, Any] = {"tried": False, "ok": None, "error": None,
                              "disabled": False}


def _maybe_autostart() -> None:
    """Bring the engine up once per process, off the request path."""
    if _AUTOSTART["tried"]:
        return
    with _AUTOSTART_LOCK:
        if _AUTOSTART["tried"]:
            return
        # Claimed before anything else can fail, so a settings read that raises
        # cannot turn every later poll into another attempt.
        _AUTOSTART["tried"] = True
        try:
            if not _load_settings().get("autostart", True):
                _AUTOSTART["disabled"] = True
                return
        except Exception as e:  # noqa: BLE001
            _AUTOSTART["error"] = str(e)
            return

        def bring_up() -> None:
            # start() is idempotent: it returns early when an engine is already
            # serving, and adopts one it did not spawn rather than fighting it
            # for the port (rule 73).
            try:
                res = engine.start(wait=60.0)
                _AUTOSTART["ok"] = bool(res.get("ok"))
                if not _AUTOSTART["ok"]:
                    _AUTOSTART["error"] = res.get("error") or "engine did not start"
            except Exception as e:  # noqa: BLE001
                _AUTOSTART["ok"] = False
                _AUTOSTART["error"] = str(e)
            # Anything already on disk that has never been converted is converted
            # now, once per session. After the engine is up, because conversion
            # health-checks the app's environment by running it.
            if _AUTOSTART["ok"]:
                try:
                    # Apps left running by an earlier session are put on the idle
                    # clock too, so nothing keeps the card forever by accident.
                    _apply_store_source()
                    _idle_adopt()
                    job = _start_sweep()
                    if job:
                        _AUTOSTART["sweep_job"] = job
                except Exception as e:  # noqa: BLE001
                    _AUTOSTART["sweep_error"] = str(e)

        threading.Thread(target=bring_up, name="athena-autostart",
                         daemon=True).start()


def _job_new(kind: str, label: str, app: Optional[str] = None) -> str:
    jid = f"{kind}-{int(time.time() * 1000):x}-{os.urandom(3).hex()}"
    with _JOBS_LOCK:
        # `started` is for the record; `last_output` and `moved` are for the one
        # question a watching person actually has — is this still going, or has it
        # stopped saying anything? A job that only carries a start time can say how
        # long it has been, which is the half of the answer that never helps.
        #
        # `app` is for the idle reaper: a job that names an app is work in flight
        # against it, and saying so here is what lets the reaper tell a long
        # generation from an app nobody is using.
        now = time.time()
        _JOBS[jid] = {"id": jid, "kind": kind, "label": label, "state": "running",
                      "app": app,
                      "started": now, "ended": None, "log": [],
                      "last_output": now, "moved": now,
                      "result": None, "error": None, "progress": None}
    return jid


def _job_progress(jid: str, done: float, total: float, label: str = "",
                  apps: Optional[List[Dict[str, Any]]] = None) -> None:
    """Record how far a job has actually got, so the panel can draw a real bar.

    A job that only appends log lines can say what it is doing but not how much
    is left. Progress is kept separate from the log so a copy can update it as
    often as it likes without flooding the visible output.

    `apps` carries a per-item breakdown for a job made of several items (an
    import of three apps), so the panel can show one bar for the whole job and a
    bar per app beside it instead of a bare "2/3".
    """
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j is None:
            return
        # Moving the bar counts as telling the watcher something, so a copy that
        # reports its progress but prints nothing is not read as silent.
        j["moved"] = time.time()
        j["progress"] = {"done": float(done), "total": float(total), "label": label}
        if apps is not None:
            j["progress"]["apps"] = apps


def _job_add(jid: str, line: str) -> None:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j is None:
            return
        j["log"].append(line)
        j["last_output"] = time.time()
        if len(j["log"]) > 600:
            del j["log"][:-600]


def _job_end(jid: str, state: str, result=None, error=None) -> None:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j is None:
            return
        # The first terminal verdict is the job's verdict. A worker that fails
        # ends the job itself, with the reason (an install that could not clone, a
        # snapshot with no disk to copy, a capability that errored); the wrapper
        # around it only knows "done", and used to stamp that on top — so every
        # consumer keying on `state` read a failure as success. A later call may
        # still add evidence through the merged result, but it cannot un-fail a job.
        prior = j.get("state")
        if prior not in (None, "", "running", "queued") and prior != state:
            state = prior
            error = j.get("error")
        j["state"] = state
        # Merge, never clobber: an inner stage ends the job with the full verdict
        # (the URL it proved, the HTTP code, the log), and the wrapper around it
        # only knows "done". Overwriting threw the evidence away and left a
        # verified build looking like a bare {"ok": true}.
        if isinstance(result, dict) and isinstance(j.get("result"), dict):
            merged = dict(j["result"])
            merged.update(result)
            result = merged
        j["result"] = result
        j["error"] = error
        j["ended"] = time.time()
        # Only finished jobs are swept. The old sweep keyed on `ended or started`, so a job
        # that ran longer than the TTL was deleted *while it was still running*: the panel's
        # next poll answered "no such job", the tab showed a failure that never happened, and
        # the `_job_running` guard that exists to stop a second install on the same app was
        # gone with it. Long jobs are the normal case here -- a torch install, a model
        # download -- and they are exactly the ones the old rule deleted.
        cutoff = time.time() - _JOB_TTL
        for k in [k for k, v in _JOBS.items()
                  if v.get("ended") and v["ended"] < cutoff
                  and str(v.get("state") or "") not in ("running", "queued")]:
            _JOBS.pop(k, None)
        snap = {"id": jid, "kind": j["kind"], "label": j["label"],
                "state": state, "error": error}
    # Outside the lock: delivery is fire-and-forget, but it is still I/O and a
    # listener must never be able to stall the job bookkeeping.
    _broadcast_job_done(snap)


def _broadcast_job_done(snap: Dict[str, Any]) -> None:
    """Tell every open Desktop window that a job is over.

    The panel that started a job polls it as well, but a job outlives the panel:
    switching tabs unmounts the card, and closing the window unmounts all of
    them. This event is what makes "install finished" arrive anyway. The UI skips
    the jobs it is already watching, so nothing is announced twice.
    """
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("athena", "job.done", snap)
    except Exception:  # noqa: BLE001
        pass


def _broadcast_screen_wanted(name: str, why: str) -> None:
    """Ask every open window to put this guest's screen in front of its user.

    Pressing Start in the panel opens the screen itself, because the panel is where the
    press happened. A guest started from a chat has no such moment: an agent says it
    started a machine, the machine comes up, and nothing on screen changes. Somebody who
    asked an agent to do something to a computer should get to watch it happen without
    being told to go and look at a different tab.

    Silent on failure, like every broadcast here — a desktop that is not listening is not
    a reason for a start to fail.
    """
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("athena", "screen.wanted",
                               {"machine": str(name or ""), "why": str(why or "")})
    except Exception:  # noqa: BLE001
        pass


def _job_running(kind: str) -> bool:
    with _JOBS_LOCK:
        return any(v["kind"] == kind and v["state"] == "running"
                   for v in _JOBS.values())


def _job_verdict(jid: str, out, what: str):
    """Hand a stage's answer to its job, letting a failed answer end the job as failed.

    `_job_spawn`'s wrapper only knows that the call returned, so it stamps `done`: that is
    deliberate for a job whose *answer* is a failure (the panel's watcher reads the answer),
    and it is a trap for a stage whose failure means nothing happened at all. A snapshot with
    no disk to copy, an export that could not write its file — those return `{"ok": False}`,
    and used to be reported as a finished snapshot with the reason visible only in the log
    nobody was reading. A stage that can fail says so through here; the answer is passed
    through either way, so the panel still sees what it always saw.
    """
    if isinstance(out, dict) and out.get("ok") is False:
        why = str(out.get("error") or out.get("note") or f"the {what} did not finish")
        _job_add(jid, f"!! {why}")
        _job_end(jid, "failed", result=out, error=why)
    return out


def _job_spawn(kind: str, label: str, work, app: Optional[str] = None) -> str:
    jid = _job_new(kind, label, app=app)

    def runner():
        try:
            _job_end(jid, "done", result=work(jid))
        except Exception as e:  # noqa: BLE001
            _job_add(jid, f"!! {e}")
            _job_end(jid, "failed", error=str(e))

    threading.Thread(target=runner, name=f"athena-{kind}", daemon=True).start()
    return jid


@router.get("/jobs/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        snap = dict(j) if j else None
    if snap is None:
        # Said plainly, because the caller is usually a poll that has been running
        # for minutes: this id was real, and it is not here. Anything the job did
        # manage to print is in the engine's own log for the app it touched —
        # where it still is. Naming the restart is what stops a person hunting for
        # a network problem that does not exist.
        return JSONResponse({
            "ok": False,
            "error": "no such job",
            "gone_with_restart": True,
            "session_started": SESSION_STARTED,
            "note": ("This job is not in this run of Hermes. Jobs belong to the "
                     "engine process that started them, so one that was running "
                     "when Hermes restarted is gone — whatever it printed is in "
                     "the app's own log. Start it again to watch it through."),
        }, status_code=404)
    snap["ok"] = True
    snap["tail"] = "\n".join(snap.get("log") or [])
    snap.pop("log", None)
    # The three ages a watcher needs, as numbers: how long the job has been going,
    # how long since it printed anything, and how long since the bar moved. Derived
    # here rather than in the panel so every watcher computes them the same way, and
    # left out entirely when the stamps are missing — an absent age is honest, and a
    # zero would read as "it just spoke".
    now = time.time()
    end = snap.get("ended") or now
    for key, stamp, ref in (("elapsed", "started", end),
                            ("since_output", "last_output", now),
                            ("since_move", "moved", now)):
        when = snap.get(stamp)
        snap[key] = round(ref - when, 1) if isinstance(when, (int, float)) else None
    return JSONResponse(snap)


# ---------------------------------------------------------------- machines
#
# A machine is a real OS Athena can start, stop, hand a command to, and show you:
# an app that only installs on Ubuntu, a build that needs Arch, a "does this work
# on Windows" question that should not be answered by guesswork. The module next
# to this one owns the specs, the registry and the WSL verbs; what lives here is
# the part that needs the engine's own furniture — Athena's home, and the job
# system, because provisioning downloads a few hundred megabytes and a job is
# what lets the panel watch it instead of a request that blocks for ten minutes.
#
# The awkward fact this is built around: a dashboard plugin route is served ONE
# REQUEST AT A TIME through httpx's ASGI transport, so it can return a dictionary
# and not a stream. Every long operation is therefore a job, and every watchable
# thing is polled.

try:
    import machines as athena_machines
except Exception:  # noqa: BLE001 — no machines, and nothing else changes
    athena_machines = None


def _machines_up() -> List[str]:
    """Names of the machines on this computer that are running right now.

    Asked of the same `state()` a row uses — the process table, not what Athena wrote
    down — so a refusal names what is actually holding memory rather than what Athena
    believes it started. A machine whose state cannot be read is left out rather than
    guessed at: this list exists to point at something, and a wrong name points at the
    wrong thing.
    """
    m = athena_machines
    host = machines_host
    if m is None or host is None:
        return []
    out: List[str] = []
    try:
        specs = m.list_specs(host.home)
    except Exception:  # noqa: BLE001 — a listing that fails is not a broken start
        return out
    for spec in specs:
        try:
            if host.state(spec) == "running":
                out.append(spec.name)
        except Exception:  # noqa: BLE001
            continue
    return out


def _machine_gate() -> Dict[str, Any]:
    """Whether this host has the memory to hold another guest, from `headroom`."""
    if athena_headroom is None:
        return {"ok": True}
    return athena_headroom.machine_gate(_load_settings(), running=_machines_up())


class MachineHost:
    """Machines as the panel and the agent both see them.

    Deliberately thin. Everything that can be decided without knowing where
    Athena lives or what a job is lives in `machines.py` and is tested without
    booting anything; this class adds the home, the job and the honest refusal.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------ readings
    @property
    def home(self) -> Path:
        return _athena_home()

    def _m(self):
        if athena_machines is None:
            raise RuntimeError("the machines module did not load")
        return athena_machines

    def wsl(self) -> Dict[str, Any]:
        try:
            return self._m().wsl_status()
        except Exception as e:  # noqa: BLE001 — a probe that fails is a probe that says so
            return {"installed": False, "version": "", "distros": [], "default": "",
                    "reason": f"could not ask WSL: {e}"}

    def qemu(self) -> Dict[str, Any]:
        try:
            return self._m().qemu_status()
        except Exception as e:  # noqa: BLE001
            return {"installed": False, "path": "", "install_command": "", "reason": str(e)}

    def setup(self) -> Dict[str, Any]:
        """What is here, what is missing, and the question to ask about it.

        Returned rather than acted on. `wsl --install` needs elevation and usually
        a reboot, so a plugin that runs it because a card was clicked is a plugin
        with a worse relationship with its user than the one it is replacing.
        """
        m = self._m()
        ws, q = self.wsl(), self.qemu()
        asks = []
        if not ws.get("installed"):
            asks.append(m.wsl_needs_setup())
        if not q.get("installed"):
            asks.append(m.qemu_needs_setup())
        return {"wsl": ws, "qemu": q, "needs_setup": asks,
                "presets": m.PRESETS, "home": str(m.machines_dir(self.home)),
                "platform": sys.platform, "fit": self.fit(), "isos": self.isos()}

    def fit(self, provider: str = "qemu") -> Dict[str, Any]:
        """The size this computer can honestly give a guest, and why.

        The first machine somebody makes should already be sized right for the machine
        they made it on, so the form is not a place where four numbers have to be
        guessed. The reasoning travels with the numbers and is shown next to them.
        """
        m = self._m()
        facts = m.host_facts(m.machines_dir(self.home))
        fit = m.fit_spec(facts.get("cores"), facts.get("memory_mb"), facts.get("free_gb"),
                         provider)
        fit["host"] = facts
        return {"ok": True, **fit}

    def isos(self, folder: str = "") -> Dict[str, Any]:
        """The image folder and what is in it — the list the panel's menu is drawn from."""
        m = self._m()
        where = Path(str(folder).replace("\\", "/")).expanduser() if folder \
            else m.iso_dir(self.home, _load_settings())
        scan = m.scan_isos(where)
        scan["ok"] = True
        scan["inbox"] = str(Path(self.home) / "isos")
        return scan

    def set_iso_dir(self, folder: str, create: bool = False) -> Dict[str, Any]:
        """Point the image folder somewhere, or back at Athena's own inbox.

        A folder that does not exist is a question, not a mkdir: the answer says the
        path was not found and that `create` will make it, because a typo silently
        turned into an empty folder is a menu that is empty for no visible reason.
        """
        m = self._m()
        want = str(folder or "").strip()
        if want in ("", "-"):
            cfg = _load_settings()
            # None, not an absent key: a save merges onto the file, so "go back to the
            # inbox" has to be said as a removal rather than as silence.
            cfg[m.ISO_DIR_SETTING] = None
            _save_settings(cfg)
            return {"ok": True, "cleared": True, **self.isos()}
        path = Path(want.replace("\\", "/")).expanduser()
        if path.exists() and not path.is_dir():
            return {"ok": False, "error": f"{path} is a file, not a folder"}
        if not path.is_dir():
            if not create:
                return {"ok": False, "needs_folder": True, "dir": str(path),
                        "error": f"{path} does not exist yet — say so and it will be made"}
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"could not create {path}: {e}"}
        cfg = _load_settings()
        cfg[m.ISO_DIR_SETTING] = str(path)
        _save_settings(cfg)
        return {"ok": True, "saved": str(path), **self.isos()}

    def folders(self, path: str = "") -> Dict[str, Any]:
        """One level of the file system, for picking a folder without typing a path.

        Read-only and dirs-only: Athena lists what is in a folder because somebody
        asked for that folder, never by walking a drive on its own.
        """
        m = self._m()
        return m.list_folders(path, home=self.home)

    def install_needed(self, what: str) -> Dict[str, Any]:
        """Install a missing prerequisite — only ever because someone asked.

        This is the "ask them first" half of setup, and it is a route rather than a
        background decision: the button says what will happen before it happens.
        Three outcomes, and each is reported as what it is — Windows asking for
        permission, a download that is now a job, or a command that has to be run
        by hand because it wants a password Athena must not type for you.
        """
        m = self._m()
        plan = m.install_plan(what)
        if not plan.get("ok"):
            return plan
        method = plan.get("method")
        if method == "elevated":
            res = m.start_elevated(plan["command"])
            if not res.get("ok"):
                return res
            self._remember(f"setup-{plan['what']}", "asked Windows to install")
            return {"ok": True, "what": plan["what"], "command": plan["command"],
                    "elevated": True, "note": plan["note"]}
        if method == "manual":
            return {"ok": False, "what": plan["what"], "command": plan["command"],
                    "manual": True, "note": plan["note"],
                    "error": plan["note"]}
        kind = f"install-{plan['what']}"
        if _job_running(kind):
            return {"ok": True, "already": True, "note": f"{plan['what']} is already installing"}
        jid = _job_spawn(kind, f"Installing {plan['what'].upper()}",
                         lambda j: self._install(plan, j))
        return {"ok": True, "what": plan["what"], "command": plan["command"],
                "job": jid, "watch": f"/jobs/{jid}", "note": plan["note"]}

    def _install(self, plan: Dict[str, Any], jid: str) -> Dict[str, Any]:
        """Install, two ways: a download we can count, or a command we can only watch."""
        if plan.get("method") == "download":
            return self._install_download(plan, jid)
        _job_add(jid, f"$ {plan['command']}")
        try:
            proc = subprocess.Popen(plan["command"], shell=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"could not start the installer: {e}")
        for line in proc.stdout or []:
            line = line.rstrip()
            if line:
                _job_add(jid, line)
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"{plan['what']} install exited {code} — the output above is "
                               f"what the installer said")
        return {"what": plan["what"], "command": plan["command"], "exit_code": code}

    def _install_download(self, plan: Dict[str, Any], jid: str) -> Dict[str, Any]:
        """Fetch an installer whose bytes can be counted, then hand it to Windows.

        Why this exists: `winget install` prints nothing measurable when its output is
        a pipe (verified — a piped winget download prints five lines and no percentage),
        so an install driven that way can say what it is doing but never how far along
        it is. Here the download is ours, so the bar is made of real bytes and a real
        total, and the hash winget published is checked before anything is run.

        The installer is then started *elevated*, because installing into Program Files
        asks Windows for permission and that prompt is the consent. If permission is
        refused, the file stays and its path is reported rather than thrown away.
        """
        m = self._m()
        name = str(plan.get("file") or "installer.exe")
        dest = Path(self.home) / "downloads" / name
        _job_add(jid, f"$ download {plan.get('url')}")
        _job_progress(jid, 0, 0, f"connecting for {name}")

        def _on(done: float, total: float) -> None:
            label = f"downloading {name}"
            if total:
                pct = int(done * 100 / total)
                label += f" — {pct}% of {round(total / (1024 * 1024))} MB"
            _job_progress(jid, done, total, label)

        res = m.download_with_progress(str(plan.get("url") or ""), dest, on_progress=_on,
                                       expect_sha256=str(plan.get("sha256") or ""))
        if not res.get("ok"):
            raise RuntimeError(str(res.get("error") or "the installer did not download"))
        mb = round(float(res.get("bytes") or 0) / (1024 * 1024))
        _job_add(jid, f"  {mb} MB in {res.get('seconds')}s"
                      + (", sha256 matches the published one" if plan.get("sha256") else ""))
        _job_progress(jid, res.get("bytes") or 0, res.get("total") or res.get("bytes") or 0,
                      "downloaded — asking Windows to install it")
        args = " ".join(str(x) for x in (plan.get("installer_args") or []))
        started = m.start_elevated((f'"{dest}" {args}').strip())
        if not started.get("ok"):
            return {"ok": False, "downloaded": str(dest),
                    "error": f"{started.get('error')} — the installer is downloaded and waiting "
                             f"at {dest}; run it yourself, then start the machine again."}
        _job_add(jid, f"  Windows was asked to install {name} — approve the prompt, then Re-read")
        return {"what": plan["what"], "downloaded": str(dest), "elevated": True,
                "command": f'"{dest}" {args}'.strip(), "version": plan.get("version", "")}

    def state(self, spec, ws: Optional[Dict[str, Any]] = None) -> str:
        """running / stopped / missing — the three answers a person needs.

        Read from WSL itself rather than from anything Athena wrote down: a distro
        can be started by another window, and a cached answer would then be a
        confident lie.
        """
        ws = ws if ws is not None else self.wsl()
        if spec.provider == "qemu":
            # Asked of the pidfile *and* the process table, per `qemu_running`: a
            # pidfile left by a crash would otherwise be read as a running machine.
            m = self._m()
            return "running" if m.qemu_running(m.machine_dir(self.home, spec.name)) else "stopped"
        if spec.provider != "wsl":
            return "idle"
        row = next((d for d in ws.get("distros", []) if d["name"] == spec.distro), None)
        if row is None:
            return "missing"
        return "running" if str(row.get("state", "")).lower() == "running" else "stopped"

    def row(self, spec, ws: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        state = self.state(spec, ws)
        needs = None
        try:
            if spec.provider == "wsl":
                needs = self._m().wsl_needs_setup(spec, ws)
            elif spec.provider == "qemu" and not self.qemu().get("installed"):
                needs = self._m().qemu_needs_setup()
            else:
                needs = None
        except Exception:  # noqa: BLE001 — a probe that fails is not a broken row
            needs = None
        last = self._last.get(spec.name) or {}
        # Where its screen and its ports are travels with the row: "Started, now what"
        # is the next thing anybody asks, and a card that has to click through to find
        # out is a card that hides the answer at the moment it matters.
        try:
            reach = self._m().qemu_reach(spec) if spec.provider == "qemu" else ""
        except Exception:  # noqa: BLE001 — a row without a sentence is still a row
            reach = ""
        # What is on the disk is derived every time, not remembered: `installed` is a fact about
        # a file, and a stored copy of it would outlive the file it describes. `medium_attached`
        # travels with it so a card can say "there is no installer in this machine any more"
        # instead of leaving somebody to wonder where the ISO went.
        folder = self._m().machine_dir(self.home, spec.name)
        try:
            installed = self._m().is_installed(spec, folder)
            attached = self._m().attach_medium(spec, folder)
        except Exception:  # noqa: BLE001 — a row without this is still a row
            installed, attached = False, False
        return {**spec.to_dict(), "summary": spec.summary(), "state": state,
                "needs_setup": needs, "last_op": last or None, "reach": reach,
                "installed": installed, "medium_attached": attached,
                "disk_bytes": self._m().disk_written_bytes(folder),
                "home": str(folder)}

    def list(self) -> Dict[str, Any]:
        m = self._m()
        ws = self.wsl()
        specs = m.list_specs(self.home)
        claimed = {s.distro for s in specs if s.provider == "wsl"}
        # Distros that exist and are not anybody's machine. Offered, never claimed:
        # the whole point of a machine is that it is not your development
        # environment, so attaching to one is a decision and not a default.
        unclaimed = [d for d in ws.get("distros", [])
                     if d["name"] not in claimed and not d["name"].startswith("docker-")]
        rows = [self.row(s, ws) for s in specs]
        ask = []
        if not ws.get("installed"):
            ask.append(m.wsl_needs_setup())
        if not self.qemu().get("installed"):
            ask.append(m.qemu_needs_setup())
        running = [r["name"] for r in rows if r.get("state") == "running"]
        summary = f"{len(rows)} machine{'' if len(rows) == 1 else 's'}"
        if running:
            summary += f", {len(running)} running ({', '.join(running)})"
        if unclaimed:
            summary += (f"; {len(unclaimed)} distro{'' if len(unclaimed) == 1 else 's'} on this "
                        f"computer Athena is not managing")
        # The asks travel with the list, not only with /machines/setup: a tab that has
        # to make a second call to discover that QEMU is missing would show a machine
        # it cannot start with no explanation on the card.
        return {"ok": True, "summary": summary, "machines": rows,
                "unclaimed": unclaimed, "wsl": ws, "qemu": self.qemu(),
                "needs_setup": ask, "home": str(m.machines_dir(self.home))}

    # ------------------------------------------------------------ lifecycle
    def create(self, spec) -> Dict[str, Any]:
        m = self._m()
        # "Nobody said" is answered here, once, where the machine is made — not at every
        # start. A spec on disk that says `window` is a fact about what this machine does;
        # one that said "" would be a question asked again on a host it was not made on.
        if not str(spec.display or ""):
            spec.display = m.default_display()
        spec.validate()
        if m.load_spec(self.home, spec.name) is not None:
            return {"ok": False, "error": f"there is already a machine called {spec.name!r}"}
        m.save_spec(self.home, spec)
        return {"ok": True, "machine": self.row(spec),
                "path": str(m.machine_dir(self.home, spec.name))}

    def start(self, name: str, watched: bool = False) -> Dict[str, Any]:
        """Bring a machine up, provisioning it first if its image is not there yet.

        `watched` is set by the agent's tool and by nothing else: a guest an agent
        started to run something in is the shape of a leak — running, out of sight, and
        holding gigabytes — so it goes on the idle clock. A guest a person started is
        theirs until they stop it, which is what `watched=False` preserves.
        """
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        # A `machine.json` is a file people edit, and this is the place that can say so in a
        # sentence: starting is what builds the command line, and a hand-edited number used to
        # raise inside the builder and answer 500. Stopping and snapshotting do not: a machine
        # whose spec has a typo still has to be stoppable, and a snapshot reads the disk.
        problem = m.spec_problem(spec)
        if problem:
            return {"ok": False, "error": problem, "spec_problem": True}
        # Before the command line is built, and asked here rather than in the route,
        # because every way into a start arrives through this method — the panel's
        # button, the agent's tool, and a machine waking from an idle stop. A guard
        # on one of those three is a guard the other two route around by accident.
        #
        # A machine that is already up is not a start: asking again is a no-op that
        # answers where its screen is, and refusing it would be refusing to tell
        # somebody about a machine they are already watching.
        if self.state(spec) != "running":
            gate = _machine_gate()
            if not gate.get("ok", True):
                return {"ok": False, "machine": name, **gate}
        if spec.provider == "qemu":
            # Before the command line exists, ask the disk what this machine is now: an
            # installer with a system on it is a machine, and it becomes one here.
            adopted = self._adopt_installed(spec)
            out = self._start_qemu(spec)
            if adopted.get("adopted"):
                out["adopted"] = True
                out["note"] = (str(out.get("note") or "") + " " + str(adopted["note"])).strip()
            return self._arm(out, name, watched)
        if spec.provider != "wsl":
            return {"ok": False, "error": f"{spec.provider!r} is not a provider this machine "
                                          f"knows how to start"}
        ws = self.wsl()
        if not ws.get("installed"):
            return {"ok": False, "error": "WSL is not installed on this machine",
                    "needs_setup": m.wsl_needs_setup(None, ws)}
        have = {d["name"] for d in ws.get("distros", [])}
        if spec.distro in have:
            return self._arm(self._boot(spec), name, watched)

        ask = m.wsl_needs_setup(spec, ws)
        if spec.attach:
            return {"ok": False, "error": (ask or {}).get("question") or
                                          f"no distro called {spec.distro!r}", "needs_setup": ask}

        kind = f"machine-{spec.name}"
        if _job_running(kind):
            return {"ok": True, "already": True, "state": "provisioning",
                    "note": f"{name} is already being provisioned — watch /jobs for it"}
        jid = _job_spawn(kind, f"Provisioning {name}", lambda j: self._provision(spec, j))
        self._remember(name, "provisioning", job=jid)
        return {"ok": True, "job": jid, "watch": f"/jobs/{jid}", "state": "provisioning"}

    def console(self, name: str, limit: int = 8000) -> Dict[str, Any]:
        """What the guest has said so far, plus where its screen is.

        A QEMU guest has no shell Athena can type into, so this is the only part of it
        that can be shown *inside* the panel: its serial console, which is where a boot
        loader and an installer talk before anything else in the guest exists.
        """
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        if spec.provider != "qemu":
            return {"ok": True, "applies": False, "text": "",
                    "note": "this machine has a shell of its own — run a command instead"}
        mdir = m.machine_dir(self.home, name)
        res = m.console_tail(mdir, limit=max(200, min(int(limit or 8000), 200000)))
        return {"ok": bool(res.get("ok", True)), "applies": True, "name": name,
                "state": self.state(spec), "display": m.resolve_display(spec),
                "vnc_port": int(spec.vnc_port or 0), "reach": m.qemu_reach(spec),
                **res}

    def screen(self, name: str, save: bool = False) -> Dict[str, Any]:
        """The guest's screen, as an image the panel can show.

        The panel cannot open a socket to a VNC server and these routes cannot upgrade to
        a websocket, so the plugin is the VNC client and the screen arrives as a JPEG in
        the reply (see `screen._JPEG_QUALITY` for why not a PNG). Read on demand rather than streamed: a person watching a machine that
        is installing an OS needs a picture every second or so, not sixty.

        `save` writes the same frame beside the machine as `screen.jpg`, which is how an
        agent (or a bug report) can look at what the guest is showing.
        """
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        if spec.provider != "qemu":
            return {"ok": True, "applies": False, "image": "",
                    "note": "this machine has a shell of its own — run a command instead"}
        if m.resolve_display(spec) == "none":
            return {"ok": True, "applies": True, "image": "", "blank": True,
                    "note": "this machine runs headless — no screen to show"}
        mdir = m.machine_dir(self.home, name)
        if not m.qemu_running(mdir):
            return {"ok": True, "applies": True, "image": "", "state": self.state(spec),
                    "note": "it is not running, so there is no screen to read yet"}
        if athena_screen is None:
            return {"ok": False, "error": "the screen reader is missing from this install "
                                          "(dashboard/screen.py)"}
        port = m.vnc_tcp_port(spec)
        # A socket where the machine has one, its loopback address where it has not — a
        # guest started before the socket change still has a screen, and a reader that
        # only knew the new spelling would have stopped showing it. Asked of the
        # filesystem rather than stored: see `machines.vnc_endpoint`.
        host = m.vnc_endpoint(m.machine_dir(self.home, name), spec)
        # Somebody is looking at this guest, so it is not idle — which is what keeps an
        # agent-started machine alive while it is being watched.
        _idle_note(_idle_machine_key(name), what="its screen")
        try:
            frame = _held_session(name, host, port).frame()
        except Exception as first:  # noqa: BLE001 — a screen that will not arrive is a sentence
            # Keeping the connection is a performance shape, not the only way to read a
            # screen. If it cannot be kept, read one frame the old way before giving up:
            # a slower picture beats no picture, and the reason is still reported.
            try:
                _held_session(name, host, port, drop=True)
                frame = athena_screen.frame_image(host, port, timeout=6.0)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "applies": True, "image": "", "error": str(e),
                        "held_connection_error": str(first), "state": self.state(spec)}
        out = {"ok": True, "applies": True, "state": self.state(spec),
               "width": frame["width"], "height": frame["height"], "name": frame["name"],
               "bytes": len(frame["image"]), "at": _now(),
               "mime": frame.get("mime", "image/jpeg"),
               # Who has the keyboard, riding along with the picture rather than asked for
               # separately: the panel draws the screen on the same clock, and a second
               # request per second to learn one word would be a tax on every frame.
               "lease": self.screen_lease(name).get("lease"),
               "image": _data_url(frame)}
        if save:
            try:
                path = mdir / "screen.jpg"
                path.write_bytes(frame["image"])
                out["saved"] = str(path)
            except OSError as e:
                out["note"] = f"the frame was read but not saved: {e}"
        return out

    def screen_input(self, name: str, events: Any, who: str = "person",
                     force: bool = False) -> Dict[str, Any]:
        """Send mouse and keyboard events to the guest's screen.

        This is what makes the panel a screen rather than a picture of one: without it
        nobody could answer an installer's first question.

        `who` is which hand on the keyboard this is, and it is not bookkeeping: a guest has
        one keyboard, so a person clicking at the panel and an agent typing an installer's
        answers are two writers on it, and the guest acts on whatever arrives in the order
        it arrives. `dashboard/lease.py` holds the record of who is driving; a person's
        input always goes through and takes it, and an agent's is refused while a person
        holds it — unless `force`, which is the person's own tool taking the keyboard back,
        and is reported in the answer rather than done quietly.
        """
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        if spec.provider != "qemu":
            return {"ok": False, "error": "this machine has a shell of its own, not a screen"}
        mdir = m.machine_dir(self.home, name)
        if not m.qemu_running(mdir):
            return {"ok": False, "error": "it is not running, so there is nothing to type into"}
        if m.resolve_display(spec) == "none":
            return {"ok": False, "error": "this machine runs headless — it has no screen"}
        events = events if isinstance(events, list) else [events or {}]
        if len(events) > 64:
            events = events[:64]
        if athena_screen is None:
            return {"ok": False, "error": "the screen reader is missing from this install "
                                          "(dashboard/screen.py)"}
        # Who is driving, asked before anything is sent: a refusal that arrives after the
        # keystroke is not a refusal, it is a keystroke with an apology. A missing lease
        # module is not a reason to refuse input — this computer is the person's, and locking
        # them out of their own screen because a file would not import is worse than the
        # thing that file prevents.
        lease = _lease_module()
        # Read before the check, because `force` is what clears it: asking afterwards would
        # leave the takeover unreported, and a keyboard taken from a person silently is the
        # one thing this must never be.
        was_held = lease.owner(name) if lease is not None else None
        if lease is not None:
            refused = lease.check(name, who, force=force)
            if refused is not None:
                return refused
        port = m.vnc_tcp_port(spec)
        # A socket where the machine has one, its loopback address where it has not — a
        # guest started before the socket change still has a screen, and a reader that
        # only knew the new spelling would have stopped showing it. Asked of the
        # filesystem rather than stored: see `machines.vnc_endpoint`.
        host = m.vnc_endpoint(m.machine_dir(self.home, name), spec)
        # Somebody is looking at this guest, so it is not idle — which is what keeps an
        # agent-started machine alive while it is being watched.
        _idle_note(_idle_machine_key(name), what="its screen")
        try:
            res = _held_session(name, host, port).input(events)
        except Exception as first:  # noqa: BLE001
            try:
                _held_session(name, host, port, drop=True)
                res = athena_screen.send_input(host, port, events, timeout=6.0)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e), "held_connection_error": str(first)}
        # `dropped` travels with the answer: the screen module names any key or modifier it could
        # not turn into a keystroke, and a panel that only reads `ok` would report a Super+Enter
        # that never left the building as a keystroke the guest had.
        #
        # The hold is taken *after* the events went, and for both callers. After, because a
        # person who has the keyboard and whose keystroke then failed to reach the guest is
        # exactly the case where an agent must not start typing; for both, because "an agent
        # is driving this screen" is a thing the panel has to be able to say, and the only
        # honest place to learn it is here.
        out_lease = None
        if lease is not None:
            mine = lease.AGENT if str(who or "").strip().lower() == lease.AGENT else lease.PERSON
            note = ""
            if (force and mine == lease.AGENT and was_held
                    and str(was_held.get("who")) == lease.PERSON):
                note = "an agent call took the keyboard from a person, with force"
            out_lease = lease.hold(name, mine, note=note)
        return {"ok": True, "name": name, "sent": res.get("sent"), "keys": res.get("keys"),
                "dropped": res.get("dropped") or [], "lease": out_lease}

    def screen_lease(self, name: str, release: bool = False) -> Dict[str, Any]:
        """Who is driving this machine's screen — and the control that hands it back.

        The panel needs both halves for the indicator to mean anything: a person who sees
        "an agent is typing here" and cannot stop it has been told about a problem instead of
        being given the button. Release is unconditional, because it is the person at their
        own computer asking for their own keyboard back.
        """
        lease = _lease_module()
        if lease is None:
            return {"ok": False, "error": "the screen lease is missing from this install "
                                          "(dashboard/lease.py)"}
        if release:
            return dict(lease.release(name), lease=lease.owner(name))
        return {"ok": True, "name": name, "lease": lease.owner(name),
                "others": lease.state()}

    def set_display(self, name: str, display: str) -> Dict[str, Any]:
        """Move a machine's screen between the panel and a window of its own.

        Where a screen lives is decided by QEMU's command line, which is decided when the
        machine starts — so asking for the other one on a running machine means stopping
        and starting it again. Saying that plainly beats a button that appears to do
        nothing, which is what changing the setting without restarting would look like.
        """
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        want = str(display or "").strip().lower()
        if want not in m.DISPLAYS or not want:
            return {"ok": False, "error": f"a screen is one of {', '.join(m.DISPLAYS[1:])}"}
        if spec.provider != "qemu":
            return {"ok": False, "error": "this machine has a shell of its own, not a screen"}
        # A machine that is still being made has a job of its own, and that job stops and
        # starts the guest as it works. Moving the screen underneath it puts two actors on
        # one machine: measured live, the job's own boot then died ("Ignoring request for
        # interrupt vector 0") and the machine was left stopped -- from the panel, a machine
        # that crashed. Refusing says when to ask again, which a button that half-worked
        # never does.
        if _job_running(f"machine-{name}"):
            return {"ok": False, "error": f"{name} is still being made — wait for that to "
                                          f"finish and then move its screen"}
        was = self.state(spec)
        spec.display = want
        m.save_spec(self.home, spec)
        out: Dict[str, Any] = {"ok": True, "name": name, "display": want,
                               "was": was, "restarted": False}
        if want == "panel":
            # A display number has to be free for the panel's VNC server to bind.
            taken = [s.vnc_port for s in m.list_specs(self.home) if s.name != name]
            spec.vnc_port = m.pick_vnc_display(taken)
            m.save_spec(self.home, spec)
            out["vnc_port"] = spec.vnc_port
        if was == "running":
            self.stop(name)
            started = self.start(name)
            out["restarted"] = True
            if not started.get("ok"):
                out.update({"ok": False, "error": started.get("error") or started.get("note")})
                return out
            out["state"] = started.get("state")
            out["reach"] = started.get("reach")
            out["display_fallback"] = started.get("display_fallback")
        out["note"] = ("it is running again, with its screen in the panel" if want == "panel"
                       else "it is running again, with a window of its own") if was == "running" \
            else ("it will show its screen in the panel the next time it starts"
                  if want == "panel" else
                  "it will open a window of its own the next time it starts")
        return out

    def edit(self, name: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Change a machine's settings, and do what the change requires.

        The rules are `machines.apply_patch`'s, not this method's: the same question is asked
        whether a person typed the number into a box or an agent sent it, and both get the
        same sentence back. What lives *here* is everything that touches the machine rather
        than its spec -- the free VNC display a panel screen needs, growing the disk file, and
        the restart that a setting written into QEMU's command line costs.

        A running machine is restarted when a change needs it, rather than saving a number
        that quietly does nothing until the next boot: the other setters in this class already
        do exactly that (`set_display`, `set_boot`), and a panel where one setting applies and
        the next one waits is a panel nobody can predict. A machine that is still being made
        is refused, for the reason measured in `set_display` -- two actors on one machine.
        """
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        if _job_running(f"machine-{name}"):
            return {"ok": False, "error": f"{name} is still being made — wait for that to "
                                         f"finish and then change its settings"}
        if "display" in changes and spec.provider != "qemu":
            return {"ok": False, "error": "this machine has a shell of its own, not a screen"}
        running = self.state(spec) == "running"
        applied = m.apply_patch(spec, changes, running=running)
        if not applied.get("ok"):
            return applied
        spec = m.MachineSpec.from_dict(applied["spec"])
        changed = dict(applied.get("changed") or {})
        out: Dict[str, Any] = {"ok": True, "name": name, "changed": changed,
                               "restart_required": bool(applied.get("restart_required")),
                               "restarted": False, "spec": spec.to_dict(),
                               "notes": list(applied.get("notes") or [])}
        if not changed:
            return out
        if "display" in changed:
            # A display number has to be free for the panel's VNC server to bind.
            taken = [s.vnc_port for s in m.list_specs(self.home) if s.name != name]
            spec.vnc_port = m.pick_vnc_display(taken)
            out["vnc_port"] = spec.vnc_port
            out["spec"] = spec.to_dict()
        m.save_spec(self.home, spec)
        if "disk_gb" in changed and not running:
            grown = m.grow_disk(m.machine_dir(self.home, name), spec)
            out["disk"] = grown
            if not grown.get("ok"):
                # The size is recorded either way, and saying so is the honest half: the
                # number is what the next start will use for a machine with no disk yet.
                out["notes"].append("the size is recorded, but the disk file was not grown: "
                                    + str(grown.get("error") or ""))
        if running and applied.get("restart_required"):
            self.stop(name)
            started = self.start(name)
            out["restarted"] = True
            if not started.get("ok"):
                out.update({"ok": False, "state": "stopped",
                            "error": started.get("error") or started.get("note")})
                return out
            out["state"] = started.get("state")
            out["reach"] = started.get("reach")
            out["display_fallback"] = started.get("display_fallback")
        what = ", ".join(sorted(changed))
        out["note"] = (f"{what} saved; it is running again with the new settings"
                       if out["restarted"] else
                       f"{what} saved" + ("; it takes effect the next time this machine starts"
                                          if applied.get("restart_required") else ""))
        return out

    def set_boot(self, name: str, where: str) -> Dict[str, Any]:
        """Choose what a machine with a medium attached starts on: its disk, or the medium.

        This is the "reboot now" problem in one setting. An installer ISO boots first (order
        `d`) only when it is asked to; by default the firmware is offered the disk and falls
        back to the CD-ROM, so a fresh machine still reaches its installer while a finished
        install boots its own system instead of the installer again. Where a machine starts is
        written into QEMU's command line, so asking for the other one on a running machine
        means stopping and starting it — said plainly rather than shown as a button that
        appears to do nothing.
        """
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        if spec.provider != "qemu":
            return {"ok": False, "error": "only a machine with a CD-ROM attached boots a medium"}
        if str((spec.image or {}).get("kind") or "") != "iso":
            return {"ok": False, "error": "this machine has no medium to boot instead"}
        # The panel's words are "the installer" and "its own system"; the spec's are `medium`
        # and `system`. Both are accepted here, and only the spec's are ever stored — a form
        # filled in with a sentence beats a form that answers "not one of system, medium".
        raw = str(where or "").strip().lower()
        want = " ".join(w for w in raw.split()
                        if w not in ("the", "a", "its", "itself", "own", "my", "boot")) or raw
        if want in ("medium", "cd", "dvd", "installer", "iso", "yes"):
            want = "medium"
        elif want in ("system", "disk", "disk-first", "no", "own"):
            want = "system"
        else:
            return {"ok": False, "error": "boot is 'system' or 'medium' "
                                          f"(not {where!r})"}
        # Same race as moving a screen: a machine still being made has a job that starts and
        # stops the guest, and a second actor on it kills the boot.
        if _job_running(f"machine-{name}"):
            return {"ok": False, "error": f"{name} is still being made — wait for that to "
                                          f"finish and then change what it boots"}
        was = self.state(spec)
        spec.boot = want
        m.save_spec(self.home, spec)
        out: Dict[str, Any] = {"ok": True, "name": name, "boot": want, "was": was,
                               "restarted": False, "boot_order": m.boot_args(want)[1]}
        if was == "running":
            self.stop(name)
            started = self.start(name)
            out["restarted"] = True
            if not started.get("ok"):
                out.update({"ok": False, "error": started.get("error") or started.get("note")})
                return out
            out["state"] = started.get("state")
            out["reach"] = started.get("reach")
        installed = "the installer" if want == "medium" else "the system on its disk"
        if want == "medium":
            out["medium_attached"] = True
        out["note"] = (f"it is running again, and it starts on {installed}" if was == "running"
                       else f"it will start on {installed} the next time it starts")
        return out

    def _adopt_installed(self, spec) -> Dict[str, Any]:
        """If this machine's disk now holds a system, make it that machine instead of an installer.

        The reported wish, in the words it was reported in: "it should replace the machine with
        the installed version after it's installed automatically". So the first start after the
        install finishes writes `boot: system` on the spec and drops the medium from the command
        line — no click, no waiting for somebody to notice that an iso field is now a lie.

        It is a decision, not a guess: the disk file crossing `INSTALLED_BYTES` is what makes it,
        and every surface says so afterwards (`installed` on the row, the sentence the tab shows,
        and the note the start returns). A disk that is wiped goes back to being an empty machine
        with a medium — because this reads the file each time rather than remembering an answer.
        """
        m = self._m()
        folder = m.machine_dir(self.home, spec.name)
        try:
            if str(spec.boot or "") or not m.is_installed(spec, folder):
                return {"adopted": False}
        except Exception:  # noqa: BLE001 — a disk nobody can stat is not a reason to refuse a start
            return {"adopted": False}
        spec.boot = "system"
        m.save_spec(self.home, spec)
        return {"adopted": True, "disk_bytes": m.disk_written_bytes(folder),
                "note": "its disk holds an installed system, so it starts on that — the "
                        "installer medium is detached, and asking for the installer again "
                        "attaches it back"}

    def _start_qemu(self, spec) -> Dict[str, Any]:
        """Boot an ISO or a disk. A job when there is an image to fetch, because a
        distro image or an ISO is hundreds of megabytes and a request is not a place
        to wait for that."""
        m = self._m()
        q = self.qemu()
        if not q.get("installed"):
            return {"ok": False, "error": "QEMU is not installed on this machine",
                    "needs_setup": m.qemu_needs_setup()}
        mdir = m.machine_dir(self.home, spec.name)
        if m.qemu_running(mdir):
            return {"ok": True, "already": True, "state": "running"}
        image = dict(spec.image or {})
        needs_fetch = bool(image.get("url")) and not image.get("path")
        if not needs_fetch:
            res = m.qemu_start(spec, mdir)
            if res.get("ok"):
                self._remember(spec.name, "started", report=res.get("report") or "")
            else:
                self._remember(spec.name, "start failed", error=res.get("error") or "")
            return res
        kind = f"machine-{spec.name}"
        if _job_running(kind):
            return {"ok": True, "already": True, "state": "provisioning",
                    "note": f"{spec.name} is already being prepared — watch /jobs for it"}
        jid = _job_spawn(kind, f"Preparing {spec.name}", lambda j: self._provision_qemu(spec, j))
        self._remember(spec.name, "provisioning", job=jid)
        return {"ok": True, "job": jid, "watch": f"/jobs/{jid}", "state": "provisioning"}

    def _provision_qemu(self, spec, jid: str) -> Dict[str, Any]:
        """Fetch the image, make the disk, boot. In that order, because a guest with
        an empty drive and no CD-ROM is a machine that boots to nothing."""
        m = self._m()
        mdir = m.machine_dir(self.home, spec.name)
        mdir.mkdir(parents=True, exist_ok=True)

        got = m.qemu_prepare_image(spec, mdir, fetch=(lambda u, d: self._fetch(u, d, jid)),
                                   log=lambda line: _job_add(jid, line))
        if not got.get("ok"):
            raise RuntimeError(got.get("error") or "the image could not be prepared")
        if got.get("fetched"):
            _job_add(jid, f"image is ready: {Path(got['path']).name}")
        # prepare() puts the local path on the spec in memory -- and only here is that
        # written down. Without this save every later start re-reads a spec that still
        # points at a URL, decides there is something to fetch, and runs this whole job
        # again: measured on a real machine, pressing Start on a finished ISO machine
        # re-provisioned it ("image already here, not fetching it twice") instead of just
        # booting it, and a screen move -- which is a stop and a start -- landed in the
        # middle of that job instead of restarting the guest. The spec keeping its URL is
        # deliberate (a machine rebuilt from it on a machine with no network still finds
        # the file beside it); what it gains is the path, so nobody fetches twice.
        m.save_spec(self.home, spec)

        image = dict(spec.image or {})
        own_disk = not (str(image.get("kind")) == "disk" and image.get("path"))
        if own_disk:
            _job_add(jid, f"making a {spec.disk_gb} GB disk")
            made = m.qemu_create_disk(spec, mdir)
            if not made.get("ok"):
                raise RuntimeError(made.get("error") or "the disk could not be created")

        _job_add(jid, m.qemu_describe(spec, mdir))
        res = m.qemu_start(spec, mdir, log=lambda line: _job_add(jid, line))
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or f"{spec.name} would not start")
        self._remember(spec.name, "started", report=res.get("report") or "")
        return {"state": res.get("state", "running"), **res}

    def _remember(self, name: str, what: str, **extra) -> None:
        with self._lock:
            self._last[name] = {"op": what, "at": datetime.datetime.now().isoformat(timespec="seconds"),
                                **extra}

    def _provision(self, spec, jid: str) -> Dict[str, Any]:
        """Fetch the image if it is not here, import it as a distro, then boot it.

        The image is the one thing a machine needs that can come from anywhere:
        a URL in a preset, or a file the person already has — which is how you end
        up with a Debian, an Arch, or anything else that ships a WSL rootfs
        instead of the Ubuntu default.
        """
        m = self._m()
        mdir = m.machine_dir(self.home, spec.name)
        mdir.mkdir(parents=True, exist_ok=True)
        img = spec.image or {}
        local = str(img.get("path") or "")
        url = str(img.get("url") or "")

        if local:
            rootfs = Path(str(local).replace("\\", "/"))
            if not rootfs.is_file():
                raise RuntimeError(f"the image this machine points at is not there: {rootfs}")
            _job_add(jid, f"using the image already on disk: {rootfs}")
        elif url:
            # An image becomes the system this machine boots: it comes over TLS or it
            # does not come, and its local name is *derived* — never taken — from the URL.
            problem = m.image_url_problem(url)
            if problem:
                raise RuntimeError(problem)
            rootfs = mdir / "image" / m.image_filename(url, "rootfs.tar.gz")
            if rootfs.is_file() and rootfs.stat().st_size > 64 * 1024 * 1024:
                _job_add(jid, f"image already fetched ({rootfs.stat().st_size // (1 << 20)} MB), "
                              f"not fetching it twice")
            else:
                _job_add(jid, f"fetching {url}")
                self._fetch(url, rootfs, jid)
        else:
            raise RuntimeError("this machine has no image: give it a rootfs URL or a file")

        _job_add(jid, f"importing {spec.distro} — this is the slow part")
        _job_progress(jid, 0, 1, "importing")
        res = m.wsl_import(spec.distro, rootfs, mdir / "disk")
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "the import failed")
        _job_add(jid, f"{spec.distro} created ({res.get('via')})")

        boot = self._boot(spec, jid)
        _job_progress(jid, 1, 1, "ready")
        return {"distro": spec.distro, "rootfs": str(rootfs), **boot}

    def _fetch(self, url: str, dest: Path, jid: str) -> None:
        """Download an image with progress, because it is hundreds of megabytes.

        This is the door as well as the route that calls it: an image comes over TLS
        or not at all (`image_url_problem`), and `urlopen` would happily read `file://`.
        """
        problem = self._m().image_url_problem(url)
        if problem:
            raise RuntimeError(problem)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        req = Request(url, headers={"User-Agent": "athena"})
        with urlopen(req, timeout=60) as r, open(part, "wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            last = 0.0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 0.6:
                    last = now
                    _job_progress(jid, got, total or got,
                                  f"downloading {got // (1 << 20)} MB"
                                  + (f" of {total // (1 << 20)} MB" if total else ""))
        part.replace(dest)
        _job_add(jid, f"fetched {got // (1 << 20)} MB to {dest}")

    def _boot(self, spec, jid: Optional[str] = None) -> Dict[str, Any]:
        """Start the distro and ask it what it is, so 'running' means something."""
        m = self._m()
        # `grep`, not `. /etc/os-release`: sourcing a file whose last line does not
        # succeed makes the whole chain return non-zero, and `&&` then eats the
        # echo — which is exactly how this printed nothing while the file clearly
        # said "Ubuntu 24.04 LTS".
        probe = m.wsl_exec(
            spec.distro,
            "uname -srm; grep -m1 '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '\"'",
            timeout=240)
        if jid:
            for line in (probe.get("output") or "").splitlines():
                if line.strip():
                    _job_add(jid, f"  {line.strip()}")
        if not probe.get("ok"):
            self._remember(spec.name, "start failed", error=probe.get("error") or "")
            return {"ok": False, "state": "stopped",
                    "error": probe.get("error") or f"{spec.distro} would not start"}
        self._remember(spec.name, "started")
        return {"ok": True, "state": "running", "distro": spec.distro,
                "report": (probe.get("output") or "").strip()}

    def _arm(self, out: Dict[str, Any], name: str, watched: bool) -> Dict[str, Any]:
        """Put a guest the agent started on the idle clock, and say so in the answer.

        Only when it is actually up. A start that answered `provisioning` is a job that
        will still be importing a rootfs minutes from now — arming it puts a key on the
        clock for a guest that does not exist yet, and the first reaper tick finds nothing
        running and forgets it, which is a watch that silently watches nothing. A first
        start that has to provision is therefore deliberately *not* armed, and that gap is
        named in `docs/threat-model.md` rather than papered over.

        The answer carries `idle`, because "this will be stopped once you stop using it"
        is not something a user should have to find in a log afterwards.
        """
        if watched and out.get("ok") and out.get("state") == "running":
            _idle_note(_idle_machine_key(name), what="an agent started this guest", watch=True)
            # On the same branch as the clock, not beside it: both are answers to "an
            # agent is using a computer you cannot see", and a start that raised the
            # screen without putting the guest on the clock would be the leak this
            # feature exists to close, with an audience.
            _broadcast_screen_wanted(name, "an agent started this guest")
            out["idle"] = {"watched": True, "window_seconds": _idle_window()}
        return out

    def stop(self, name: str) -> Dict[str, Any]:
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        if spec.provider == "qemu":
            res = m.qemu_stop(m.machine_dir(self.home, name))
            if res.get("ok"):
                self._remember(name, "stopped", how=res.get("how") or "")
            return res
        if spec.attach:
            # Terminating a distro somebody else started would take their shells
            # with it, so an attached machine is left alone.
            return {"ok": False, "error": f"{name} runs in a distro you own ({spec.distro}); "
                                          f"Athena will not shut that down for you"}
        res = m.wsl_terminate(spec.distro)
        if res.get("ok"):
            self._remember(name, "stopped")
        return {**res, "state": "stopped" if res.get("ok") else self.state(spec)}

    def exec_(self, name: str, command: str, user: str = "", cwd: str = "",
              timeout: int = 300) -> Dict[str, Any]:
        """Run a command in a machine. This is the whole point of having one."""
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        if not str(command or "").strip():
            return {"ok": False, "error": "no command to run"}
        if spec.provider == "qemu":
            # Not a shrug: a QEMU guest has no channel Athena can type into — it has
            # an SSH port, forwarded by `hostfwd`, and a console. Saying which is the
            # difference between a wall and a door.
            port = int((spec.ssh or {}).get("port") or 0)
            via = (f"it forwards port {port} to the guest's 22, so `ssh -p {port} "
                   f"user@127.0.0.1` works once the guest has an sshd`" if port else
                   "it has no forwarded port, so add one to the machine's ssh setting "
                   "first")
            return {"ok": False, "error": f"commands cannot be run inside {name} the way they "
                                          f"can in a WSL machine — a QEMU guest has no shell "
                                          f"channel, only a console and its network; {via}"}
        if spec.provider != "wsl":
            return {"ok": False, "error": f"commands cannot be run inside a {spec.provider!r} "
                                          f"machine"}
        if self.state(spec) == "missing":
            return {"ok": False, "error": f"{name} has no distro yet — start it first",
                    "needs_setup": m.wsl_needs_setup(spec)}
        res = m.wsl_exec(spec.distro, command, user=user, cwd=cwd, timeout=max(1, min(3600, int(timeout))),
                         owned=not spec.attach)
        self._remember(name, "ran a command", command=command, ok=bool(res.get("ok")))
        return res

    def snapshot(self, name: str) -> Dict[str, Any]:
        """A distro export: a real snapshot, as a tarball rather than a hypervisor."""
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        if spec.provider == "qemu":
            mdir = m.machine_dir(self.home, spec.name)
            if m.qemu_running(mdir):
                return {"ok": False, "error": f"stop {name} first — its disk is being written "
                                              f"to right now, and a copy of that is not a snapshot"}
            kind = f"machine-snapshot-{name}"
            if _job_running(kind):
                return {"ok": True, "already": True}
            jid = _job_spawn(kind, f"Snapshotting {name}",
                             lambda j: _job_verdict(j, m.qemu_snapshot(
                                 spec, mdir, mdir / "exports",
                                 log=lambda line: _job_add(j, line)), "snapshot"))
            return {"ok": True, "job": jid, "watch": f"/jobs/{jid}"}
        if spec.provider != "wsl":
            return {"ok": False, "error": f"snapshots of a {spec.provider!r} machine are not "
                                          f"something this knows how to take"}
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = m.machine_dir(self.home, spec.name) / "exports" / f"{spec.distro}-{stamp}.tar"
        kind = f"machine-snapshot-{name}"
        if _job_running(kind):
            return {"ok": True, "already": True}
        jid = _job_spawn(kind, f"Snapshotting {name}",
                         lambda j: _job_verdict(j, {**m.wsl_export(spec.distro, dest),
                                                    "note": "import it back with wsl --import, or "
                                                            "restore from the panel once that "
                                                            "exists"}, "snapshot"))
        return {"ok": True, "job": jid, "watch": f"/jobs/{jid}"}

    def forgotten(self) -> Dict[str, Any]:
        """Machines Athena was asked to forget, and what each one still has on disk."""
        m = self._m()
        rows = m.forgotten_machines(self.home)
        return {"ok": True, "machines": rows, "count": len(rows),
                "folder": str(m.forgotten_dir(self.home))}

    def restore(self, name: str) -> Dict[str, Any]:
        """Bring a forgotten machine back into the list, whole — disk and all."""
        m = self._m()
        out = m.restore_machine(self.home, name)
        if out.get("ok"):
            out["note"] = f"{name} is back in the list, with the disk it was forgotten with."
            out["machine"] = self.row(m.load_spec(self.home, name)) if \
                m.load_spec(self.home, name) is not None else None
        return out

    def delete(self, name: str, unregister: bool = False) -> Dict[str, Any]:
        """Forget a machine, or destroy it — two buttons, two different things done.

        The panel offers "Forget it (keeps the distro)" and "Destroy it and its disk", and
        for a QEMU machine this used to do the same destructive thing twice: both paths
        removed the folder, and the disk file was in the folder, so a button that promised
        to keep your distro deleted it. Forgetting is now what it says — the machine's
        folder moves to `forgotten/<name>`, the disk stays in it, Athena stops listing the
        machine, and `restore()` (or the panel) brings the whole thing back. `unregister`
        is the destructive half and is the only path that removes files.
        """
        m = self._m()
        spec = m.load_spec(self.home, name)
        if spec is None:
            return {"ok": False, "error": f"no machine called {name!r}"}
        removed = {"unregistered": False}
        if unregister and spec.attach:
            return {"ok": False, "error": f"{name} is attached to your own distro ({spec.distro}); "
                                          f"Athena will not delete it"}
        folder = m.machine_dir(self.home, name)

        if not unregister:
            # Reversible: move it out of the list and leave everything else alone. A WSL
            # machine's distro is not in this folder at all — it is registered with WSL —
            # so for one of those, forget is how you stop managing a distro that stays.
            kept = None
            if spec.provider == "qemu":
                m.qemu_stop(folder)
                dest = m.forgotten_machine_dir(self.home, name)
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        return {"ok": False, "error": f"{name} has already been forgotten once; "
                                                      f"bring that one back first"}
                    shutil.move(str(folder), str(dest))
                    kept = str(dest)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": f"it could not be put aside: {e}"}
            else:
                shutil.rmtree(folder, ignore_errors=True)
            with self._lock:
                self._last.pop(name, None)
            disk = 0
            if kept:
                try:
                    disk = m.disk_written_bytes(Path(kept))
                except Exception:  # noqa: BLE001
                    disk = 0
            return {"ok": True, "name": name, "forgotten": True, "kept_at": kept,
                    "disk_bytes": disk, "folder_removed": not folder.exists(),
                    "note": (f"{name} is no longer in the list. Its disk is at {kept} "
                             f"({disk / 2**30:.2f} GiB) and can be brought back."
                             if kept else
                             f"{name} is no longer in the list; its distro stays registered "
                             f"with WSL, and Athena will not stop or delete it.")}

        if spec.provider == "wsl" and not spec.attach:
            res = m.wsl_unregister(spec.distro)
            if not res.get("ok"):
                return {"ok": False, "error": res.get("error") or "could not unregister the distro",
                        **removed}
            removed["unregistered"] = True
        elif spec.provider == "qemu":
            m.qemu_stop(folder)
            removed["unregistered"] = True
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"the distro is gone but the folder would not delete: {e}",
                    **removed}
        with self._lock:
            self._last.pop(name, None)
        return {"ok": True, "name": name, "folder_removed": not folder.exists(), **removed}


machines_host = MachineHost()


# ------------------------------------------------------------------- routes

class MachineIn(BaseModel):
    """What the panel and the agent both send to make a machine.

    `image_kind` exists because a local file is ambiguous: a `.iso` and a WSL rootfs
    tarball are both "a path somebody typed". Guessing from the filename would be right
    most of the time, which is the worst kind of right.

    `cpus`/`memory_mb`/`disk_gb` default to **0, which means "size it for this
    computer"** — the fit comes from the host's own core count, RAM and free space, so
    a caller that does not care gets a machine that fits rather than one sized from
    somebody else's habits. A caller that does care says so, and the number is used.
    """
    name: str
    # Empty, not "wsl": the provider follows the *preset* unless somebody sets it by
    # hand, and a default of "wsl" here quietly overrode every preset that is QEMU
    # (`custom-iso`), which is the one case the field exists for.
    provider: str = ""
    preset: str = ""
    os: str = ""
    image_path: str = ""
    image_url: str = ""
    image_kind: str = ""
    distro: str = ""
    attach: bool = False
    auto: bool = False
    cpus: int = 0
    memory_mb: int = 0
    disk_gb: int = 0
    # Empty = this computer decides (`default_display()`): a machine somebody is going
    # to install into wants a window, and a headless host cannot open one.
    display: str = ""
    ports: List[Any] = Field(default_factory=list)
    vnc_port: int = 0
    ssh_port: int = 0
    notes: str = ""


class MachineExecIn(BaseModel):
    command: str
    user: str = ""
    cwd: str = ""
    timeout: int = 300


@router.get("/machines")
def machines_list() -> JSONResponse:
    try:
        return JSONResponse(machines_host.list())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e), "machines": []})


@router.get("/machines/setup")
def machines_setup() -> JSONResponse:
    try:
        return JSONResponse({"ok": True, **machines_host.setup()})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/machines/isos")
def machines_isos(folder: str = "") -> JSONResponse:
    """What is in the image folder. A read: it never makes the folder appear."""
    try:
        return JSONResponse(machines_host.isos(folder))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e), "isos": []})


class IsoDirIn(BaseModel):
    dir: str = ""
    create: bool = False


@router.post("/machines/isos")
def machines_set_iso_dir(body: IsoDirIn) -> JSONResponse:
    try:
        return JSONResponse(machines_host.set_iso_dir(body.dir, create=bool(body.create)))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/machines/folders")
def machines_folders(path: str = "") -> JSONResponse:
    """The folders inside one folder — so an image folder can be picked, not typed."""
    try:
        return JSONResponse(machines_host.folders(path))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e), "dirs": []})


@router.get("/machines/fit")
def machines_fit(provider: str = "qemu") -> JSONResponse:
    """The size this computer can give a guest, for a form that starts out right."""
    try:
        return JSONResponse(machines_host.fit(provider))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})


class SetupIn(BaseModel):
    what: str = "wsl"


@router.post("/machines/setup/install")
def machines_setup_install(body: SetupIn) -> JSONResponse:
    try:
        return JSONResponse(machines_host.install_needed(body.what))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/machines")
def machine_create(body: MachineIn) -> JSONResponse:
    m = athena_machines
    if m is None:
        return JSONResponse({"ok": False, "error": "the machines module did not load"})
    preset = m.PRESETS.get(body.preset) or {}
    provider = body.provider or preset.get("provider") or "wsl"
    # First use: a machine that is sized for the computer it is made on. `auto`, or a
    # zero in any of the three fields, means "choose for me" rather than "guess" —
    # the numbers that come back were computed from this host, not from habit.
    if body.auto or not (body.cpus and body.memory_mb and body.disk_gb):
        fit = machines_host.fit(provider)
        body.cpus = body.cpus or int(fit["cpus"])
        body.memory_mb = body.memory_mb or int(fit["memory_mb"])
        body.disk_gb = body.disk_gb or int(fit["disk_gb"])
    image = dict(preset.get("image") or {})
    # A preset knows what its own image is; anything typed by hand says so explicitly,
    # and only falls back to a sensible default per provider.
    kind = body.image_kind or image.get("kind") or ("iso" if provider == "qemu" else "rootfs")
    if body.image_path:
        image.update({"kind": kind, "path": body.image_path})
    if body.image_url:
        image.update({"kind": kind, "url": body.image_url})
    try:
        spec = m.MachineSpec(
            name=body.name, provider=provider,
            os=body.os or preset.get("os") or "", image=image, distro=body.distro,
            attach=body.attach, cpus=body.cpus, memory_mb=body.memory_mb, disk_gb=body.disk_gb,
            display=body.display, notes=body.notes,
            # A port pair may arrive as the text somebody typed ("8080:80") or as the
            # pair the panel parsed; both reach the same rule, and neither reaches the
            # spec unchecked. The spec then checks its own shape again — that is the
            # copy that matters, because the spec is what a `start` months later reads.
            ports=[m.parse_port_pair(p) for p in (body.ports or [])],
            # A machine whose screen goes in the panel needs a VNC display of its own:
            # QEMU's port for display N is 5900+N, and two machines sharing :0 would mean
            # one of them drawing into a screen nobody is watching.
            vnc_port=int(body.vnc_port or (m.pick_vnc_display(
                _vnc_displays_taken()) if (body.display or m.default_display()) != "window"
                else 0)),
            ssh={"port": int(body.ssh_port)} if body.ssh_port else {},
        )
        res = machines_host.create(spec)
    except m.SpecError as e:
        # 200 with ok:false, like every other operation here: the panel shows the
        # sentence, and "the request was understood, the operation failed" is what
        # actually happened.
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse(res)


@router.post("/machines/{name}/forget")
def machine_forget(name: str) -> JSONResponse:
    """Stop managing a machine, keeping what it has.

    A verb route and not a query string on the DELETE, because the panel's own verb helper
    sends POST: `act('/machines/X?unregister=1')` went to a DELETE-only route and came back
    405, so "Destroy it and its disk" never destroyed anything and the panel showed an error.
    Two outcomes, two routes, and a method the caller actually sends.
    """
    return JSONResponse(machines_host.delete(name, unregister=False))


@router.post("/machines/{name}/destroy")
def machine_destroy(name: str) -> JSONResponse:
    """Destroy a machine and its disk — the only path that removes files."""
    return JSONResponse(machines_host.delete(name, unregister=True))


@router.get("/machines/forgotten")
def machines_forgotten() -> JSONResponse:
    """The machines that were forgotten rather than destroyed, and what is still on disk.

    Declared before `/machines/{name}` on purpose: these routes match in the order they
    are written, so a literal path written after the parameterised one is never reached —
    and a request for the forgotten list would answer "no machine called 'forgotten'".
    """
    return JSONResponse(machines_host.forgotten())


@router.get("/machines/{name}")
def machine_detail(name: str) -> JSONResponse:
    m = athena_machines
    spec = m.load_spec(machines_host.home, name) if m else None
    if spec is None:
        return JSONResponse({"ok": False, "error": f"no machine called {name!r}"})
    return JSONResponse({"ok": True, "machine": machines_host.row(spec)})


@router.post("/machines/{name}/start")
def machine_start(name: str) -> JSONResponse:
    """Start a machine, or say why it cannot be started.

    This used to hand on whatever `start()` raised, which is a 500 and a traceback in the log for a
    spec somebody edited by hand. Everything this route can refuse is a fact about this machine, so
    it is answered as a sentence about the machine rather than as a server problem.
    """
    try:
        return JSONResponse(machines_host.start(name))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "machine": name,
                             "error": f"this machine could not be started: {e}"}, status_code=500)


def _b64(raw: bytes) -> str:
    """Bytes as the text an HTTP reply can carry. The panel turns it back into an image."""
    return base64.b64encode(raw).decode("ascii")


def _data_url(frame: Dict[str, Any]) -> str:
    """A frame's bytes as a data URL the panel can put straight into an `img`.

    The mime travels with the picture rather than being assumed: the live one is a JPEG and the
    one-off reader hands back a PNG, and a data URL that lies about its type is a picture that
    does not draw.
    """
    return ("data:" + str(frame.get("mime") or "image/jpeg") + ";base64,"
            + _b64(frame["image"]))

def _now() -> str:
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _vnc_displays_taken() -> List[int]:
    """Every VNC display number the machines on this computer already claim."""
    m = athena_machines
    out: List[int] = []
    if not m:
        return out
    try:
        for spec in m.list_specs(machines_host.home):
            out.append(int(spec.vnc_port or 0))
    except Exception:  # noqa: BLE001 — a listing that fails must not stop a create
        return out
    return out


@router.get("/machines/{name}/console")
def machine_console(name: str, limit: int = 8000) -> JSONResponse:
    """What the guest has said so far. A GET because reading is all it does, and a
    poll every couple of seconds while a machine boots is the point of it."""
    try:
        return JSONResponse(machines_host.console(name, limit))
    except Exception as e:  # noqa: BLE001 — the panel gets a sentence, not a 500
        return JSONResponse({"ok": False, "error": str(e), "text": ""})


class DisplayBody(BaseModel):
    """Where a machine's screen should live: `panel` or `window`."""
    display: str = ""


class MachineEditIn(BaseModel):
    """A settings change: any subset of what `machines.PATCHABLE` allows.

    Both spellings are accepted on purpose -- `{"changes": {"cpus": 6}}` and a flat
    `{"cpus": 6}` -- because the form that will send this is built from the same field names,
    and a wrapper is the kind of thing that gets forgotten on one side and turns a whole
    settings form into a 422 nobody can read. Which fields are legal is answered in one place,
    `machines.apply_patch`, which refuses anything else by name.
    """
    changes: Dict[str, Any] = {}
    model_config = {"extra": "allow"}

    def wanted(self) -> Dict[str, Any]:
        top = {k: v for k, v in (self.model_extra or {}).items()}
        return {**top, **dict(self.changes or {})}


@router.patch("/machines/{name}")
def machine_edit(name: str, body: MachineEditIn) -> JSONResponse:
    """Change a machine's cores, memory, disk, screen, boot order, image, ports or notes."""
    try:
        return JSONResponse(machines_host.edit(name, body.wanted()))
    except Exception as e:  # noqa: BLE001 — the panel gets a sentence, not a 500
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/machines/{name}/display")
def machine_display(name: str, body: DisplayBody) -> JSONResponse:
    """Move a screen between the panel and a window. Pop out, and come back again."""
    try:
        return JSONResponse(machines_host.set_display(name, body.display))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})


class ScreenInput(BaseModel):
    """One batch of mouse or keyboard input, as the panel sends it."""
    events: List[Dict[str, Any]] = []


class BootBody(BaseModel):
    """What a machine should start on next.

    The panel posts `boot`; `where` is kept because that was this route's first
    spelling and an already-loaded panel still sends it. Reading
    `body.boot or body.where` means neither name is a silent no-op — which is what
    made the "change what it boots" button fail on every press, blaming a value
    nobody typed.
    """
    boot: str = ""
    where: str = ""


@router.post("/machines/{name}/boot")
def machine_boot(name: str, body: BootBody) -> JSONResponse:
    """What this machine starts on: its own system, or the medium attached to it."""
    try:
        return JSONResponse(machines_host.set_boot(name, body.boot or body.where))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/machines/{name}/screen")
def machine_screen(name: str, save: int = 0) -> JSONResponse:
    """The guest's screen right now. A GET because reading is all it does, and the panel
    asks for it again every second while a machine is doing something worth watching."""
    try:
        return JSONResponse(machines_host.screen(name, save=bool(save)))
    except Exception as e:  # noqa: BLE001 — the panel gets a sentence, not a 500
        return JSONResponse({"ok": False, "error": str(e), "image": ""})


@router.post("/machines/{name}/screen/input")
def machine_screen_input(name: str, body: ScreenInput) -> JSONResponse:
    """Mouse and keyboard, from the panel to the guest. Without this the panel would be a
    picture of a machine rather than a machine.

    This door is the person's: `who` is left at its default because everything arriving here
    came from a hand on the panel, and a person's input is never refused.
    """
    try:
        return JSONResponse(machines_host.screen_input(name, body.events))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/machines/{name}/lease")
def machine_lease(name: str) -> JSONResponse:
    """Who is driving this screen: nobody, the person at the panel, or an agent.

    The panel polls this beside the frame so the Screen tab can say which it is — the state
    a person needs before they start typing into what they think is an idle installer.
    """
    try:
        return JSONResponse(machines_host.screen_lease(name))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/machines/{name}/lease/release")
def machine_lease_release(name: str) -> JSONResponse:
    """Hand the keyboard back. The person's own control, so it asks nothing of them."""
    try:
        return JSONResponse(machines_host.screen_lease(name, release=True))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/machines/{name}/stop")
def machine_stop(name: str) -> JSONResponse:
    return JSONResponse(machines_host.stop(name))


@router.post("/machines/{name}/exec")
def machine_exec(name: str, body: MachineExecIn) -> JSONResponse:
    return JSONResponse(machines_host.exec_(name, body.command, user=body.user,
                                            cwd=body.cwd, timeout=body.timeout))


@router.post("/machines/{name}/snapshot")
def machine_snapshot(name: str) -> JSONResponse:
    return JSONResponse(machines_host.snapshot(name))


@router.delete("/machines/{name}")
def machine_delete(name: str, unregister: bool = False) -> JSONResponse:
    return JSONResponse(machines_host.delete(name, unregister=unregister))


@router.post("/machines/forgotten/{name}/restore")
def machine_restore(name: str) -> JSONResponse:
    return JSONResponse(machines_host.restore(name))



# ------------------------------------------------------- import / migration

def _copy_tree(src: Path, dst: Path, on_bytes=None) -> int:
    """Copy a tree file by file, reporting bytes as they land.

    `shutil.copytree` reports nothing until it is finished, so copying an app
    that holds tens of gigabytes leaves a progress bar sitting at zero and then
    jumping to done. Walking it by hand costs nothing extra and gives the panel a
    number to draw.

    Links inside the tree are recreated as links. Following a junction would
    copy its target's contents into a duplicate of something the app only meant
    to point at — and an app linking to its own folder would recurse forever.
    """
    src = Path(src)
    total = 0
    for root, dirs, files in os.walk(src):
        out = dst / Path(root).relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        for d in list(dirs):
            sp = Path(root) / d
            try:
                if os.lstat(sp).st_reparse_tag in (0xa0000003, 0xa000000c):
                    dp = out / d
                    if not os.path.lexists(dp):
                        os.symlink(os.readlink(sp), dp, target_is_directory=True)
                    dirs.remove(d)
            except (OSError, AttributeError):
                pass
        for f in files:
            sp = Path(root) / f
            try:
                shutil.copy2(sp, out / f)
                size = sp.stat().st_size
                total += size
                if on_bytes:
                    on_bytes(size)
            except OSError:
                # A file held open by a running app is not a reason to abandon
                # the whole copy; the app can be run and repaired afterwards.
                pass
    return total


# `_app_port` is defined once, further down, beside the other socket helpers.
# A second definition lived here and was shadowed by it — same source, different
# answer for "no port" (None vs 0), so editing this one changed nothing.


def _app_url(app_id: str) -> Optional[str]:
    """The live URL of an app's web UI, or None when it is not serving one."""
    try:
        for rows in _runtime().running(app_id).values():
            for r in rows:
                if r.get("url"):
                    return r["url"]
    except Exception:  # noqa: BLE001
        return None
    return None


_APPCALL = None


def _appcall_module():
    """The capability module, loaded once — optional, the plugin works without it."""
    global _APPCALL
    if _APPCALL is not None:
        return _APPCALL or None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "athena_appcall", str(_DASH_DIR / "appcall.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["athena_appcall"] = mod
        spec.loader.exec_module(mod)
        _APPCALL = mod
    except Exception:  # noqa: BLE001
        _APPCALL = False
    return _APPCALL or None


def _write_install_guide(app_id: str) -> None:
    """Leave a manifest and a tutorial beside an app the moment it installs.

    This is what makes a new app self-explaining to the agent — it can be asked
    what it is, what it will be called, and how to run it, without anyone reading
    the app's own repository first. The capabilities are deliberately left for
    the first ask: this does not start the app, because putting a model on the
    GPU the second something finishes installing is not a favour.
    """
    ac = _appcall_module()
    if ac is None:
        return
    try:
        folder = _app_folder(app_id)
        if folder is None:
            return
        ac.static_manifest(engine.home, folder)
    except Exception:  # noqa: BLE001
        pass


# An app id is a directory NAME, never a path. `.` and `..` are directories, not
# names — and `engine.home / "api" / ".."` is the Athena home itself, so a bare
# `.is_dir()` test said "yes, that is an app" for the whole install (every app,
# every model cache, settings.json) and the uninstall route deletes what it
# resolves. Nothing here is resolved before the id is known to be a name.
_APP_ID_BAD = re.compile(r'[\\/:*?"<>|\x00]')


def _app_id_ok(app_id: str) -> bool:
    """Whether a string can name an app folder at all."""
    s = str(app_id or "")
    if not s or s in (".", ".."):
        return False
    return not _APP_ID_BAD.search(s)


def _app_dir(app_id: str) -> Optional[Path]:
    """The `api/` entry an id would name, or None if it cannot name one.

    Through `_athena_home()`, which is the one accessor for where Athena lives: reaching for
    `engine.home` directly here is how this resolver ended up disagreeing with the others about
    which home it was looking in.
    """
    if not _app_id_ok(app_id):
        return None
    return _athena_home() / "api" / str(app_id)


def _direct_child(path: Path, parent: Path) -> bool:
    """Whether `path` really is one level inside `parent`.

    A second lock on the same door: resolving collapses `..`, so even an id that
    slipped past the name check cannot be handed a folder outside `api/`.
    """
    try:
        return path.resolve().parent == parent.resolve()
    except Exception:  # noqa: BLE001
        return False


def _app_folder_candidates(base: Path) -> tuple:
    """The spellings one app id may name on disk, in the order to try.

    Apps live under their store name, which carries a `.git` suffix, while everything that
    *reports* an app (the panel, the jobs, an agent's tool call) may use the bare name. One
    list, because three resolvers each carried their own version of it and they had drifted:
    `_app_folder_for` tried two spellings out of three, so a publish or an update asked for
    `foo` while the folder on disk was `foo.git` and was told the app was not installed.
    """
    return (base, base.with_name(base.name + ".git"),
            base.with_name(base.name.replace(".git", "")))


def _app_folder(app_id: str) -> Optional[Path]:
    """The app's folder on disk, accepting the id with or without `.git`."""
    base = _app_dir(app_id)
    if base is None:
        return None
    for cand in _app_folder_candidates(base):
        if cand.is_dir() and _direct_child(cand, base.parent):
            return cand
    return None


def _activation_scripts(venv: Path) -> List[Path]:
    """The scripts a virtualenv ships to put itself on PATH."""
    out: List[Path] = []
    for sub in ("Scripts", "bin"):
        d = venv / sub
        if not d.is_dir():
            continue
        for sp in sorted(d.iterdir()):
            if sp.is_file() and sp.name.lower().startswith("activate"):
                out.append(sp)
    return out


def _venv_self_paths(text: str, name: str) -> List[str]:
    """Every path a virtualenv's own scripts give for that environment.

    Matched by the path, not by the assignment that carries it. virtualenv puts
    the environment's path in a `for %%i in ("<path>")` loop and then writes
    `set "VIRTUAL_ENV=%%~fi"`; uv writes `set VIRTUAL_ENV=<path>` outright; the
    POSIX scripts write `VIRTUAL_ENV="/c/..."`. Only the path itself is in all
    three, so matching `VIRTUAL_ENV=` at the start of a line sees one of them
    and misses the rest — which is exactly how an imported app ends up
    activating a folder that is not there.
    """
    pat = re.compile(r"""([A-Za-z]:[\\/][^"'\r\n)]*?[\\/]""" + re.escape(name)
                     + r""")(?=["'\r\n)\s\\/]|$)""")
    return sorted({m.group(1) for m in pat.finditer(text or "")})


def _venv_paths_for(venv: Path) -> tuple:
    """This environment's own path, in the two shapes a script may use."""
    real = str(venv).rstrip("\\/")
    posix = ("/" + real[0].lower() + real[2:].replace("\\", "/")).rstrip("/")
    return real, posix


def _stale_activations(venv: Path) -> List[Path]:
    """Activation scripts that still name the install this one was copied from.

    Reported by health and rewritten by the retarget — one predicate, so the
    check can never pass while the repair has something left to do.
    """
    real, posix = _venv_paths_for(venv)
    stale: List[Path] = []
    for sp in _activation_scripts(venv):
        try:
            s = sp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(r.rstrip("\\/") not in (real, posix)
               for r in _venv_self_paths(s, venv.name)):
            stale.append(sp)
    return stale


def _point_activations_at(venv: Path) -> List[str]:
    """Rewrite a virtualenv's activation scripts to name this install."""
    real, posix = _venv_paths_for(venv)
    done: List[str] = []
    for sp in _activation_scripts(venv):
        try:
            s = sp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        new = s
        for ref in _venv_self_paths(s, venv.name):
            if ref.rstrip("\\/") in (real, posix):
                continue
            new = new.replace(ref, real if ref[1:2] == ":" else posix)
        if new != s:
            sp.write_text(new, encoding="utf-8")
            done.append(sp.name)
    return done


def _retarget_venv(app_dir: Path, home: Path) -> List[str]:
    """Point a copied app's virtualenvs at this install, not the one they came from.

    A virtualenv is not relocatable and copying one does not adapt it. `pyvenv.cfg`
    records the base interpreter it was built against, and `Scripts/activate*`
    hardcodes the environment's own absolute path — so an app imported from
    another install activates *that* install's environment. When the other install
    is missing, activation fails quietly and the app runs on the base interpreter,
    where none of its packages exist: RMBG died on `No module named 'cv2'` while
    its own python imported cv2 4.11.0 without complaint.

    Returns the paths that were rewritten, for the job log.
    """
    base = home / "bin" / "miniforge"
    fixed: List[str] = []
    for cfg in app_dir.rglob("pyvenv.cfg"):
        if ".git" in cfg.parts:
            continue
        venv = cfg.parent
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        old_home = ""
        for line in text.splitlines():
            if line.lower().lstrip().startswith("home"):
                old_home = line.split("=", 1)[-1].strip()
                break
        if base.is_dir() and old_home and Path(old_home) != base:
            # A function replacement: the new path is a Windows path, and
            # re.sub reads backslashes in a replacement string as escapes.
            cfg.write_text(re.sub(r"(?im)^home\s*=.*$", lambda _m: f"home = {base}", text),
                           encoding="utf-8")
            fixed.append(f"{venv.name}/pyvenv.cfg")

        for name in _point_activations_at(venv):
            fixed.append(f"{venv.name}/Scripts/{name}")
    return fixed


# ---------------------------------------------------------------- health/fix
# A broken app should be repairable where it stands. Sending someone back to the
# store to re-install in order to fix a path pointing at the wrong folder is not
# a repair, it is a re-download — and for an app of tens of gigabytes, an absurd
# one. Fix verifies the pieces that actually break, repairs the ones with a
# deterministic repair, and only runs the app's own install script when the
# environment is genuinely unprovisioned.

# Below this many entries in site-packages, an environment is a shell: the
# interpreter is there but nothing it needs was ever installed into it.
PACKAGE_FLOOR = 12


def _venv_python(venv: Path) -> Optional[Path]:
    for cand in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if cand.exists():
            return cand
    return None


def _venv_smoke(py: Path) -> Optional[str]:
    """Run an environment's own interpreter. Returns None when it works.

    The only honest test of a repaired environment: a venv whose files all look
    right can still resolve to nothing if its base interpreter is gone.
    """
    try:
        p = subprocess.run([str(py), "-c", "import sys; print(sys.prefix)"],
                           capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:  # noqa: BLE001
        return f"cannot run ({e})"
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "exited nonzero").strip().splitlines()
        return tail[-1][:240] if tail else "exited nonzero"
    return None


def _git_out(cwd: Path, *args: str) -> Optional[str]:
    """Run git without ever letting it stop to ask a question."""
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                           text=True, timeout=300,
                           env=athena_engine.child_env(NON_INTERACTIVE_GIT_ENV))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _app_venvs(folder: Path) -> List[Path]:
    out: List[Path] = []
    for cfg in folder.rglob("pyvenv.cfg"):
        if ".git" not in cfg.parts:
            out.append(cfg.parent)
    return out


def _app_health(app_id: str, deep: bool = False) -> Dict[str, Any]:
    """Check the pieces of an app that actually break, and say what can be fixed.

    Every check here corresponds to a real failure seen in this plugin: a
    virtualenv still bound to the install it was copied from, an uninitialized
    submodule, a cache environment written before the shared store existed, a
    checkout that cloned nothing. `deep` also starts each environment's
    interpreter, which is too slow to do on a route the UI polls.
    """
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, fixable: bool = False,
            needs_install: bool = False, informational: bool = False,
            next_step: Optional[Dict[str, Any]] = None) -> None:
        """`informational` is for facts worth showing that are not breakage.

        A check that cannot be true of a healthy app would make every app look
        broken, which is worse than saying nothing.

        `next_step` is the same idea one level up: a failing check that has a
        known answer carries it, so the answer reaches whoever reads the check
        instead of having to be worked out again from the detail text.
        """
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "fixable": bool(fixable), "needs_install": bool(needs_install),
                       "informational": bool(informational), "next_step": next_step})

    folder = _app_folder(app_id)
    if not folder:
        add("App folder", False, "no folder for this app")
        return {"ok": False, "app": app_id, "checks": checks,
                "needs_install": False, "fixable": False}

    launcher = athena_naming.launcher_of(folder)
    add("Manifest", bool(launcher),
        f"{launcher.name} present" if launcher
        else f"{athena_naming.LAUNCHER} is missing — this folder is not an app")

    code = folder / "app"
    files = [p.name for p in code.iterdir() if p.name != ".git"] if code.is_dir() else []
    add("Code", bool(files),
        f"app/ has {len(files)} entr{'y' if len(files) == 1 else 'ies'}" if files
        else "app/ is missing or empty — the repository never cloned",
        needs_install=not files)

    # A submodule that was never initialized leaves the app looking complete
    # while the code it needs is an empty directory.
    if (code / ".gitmodules").is_file():
        st = _git_out(code, "submodule", "status")
        missing = [l for l in (st or "").splitlines() if l.startswith("-")]
        add("Submodules", not missing,
            "all initialized" if not missing else f"{len(missing)} not initialized",
            fixable=bool(missing))
    else:
        add("Submodules", True, "none")

    venvs = _app_venvs(folder)
    if not venvs:
        add("Environment", True, "none required")
    else:
        stale: List[str] = []
        for venv in venvs:
            cfg = venv / "pyvenv.cfg"
            try:
                text = cfg.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if str(engine.home / "bin" / "miniforge") not in text:
                stale.append(f"{venv.name}/pyvenv.cfg")
            for sp in _stale_activations(venv):
                stale.append(f"{venv.name}/Scripts/{sp.name}")
        add("Environment paths", not stale,
            "point at this install" if not stale
            else f"{len(stale)} file(s) still point at another install",
            fixable=bool(stale))

        empty = [v.name for v in venvs
                 if not (v / "Lib" / "site-packages").is_dir()
                 or len(list((v / "Lib" / "site-packages").iterdir())) < PACKAGE_FLOOR]
        if empty:
            add("Packages", False,
                f"{', '.join(empty)}: nothing installed yet", needs_install=True)
        elif deep:
            broken: List[str] = []
            for venv in venvs:
                py = _venv_python(venv)
                if py is None:
                    broken.append(f"{venv.name}: no interpreter")
                    continue
                err = _venv_smoke(py)
                if err:
                    broken.append(f"{venv.name}: {err}")
            add("Interpreter", not broken,
                "runs" if not broken else "; ".join(broken)[:240],
                needs_install=bool(broken))
        else:
            add("Packages", True, f"{len(venvs)} environment(s)")

    if _load_settings().get("share_caches", True):
        # Compared against the same targets the repair writes, per key. A
        # substring search over the file would be wrong: the engine writes these
        # with forward slashes, so a correct ENVIRONMENT does not contain the
        # native Windows path, and the check would report a healthy app broken.
        have = _parse_env_file(folder / "ENVIRONMENT")
        targets = _shared_targets(engine.home)
        missing = [k for k, v in targets.items() if have.get(k) != v]
        add("Shared caches", not missing,
            f"all {len(targets)} keys point at the shared store" if not missing
            else f"{len(missing)} of {len(targets)} cache key(s) still private "
                 f"({', '.join(missing[:3])}…) — this app may download its own "
                 f"copy of models it already has",
            fixable=True)

    # Whether the install action has ever run *in this copy*. Reported, never
    # auto-run: an imported app whose install ran in the install it came from
    # looks identical to one that was never provisioned, and re-running a build
    # over a working environment is how a repair breaks something. It is here so
    # that a launch failing on a missing module has a visible explanation instead
    # of looking like a broken app.
    if (folder / "install.js").is_file():
        logdir = engine.home / "logs" / "shell" / "cleaned" / "api" / folder.name
        never = not (logdir.is_dir()
                     and any(p.name.startswith("install.js") for p in logdir.iterdir()))
        add("Install history", not never,
            "the install action has run in this copy" if not never
            else "the install action has never run in this copy — if a launch "
                 "fails on a missing module, run Install on this card",
            informational=True)

    add("Toolchain", (engine.home / "bin" / "miniforge").is_dir(),
        "Miniforge present" if (engine.home / "bin" / "miniforge").is_dir()
        else "Miniforge is missing — apps cannot run until it is installed")

    # Can the app do the thing it exists for? A ComfyUI app with no reachable
    # weights is structurally perfect and completely useless, and on a machine
    # whose weights live outside the app that is the *normal* state of a fresh
    # install — so the verdict, and the step that ends it, is the most
    # actionable fact about the app and belongs where the other answers are.
    # Informational, because nothing is broken: the app is fine, it has no
    # models, and `next_step` says what to do about that. Directory walks only —
    # this route is polled, so nothing here may start an interpreter.
    ac = _appcall_module()
    if ac is not None:
        try:
            if ac.detect_kind(folder) == "comfyui":
                models = ac._comfy_models(folder)
                add("Weights", bool(models.get("ready")),
                    (f"{models.get('count')} weight file(s) reachable"
                     if models.get("ready") else str(models.get("note") or "")),
                    informational=True, next_step=models.get("next_step") or None)
        except Exception:  # noqa: BLE001
            pass

    # Why it last stopped, when the log says. `informational` on purpose: a log is
    # a record, not a diagnosis — an app that failed an hour ago and has run fine
    # since is not broken now, and marking it so would make the panel cry wolf. It
    # is a fact worth showing, next to the action that answers it.
    failure: Optional[Dict[str, Any]] = None
    try:
        failure = last_failure(app_id)
    except Exception:  # noqa: BLE001
        failure = None
    if failure is not None:
        add("Last failure", False, str(failure.get("why") or ""),
            informational=True, next_step=failure.get("fix") or None)

    # What the app will ask *you* for, next to whether it is broken. Kept
    # informational: an app that wants a key you have not given it is not damaged,
    # and a check that fails on a perfectly working app is a check nobody reads.
    access = _keys_cached(folder.name)
    add("Access", True, _access_line(access), informational=True)

    return {"ok": all(c["ok"] for c in checks if not c["informational"]),
            "app": app_id, "checks": checks, "failure": failure, "access": access,
            "needs_install": any(c["needs_install"] for c in checks),
            "fixable": any(c["fixable"] for c in checks)}


def _resolve_href(app_id: str, href: str) -> Optional[str]:
    """Turn a menu entry's href into an absolute script path inside the app.

    A menu href is written relative to the app, and it comes from the app's own
    file, so anything that climbs out of the folder is refused rather than run.
    """
    folder = _app_folder(app_id)
    if not folder or not href:
        return None
    try:
        cand = (folder / href).resolve()
        cand.relative_to(folder.resolve())
    except (ValueError, OSError):
        return None
    return str(cand)


# An app's menu is a function in its own launcher, and the engine calls it to
# render the app's page. Nothing returns that menu as data, so for an app with no
# default action Athena has to resolve it the same way — see menu.js.
MENU_TIMEOUT = 60.0


def _app_menu(app_id: str) -> Dict[str, Any]:
    """Every runnable entry an app offers, flattened, with the input each needs.

    An app can hide all its runnable scripts inside submenus and mark no default
    at the top level. Hunyuan3D-2-LowVRAM is one, and because its start.js builds
    `--profile {{args.profile}}` with no fallback, launching it bare genuinely
    cannot work. The engine cannot pick for it either, so "no runnable script"
    was the wrong thing to say — the honest answer is the list of choices.
    """
    folder = _app_folder(app_id)
    if not folder:
        return {"ok": False, "error": "app not found"}
    script = Path(__file__).with_name("menu.js")
    node = _find_node()
    if not node:
        return {"ok": False, "error": "no usable node runtime found"}
    if not script.exists():
        return {"ok": False, "error": "menu resolver missing"}

    # Answer the menu's own questions truthfully: whether each script is running
    # and where it is serving, so an app that branches on that resolves the same
    # way it does in the engine.
    running: Dict[str, bool] = {}
    urls: Dict[str, str] = {}
    try:
        for rows in _runtime().running(app_id).values():
            for r in rows:
                base = os.path.basename(str(r.get("uri") or ""))
                if base:
                    running[base] = True
                    if r.get("url"):
                        urls[base] = r["url"]
    except Exception:  # noqa: BLE001
        pass

    try:
        proc = subprocess.run(
            [node, str(script), str(folder),
             json.dumps({"running": running, "urls": urls})],
            capture_output=True, text=True, timeout=MENU_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "the app's menu took too long to resolve"}
    raw = (proc.stdout or "").strip().splitlines()
    if not raw:
        return {"ok": False, "error": (proc.stderr or "menu resolver said nothing")[:400]}
    try:
        data = json.loads(raw[-1])
    except ValueError:
        return {"ok": False, "error": "menu resolver returned no JSON"}
    if data.get("ok"):
        data["app"] = app_id
    return data


def _sizes_file() -> Path:
    d = engine.home / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "import-sizes.json"


def _load_sizes() -> Dict[str, Any]:
    try:
        return json.loads(_sizes_file().read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_sizes(data: Dict[str, Any]) -> None:
    p = _sizes_file()
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        tmp.replace(p)          # atomic: a half-written cache reads as empty
    except Exception:  # noqa: BLE001
        pass


def _dir_size(path: Path, budget: float = 90.0, cap: int = 4_000_000):
    """Walk a folder for its size, giving up after `budget` seconds.

    Returns (bytes, complete). An app folder holds hundreds of thousands of
    files, so this is minutes of work and there is no way to make it instant —
    the caller is expected to be a background job. `complete` is False when the
    walk ran out of time, so the number is shown as a floor rather than a fact.
    """
    total = 0
    seen = 0
    last_check = 0
    deadline = time.monotonic() + budget
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            seen += 1
                            if seen >= cap:
                                return total, False
                    except OSError:
                        continue
        except OSError:
            continue
        if seen - last_check >= 5000:
            last_check = seen
            if time.monotonic() > deadline:
                return total, False
    return total, True


def _source_apps():
    """The app folders in the user's own install, if there is one."""
    home = pinokio_home()
    if not home:
        return None, []
    api = Path(home) / "api"
    if not api.is_dir():
        return str(home), []
    return str(home), [d for d in sorted(api.iterdir(), key=lambda p: p.name.lower())
                       if d.is_dir()]


def _size_job(dirs: List[Path]) -> None:
    """Refresh cached sizes for app folders, one at a time, in the background."""
    if not dirs:
        return
    jid = _job_spawn("size", "Measuring apps", lambda j: _size_worker(j, dirs))
    return jid


def _size_worker(jid: str, dirs: List[Path]) -> Dict[str, Any]:
    cache = _load_sizes()
    for d in dirs:
        key = str(d)
        _job_add(jid, f"Measuring {d.name}…")
        size, complete = _dir_size(d)
        cache[key] = {"size": size, "complete": complete, "at": time.time()}
        _save_sizes(cache)
        _job_add(jid, f"  {d.name}: {size} bytes{' (partial)' if not complete else ''}")
    return {"measured": len(dirs)}


@router.get("/import/scan")
def import_scan() -> JSONResponse:
    """Find an existing Pinokio install and list its apps — without sizing them.

    Sizing is what made this feel broken: the first version measured every app
    before replying, which for a real install means walking hundreds of thousands
    of files and minutes of silence, during which the panel could only say it had
    found nothing. The list is returned immediately from cached sizes, and what
    is missing is measured in the background and picked up on the next poll.
    """
    home, dirs = _source_apps()
    athena_api = engine.home / "api"
    sizes = _load_sizes()

    found = []
    missing = []
    for d in dirs:
        cached = sizes.get(str(d))
        if cached is None:
            missing.append(d)
        # Pinokio's app folders carry a ".git" suffix and anything created here
        # does not, so both spellings are checked. Without that an app that had
        # already been imported still read as "not imported", and importing it
        # again failed with "already imported" — two wrong answers from one name.
        base = d.name[:-4] if d.name.lower().endswith(".git") else d.name
        found.append({
            "id": base,
            "folder": d.name,
            "path": str(d),
            "source": str(home),
            "size": (cached or {}).get("size"),
            "size_complete": (cached or {}).get("complete", False),
            "imported": _present(athena_api / d.name) or _present(athena_api / base),
            "has_manifest": athena_naming.has_launcher(d),
        })

    # Only one measuring run at a time: the panel polls this route, and starting
    # a fresh walk of 264 GB on every poll would be the original bug again.
    if missing and not _job_running("size"):
        _size_job(missing)

    known = [a["size"] for a in found if a["size"] is not None]
    return JSONResponse({
        "source_home": home,
        "apps": found,
        "count": len(found),
        "total_size": sum(known),
        "total_size_complete": len(known) == len(found) and bool(found),
        "sizing": bool(missing),
        "athena_home": str(engine.home),
    })


@router.post("/import/run")
def import_run(body: ImportIn) -> JSONResponse:
    """Link or copy the chosen apps. Returns a job — copying is not quick."""
    src_root = Path(body.source) / "api"
    dst_root = engine.home / "api"

    def work(jid: str) -> Dict[str, Any]:
        dst_root.mkdir(parents=True, exist_ok=True)
        sizes = _load_sizes()
        total_apps = len(body.apps)

        # Weigh every app before copying anything, so one bar can cover the whole
        # job instead of restarting at each app. The scan has usually measured
        # these already; only an unmeasured folder costs a walk.
        plan: List[Dict[str, Any]] = []
        for name in body.apps:
            src = src_root / name
            if not src.exists():
                for d in src_root.iterdir():
                    if d.name.replace(".git", "") == name:
                        src = d
                        break
            cached = (sizes.get(str(src)) or {}).get("size")
            plan.append({"name": name, "src": src,
                         "size": int(cached) if cached else 0})

        grand = sum(p["size"] for p in plan)
        done_bytes = [0]
        bars = [{"name": p["name"], "done": 0, "total": p["size"],
                 "state": "waiting"} for p in plan]
        last = [0.0]

        def _push(label: str, force: bool = False) -> None:
            # A copy of tens of gigabytes touches a lot of files; pushing a
            # progress update for each one would spend more time locking than
            # copying. Four updates a second is smoother than the eye can read.
            now = time.time()
            if not force and now - last[0] < 0.25:
                return
            last[0] = now
            _job_progress(jid, done_bytes[0], grand, label=label, apps=bars)

        results = []
        for i, p in enumerate(plan, 1):
            name, src = p["name"], p["src"]
            bar = bars[i - 1]
            if not src.exists():
                results.append({"app": name, "ok": False, "error": "not found"})
                _job_add(jid, f"[{i}/{total_apps}] {name}: not found")
                bar["state"] = "failed"
                _push(f"[{i}/{total_apps}] {name}", True)
                continue
            # A runnable app carries a launcher — that is what the engine
            # opens. A folder without one imports as an entry that can never
            # launch, so say so instead of producing a dead entry.
            if not athena_naming.has_launcher(src):
                results.append({"app": name, "ok": False,
                                "error": f"no {athena_naming.LAUNCHER} — not a runnable app"})
                _job_add(jid, f"[{i}/{total_apps}] {name}: skipped — no "
                              f"{athena_naming.LAUNCHER} launcher, so it is not runnable")
                bar["state"] = "skipped"
                _push(f"[{i}/{total_apps}] {name}", True)
                continue
            dst = dst_root / src.name
            verb = "linking" if body.mode == "adopt" else "copying"
            _job_add(jid, f"[{i}/{total_apps}] {name} ({verb})…")
            bar["state"] = "linking" if body.mode == "adopt" else "copying"
            _push(f"[{i}/{total_apps}] {name}", True)
            try:
                if _present(dst):
                    results.append({"app": name, "ok": False, "error": "already imported"})
                    _job_add(jid, "    already imported, skipped")
                    bar["state"] = "skipped"
                elif body.mode == "adopt":
                    _link_dir(src.resolve(), dst)
                    results.append({"app": name, "ok": True, "to": str(dst),
                                    "mode": "linked", "reused": str(src)})
                    _job_add(jid, f"    linked -> {dst}")
                    bar["state"] = "linked"
                    bar["done"] = bar["total"]
                else:
                    # A file-by-file copy reports bytes as they land; copytree
                    # reports nothing until it is finished, which for a 56 GB app
                    # is a bar frozen at zero for twenty minutes.
                    def _on(n, _b=bar, _i=i, _n=name):
                        _b["done"] += n
                        done_bytes[0] += n
                        _push(f"[{_i}/{total_apps}] {_n}")
                    _copy_tree(src, dst, on_bytes=_on)
                    results.append({"app": name, "ok": True, "to": str(dst),
                                    "mode": "copied"})
                    _job_add(jid, f"    copied -> {dst}")
                    bar["state"] = "copied"
                    # The estimate can be short if the folder grew since it was
                    # measured; never let the bar show more than complete.
                    bar["total"] = max(bar["total"], bar["done"])
                    if grand < done_bytes[0]:
                        grand = done_bytes[0]
                _push(f"[{i}/{total_apps}] {name}", True)
            except Exception as e:  # noqa: BLE001
                # One app failing must not abandon the rest of the queue.
                bar["state"] = "failed"
                results.append({"app": name, "ok": False, "error": str(e)})
                _job_add(jid, f"    failed: {e}")
                _push(f"[{i}/{total_apps}] {name}", True)
                continue

            # A copied app brings its virtualenvs with it, and a virtualenv is
            # not relocatable: it still points at the install it was built in and
            # activates *that* environment. Left alone, the app starts on the
            # wrong interpreter and every one of its packages is missing. An
            # adopted app is the original folder itself, whose venv is correct
            # where it stands, so it is left alone.
            if body.mode != "adopt":
                try:
                    touched = _retarget_venv(dst, engine.home)
                    if touched:
                        _job_add(jid, f"    {len(touched)} virtualenv file(s) "
                                      f"repointed at Athena: {', '.join(touched[:3])}")
                except Exception as e:  # noqa: BLE001
                    _job_add(jid, f"    venv retarget skipped: {e}")

            # Imported apps arrive pinning a folder-private model cache, which is
            # why two apps that want the same model each download their own.
            # Retarget them at the shared store now, on the way in. In adopt/link
            # mode `dst` is a junction onto the original, so this edits that
            # original's ENVIRONMENT too — say so, because it is a change to a
            # folder the user made outside Athena.
            if _load_settings().get("share_caches", True):
                try:
                    envf = _write_shared_env(dst, engine.home)
                    if envf["changed"]:
                        where = "the original" if body.mode == "adopt" else "the copy"
                        _job_add(jid, f"    {envf['changed']} cache setting(s) in "
                                      f"{where} now point at the shared store")
                except Exception as e:  # noqa: BLE001
                    _job_add(jid, f"    could not share caches: {e}")
        # Every app that landed goes through conversion: checked, repaired,
        # given its guide, and installed if its environment is empty. Its own
        # job per app, so the import finishes when it says it does and each
        # conversion is watchable on its own row.
        for r in results:
            if r.get("ok"):
                landed = str(r["app"])
                _job_spawn("convert", f"Converting {landed} to an Athena app",
                           lambda cjid, a=landed: _convert_app(a, cjid, provision=False))
        return {"results": results,
                "imported": sum(1 for r in results if r.get("ok")),
                "mode": body.mode}

    if not body.apps:
        return JSONResponse({"ok": False, "error": "no apps selected"}, status_code=400)
    jid = _job_spawn("import", f"Importing {len(body.apps)} apps", work)
    return JSONResponse({"ok": True, "job": jid, "count": len(body.apps),
                         "mode": body.mode})


# --------------------------------------------------------------- disk saver
#
# The engine ships a disk-usage feature — it calls it "Save Space" — that finds
# files which are byte-identical across your installs and replaces each duplicate
# with a hardlink to one stored copy, so every app still sees normal files while
# only one copy of the bytes occupies the disk. It walks your whole app folder,
# hashes candidates of 10 MiB or larger with SHA-256, groups them by content plus
# device and permission metadata, and keeps one representative per group.
#
# These routes drive the engine's own `/vault/action` rather than reimplementing
# the algorithm, so every safety rule it carries applies here unchanged: it never
# touches a file belonging to a running app, never crosses a filesystem
# (hardlinks cannot), re-verifies content and metadata immediately before
# replacing anything, and only ever frees a stored copy whose link count has
# dropped to one. Reimplementing that here would mean reimplementing the risk.
#
# `isSameOriginRequest` guards those endpoints, and it passes any request that
# carries no Origin header — which is exactly what a server-side caller sends.

def _fmt_bytes(n: Any) -> str:
    """A byte count a person can read."""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:,.0f} {unit}" if unit == "B" else f"{v:,.1f} {unit}"
        v /= 1024
    return f"{v:,.1f} TB"


def _disk_job(kind: str, label: str, body: Dict[str, Any]) -> JSONResponse:
    if _job_running(kind):
        return JSONResponse({"ok": False, "error": f"{label} is already running"},
                            status_code=409)
    jid = _job_spawn(kind, label, lambda j: _disk_worker(j, body, label))
    return JSONResponse({"ok": True, "job": jid, "action": body.get("action")})


def _disk_worker(jid: str, body: Dict[str, Any], label: str) -> Dict[str, Any]:
    """Run one engine disk action and translate the reply into readable lines."""
    _job_add(jid, f"{label}…")

    if not engine.probe():
        _job_add(jid, "Starting the engine…")
        started = engine.start(wait=120)
        if not started.get("ok"):
            msg = "the engine did not start"
            _job_add(jid, msg)
            _job_end(jid, "failed", error=msg)
            return {"ok": False}

    action = body.get("action")

    try:
        # Scanning answers immediately and works in the background; the mutating
        # actions block until they are finished. One endpoint, two shapes.
        data = engine.call("POST", "/vault/action", body=body, timeout=24 * 3600)
    except Exception as e:  # noqa: BLE001
        msg = f"the engine refused the request: {e}"
        _job_add(jid, msg)
        _job_end(jid, "failed", error=msg)
        return {"ok": False}

    if not isinstance(data, dict):
        msg = "the engine replied with something unexpected"
        _job_add(jid, msg)
        _job_end(jid, "failed", error=msg, result={"reply": str(data)[:300]})
        return {"ok": False}

    if data.get("error"):
        msg = str(data["error"])
        _job_add(jid, msg)
        _job_end(jid, "failed", error=msg)
        return {"ok": False}

    if action == "scan":
        return _watch_scan(jid)

    if action == "deduplicate":
        _job_add(jid, f"Linked {data.get('converted', 0):,} duplicate files.")
        _job_add(jid, f"Reclaimed {_fmt_bytes(data.get('bytes_saved'))}.")
        for key, why in (("locked", "left alone — the app is running"),
                         ("stale", "left alone — changed while scanning"),
                         ("incompatible", "left alone — cannot be hardlinked"),
                         ("unavailable", "left alone"),
                         ("failed", "failed")):
            n = data.get(key)
            if n:
                _job_add(jid, f"  {n:,} {why}")
    elif action == "reclaim_all":
        _job_add(jid, f"Freed {data.get('reclaimed', 0):,} unshared copies.")
        _job_add(jid, f"Space recovered: {_fmt_bytes(data.get('bytes_freed'))}.")
        if data.get("failed"):
            _job_add(jid, f"  {data['failed']:,} could not be freed")
    elif action in ("separate_all", "separate_files"):
        # The engine answers with its own file-action summary, and its key names
        # here are not the ones `deduplicate` uses. Log the counters it actually
        # sent rather than assuming a set of names and printing a confident zero.
        for key in ("separated", "converted", "files", "files_completed"):
            n = data.get(key)
            if isinstance(n, int) and n:
                _job_add(jid, f"Restored {n:,} files to independent copies.")
                break
        for key, why in (("locked", "left alone — the app is running"),
                         ("stale", "left alone — changed while scanning"),
                         ("failed", "failed")):
            n = data.get(key)
            if n:
                _job_add(jid, f"  {n:,} {why}")

    _job_add(jid, "Done.")
    _job_end(jid, "done", result=data)
    return data


def _watch_scan(jid: str) -> Dict[str, Any]:
    """Follow the engine's own scan progress until the phase settles."""
    deadline = time.time() + 6 * 3600
    last = ""
    while time.time() < deadline:
        try:
            st = engine.call("GET", "/info/dedup", query={"progress": 1}, timeout=60)
        except Exception as e:  # noqa: BLE001
            msg = f"lost contact with the engine: {e}"
            _job_add(jid, msg)
            _job_end(jid, "failed", error=msg)
            return {"ok": False}
        scan = (st or {}).get("scan") or {}
        line = (f"{scan.get('phase') or 'working'} — "
                f"{int(scan.get('files') or 0):,} files, "
                f"{_fmt_bytes(scan.get('bytes_total'))}")
        if line != last:
            _job_add(jid, line)
            last = line
        if not scan.get("active"):
            _job_add(jid, f"Scan finished: {scan.get('outcome') or scan.get('phase')}. "
                          "Nothing has changed yet — run Reclaim to replace "
                          "duplicates with links.")
            _job_end(jid, "done", result=scan)
            return scan
        time.sleep(2)
    _job_end(jid, "failed", error="the scan did not finish in time")
    return {"ok": False}


@router.get("/disk")
def disk_status() -> JSONResponse:
    """What the disk-usage feature knows right now, for the panel to render.

    The engine's `/vault` route renders an HTML page and carries no data, so the
    UI fetches `/info/dedup` instead — the same JSON status the engine's own
    interface polls. `progress=1` asks for just the moving parts.
    """
    if not engine.probe():
        return JSONResponse({"ok": False, "engine_running": False,
                             "reason": "the engine is not running"})
    try:
        data = engine.call("GET", "/info/dedup", timeout=120)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "engine_running": True, "reason": str(e)})
    if not isinstance(data, dict) or "enabled" not in data:
        return JSONResponse({"ok": False, "engine_running": True,
                             "reason": "the engine did not report on disk usage"})
    return JSONResponse({"ok": True, "engine_running": True,
                         "summary": _disk_summary(data), "disk": data})


def _disk_summary(d: Dict[str, Any]) -> Dict[str, Any]:
    """The handful of numbers the panel shows, pulled out of the engine's reply."""
    scan = d.get("scan") or {}
    last = d.get("last_scan") or {}
    return {
        "enabled": bool(d.get("enabled")),
        "mode": d.get("mode"),
        "scanning": bool(scan.get("active")),
        "phase": scan.get("phase"),
        "files": scan.get("files"),
        "dirs": scan.get("dirs"),
        "scanned_bytes": scan.get("bytes_total"),
        "logical_bytes": d.get("logical_bytes"),
        "bytes_on_disk": d.get("bytes_on_disk"),
        "saved_by_sharing": d.get("saved_by_sharing"),
        "reclaimable": d.get("reclaimable"),
        "pending_bytes": d.get("pending_bytes"),
        "candidate_min_bytes": d.get("candidate_min_bytes"),
        "last_outcome": last.get("outcome"),
        "last_scan": last.get("finished") or last.get("started"),
        "duplicate_items": len(d.get("items") or []),
    }


@router.post("/disk/scan")
def disk_scan() -> JSONResponse:
    """Look for duplicated files. Read-only — it changes nothing."""
    return _disk_job("scan", "Looking for duplicate files", {"action": "scan"})


@router.post("/disk/dedupe")
def disk_dedupe() -> JSONResponse:
    """Replace every duplicate with a hardlink to a single stored copy."""
    return _disk_job("dedupe", "Replacing duplicates with links",
                     {"action": "deduplicate"})


@router.post("/disk/free")
def disk_free() -> JSONResponse:
    """Free stored copies that nothing links to any more."""
    return _disk_job("free", "Freeing unshared copies", {"action": "reclaim_all"})


class SeparateBody(BaseModel):
    """The files to restore, when this is undoing one specific selection."""

    paths: List[str]


@router.post("/disk/separate")
def disk_separate() -> JSONResponse:
    """The reverse of Reclaim: give every shared file back its own copy.

    Costs disk space and buys independence — nothing an app does to its files can
    reach another app any more. Reclaim has always been a one-way door; this is
    the way back, and it mostly exists so that the reclaim button is not the only
    irreversible thing in the panel.

    Safe with apps running: the engine leaves a file belonging to a running app
    alone, exactly as it does when reclaiming.
    """
    return _disk_job("separate", "Restoring independent copies",
                     {"action": "separate_all", "view": "shared"})


@router.post("/disk/separate-files")
def disk_separate_files(body: SeparateBody) -> JSONResponse:
    """Restore just the listed files, for undoing part of a reclaim."""
    if not body.paths:
        return JSONResponse({"ok": False, "error": "no files were chosen"},
                            status_code=400)
    return _disk_job("separate", "Restoring independent copies",
                     {"action": "separate_files", "paths": list(body.paths)})


# --------------------------------------------------------------- shared store
#
# Apps arrive with their own idea of where caches live, and the engine's default
# ENVIRONMENT template gives each app a folder-private copy: `HF_HOME=./cache/HF_HOME`
# resolves against the app's own directory. Two apps that use the same model
# therefore download it twice, and the same wheel, the same tokenizer and the same
# torch hub bundle land on disk once per app.
#
# The engine documents the fix itself, on the HF_HOME line of its own template:
# "delete this line ... without redundancy". Only values starting `./` are
# resolved, so an absolute path passes through untouched and a blank value drops
# the key entirely — both of which send the app to the one shared location
# `<home>/cache/<KEY>`, the same place the root ENVIRONMENT already names.
#
# This makes that explicit and enforced rather than a thing to remember: every app
# is pointed at the shared tree, and anything already downloaded into a private
# cache is moved into it. Nothing is deleted and nothing is overwritten — a name
# that already exists in the shared copy wins, and the duplicate is left for the
# disk-usage pass, which can prove the two are identical before touching either.
#
# Config and state are deliberately not shared. Two apps that both write
# `XDG_CONFIG_HOME/<something>/settings.json` would tread on each other, and a
# wrong config is worse than a duplicate file. Caches only.

# The keys the engine's own template manages, each mapped to the shared
# `<home>/cache/<KEY>` the root ENVIRONMENT already points at.
SHARED_KEYS = {
    "HF_HOME": "HF_HOME",              # models, tokenizers, datasets — the big one
    "TORCH_HOME": "TORCH_HOME",        # torch hub bundles
    "XDG_CACHE_HOME": "XDG_CACHE_HOME",
    "UV_CACHE_DIR": "UV_CACHE_DIR",    # uv downloads
    "PIP_CACHE_DIR": "PIP_CACHE_DIR",  # pip wheels
}

# Keys apps set for themselves, which fragment the model store if left private.
# Each is redirected under the shared HF_HOME, matching where HuggingFace's own
# libraries put them when HF_HOME governs.
SHARED_HF_SUBKEYS = {
    "HF_HUB_CACHE": "hub",
    "HUGGINGFACE_HUB_CACHE": "hub",
    "HF_ASSETS_CACHE": "assets",
    "TRANSFORMERS_CACHE": "transformers",
    "DIFFUSERS_CACHE": "diffusers",
}


def _shared_targets(home: Path) -> Dict[str, str]:
    """Where each shareable key points once everything is unified."""
    out = {k: str(home / "cache" / sub).replace("\\", "/")
           for k, sub in SHARED_KEYS.items()}
    for k, sub in SHARED_HF_SUBKEYS.items():
        out[k] = str(home / "cache" / "HF_HOME" / sub).replace("\\", "/")
    return out


def _parse_env_file(path: Path) -> Dict[str, str]:
    """Read KEY=VALUE lines, ignoring comments and blanks."""
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


def _app_private_dirs(home: Path, app_dir: Path) -> Dict[str, Path]:
    """Private cache directories an app's ENVIRONMENT sends it to.

    A `./` value resolves against the app folder, which is the duplication. An
    absolute value is the app already pointing somewhere shared, so there is
    nothing to move.
    """
    env = _parse_env_file(app_dir / "ENVIRONMENT")
    found: Dict[str, Path] = {}
    for key in list(SHARED_KEYS) + list(SHARED_HF_SUBKEYS):
        val = env.get(key)
        if not val or not val.startswith("./"):
            continue
        target = (app_dir / val[2:]).resolve()
        try:
            target.relative_to(app_dir.resolve())
        except ValueError:
            continue
        if target.is_dir():
            found[key] = target
    return found


def _merge_into(src: Path, dst: Path) -> Dict[str, int]:
    """Move everything under `src` into `dst`, never overwriting.

    A move is a rename on the same volume and a copy across volumes, so the cheap
    one is tried first. A name already present in `dst` is left where it is —
    deciding whether two files sharing a name are the same file is the disk-usage
    pass's job, and it can actually prove it.
    """
    stats = {"moved": 0, "bytes": 0, "kept": 0}
    dst.mkdir(parents=True, exist_ok=True)
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        out_dir = dst / rel
        for name in files:
            s = Path(root) / name
            d = out_dir / name
            try:
                if d.exists():
                    stats["kept"] += 1
                    continue
                out_dir.mkdir(parents=True, exist_ok=True)
                size = s.stat().st_size
                try:
                    os.replace(s, d)
                except OSError:
                    shutil.copy2(s, d)
                    s.unlink()
                stats["moved"] += 1
                stats["bytes"] += size
            except Exception:  # noqa: BLE001
                stats["kept"] += 1
    return stats


def _write_shared_env(app_dir: Path, home: Path) -> Dict[str, Any]:
    """Point one app's shareable keys at the shared tree.

    Existing lines are rewritten in place so an app's ENVIRONMENT keeps its shape
    and its comments; a key the app never mentioned is appended.
    """
    targets = _shared_targets(home)
    path = app_dir / "ENVIRONMENT"
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = []
    seen = set()
    out = []
    changed = 0
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.partition("=")[0].strip()
            if key in targets:
                seen.add(key)
                if s != f"{key}={targets[key]}":
                    out.append(f"{key}={targets[key]}")
                    changed += 1
                    continue
        out.append(line)
    for key in sorted(targets):
        if key not in seen:
            out.append(f"{key}={targets[key]}")
            changed += 1
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return {"changed": changed, "path": str(path)}


def _sz(path: Path) -> int:
    """Bytes only. `_dir_size` also reports whether its walk finished."""
    return _dir_size(path)[0]


def _shared_apps(home: Path) -> List[Path]:
    api = home / "api"
    if not api.is_dir():
        return []
    return sorted(d for d in api.iterdir()
                  if d.is_dir() and athena_naming.has_launcher(d))


# One walk per poll interval, not one per poll. The panel asks `/shared` every 20 seconds, and a
# fresh answer walks every app's private folders to add them up — so the poll was paying for a disk
# scan it could not have needed, on a schedule nobody chose. The answer is cached for about one
# interval; anything that moves files forgets it, and `?fresh=true` insists.
_SHARED_CACHE: Dict[str, Any] = {}
_SHARED_CACHE_AT: float = 0.0
SHARED_CACHE_TTL = 20.0


def forget_shared() -> None:
    """Drop the cached `/shared` answer, after anything that moves files around."""
    global _SHARED_CACHE, _SHARED_CACHE_AT
    _SHARED_CACHE, _SHARED_CACHE_AT = {}, 0.0


def _shared_report(home: Path) -> Dict[str, Any]:
    """The walk itself, with no cache in the way — what the route answers and what it keeps."""
    cache = home / "cache"

    apps = []
    private_total = 0
    for d in _shared_apps(home):
        priv = _app_private_dirs(home, d)
        size = sum(_sz(p) for p in priv.values())
        private_total += size
        apps.append({
            "app": d.name[:-4] if d.name.lower().endswith(".git") else d.name,
            "folder": d.name,
            "private": [{"key": k, "path": str(p), "size": _sz(p)}
                        for k, p in priv.items()],
            "private_bytes": size,
            "unified": not priv,
        })

    return {
        "shared_root": str(cache),
        "targets": _shared_targets(home),
        "shared_bytes": _sz(cache / "HF_HOME") + _sz(cache / "TORCH_HOME"),
        "apps": apps,
        "app_count": len(apps),
        "unified_count": sum(1 for a in apps if a["unified"]),
        "private_bytes": private_total,
        "keys": sorted(list(SHARED_KEYS) + list(SHARED_HF_SUBKEYS)),
        "sharing": bool(_load_settings().get("share_caches", True)),
    }


@router.get("/shared")
def shared_status(fresh: bool = False) -> JSONResponse:
    """What could be shared, and what each app is currently keeping to itself.

    Cached for `SHARED_CACHE_TTL` seconds — see `_shared_report`, which does the walking. A
    report that is a few seconds old is the same report; a disk walk paid on a timer is not free.
    """
    global _SHARED_CACHE, _SHARED_CACHE_AT
    home = engine.home
    key = str(home)
    if (not fresh and _SHARED_CACHE.get("key") == key
            and (time.time() - _SHARED_CACHE_AT) < SHARED_CACHE_TTL):
        return JSONResponse({**_SHARED_CACHE["body"], "cached": True})
    body = _shared_report(home)
    _SHARED_CACHE = {"key": key, "body": body}
    _SHARED_CACHE_AT = time.time()
    return JSONResponse({**body, "cached": False})


@router.post("/shared/unify")
def shared_unify() -> JSONResponse:
    """Point every app at one shared cache tree, moving what is already there."""
    if _job_running("shared"):
        return JSONResponse({"ok": False, "error": "a share pass is already running"},
                            status_code=409)
    home = engine.home

    def work(jid: str) -> Dict[str, Any]:
        results = []
        moved_total = 0
        apps = _shared_apps(home)
        if not apps:
            _job_add(jid, "No apps installed yet.")
        targets = _shared_targets(home)
        for i, app_dir in enumerate(apps, 1):
            name = app_dir.name[:-4] if app_dir.name.lower().endswith(".git") else app_dir.name
            _job_add(jid, f"[{i}/{len(apps)}] {name}")
            try:
                priv = _app_private_dirs(home, app_dir)
                moved = 0
                for key, src in priv.items():
                    st = _merge_into(src, Path(targets[key]))
                    moved += st["bytes"]
                    if st["moved"]:
                        _job_add(jid, f"    {key}: moved {st['moved']:,} files "
                                      f"({_fmt_bytes(st['bytes'])}) into the shared copy")
                    if st["kept"]:
                        _job_add(jid, f"    {key}: {st['kept']:,} already there, left "
                                      "alone — the disk pass can prove those identical")
                    try:
                        if src.is_dir() and not any(src.iterdir()):
                            src.rmdir()
                    except Exception:  # noqa: BLE001
                        pass
                env = _write_shared_env(app_dir, home)
                moved_total += moved
                _job_add(jid, f"    pointed {env['changed']} setting(s) at the shared copy")
                results.append({"app": name, "ok": True, "moved_bytes": moved,
                                "keys": list(priv)})
            except Exception as e:  # noqa: BLE001
                _job_add(jid, f"    failed: {e}")
                results.append({"app": name, "ok": False, "error": str(e)})
        _job_add(jid, f"Done — {_fmt_bytes(moved_total)} folded into the shared store.")
        # The cached report was a picture of the disk *before* this pass, and the panel asks for a
        # new one the moment the job ends. Without this it would be handed the old numbers and
        # draw an app as still keeping its own copy of something that just moved.
        forget_shared()
        return {"results": results, "moved_bytes": moved_total, "apps": len(apps),
                "unified": sum(1 for r in results if r.get("ok"))}

    jid = _job_spawn("shared", "Sharing caches between apps", work)
    return JSONResponse({"ok": True, "job": jid})


# ---------------------------------------------------------------- schedules
# An app that should run again tomorrow does not need a model to say so: the schedule
# is Hermes' own cron, driven by a script that calls the app directly — no agent turn,
# so a daily run costs the electricity and nothing else, and it wakes nobody up.


_CRON: Any = None


def _cron_jobs() -> Optional[Any]:
    """Hermes' own cron, through Hermes' own command line. None where there is none.

    It used to `import cron.jobs` and walk `sys.path` for it — a plugin reaching into
    another package's private module. `engine.HermesCron` drives `hermes cron
    create/list/remove` instead.
    """
    global _CRON
    if _CRON is None:
        try:
            _CRON = athena_engine.HermesCron()
        except Exception:  # noqa: BLE001
            _CRON = False
    return _CRON or None


def _schedules_dir() -> Path:
    d = engine.home / "schedules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _schedules_index() -> List[Dict[str, Any]]:
    p = _schedules_dir() / "index.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    rows = data.get("schedules") if isinstance(data, dict) else data
    return [r for r in (rows or []) if isinstance(r, dict)]


def _write_schedules_index(rows: List[Dict[str, Any]]) -> None:
    p = _schedules_dir() / "index.json"
    tmp = p.with_name("index.json.tmp")
    tmp.write_text(json.dumps({"schedules": rows, "updated": time.time()}, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)


def _schedule_runs(app_id: str, capability: str) -> Dict[str, Any]:
    """How many times a schedule has actually run, and when it last did.

    Read back from the recipes the runs wrote, because that is the same record
    the Runs tab shows: a schedule that says "3 runs" while Runs shows two has
    one of them lying.
    """
    root = engine.home / "runs"
    n, last = 0, None
    try:
        for rec in root.glob(f"{app_id}*/*/recipe.json"):
            try:
                data = json.loads(rec.read_text(encoding="utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
            if capability and (data.get("capability") or "") != capability:
                continue
            n += 1
            m = rec.stat().st_mtime
            last = m if last is None or m > last else last
    except Exception:  # noqa: BLE001
        return {"runs": n, "last": None}
    return {"runs": n, "last": last}


def _planned_call(app_id: str, capability: str, inputs: Any,
                  then: Optional[List[Dict[str, Any]]] = None):
    """Everything the call route refuses, refused here too — before a job or a
    schedule exists. Returns `(error_dict, status, ctx)`.

    Deliberately a second copy of the call route's checks rather than a shared
    helper: those checks are interleaved with that route's job spawn, and a
    schedule must be refused for the same reasons as a call, so this asks the
    same questions in the same order and says the same sentences.
    """
    folder = _app_folder(app_id)
    if folder is None:
        return ({"ok": False, "error": f"no installed app called {app_id}"}, 404, None)
    ac = _appcall_module()
    if ac is None:
        return ({"ok": False,
                 "error": "the capability probe is unavailable in this build"}, 500, None)
    wanted = str(capability or "").strip()
    if not wanted:
        return ({"ok": False, "error": "name the capability to run"}, 400, None)
    man = ac.load(engine.home, folder)
    if not man or not (man.get("capabilities") or []):
        return ({"ok": False, "app": app_id,
                 "error": ("athena has no list of what this app can do yet — it is "
                           "written the first time the app is asked, and the app has "
                           "to be running for that")}, 400, None)
    cap = next((c for c in man["capabilities"]
                if wanted in (c.get("name"), c.get("intent"))), None)
    if cap is None:
        return ({"ok": False, "app": app_id,
                 "error": f"{app_id} has no capability called {wanted!r}",
                 "available": [c.get("intent") for c in man["capabilities"]]}, 400, None)
    raw = inputs if inputs is not None else []
    if not isinstance(raw, list):
        raw = [raw]
    required = [i for i in (cap.get("inputs") or []) if i.get("required")]
    if len(raw) < len(required):
        return ({"ok": False, "app": app_id, "capability": cap.get("name"),
                 "intent": cap.get("intent"),
                 "error": (f"{cap.get('name')} needs {len(required)} input(s) — pass "
                           "them in order, local file paths for the ones that take a "
                           "file"),
                 "inputs": [{"name": i.get("name"), "component": i.get("component"),
                             "takes_a_local_file": i.get("accepts_local_path")}
                            for i in required]}, 400, None)
    stages, stage_err = _then_stages(then)
    if stage_err:
        return ({"ok": False, "app": app_id, "error": stage_err}, 400, None)
    if stages:
        chain_err = _chain_check(ac, cap, stages)
        if chain_err:
            return (chain_err, 400, None)
    return (None, 200, {"folder": folder, "capability": str(cap.get("name")),
                        "intent": cap.get("intent"), "inputs": raw, "stages": stages})


def _script_present(p: Any) -> bool:
    """Whether the script this schedule runs is still where it was written."""
    try:
        return Path(str(p)).is_file()
    except Exception:  # noqa: BLE001
        return False


def run_one(app_id: str, capability: str, inputs: Optional[List[Any]] = None,
            timeout: float = 900.0, stages: Optional[List[Dict[str, Any]]] = None,
            ) -> Dict[str, Any]:
    """Run one capability (or a whole chain) with no job behind it.

    This is what a scheduled run calls. The job system belongs to the panel — a
    scheduler's log is its own stdout, which cron keeps — so the job id here is a
    label that goes nowhere, and the record a person reads afterwards is the
    recipe the call writes beside its outputs, exactly as for a hand-started run.
    """
    folder = _app_folder(app_id)
    if folder is None:
        return {"ok": False, "error": f"no installed app called {app_id}"}
    jid = f"schedule-{app_id}"
    try:
        if stages:
            return _run_pipeline(jid, app_id, folder, capability, list(inputs or []),
                                 list(stages), timeout=float(timeout or 900))
        return _run_capability(jid, app_id, folder, capability, list(inputs or []),
                               timeout=float(timeout or 900))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _schedule_script(app_id: str, capability: str, inputs: List[Any],
                     timeout: float, stages: Optional[List[Dict[str, Any]]]) -> str:
    """The text of the script a schedule runs.

    It imports the plugin's own API and calls the same code a hand-started run
    calls, so a scheduled run is not a second implementation of anything — if the
    two ever diverged, the scheduled one would be the one nobody watches.
    """
    api = str(_DASH_DIR / "plugin_api.py")
    return (
        "#!/usr/bin/env python\n"
        '"""One scheduled run, written by Athena and run by Hermes\' own cron.\n\n'
        "A script and not an agent turn on purpose: an app that runs every morning\n"
        "must not cost a model call to start. Run this by hand and it does exactly\n"
        "what the schedule does, which is how a schedule is tested.\n"
        '"""\n'
        "import importlib.util\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        f"API = Path({api!r})\n"
        'spec = importlib.util.spec_from_file_location("athena_plugin_api", str(API))\n'
        "mod = importlib.util.module_from_spec(spec)\n"
        'sys.modules["athena_plugin_api"] = mod\n'
        "spec.loader.exec_module(mod)\n\n"
        f"res = mod.run_one({app_id!r}, {capability!r}, {list(inputs or [])!r},\n"
        f"                   timeout={float(timeout or 900)!r}, stages={stages!r})\n"
        "files = res.get(\"files\") or []\n"
        "if res.get(\"ok\"):\n"
        f"    print({app_id!r} + \" wrote \" + str(len(files)) + \" file(s)\")\n"
        "    for f in files:\n"
        "        print(\"  \" + str(f))\n"
        "else:\n"
        f"    print({app_id!r} + \" did not finish: \" + str(res.get(\"error\")"
        " or \"no reason given\"))\n"
        "    sys.exit(1)\n"
    )


class ScheduleBody(BaseModel):
    """A repeat: which capability, with what, and how often.

    `schedule` is Hermes' own cron syntax, read by the same parser that will fire the
    job, so a phrase Hermes cannot read is refused here in Hermes' own words.
    """
    capability: Optional[str] = None
    inputs: Optional[List[Any]] = None
    schedule: Optional[str] = None
    name: Optional[str] = None
    timeout: Optional[float] = None
    then: Optional[List[Dict[str, Any]]] = None


@router.post("/apps/{app_id}/schedule")
def app_schedule(app_id: str, body: ScheduleBody) -> JSONResponse:
    """Arrange for one of an app's capabilities to run again, on its own.

    Refused inside a scheduled run unless `cron.allow_agent_scheduling` is on: a run
    that arranges more runs multiplies.
    """
    blocked = athena_engine.scheduling_blocked()
    if blocked:
        return JSONResponse({"ok": False, "app": app_id, "error": blocked},
                            status_code=403)
    err, status, ctx = _planned_call(app_id, (body.capability or ""),
                                     body.inputs, body.then)
    if err:
        return JSONResponse(err, status_code=status)
    when = str(body.schedule or "").strip()
    if not when:
        return JSONResponse({"ok": False, "app": app_id,
                             "error": ("say when it should run — \"every day at 8am\", "
                                       "\"every 6 hours\", or a cron expression"),
                             "examples": ["every day at 8am", "every 6 hours",
                                          "0 7 * * 1"]}, status_code=400)
    cj = _cron_jobs()
    if cj is None:
        return JSONResponse({"ok": False, "app": app_id,
                             "error": ("this build has no scheduled jobs — "
                                       "athena cannot find Hermes' cron store")},
                            status_code=503)
    try:
        cj.parse_schedule(when)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "app": app_id, "schedule": when,
                             "error": f"Hermes cannot read {when!r}: {str(e)[:180]}",
                             "examples": ["every day at 8am", "every 6 hours",
                                          "0 7 * * 1"]}, status_code=400)
    timeout = float(body.timeout or 900)
    text = _schedule_script(app_id, ctx["capability"], ctx["inputs"], timeout,
                            ctx["stages"])
    sid = f"{app_id}-{ctx['capability']}-{os.urandom(2).hex()}".replace("/", "_")
    path = _schedules_dir() / f"{sid}.py"
    path.write_text(text, encoding="utf-8")
    label = str(body.name or "").strip() or f"{app_id}: {ctx['intent'] or ctx['capability']}"
    try:
        rec = cj.create_job(prompt=None, schedule=when, name=label[:120],
                            script=str(path), no_agent=True,
                            interpreter=sys.executable, workdir=str(engine.home))
    except Exception as e:  # noqa: BLE001
        try:
            path.unlink()
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse({"ok": False, "app": app_id, "schedule": when,
                             "error": f"the job could not be stored: {str(e)[:200]}"},
                            status_code=500)
    row = {"id": sid, "app": app_id, "capability": ctx["capability"],
           "intent": ctx["intent"], "inputs": ctx["inputs"], "schedule": when,
           "name": label, "script": str(path),
           "job": rec.get("id") or rec.get("job_id"), "created": time.time(),
           "stages": ctx["stages"] or []}
    rows = _schedules_index()
    rows.append(row)
    _write_schedules_index(rows)
    return JSONResponse({"ok": True, "schedule": row,
                         "next_run": rec.get("next_run"),
                         "runs": _schedule_runs(app_id, ctx["capability"]),
                         "note": ("the run lands in Runs beside the ones you start by "
                                  "hand; it fires while Hermes is running")})


@router.get("/schedules")
def schedules_index(app: str = "") -> JSONResponse:
    """Every repeat Athena has arranged, and whether it is still going to fire.

    Read off the index *and* the cron store, because those are two different
    records: a job removed from the store while the index still lists it is a
    schedule that will never run, and saying "gone" is the only honest answer.
    """
    cj = _cron_jobs()
    wants = str(app or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    for row in _schedules_index():
        if wants and wants not in str(row.get("app") or "").lower():
            continue
        job = None
        if cj is not None and row.get("job"):
            try:
                job = cj.get_job(str(row["job"]))
            except Exception:  # noqa: BLE001
                job = None
        state = "gone" if job is None else ("paused" if job.get("paused") else "waiting")
        out = dict(row)
        out["state"] = state
        out["enabled"] = bool(job and job.get("paused") is not True)
        out["next_run"] = (job or {}).get("next_run")
        out["last_run"] = (job or {}).get("last_run")
        out["runs"] = _schedule_runs(str(row.get("app") or ""),
                                     str(row.get("capability") or ""))
        if state == "gone":
            out["note"] = ("the cron job is not in the store any more, so this will "
                           "not run — schedule it again")
        elif not _script_present(row.get("script")):
            out["note"] = "the script this schedule runs is missing, so it cannot fire"
        rows.append(out)
    return JSONResponse({"ok": True, "count": len(rows), "hermes_cron": cj is not None,
                         "note": ("scheduled runs fire while Hermes is running"),
                         "schedules": rows})


@router.delete("/schedules/{sid}")
def schedule_delete(sid: str) -> JSONResponse:
    """Stop a repeat: the job, the script, and the row that named them."""
    rows = _schedules_index()
    row = next((r for r in rows if str(r.get("id")) == sid), None)
    if row is None:
        return JSONResponse({"ok": False, "error": f"no schedule called {sid}"},
                            status_code=404)
    cj = _cron_jobs()
    removed = False
    if cj is not None and row.get("job"):
        try:
            removed = bool(cj.remove_job(str(row["job"])))
        except Exception:  # noqa: BLE001
            removed = False
    gone = True
    try:
        Path(str(row.get("script"))).unlink()
    except Exception:  # noqa: BLE001
        gone = False
    _write_schedules_index([r for r in rows if str(r.get("id")) != sid])
    return JSONResponse({"ok": True, "id": sid, "job_removed": removed,
                         "script_removed": gone,
                         "note": ("it will not run again" if (removed or not row.get("job"))
                                  else "the cron job was already gone")})


@router.post("/apps/{app_id}/{action}")
def app_action(app_id: str, action: str, body: Optional[ActionBody] = None,
               origin: str = "ui") -> JSONResponse:
    """Run one of the app's own scripts, through Athena's runtime client.

    `origin` decides whether the idle window applies to this app at all. Only
    Hermes's own launch ("agent", passed by the agent tools) arms it: an app the
    user started is theirs to close, and nothing Athena does should stop it
    underneath them. The UI path is the default, and it is the safe one — an app
    that is never armed is never reaped.
    """
    if action == "stop":
        try:
            out = _runtime().stop(app_id)
            idle = _idle_module()
            if idle is not None and out.get("ok"):
                idle.forget(app_id)  # stopped on purpose: not idle, just gone
            return JSONResponse(out)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    if action not in APP_SCRIPTS:
        return JSONResponse({"ok": False, "error": f"unknown action {action}"}, status_code=400)
    if not engine.probe():
        started = engine.start(wait=90)
        if not started.get("ok"):
            return JSONResponse({"ok": False, "error": "engine did not start",
                                 "detail": started}, status_code=502)
    try:
        seed_credentials(engine.home)
    except Exception:  # noqa: BLE001
        pass
    try:
        # An action can name the exact menu entry to run, with the values that
        # entry supplies. Without that, the app's own menu decides — not the
        # engine's opinion, because the engine caches a resolved action: after a
        # single launch it will hand back a "default" the app never declared, and
        # that launches with empty args. That is exactly how Hunyuan3D started
        # and then died, its `--profile` blank.
        uri = None
        inp = body.input if body else None
        if action == "launch":
            if body and body.href:
                uri = _resolve_href(app_id, body.href)
                if not uri:
                    return JSONResponse({"ok": False, "error": "href escapes the app"},
                                        status_code=400)
            else:
                menu = _app_menu(app_id)
                top = menu.get("topDefault") if menu.get("ok") else None
                if top and top.get("href"):
                    uri = _resolve_href(app_id, top["href"])
                    if inp is None:
                        inp = top.get("input") or None
                elif menu.get("ok") and menu.get("leaves"):
                    # Not a failure to report as one: the app simply insists on
                    # being told which entry to run.
                    return JSONResponse({"ok": False, "app": app_id, "action": action,
                                         "needs_choice": True,
                                         "choices": menu["leaves"], "error": None})
                # Neither path applied only when the menu could not be read;
                # fall through and let the engine try its own resolution.
        # The two questions worth asking before work starts, and the only two
        # of their kind: is there disk for an install, is there card for a
        # launch. Both are warnings unless the disk is genuinely full.
        heads: Dict[str, Any] = {}
        if athena_headroom is not None:
            cfg = _load_settings()
            if action in ("install", "update"):
                gate = athena_headroom.install_gate(_athena_home(), cfg)
                if not gate.get("ok"):
                    # 200 with ok=False, like every other refusal on this route:
                    # the panel's contract is a body it can read, and a status
                    # code would replace the sentence with "HTTP 507".
                    gate["app"] = app_id
                    gate["action"] = action
                    return JSONResponse(gate)
                if gate.get("message"):
                    heads["disk_warning"] = gate["message"]
            elif action == "launch":
                g = athena_headroom.gpu_gate(cfg, running=_running_ports().values())
                if g:
                    heads["vram_warning"] = g["message"]

        out = _runtime().start(app_id, APP_SCRIPTS[action], uri=uri, input=inp)
        out["action"] = action
        out.update(heads)
        if out.get("ok"):
            # Someone is using this app, so the idle clock starts now — and it
            # only becomes a countdown if Hermes is the one who started it.
            _idle_note(app_id, f"the {action} action", watch=(origin == "agent"))
            out["idle"] = {"watched": origin == "agent", "window_seconds": _idle_window()}
        if not out.get("ok"):
            # A bind that failed is the app's own output, not an Athena error;
            # say who holds the port so the terminal is not the only place the
            # answer exists.
            run = out.get("run") if isinstance(out.get("run"), dict) else {}
            note = port_failure_note(f"{out.get('error') or ''}\n{out.get('log') or ''}\n"
                                     f"{run.get('logs') or ''}")
            if note:
                out["port_failure"] = note
                out.setdefault("port_warning", note)
        if not out.get("ok") and out.get("reason") == "needs_choice":
            menu = _app_menu(app_id)
            out["needs_choice"] = True
            out["choices"] = menu.get("leaves") or []
            if menu.get("ok"):
                out.pop("error", None)
            else:
                out["error"] = menu.get("error") or out.get("error")
            return JSONResponse(out)
        # An app that serves a web UI needs a moment to bind its port, and the
        # engine only learns the URL once it has. Watch briefly so the panel can
        # open its viewer straight after Launch, rather than polling blind and
        # guessing whether anything happened.
        if action == "launch" and out.get("ok"):
            # An app that is already up answers immediately. One that is only
            # starting will not bind its port for a good while — and the engine
            # does not list a script at all until it does, so while an app boots
            # there is nothing to tell "still starting" apart from "finished
            # without a UI". Blocking on a guess would mean either hanging for a
            # minute or giving up on apps that were fine, so this waits only
            # briefly for the common fast cases and otherwise hands back and lets
            # the viewer watch. Its panel switches to the app's own interface the
            # moment the address appears.
            for _ in range(3):
                url = _app_url(app_id)
                if url:
                    out["url"] = url
                    out["port"] = _port_of(url)
                    break
                time.sleep(1.25)
            if not out.get("url"):
                out["url_note"] = ("starting — its interface opens as soon as it "
                                   "is up; output is on the Terminal tab")
        return JSONResponse(out)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


# Registered last on purpose. Starlette matches routes in declaration order, so a
# wildcard verb route declared above the literal ones shadows them: `POST
# /apps/{id}/revert` (declared at the top of the next block) and `POST
# /apps/{id}/schedule` were both unreachable, answered as "unknown action" by this
# handler, and green because every test looked the route up by name instead of
# asking the router. Keep this below every literal `/apps/{app_id}/<verb>` route.

@router.get("/runs")
def runs_index(limit: int = 60, app: str = "") -> JSONResponse:
    """Every job an app has run for the agent, newest first.

    The record is the recipe each call writes beside its outputs, so this is a
    history rather than a transcript of the current conversation: it survives the
    agent's context window and a restart, and it says what was asked for, what
    went in, and what came out.
    """
    root = engine.home / "runs"
    if not root.is_dir():
        return JSONResponse({"ok": True, "count": 0, "runs": []})
    try:
        cap = max(1, min(int(limit or 60), 400))
        recipes = sorted(root.glob("*/*/recipe.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)
    wants = str(app or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    for rec in recipes:
        run_dir, app_dir = rec.parent, rec.parent.parent
        if wants and wants not in app_dir.name.lower():
            continue
        try:
            data = json.loads(rec.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        files: List[Dict[str, Any]] = []
        for f in (data.get("files") or []):
            if isinstance(f, dict) and f.get("path"):
                files.append({"name": f.get("name") or Path(str(f["path"])).name,
                              "path": f["path"], "bytes": f.get("bytes"),
                              "kind": f.get("kind") or "other"})
        rows.append({
            "id": f"{app_dir.name}/{run_dir.name}",
            "app": data.get("app") or app_dir.name,
            "run": run_dir.name,
            "when": run_dir.name,
            "kind": data.get("kind"),
            "capability": data.get("capability"),
            "intent": data.get("intent"),
            "inputs": [str(i) for i in (data.get("inputs") or [])],
            "files": files,
            "likely_output": data.get("likely_output"),
            "state": data.get("state") or "ok",
            "started": data.get("started"),
            "finished": data.get("finished"),
            "error": data.get("error"),
            "dir": str(run_dir),
        })
        if len(rows) >= cap:
            break
    return JSONResponse({"ok": True, "count": len(rows), "runs": rows})


@router.get("/settings")
def get_settings() -> JSONResponse:
    return JSONResponse(_load_settings())


@router.post("/settings")
def set_settings(body: SettingIn) -> JSONResponse:
    cfg = _load_settings()
    if body.home:
        cfg["home"] = body.home
    if body.port:
        cfg["port"] = int(body.port)
    if body.autostart is not None:
        cfg["autostart"] = bool(body.autostart)
    if body.share_caches is not None:
        cfg["share_caches"] = bool(body.share_caches)
    if body.idle_stop_seconds is not None:
        idle = _idle_module()
        cfg["idle_stop_seconds"] = (idle.clamp(body.idle_stop_seconds) if idle
                                    else int(body.idle_stop_seconds))
    if body.store_index is not None:
        cfg["store_index"] = str(body.store_index).strip()
    if body.apps_layout is not None:
        cfg["apps_layout"] = layout_words(body.apps_layout)
    # Clamped at 0 and beyond: a negative threshold would warn on every install,
    # which is the same as warning on none of them.
    if body.disk_warn_free_gb is not None:
        cfg["disk_warn_free_gb"] = max(0.0, float(body.disk_warn_free_gb))
    if body.vram_warn_free_gb is not None:
        cfg["vram_warn_free_gb"] = max(0.0, float(body.vram_warn_free_gb))
    if body.machine_min_free_mb is not None:
        cfg["machine_min_free_mb"] = max(0, int(body.machine_min_free_mb))
    _save_settings(cfg)
    _apply_store_source(cfg)
    return JSONResponse(cfg)


def layout_words(raw: Any) -> str:
    """`cards` or `rows`, from whatever was posted.

    Two words is the whole vocabulary of the Apps tab's layout, so anything else
    is stored as the default rather than kept as a third state that nothing knows
    how to draw. Rows is the roomier choice and the one worth accepting synonyms
    for; cards is what an unexpected value gets, because it is the layout that
    shows the most.
    """
    return "rows" if str(raw).strip().lower() in ("rows", "row", "list", "compact") else "cards"


def _apply_store_source(cfg: Optional[Dict[str, Any]] = None) -> None:
    """Hand the catalogue its source, once, where the setting is written.

    The catalogue cannot read the settings file itself (the settings file
    belongs to this layer), so the resolved URL is published to a small file the
    catalogue does read. One writer, one reader, neither module having to trust
    the other's import order.
    """
    if athena_catalog is None:
        return
    try:
        cfg = cfg or _load_settings()
        # The configured home, not the running one: changing the home is exactly
        # when the new home's store has to be pointed at its source.
        home = Path(str(cfg.get("home") or engine.home))
        athena_catalog.set_source(home, cfg.get("store_index"))
    except Exception:  # noqa: BLE001 - a store source is never worth a failure
        pass


# ---------------------------------------------------------------- idle apps

def _cost_module() -> Any:
    """`dashboard/cost.py` — the running record of what the card has cost.

    Same door as `idle.py`, for the same reason: this file is loaded twice, once
    by Hermes as a router and once by the agent tools by path, so a module
    imported twice would be two objects — a total read in one half would be a
    different total in the other. Keyed in `sys.modules`, both doors get one.

    The subscription is made here rather than in `system.py`: the monitor should
    not have to know a cost total exists, and `attach` is idempotent, so the
    second copy of this file calling it changes nothing.
    """
    mod = sys.modules.get("athena_cost")
    if mod is not None:
        return mod
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "athena_cost", str(_DASH_DIR / "cost.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["athena_cost"] = mod
        spec.loader.exec_module(mod)
        if athena_system is not None:
            try:
                mod.attach(athena_system, _athena_home)
            except Exception:  # noqa: BLE001
                # The record is still a record without a subscription — it simply
                # stays empty until the next process. Losing the monitor's samples
                # must not cost the panel a working answer.
                traceback.print_exc()
        return mod
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return


def _idle_module() -> Any:
    """`dashboard/idle.py` — the shared record of which app is in use.

    Loaded ONCE, by name. The agent tools load this file's own copy of
    plugin_api by path, so a module imported twice would carry two separate
    dicts: the half that sees an agent call and the half that runs the reaper
    would disagree about which app is busy. Keyed in `sys.modules`, both doors
    get the same object.
    """
    mod = sys.modules.get("athena_idle")
    if mod is not None:
        return mod
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "athena_idle", str(_DASH_DIR / "idle.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["athena_idle"] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return None


def _lease_module() -> Any:
    """`dashboard/lease.py` — who is driving a machine's screen right now.

    Loaded once, by name, for the same reason `_idle_module` is: the agent tools load this
    file's own copy of plugin_api by path, and a module imported twice would carry two
    separate dictionaries — the half that sees a person typing at the panel and the half an
    agent's keystroke goes through would disagree about who holds the keyboard, which is the
    one thing this module exists to decide.
    """
    mod = sys.modules.get("athena_lease")
    if mod is not None:
        return mod
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "athena_lease", str(_DASH_DIR / "lease.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["athena_lease"] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return None


def _idle_window() -> float:
    """How long an app may sit unused before it is stopped; 0 means never."""
    idle = _idle_module()
    if idle is None:
        return 0.0
    return idle.clamp(_load_settings().get("idle_stop_seconds", idle.DEFAULT_SECONDS))


# A machine on the idle clock is keyed with this prefix. `idle` is a registry keyed by
# any string and an app id never contains a colon, so the prefix cannot collide with a
# real app — which is what lets one window, one reaper thread and one `due()` serve
# apps and guests alike. A second registry would have been a second thing to keep in
# step, and the two would have drifted the first time the window was edited.
MACHINE_IDLE_PREFIX = "machine:"


def _idle_machine_key(name: str) -> str:
    """The idle-registry key for a machine, so apps and guests cannot be confused."""
    return MACHINE_IDLE_PREFIX + str(name or "")


def _idle_machine_name(key: str) -> str:
    """The machine name inside an idle key, or `""` when the key is not a machine's.

    `""` and not None, because every caller's next move is to branch on truthiness.
    """
    key = str(key or "")
    if key.startswith(MACHINE_IDLE_PREFIX):
        return key[len(MACHINE_IDLE_PREFIX):]
    return ""


def _idle_note(app_id: str, what: str = "", watch: Optional[bool] = None) -> None:
    """Note that this app is being used right now. Never raises.

    `watch=True` arms it for the reaper; the default leaves that decision alone,
    so a heartbeat or a click in the UI cannot accidentally put an app the user
    started on a timer.
    """
    idle = _idle_module()
    if idle is None:
        return
    try:
        idle.touch(app_id, what=what, watch=watch)
        _idle_reaper_start()
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _app_port(app_id: str) -> int:
    """The port an app's web interface is on, from its own live URL; 0 if none.

    The engine is the only source for this: the port is chosen by the app when it
    starts, not declared anywhere Athena could read ahead of it.
    """
    try:
        rows = _runtime().running(app_id)
    except Exception:  # noqa: BLE001
        return 0
    for entries in (rows or {}).values():
        for e in (entries or []):
            m = re.match(r"^https?://[^/:]+:(\d+)", str(e.get("url") or ""))
            if m:
                return int(m.group(1))
    return 0


def _idle_users(app_id: str) -> int:
    """Live connections to this app's port — is anyone using it? (Windows only.)"""
    idle = _idle_module()
    return idle.users_on_port(_app_port(app_id)) if idle is not None else 0


def _idle_jobs(app_id: str) -> List[str]:
    """Running Athena jobs that name this app — work in flight, not idleness."""
    idle = _idle_module()
    with _JOBS_LOCK:
        return idle.jobs_named(app_id, list(_JOBS.values())) if idle is not None else []


def _idle_keep(app_id: str, why: str) -> None:
    """Say why an app that had gone quiet was left alone. Best effort."""
    try:
        stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        line = (f"[{stamp}] idle: kept {app_id} — {why} "
                f"(window {int(_idle_window())}s)\n")
        logdir = Path(engine.home) / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        with (logdir / "idle.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        pass


_IDLE_REAPER = False
_IDLE_LOCK = threading.Lock()


def _idle_reaper_start() -> None:
    """One daemon thread, started on first use, doing nothing when the window is 0."""
    global _IDLE_REAPER
    with _IDLE_LOCK:
        if _IDLE_REAPER:
            return
        _IDLE_REAPER = True

    def loop() -> None:
        while True:
            time.sleep(5.0)
            idle = _idle_module()
            if idle is None:
                continue
            try:
                window = _idle_window()
                if window <= 0:
                    continue
                for name in idle.due(window):
                    try:
                        _idle_reap(name)
                    except Exception as e:  # noqa: BLE001
                        idle.forget(name)
                        _idle_event(name, {"ok": False, "error": str(e)})
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    threading.Thread(target=loop, name="athena-idle-reaper", daemon=True).start()


def _idle_reap(app_id: str) -> str:
    """Act on one thing that has gone quiet. Returns `gone`, `kept`, or `stopped`.

    Split out of the reaper's loop so the decision can be tested against a real
    socket: whether an app is in use is not a detail worth trusting to a sleep in
    a daemon thread.

    The key decides which half runs — a guest is stopped rather than an app, and the
    two have nothing else in common, so the branch is the first thing here rather than
    a test buried in the middle.
    """
    machine = _idle_machine_name(app_id)
    if machine:
        return _idle_reap_machine(machine)
    idle = _idle_module()
    if idle is None:
        return "gone"
    if not _runtime().running(app_id):
        idle.forget(app_id)  # already stopped on its own
        return "gone"
    # Work in flight is not idleness, however long the clock has been running.
    # Consulted before the socket, because a job may be doing its work with no
    # browser attached at all — a generation whose pane was closed is still a
    # generation, and the socket cannot see it. A hold whose owner died is
    # still named in `/idle` under `work`, which is what makes a leak findable
    # instead of silent.
    if idle.working(app_id):
        _idle_keep(app_id, "work in flight")
        return "kept"
    jobs = _idle_jobs(app_id)
    if jobs:
        _idle_keep(app_id, f"{len(jobs)} running job(s)")
        return "kept"
    # Queried before the stop, never after: an app with someone on the other end
    # of a socket is in use, and being wrong here costs a user their session.
    users = _idle_users(app_id)
    if users:
        idle.touch(app_id, what=f"{users} live connection(s)")
        _idle_keep(app_id, f"{users} live connection(s)")
        return "kept"
    # One last look, because the sweep that produced this name ran a moment ago
    # and a job can start in between. Stopping an app the instant work begins is
    # the same bug as stopping it mid-work, just harder to reproduce.
    if idle.working(app_id):
        _idle_keep(app_id, "work in flight")
        return "kept"
    quiet = int(idle.waiting(app_id))
    out = _runtime().stop(app_id)
    idle.forget(app_id)
    _idle_event(app_id, out, quiet)
    return "stopped"


def _idle_reap_machine(name: str) -> str:
    """Stop a guest that has gone quiet. Returns `gone` or `stopped`.

    Only ever reached for a machine an agent started: a guest a person pressed Start on
    is never armed in the first place, so it is never due and never gets here. That
    asymmetry is the whole point of the feature — an app left running costs a port and
    some memory, and a guest left running costs gigabytes, a screen an agent opened and
    then forgot, and a virtual machine still writing to a disk somebody thought was
    closed.

    "Somebody is using it" is asked before the stop and never after, the same way it is
    for an app. For a guest the signal is its screen: the routes that serve a frame or
    take a keystroke touch this key, so a guest being watched is a guest that is not
    idle, and having the Screen tab open on it is enough to keep it alive.
    """
    idle = _idle_module()
    key = _idle_machine_key(name)
    m = athena_machines
    host = machines_host
    if m is None or host is None:
        if idle is not None:
            idle.forget(key)
        return "gone"
    try:
        spec = m.load_spec(host.home, name)
    except Exception:  # noqa: BLE001 — a spec that cannot be read is not a guest to stop
        spec = None
    if spec is None:
        # Forgotten, or deleted, while it was on the clock. There is nothing to stop,
        # and forgetting the key is what stops the reaper asking again every tick.
        if idle is not None:
            idle.forget(key)
        return "gone"
    try:
        state = host.state(spec)
    except Exception:  # noqa: BLE001
        state = ""
    if state != "running":
        if idle is not None:
            idle.forget(key)  # already stopped on its own
        return "gone"
    quiet = int(idle.waiting(key)) if idle is not None else 0
    out = host.stop(name)
    if idle is not None:
        idle.forget(key)
    _idle_event(key, out, quiet)
    return "stopped"


def _idle_adopt() -> List[str]:
    """Put apps that are already running on the clock.

    An app running now was started by hand, by an earlier session, or before a
    restart — none of which this process witnessed. Without this, a leftover app
    would never be tracked, and "stop it when nothing is using it" would silently
    apply to nothing at all. Given a fresh window rather than the whole one they
    may already have been idle: an app someone started a moment before the
    restart should not be killed the moment the backend comes up.
    """
    idle = _idle_module()
    if idle is None:
        return []
    try:
        running = _runtime().running()
    except Exception:  # noqa: BLE001
        return []
    adopted = []
    for folder in running:
        name = str(folder)[:-4] if str(folder).lower().endswith(".git") else str(folder)
        if name not in idle.tracked():
            # Tracked, never armed: an app that survived a restart was not started
            # by this run of Hermes, and guessing that it should be on a timer is
            # how an app gets stopped while someone is watching it.
            idle.touch(folder, what="already running when Athena started")
            adopted.append(name)
    if adopted:
        _idle_reaper_start()
    return adopted


def _idle_event(app_id: str, out: Dict[str, Any], quiet: int = 0) -> None:
    """Say what was reaped, and why, where a user will find it."""
    try:
        stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        line = (f"[{stamp}] idle: stopped {app_id} — unused for {quiet}s "
                f"(window {int(_idle_window())}s, {'ok' if out.get('ok') else out.get('error')})\n")
        logdir = Path(engine.home) / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        with (logdir / "idle.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        pass


@router.get("/idle")
def idle_state() -> JSONResponse:
    """Which apps are being tracked, how long they have been quiet, and the window."""
    idle = _idle_module()
    window = _idle_window()
    return JSONResponse({"ok": True, "window_seconds": window,
                         "tracked": idle.snapshot() if idle else []})


# The reverse-proxy to the engine used to live here, under the engine's own
# name. Nothing ever called it -- the UI reaches the engine through the engine's
# own client and the agent through the tools -- and it was the one route that
# blocked the event loop on a 120 s urlopen, so it is gone rather than renamed.
