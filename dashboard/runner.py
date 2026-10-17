"""The capability runner: handing a file to an app, running one of its capabilities,
and chaining one app's output into the next one's input.

Split out of `plugin_api.py`, which was 9,400 lines of routes interleaved with the
machinery behind them (rule 136). What is left here is the half of "ask the app what it
can do" that actually *does* something: bring the app up, run the capability, collect
what it wrote, and — when a chain was asked for — hand those files on to the next app.

Three shapes of dependency, and why they are parameters rather than imports:

* `engine` — the supervisor singleton. This module never imports it; it is handed in.
  A module that reaches for a global cannot be loaded, tested, or read without one
  being constructed first.
* `ac_loader`, `idle_loader`, `app_folder`, `app_url`, `runtime`, `job_add`,
  `seed_credentials` — every seam `plugin_api` owns and a harness patches. They arrive
  as callables, one call at a time, so a patch on `plugin_api` is a patch this module
  obeys (rule 138: a test patches the module that owns the function, and the injection
  is what keeps that module still the owner).
* The `{files}`/`{file}` sentinels — vocabulary, not a seam, so they live here.

Imported by name, because `plugin_api.py` is itself loaded by path (rule 136).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---- the vocabulary of a chain ------------------------------------------------
# Where the files a stage produced go in the *next* stage's argument list: `{files}`
# for all of them where one component takes many, `{file}` for the first where a
# component takes one. They were `_FILES_SENTINEL`/`_FILE_SENTINEL` in `plugin_api`;
# the leading underscore went with the module, since nothing reaches across files for
# a private name.
FILES_SENTINEL = "{files}"
FILE_SENTINEL = "{file}"


# The two request bodies stay in `plugin_api`: FastAPI reads them off the route
# signature, so the class the route is declared with is the class it must be handed.


# ---- bringing an app up -------------------------------------------------------


def app_frames(engine: Any, runtime: Any, athena_runtime: Any) -> List[Dict[str, Any]]:
    """Installed apps with their folder and whether each is serving.

    Folders under the app home are the truth, as they are for `/apps`; liveness
    is asked of the engine when it happens to be up. An app the engine has never
    heard of is still a candidate — it is running that it is not.
    """
    root = engine.home / "api"
    try:
        folders = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda p: p.name.lower())
    except OSError:
        folders = []
    live: Dict[str, str] = {}
    if engine.probe() and athena_runtime is not None:
        try:
            for folder, rows in runtime().running().items():
                url = next((r["url"] for r in rows if r.get("url")), None)
                if url:
                    live[folder] = url
                    base_f = folder[:-4] if folder.lower().endswith(".git") else folder
                    live.setdefault(base_f, url)
        except Exception:  # noqa: BLE001
            live = {}
    frames: List[Dict[str, Any]] = []
    for d in folders:
        name = d.name
        base = name[:-4] if name.lower().endswith(".git") else name
        frames.append({"id": name, "name": base, "folder": str(d),
                       "running": bool(live.get(name) or live.get(base)),
                       "url": live.get(name) or live.get(base)})
    return frames


def bring_up(app_id: str, seconds: float = 180.0, *, engine: Any, app_url: Any,
             runtime: Any, seed_credentials: Any) -> Dict[str, Any]:
    """Make sure an app is serving, starting it if it is not.

    Returns `{"url": ...}` or `{"error": <a sentence the panel can show>}`. It
    starts the engine first when that is down as well: an app cannot be brought
    up through a runtime that is not listening.
    """
    url = app_url(app_id)
    if url:
        return {"url": url, "note": "was already running"}
    if not engine.probe():
        started = engine.start(wait=90)
        if not started.get("ok"):
            return {"error": "athena's engine did not start, so the app could not be"}
    try:
        seed_credentials(engine.home)
    except Exception:  # noqa: BLE001
        pass
    try:
        res = runtime().start(app_id)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    if not res.get("ok"):
        if res.get("reason") == "needs_choice":
            # Not a failure to report as one: the app insists on being told which
            # entry to run, and only its card can ask that question.
            return {"error": ("this app has no default action, so it has to be "
                              "launched once from its own card first")}
        return {"error": str(res.get("error") or res)}
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        url = app_url(app_id)
        if url:
            return {"url": url, "note": "started for this call"}
        time.sleep(1.5)
    return {"error": f"{app_id} was started but is not answering yet"}


def run_capability(jid: str, app_id: str, folder: Path, capability: str,
                   inputs: List[Any], timeout: float = 900.0, *, engine: Any,
                   ac_loader: Any, idle_loader: Any, job_add: Any,
                   bring_up: Any) -> Dict[str, Any]:
    """Start the app if needed, run one capability on it, and report what came back.

    The order matters and is the same one the agent tool uses: the app has to be
    serving before its manifest is worth trusting, because a restarted app owns a
    new port and a stale url is the difference between a call that works and one
    that hangs. A probe is taken again when the url moved.
    """
    ac = ac_loader()
    if ac is None:
        return {"ok": False, "error": "the capability probe is unavailable in this build"}
    # Held busy from BEFORE the app is brought up, not just around `ac.call`.
    # Booting a large model is work: Wan2GP and Hunyuan3D can spend minutes
    # loading weights before the app answers on its port, and the idle reaper
    # used to see an armed app with no connection and stop it at 120 s — before
    # the call this job exists to make had even started. The hold belongs in a
    # `finally` so a failure in `bring_up` cannot leave the app on a clock.
    idle = idle_loader()
    if idle is not None:
        try:
            idle.hold(app_id, "call")
        except Exception:  # noqa: BLE001
            idle = None
    try:
        up = bring_up(app_id)
        if not up.get("url"):
            job_add(jid, "!! " + str(up.get("error")))
            return {"ok": False, "error": up.get("error")}
        url = str(up["url"])
        job_add(jid, f"{app_id}: {up.get('note')} ({url})")
        man = ac.load(engine.home, folder) or {}
        if man.get("url") != url:
            job_add(jid, "the url moved, so the app was asked again what it can do")
            try:
                man = ac.probe(engine.home, folder, url) or man
            except Exception as e:  # noqa: BLE001
                job_add(jid, f"!! could not re-probe: {e}")
        # Held busy for the whole call: a generation can run for twenty minutes,
        # and that is the busiest an app ever is. The idle reaper must not read
        # "nothing new" as "nobody wants this" while the work is still in flight.
        res = ac.call(engine.home, folder, capability, inputs, timeout=timeout, manifest=man)
    finally:
        if idle is not None:
            try:
                idle.release(app_id, "call")
            except Exception:  # noqa: BLE001
                pass
    for line in (res.get("log") or [])[-8:]:
        job_add(jid, str(line))
    files = [str(f.get("path") if isinstance(f, dict) else f) for f in (res.get("files") or [])]
    if not res.get("ok"):
        job_add(jid, "!! " + str(res.get("error") or "the app did not return a result"))
        return {"ok": False, "error": res.get("error") or "the app did not return a result",
                "capability": res.get("capability"), "intent": res.get("intent"),
                "run_dir": res.get("run_dir"), "files": files}
    for f in files:
        job_add(jid, "wrote " + f)
    return {"ok": True, "app": app_id, "capability": res.get("capability"),
            "intent": res.get("intent"), "files": files, "run_dir": res.get("run_dir"),
            "recipe": res.get("recipe"), "note": res.get("note")}


# ---- pipelines: one app's output into the next one's input --------------------
# A chain is one job, not several, because a person watching it wants one bar and
# one log. Everything that can be refused is still refused before the job exists:
# every stage named has to be installed, has to have that capability in its
# manifest, and has to be able to take what the stage before it says it produces.
# The one thing that cannot be known up front is how many files a stage will
# actually make, so that is answered at run time, out loud.


def then_stages(rows: Optional[List[Any]]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """The stages that follow the first call, normalised — or the reason not to.

    Stage numbers are 1-based and count the first call, so the first *following*
    stage is stage 2; the number is included in every message the person reads,
    because "which step broke" is the only question a pipeline raises.
    """
    out: List[Dict[str, Any]] = []
    for n, raw in enumerate(rows or []):
        r = raw if isinstance(raw, dict) else {}
        app = str(r.get("app") or r.get("app_id") or "").strip()
        cap = str(r.get("capability") or r.get("intent") or "").strip()
        if not app:
            return None, f"stage {n + 2} does not name an app"
        if not cap:
            return None, f"stage {n + 2} ({app}) does not name a capability"
        extra = r.get("inputs")
        if extra is not None and not isinstance(extra, list):
            extra = [extra]
        out.append({"app": app, "capability": cap, "inputs": extra, "n": n + 2})
    return out, None


def stage_slots(stage: Dict[str, Any]) -> int:
    """How many inputs a following stage can be given.

    Without `inputs`, the files from the stage before go in as the inputs, so
    there is room for one input (however many files that turns out to be is not
    known until the stage before it has run). With `inputs`, each literal counts
    and each `{files}`/`{file}` sentinel counts as the one input the files fill.
    """
    if not stage.get("inputs"):
        return 1
    return len(stage["inputs"])


def stage_inputs(stage: Dict[str, Any], handed: List[str]) -> List[Any]:
    """The argument list for a following stage: its literals, with the files spliced in."""
    if not stage.get("inputs"):
        return list(handed)
    out: List[Any] = []
    for item in stage["inputs"]:
        if isinstance(item, str) and item == FILES_SENTINEL:
            out.extend(handed)
        elif isinstance(item, str) and item == FILE_SENTINEL:
            out.append(handed[0] if handed else "")
        else:
            out.append(item)
    return out


def stage_target(app_id: str, capability: str, *, ac: Any, engine: Any,
                 app_folder: Any) -> Tuple[Optional[Path], Optional[Dict[str, Any]], Optional[str]]:
    """The folder and capability row for a following stage — or the reason it cannot run."""
    folder = app_folder(app_id)
    if folder is None:
        return None, None, f"no installed app called {app_id}"
    man = ac.load(engine.home, folder)
    if not man or not (man.get("capabilities") or []):
        return None, None, "athena has no list of what this app can do yet"
    cap = next((c for c in man["capabilities"]
                if capability in (c.get("name"), c.get("intent"))), None)
    if cap is None:
        return None, None, (f"it has no capability called {capability!r} — it has "
                            + ", ".join(str(c.get("intent")) for c in man["capabilities"]))
    return folder, cap, None


def chain_check(first_cap: Dict[str, Any], stages: List[Dict[str, Any]], *,
                ac: Any, engine: Any, app_folder: Any) -> Optional[Dict[str, Any]]:
    """Whether every stage can run, asked before any of it starts.

    Returns an error dict naming the stage, or None when the chain is sound. The
    handover is checked component-to-component from the two manifests, so a chain
    that cannot work is refused instead of being discovered halfway through; a
    stage that needs more inputs than the files can fill is refused here too.
    """
    prev = first_cap
    for stage in stages:
        n = stage["n"]
        slots = stage_slots(stage)
        folder, cap, err = stage_target(stage["app"], stage["capability"],
                                        ac=ac, engine=engine, app_folder=app_folder)
        if err:
            return {"ok": False, "app": stage["app"],
                    "error": f"stage {n} ({stage['app']}): {err}"}
        required = [i for i in (cap.get("inputs") or []) if i.get("required")]
        if len(required) > slots:
            return {"ok": False, "app": stage["app"],
                    "error": (f"stage {n} ({stage['app']} — {cap.get('intent') or cap.get('name')}): "
                              f"it needs {len(required)} input(s), and only what the stage "
                              f"before it makes can be handed over; give its other inputs "
                              f"as `inputs`, using {FILES_SENTINEL} where the files go"),
                    "needs": [i.get("name") for i in required]}
        verdict = ac.links(prev, cap)
        if not verdict.get("ok"):
            return {"ok": False, "app": stage["app"],
                    "error": (f"stage {n} ({stage['app']} — {cap.get('intent') or cap.get('name')}): "
                              f"{verdict.get('reason')}"),
                    "produces": verdict.get("kinds"), "takes": verdict.get("wants")}
        prev = cap
    return None


def run_pipeline(jid: str, app_id: str, folder: Path, capability: str, inputs: List[Any],
                 stages: List[Dict[str, Any]], timeout: float = 900.0, *, engine: Any,
                 ac_loader: Any, idle_loader: Any, job_add: Any, bring_up: Any,
                 app_folder: Any) -> Dict[str, Any]:
    """Run one capability and hand what it makes to the next one, in order.

    The rule that keeps this honest: a stage that has nothing to hand on stops the
    chain and says so in the log, because silence would leave a step that looks
    like it ran and did nothing. The same goes for a stage that fails — the job
    ends failed with that stage named, and the files that did come out are still
    reported, since they exist on disk either way.
    """
    ac = ac_loader()
    done: List[Dict[str, Any]] = []
    last = run_capability(jid, app_id, folder, capability, inputs, timeout=timeout,
                          engine=engine, ac_loader=ac_loader, idle_loader=idle_loader,
                          job_add=job_add, bring_up=bring_up)
    done.append({"stage": 1, "app": app_id, "capability": capability,
                 "ok": bool(last.get("ok")), "intent": last.get("intent"),
                 "files": last.get("files") or [], "error": last.get("error")})
    if not last.get("ok"):
        job_add(jid, f"!! stage 1 ({app_id}) failed, so the chain stopped")
        return {"ok": False, "error": last.get("error"), "stages": done, "stopped_at": 1,
                "files": last.get("files") or [], "run_dir": last.get("run_dir")}
    handed = [str(f) for f in (last.get("files") or []) if f]
    for stage in stages:
        n = stage["n"]
        before = n - 1
        if not handed:
            job_add(jid, f"!! stage {n} was not started: stage {before} produced no "
                         f"files to hand on")
            return {"ok": False, "stages": done, "stopped_at": n, "files": [],
                    "error": f"stage {before} produced no files, so stage {n} was not started"}
        fol, cap, err = stage_target(stage["app"], stage["capability"],
                                     ac=ac, engine=engine, app_folder=app_folder)
        if err:
            job_add(jid, f"!! stage {n} ({stage['app']}): {err}")
            return {"ok": False, "stages": done, "stopped_at": n, "files": handed,
                    "error": f"stage {n} ({stage['app']}): {err}"}
        job_add(jid, f"--- stage {n}: {stage['app']} — {cap.get('intent') or cap.get('name')} "
                     f"({len(handed)} file(s) handed over)")
        res = run_capability(jid, stage["app"], fol, str(cap.get("name")),
                             stage_inputs(stage, handed), timeout=timeout,
                             engine=engine, ac_loader=ac_loader, idle_loader=idle_loader,
                             job_add=job_add, bring_up=bring_up)
        done.append({"stage": n, "app": stage["app"], "capability": str(cap.get("name")),
                     "intent": cap.get("intent"), "ok": bool(res.get("ok")),
                     "files": res.get("files") or [], "error": res.get("error")})
        last = res
        if not res.get("ok"):
            job_add(jid, f"!! stage {n} ({stage['app']}) failed, so the chain stopped")
            return {"ok": False, "stages": done, "stopped_at": n,
                    "files": res.get("files") or [],
                    "error": f"stage {n} ({stage['app']}): {res.get('error')}"}
        handed = [str(f) for f in (res.get("files") or []) if f]
    return {"ok": True, "app": app_id, "capability": capability, "stages": done,
            "files": handed, "run_dir": last.get("run_dir"), "note": last.get("note")}
