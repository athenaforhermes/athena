"""Athena — the local AI app store for Hermes.

Agent-facing half of the unified `athena` plugin. Registers tools that let any
Hermes agent install, launch, stop, inspect and search AI apps natively — the app
lives inside Hermes, so an agent never has to leave the app or sign in again.

The engine supervisor lives in `dashboard/plugin_api.py` (shared with the HTTP
API). We load it by path so there is exactly one EngineManager and one child
process, whether the caller is the UI or a model tool call.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_API_PATH = _HERE / "dashboard" / "plugin_api.py"

_api = None


def _load_api():
    """Import dashboard/plugin_api.py as a module (single shared engine).

    Cached for the life of the process on purpose: the panel, the agent tool and the idle
    reaper must all reach the *same* module, because the records they share live in it.
    Re-importing on a file change (which was tried) gives each caller a module of its own
    and the shared records stop being shared — `t_idle` and `t_agent_tools` both catch it.

    The consequence, and it is a real one: an edit to this file is picked up when the
    plugin reloads, and an edit to anything under `dashboard/` is **not** — those modules
    stay in `sys.modules` with the copy that was loaded first. A fix can therefore be green
    in the suite and dead in the running engine, which is how the memory floor shipped and
    then answered `machine_gate` with `AttributeError` on the first real start. The remedy
    is restarting the host, not reloading here.
    """
    global _api
    if _api is not None:
        return _api
    spec = importlib.util.spec_from_file_location("athena_plugin_api", str(_API_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["athena_plugin_api"] = mod
    spec.loader.exec_module(mod)
    _api = mod
    return mod


def _ok(**kw) -> str:
    return json.dumps({"success": True, **kw}, default=str)


def _err(msg: str, **kw) -> str:
    return json.dumps({"success": False, "error": msg, **kw}, default=str)


def _ensure_running() -> Dict[str, Any]:
    """Start the engine on demand — an agent tool should just work."""
    mod = _load_api()
    if mod.engine.probe():
        return {"ok": True, "already": True}
    return mod.engine.start()


def _app_id(a: Dict[str, Any]) -> str:
    return str(a.get("name") or a.get("id") or "")


def _body(x: Any) -> Dict[str, Any]:
    """Normalise whatever a route function returns into a plain dict.

    A route handler answers with a `JSONResponse` over HTTP and is an ordinary
    function when it is called from here. This is the one place that difference
    is absorbed, so no handler has to know which door it came through.
    """
    body = getattr(x, "body", None)
    if isinstance(body, (bytes, bytearray)):
        try:
            parsed = json.loads(body.decode("utf-8", "replace") or "{}")
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except Exception:  # noqa: BLE001
            return {"raw": body.decode("utf-8", "replace")[:2000]}
    if isinstance(x, dict):
        return x
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except Exception:  # noqa: BLE001
            return {"raw": body[:2000]}
    if isinstance(x, (list, str, int, float, bool)) or x is None:
        return {"result": x}
    return {"result": str(x)[:2000]}


def _wait_job(mod, jid: Optional[str], seconds) -> Dict[str, Any]:
    """Follow a job for a bounded time and report where it got to.

    Long work is a job (deleting stops the app, then walks a huge tree; fixing
    can re-run an install). Blocking a tool call for all of it is bad, and not
    following it at all is worse — so this waits a little and hands back the
    state plus the tail of the log, and `athena_job` picks it up after.
    """
    if not jid:
        return {}
    deadline = time.time() + max(0.0, float(seconds or 0))
    snap = _body(mod.job_status(jid))
    while str(snap.get("state") or "") in ("running", "queued") and time.time() < deadline:
        time.sleep(0.4)
        snap = _body(mod.job_status(jid))
    log = snap.get("log")
    if isinstance(log, list):
        snap["log_tail"] = log[-25:]
        snap.pop("log", None)
    return snap


def _app_kind(folder: Optional[str]) -> str:
    """What kind of app this is, read from its own files.

    Only two shapes exist in this catalogue and both describe themselves over
    HTTP once running — so this is a cheap pre-answer, not the source of truth.
    The capability probe replaces it with what the app actually answers.
    """
    if not folder:
        return "unknown"
    app = Path(folder) / "app"
    if (app / "comfyui_version.py").exists() and (app / "execution.py").exists():
        return "comfyui"
    if (app / "server.py").exists() and (app / "nodes.py").exists():
        return "comfyui"
    for f in sorted(app.glob("*.py"))[:30]:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if "import gradio" in text or "gradio as gr" in text or "gr.Blocks" in text:
            return "gradio"
    return "unknown"


def _live(mod) -> Dict[str, Dict[str, str]]:
    """One engine call: which apps are up, and the URL each one opened on.

    `/info/scripts` is keyed by the resolved script path rather than the app
    folder, and an app picks its own port at launch — so this is the only honest
    source for "where is it", and asking once covers every app.
    """
    try:
        rt = mod.athena_runtime.Runtime(mod.engine.port, mod.engine.home)
        run = rt.running() or {}
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for folder, entries in run.items():
        for e in (entries or []):
            if isinstance(e, dict):
                out[str(folder)] = {"url": e.get("url") or "", "script": e.get("uri") or ""}
    return out


def _installed(mod) -> List[Dict[str, Any]]:
    """Installed apps, read from disk — never inferred from the engine (rule 18)."""
    home = Path(mod.engine.home) / "api"
    rows: List[Dict[str, Any]] = []
    if not home.is_dir():
        return rows
    for d in sorted(home.iterdir()):
        if not d.is_dir():
            continue
        name = d.name[:-4] if d.name.lower().endswith(".git") else d.name
        rows.append({"id": d.name, "name": name, "folder": str(d)})
    return rows


# ------------------------------------------------------------------ handlers


def handle_status(params, **kwargs):
    del params, kwargs
    try:
        mod = _load_api()
        st = mod.engine.status()
        creds = mod.collect_credentials()
        return _ok(
            engine=st,
            credentials={k: bool(v.get("available")) for k, v in creds.items()
                         if isinstance(v, dict) and "available" in v},
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_apps(params, **kwargs):
    """Every installed app: is it running, what does it serve, where does it live."""
    del params, kwargs
    try:
        mod = _load_api()
        if not mod.engine.probe():
            started = mod.engine.start()
            if not started.get("ok"):
                return _err("engine unavailable: " + str(started.get("error")))
        # The engine's own view carries port + title + description, the disk
        # carries the truth about what is installed (rule 18), and the runtime
        # view holds the only live URL. Ask all three.
        engine_apps: Dict[str, Dict[str, Any]] = {}
        try:
            data = mod.engine.call("GET", "/info/apps", timeout=45)
            for a in (data.get("apps") or []):
                if isinstance(a, dict):
                    engine_apps[_app_id(a)] = a
        except Exception:  # noqa: BLE001
            pass
        live = _live(mod)

        apps = []
        for row in _installed(mod):
            aid, folder = row["id"], row["folder"]
            ea = engine_apps.get(aid) or engine_apps.get(row["name"]) or {}
            hit = live.get(aid) or live.get(row["name"]) or {}
            url = hit.get("url") or ""
            entry: Dict[str, Any] = {
                "id": aid,
                "name": row["name"],
                "title": ea.get("title") or row["name"],
                "description": (ea.get("description") or "")[:200],
                "running": bool(url) or bool(ea.get("running")),
                "url": url or None,
                "port": ea.get("port"),
                "kind": _app_kind(folder),
                "path": folder,
            }
            man = Path(folder) / "agent.json"
            if man.is_file():
                try:
                    data = json.loads(man.read_text(encoding="utf-8"))
                    entry["capabilities"] = len(data.get("capabilities") or [])
                    entry["manifest"] = str(man)
                except Exception:  # noqa: BLE001
                    pass
            # What it will ask for, so a launch is not a surprise an account turns
            # out to be needed for. Read from the scan cache — a directory walk per
            # app on a listing call is exactly what the cache exists to avoid, and
            # `scanned: False` is the honest answer for one nobody has read.
            try:
                entry["keys"] = mod._keys_cached(Path(folder).name)
            except Exception:  # noqa: BLE001
                pass
            apps.append(entry)
        return _ok(count=len(apps), apps=apps)
    except Exception as e:  # noqa: BLE001
        return _err(str(e), trace=traceback.format_exc()[-800:])


def handle_search(params, **kwargs):
    """Search the app store itself — not just what is already installed."""
    try:
        mod = _load_api()
        q = str(params.get("query") or params.get("q") or "").strip()
        if not q:
            return _err("query is required")
        cat = getattr(mod, "athena_catalog", None)
        if cat is None:
            return _err("catalog client unavailable")
        res = cat.search(mod.engine.home, q, limit=40)
        apps = []
        for a in (res.get("apps") or [])[:40]:
            apps.append({
                "name": a.get("name") or a.get("slug"),
                "repo": a.get("repo") or "",
                "version": a.get("version") or "",
                "description": (a.get("description") or "")[:300],
                # hand this straight back to athena_install
                "install_with": a.get("slug") or a.get("repo"),
            })
        return _ok(query=q, count=len(apps), apps=apps, source=res.get("source"))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_install(params, **kwargs):
    try:
        mod = _load_api()
        ref = str(params.get("url") or params.get("name") or "").strip()
        if not ref:
            return _err("url is required (a git repo, an owner/repo, or a store app)")
        # A store app name or page is not a git URL — resolve it to the repo the
        # app actually lives in before handing anything to the engine.
        url = ref
        resolved: dict = {}
        cat = getattr(mod, "athena_catalog", None)
        # Anything that is not already a plain clone URL gets resolved, so a
        # store URL carrying a branch (`…/athena-store#athena-wan2gp`) installs
        # the app the same way the Store tab does.
        plain_url = (ref.lower().startswith(("http://", "https://", "git@"))
                     and ref.rstrip("/").endswith(".git"))
        if cat is not None and not plain_url:
            try:
                resolved = cat.resolve(mod.engine.home, ref)
                url = resolved.get("git") or ref
                if not resolved.get("git"):
                    return _err(resolved.get("error")
                                or f"no app called '{ref}' in the store")
            except Exception:  # noqa: BLE001
                url = ref
        if not mod.engine.probe():
            started = mod.engine.start()
            if not started.get("ok"):
                return _err("engine unavailable: " + str(started.get("error")))
        # Ensure apps inherit Hermes-held credentials before they install.
        try:
            seed = mod.seed_credentials(mod.engine.home)
        except Exception:
            seed = {"written": []}
        # Clone into Athena's app folder ourselves, then let the engine run the
        # app's own install script through the runtime channel.
        got = mod.clone_app(url, mod.engine.home, ref=resolved.get("ref"),
                            name=resolved.get("folder"))
        if not got.get("ok"):
            return _err("install failed: " + str(got.get("error")))
        run = mod.athena_runtime.Runtime(mod.engine.port, mod.engine.home).start(got["name"])
        return _ok(app=got["name"], path=got["path"], already=got.get("already"),
                   run=run, credentials_seeded=seed.get("written", []))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def _pick_leaf(leaves: List[Dict[str, Any]], want: str) -> Optional[Dict[str, Any]]:
    """The menu entry a caller meant, by text, by path, or by script.

    An app that marks no default has to be told which entry to run, and a model
    will name it the way a person would ("Start"), not by href or index. Path is
    the disambiguator when two entries share a label.
    """
    w = str(want or "").strip().lower()
    if not w:
        return None
    scored: List[tuple] = []
    for e in leaves:
        if not isinstance(e, dict):
            continue
        text = str(e.get("text") or "").strip().lower()
        path = " / ".join(str(p) for p in (e.get("path") or [])).strip().lower()
        href = str(e.get("href") or "").strip().lower()
        if w == text or w == path or w == href:
            scored.append((0, len(text), e))
        elif text.startswith(w) or path.startswith(w):
            scored.append((1, len(text), e))
        elif w in text or w in path or w in href:
            scored.append((2, len(text), e))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[0][2]


def _leaf_choices(leaves: List[Dict[str, Any]], limit: int = 20) -> List[Dict[str, Any]]:
    """What to hand back when an app insists on being told which entry to run."""
    out: List[Dict[str, Any]] = []
    for e in leaves or []:
        if not isinstance(e, dict):
            continue
        out.append({"text": e.get("text"),
                    "path": e.get("path"),
                    "script": e.get("href"),
                    "input": e.get("input") or {}})
        if len(out) >= limit:
            break
    return out


def handle_launch(params, **kwargs):
    try:
        mod = _load_api()
        app = str(params.get("app") or params.get("id") or "").strip()
        if not app:
            return _err("app is required")
        if not mod.engine.probe():
            started = mod.engine.start()
            if not started.get("ok"):
                return _err("engine unavailable: " + str(started.get("error")))
        # Through the route's own function, not around it: it is the one place
        # that knows how to turn a menu choice into the script and the values
        # that entry supplies (rule 92's sibling — one door, two callers).
        want = str(params.get("script") or params.get("choice") or "").strip()
        body = None
        if want:
            menu = _body(mod.app_menu(app))
            leaves = (menu.get("leaves") or []) if isinstance(menu, dict) else []
            leaf = _pick_leaf(leaves, want)
            if leaf is None:
                return _err(f"this app has no entry matching {want!r}",
                            reason="needs_choice", choices=_leaf_choices(leaves))
            body = mod.ActionBody(href=leaf.get("href"), input=leaf.get("input") or None)
        # origin="agent": Hermes started this one, so it is the kind that gets
        # stopped when it goes idle. An app the user launches from the UI is
        # never armed, and this is the only place that says otherwise.
        res = _body(mod.app_action(app, "launch", body, origin="agent"))
        if res.get("needs_choice") or (not res.get("ok")
                                       and res.get("reason") == "needs_choice"):
            choices = res.get("choices") or _leaf_choices(
                (_body(mod.app_menu(app)).get("leaves") or []))
            return _err(res.get("error") or "this app has to be told which entry to run",
                        reason="needs_choice", choices=choices,
                        note=("call again with script=<text from choices> — e.g. the "
                              "entry shown as 'Start'"))
        # An app binds its port about ten seconds in and the engine lists nothing
        # until then — a short look is worth it, but never block a launch on it
        # (rule 32). The panel polls `athena_apps` for the rest.
        url = ""
        wait = min(float(params.get("wait") or 0), 20.0)
        deadline = time.time() + wait
        while wait and time.time() < deadline:
            url = (_live(mod).get(app) or {}).get("url") or ""
            if url:
                break
            time.sleep(1.0)
        return _ok(app=app, launch=res, url=url or None,
                   note="" if url else "booting — the port appears a few seconds "
                                       "after launch; call athena_apps for it")
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_stop(params, **kwargs):
    try:
        mod = _load_api()
        app = str(params.get("app") or params.get("id") or "").strip()
        if not app:
            return _err("app is required")
        res = mod.athena_runtime.Runtime(mod.engine.port, mod.engine.home).stop(app)
        idle = _idle(mod)
        if idle is not None and res.get("ok"):
            idle.forget(app)  # stopped on purpose: not idle, just gone
        return _ok(app=app, stop=res)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_logs(params, **kwargs):
    """An app's own output — and, when the log names a failure, what it means.

    The log is the answer to "why did this stop", but the half that is worth
    having is the reading: an agent handed 120 lines of a Python traceback will
    derive a different cause each time it reads them, and the useful sentence in
    there was written by the program that failed, not by the reader. So the
    reading comes with the lines. `why=1` returns it alone, which is the call an
    agent should make first and most of the time.

    (This also fixed a latent crash: the old body referenced `res`, a name that
    never existed, on the path where the log did not come back as text.)
    """
    try:
        mod = _load_api()
        app = str(params.get("app") or params.get("id") or "").strip()
        if not app:
            return _err("app is required")
        text = mod.app_log_text(app, limit=300_000)
        if not isinstance(text, str):
            text = ""

        reading = None
        try:
            tri = mod._triage_module()
            if tri is not None and text and not text.startswith("(no "):
                found = tri.triage(text, {"app": app})
                # Only a named cause is worth returning: an empty reading on every
                # healthy app would train a reader to ignore the field.
                reading = found if found.get("found") else None
        except Exception:  # noqa: BLE001
            reading = None

        if params.get("why") or params.get("triage"):
            if reading is not None:
                return _ok(app=app, reading=reading)
            return _ok(app=app, lines=text.splitlines()[-40:] if text else [],
                       note="nothing in this app's output names a failure Athena can "
                            "read; the last lines are here instead")

        limit = int(params.get("lines") or 120)
        return _ok(app=app, lines=text.splitlines()[-limit:], reading=reading)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))



def handle_history(params, **kwargs):
    """What has already been run here, read back from the record.

    The agent's own memory of a job is the context window; this is the disk. It
    answers from the same function the Runs tab reads, so the two can never
    disagree about what happened.
    """
    del kwargs
    try:
        mod = _load_api()
        want = int(params.get("limit") or 20)
        app = str(params.get("app") or "").strip()
        data = _body(mod.runs_index(limit=want, app=app))
        if not isinstance(data, dict) or not data.get("ok", True):
            return _err(str(data.get("error") if isinstance(data, dict) else data))
        runs = []
        for r in (data.get("runs") or []):
            files = r.get("files") or []
            runs.append({
                "app": r.get("app"),
                "when": r.get("when"),
                "state": r.get("state"),
                "capability": r.get("capability"),
                "asked_for": r.get("intent"),
                "inputs": r.get("inputs"),
                "files": [{"name": f.get("name"), "kind": f.get("kind"),
                           "bytes": f.get("bytes"), "path": f.get("path")} for f in files],
                "produced": r.get("likely_output"),
                "recipe": str(Path(str(r.get("dir") or "")) / "recipe.json"),
                "error": r.get("error"),
            })
        return _ok(count=len(runs), runs=runs,
                   note=("The same list is in Athena's Runs tab, where the files are "
                         "previewed. A run still marked 'running' is going on right "
                         "now; its record is rewritten when it ends."))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_store_detail(params, **kwargs):
    """One store record in full — what an app is before installing it."""
    del kwargs
    try:
        mod = _load_api()
        ref = str(params.get("ref") or params.get("name") or "").strip()
        if not ref:
            return _err("ref is required (a store slug, page URL or owner/repo)")
        cat = getattr(mod, "athena_catalog", None)
        if cat is None:
            return _err("catalog client unavailable")
        return _ok(ref=ref, detail=cat.detail(mod.engine.home, ref))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_publish(params, **kwargs):
    """Put the installed apps (and the store's index) on GitHub, as a job."""
    del kwargs
    try:
        mod = _load_api()
        app = str(params.get("app") or params.get("id") or "").strip()
        if not app:
            return _err("app is required")
        res = _body(mod.publish_app(app))
        if res.get("error") and not res.get("job"):
            return _err(str(res.get("error")))
        snap = _wait_job(mod, res.get("job"), params.get("wait", 45))
        return _ok(app=app, publish=res, job=res.get("job"),
                   branch=res.get("branch"), store=res.get("store"),
                   state=snap.get("state"), log_tail=snap.get("log_tail"),
                   result=snap.get("result"))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_delete(params, **kwargs):
    """Remove an installed app — stop the app first, then delete the folder."""
    del kwargs
    try:
        mod = _load_api()
        app = str(params.get("app") or params.get("id") or "").strip()
        if not app:
            return _err("app is required")
        res = _body(mod.app_uninstall(app))
        if res.get("error") and not res.get("job"):
            return _err(str(res.get("error")))
        snap = _wait_job(mod, res.get("job"), params.get("wait", 45))
        return _ok(app=app, uninstall=res, job=res.get("job"),
                   state=snap.get("state"), log_tail=snap.get("log_tail"),
                   result=snap.get("result"))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_fix(params, **kwargs):
    """Verify an app in place and repair it — including provisioning it."""
    del kwargs
    try:
        mod = _load_api()
        app = str(params.get("app") or params.get("id") or "").strip()
        if not app:
            return _err("app is required")
        body = mod.FixBody(provision=bool(params.get("provision", True)))
        res = _body(mod.app_fix(app, body))
        if res.get("error") and not res.get("job"):
            return _err(str(res.get("error")))
        snap = _wait_job(mod, res.get("job"), params.get("wait", 45))
        out: Dict[str, Any] = {"app": app, "job": res.get("job"),
                               "state": snap.get("state"),
                               "log_tail": snap.get("log_tail"),
                               "result": snap.get("result")}
        # The point of Fix is a working app, so say what it found afterwards.
        try:
            out["health_after"] = _body(mod.app_health(app))
        except Exception:  # noqa: BLE001
            pass
        return _ok(**out)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_app_health(params, **kwargs):
    """Check an app's structure without changing anything."""
    del kwargs
    try:
        mod = _load_api()
        app = str(params.get("app") or params.get("id") or "").strip()
        if not app:
            return _err("app is required")
        if params.get("deep"):
            return _ok(app=app, health=mod._app_health(app, deep=True))
        return _ok(app=app, health=_body(mod.app_health(app)))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_report(params, **kwargs):
    """What is asking something of you, in one answer.

    The one call that answers "is anything wrong here" without walking the app
    list first: broken apps (with the check that failed and the step that fixes
    it), apps that stopped recently and are not running (with the log's own
    reading), updates already known to be waiting, weights nothing reads, and
    launches queued for the card. Every fact in it is a read, and the update
    answers are the cached ones — so this never reaches the network and can be
    asked as often as wanted.
    """
    del kwargs
    try:
        mod = _load_api()
        limit = int(params.get("limit") or 200)
        out = mod.report_view(limit)
        # `needs` is the whole payload's shape too, so the agent reading this sees
        # the list first rather than the summary of it.
        return _ok(**out)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_models(params, **kwargs):
    """What this machine can generate with — and getting it the weights it lacks.

    Reading and acting are the same tool on purpose: the read answers with the
    step that would fix it plus the folders on this machine that already hold
    weights, so the caller can act in one more call without knowing a path. The
    answer to the write is the re-checked verdict, not a promise. Asked with no
    arguments it answers for the whole shelf; asked with a `repo` it answers
    about HuggingFace, and with `download` it starts the pull as a job.
    """
    del kwargs
    try:
        mod = _load_api()
        app = str(params.get("app") or params.get("id") or "").strip()
        folder = str(params.get("folder") or "").strip()
        repo = str(params.get("repo") or "").strip()
        # No app, no folder and no repository is the shelf's question, not a
        # mistake: "what does this machine hold, and what does nothing read" is
        # asked of the machine rather than of an app, and the same data answers
        # it from that end.
        if not app and not folder and not repo:
            out = _body(mod.models_shelf())
            if out.get("ok") is False:
                return _err(str(out.get("error") or "could not read this machine's weights"))
            out.pop("ok", None)
            return _ok(**out)
        if repo:
            # A repository name is a question about the network, not about this
            # disk: what it holds, what it costs, and whether a copy is already
            # here. Asking starts nothing — `download` is the part that starts.
            info = _body(mod.hf_remote(repo))
            if info.get("ok") is False:
                return _err(str(info.get("error") or f"could not read {repo}"), repo=repo)
            if not params.get("download"):
                # The full file list of a big repository is noise around the
                # answer; the largest few say what it is and the count says the
                # rest, and `include` is how a caller asks for one file.
                biggest = info.pop("files", [])[:12]
                info.pop("ok", None)
                return _ok(files=biggest, **info)
            started = _body(mod.models_download(mod.PullBody(
                repo=repo, app=app or None, folder=folder or None,
                key=str(params.get("key") or "checkpoints"),
                include=str(params.get("include") or "") or None,
                revision=str(params.get("revision") or "") or None)))
            if started.get("ok") is False:
                return _err(str(started.get("error") or f"could not start pulling {repo}"),
                            repo=repo)
            started.pop("ok", None)
            return _ok(**started)
        if not app:
            return _err("app is required to point weights at an app")
        # The route's body carries `app` and `ok` as well, and both are already
        # decided here — one is this call's own argument and the other is the
        # envelope every tool answers in. Spreading them in as well is a
        # TypeError, which is exactly how this read as "the tool is broken".
        if not folder:
            out = _body(mod.app_models(app))
            # A route that answers "app not found" is a failed call, not a
            # successful call that happens to contain an error string.
            if out.get("ok") is False:
                return _err(str(out.get("error") or f"could not read {app}"),
                            **{k: v for k, v in out.items()
                               if k not in ("ok", "error", "app")})
        else:
            body = mod.ModelsBody(action=str(params.get("action") or "point"), folder=folder,
                                 force=bool(params.get("force")))
            out = _body(mod.app_models_apply(app, body))
            if out.get("ok") is False:
                return _err(str(out.get("error") or "could not point the app at those weights"),
                            **{k: v for k, v in out.items() if k not in ("ok", "error", "app")})
        for key in ("ok", "app"):
            out.pop(key, None)
        return _ok(app=app, **out)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_settings(params, **kwargs):
    """Read Athena's settings, or change them by passing the fields to change."""
    del kwargs
    try:
        mod = _load_api()
        fields = {k: params.get(k) for k in ("home", "port", "autostart", "share_caches")
                  if params.get(k) is not None}
        if fields:
            res = _body(mod.set_settings(mod.SettingIn(**fields)))
            return _ok(changed=fields, settings=res)
        return _ok(settings=_body(mod.get_settings()))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def _refusal_text(exc: Exception) -> str:
    """The sentence to hand a model when a call was refused, or "" when it was not a refusal.

    `files.enforce` raises an `HTTPException` whose detail is a dict: the message a person should
    read, what *is* allowed, and why. Stringifying that loses all three (`403: {'error': ...}`), and
    matching on a phrase is how a refusal gets missed when the wording moves -- so a refusal is
    recognised by its status code and unpacked here.
    """
    if getattr(exc, "status_code", None) != 403:
        return ""
    detail = getattr(exc, "detail", None)
    if not isinstance(detail, dict):
        return str(detail or exc)
    parts = [str(detail.get("error") or "refused")]
    roots = detail.get("roots") or []
    if roots:
        parts.append("allowed: " + ", ".join(str(r) for r in roots[:6]))
    if detail.get("why"):
        parts.append(str(detail["why"]))
    return " - ".join(p for p in parts if p)


def handle_files(params, **kwargs):
    """Browse and edit files — Athena's folders, and the person's own machine.

    Two scopes, and both are enforced here rather than left to the routes: reads reach Athena's
    home and every fixed disk (a person picking a file for an app is doing on their own machine
    what they can already do in Explorer), writes reach Athena's home and the person's own folder.
    The file routes are guarded by a router-level dependency that an in-process caller never runs,
    so this calls the same enforcement itself before dispatching, with the scope the op needs —
    and `_deny_secret` still refuses credential stores on the way through.
    """
    del kwargs
    try:
        mod = _load_api()
        files = getattr(mod, "athena_files", None)
        if files is None:
            return _err("file browser unavailable")
        op = str(params.get("op") or "list").strip().lower()
        path = str(params.get("path") or "").strip()
        paths = params.get("paths") or ([path] if path else [])
        if not isinstance(paths, list):
            paths = [paths]
        # The read floor, then the write ceiling for the ops that change something. Listed as a
        # set rather than checked per-op below, because the check has to happen before any route
        # runs -- a refused write must not have already happened when the refusal is discovered.
        files.enforce(paths)
        if op in ("write", "mkdir", "delete"):
            files.enforce(paths, write=True)

        def reported(op_name, **body):
            """The answer of a route, refused when the route refused.

            Not just the writes: `read` spread its body into `_ok` too, so a read the deny list
            turned down came back as `success: true` carrying an `error` key the schema does not
            mention — the same hole the write ops had, one floor up.

            `_ok` is unconditional, and the write routes hand their verdict back
            as a body: spreading that into `_ok` turned a refusal — too large, not
            found, outside the roots — into `success: true` carrying an `error`
            key an agent has no reason to go looking for. The schema already
            promises `write: ok/size`, so a failed write has to say so here.
            """
            if body.get("error") or body.get("ok") is False:
                # `refused` as well as failed: to an agent the two mean different things -- a
                # refusal is "not that path" and worth choosing another, a failure is worth
                # retrying. The routes answer with a body, not an exception, so the status code
                # never reaches here; an error key on these ops does mean the op did not happen.
                return _err(str(body.get("error") or "the file route refused"), refused=True)
            return _ok(op=op_name, **body)

        if op == "roots":
            return _ok(op=op, roots=files.roots())
        if op == "list":
            return reported(op, **_body(files.list_dir(
                files.PathIn(path=path, show_hidden=bool(params.get("show_hidden"))))))
        if op == "stat":
            return reported(op, **_body(files.stat_path(
                files.StatIn(path=path, dir_size=bool(params.get("dir_size"))))))
        if op == "read":
            return reported(op, **_body(files.read_text(
                files.ReadIn(path=path, max_kb=int(params.get("max_kb") or 512)))))
        if op == "write":
            text = params.get("text")
            if text is None:
                return _err("text is required for write")
            p = Path(path.replace("\\", "/"))
            created = False
            if not p.is_file():
                files.enforce([str(p.parent)], write=True)
                made = _body(files.mkfile(files.MkfileIn(parent=str(p.parent), name=p.name)))
                if made.get("error"):
                    return _err("could not create: " + str(made.get("error")))
                created = True
            return reported(op, created=created, **_body(files.write_text(
                files.WriteIn(path=path, text=str(text),
                              max_kb=int(params.get("max_kb") or 1024)))))
        if op == "mkdir":
            name = str(params.get("name") or "").strip()
            if not name:
                return _err("name is required for mkdir")
            return reported(op, **_body(files.mkdir(files.MkdirIn(parent=path, name=name))))
        if op == "delete":
            if not paths:
                return _err("paths is required for delete")
            return reported(op, **_body(files.delete(files.DeleteIn(paths=paths))))
        if op == "search":
            q = str(params.get("query") or "").strip()
            if not q:
                return _err("query is required for search")
            return reported(op, **_body(files.search_files(files.SearchIn(
                root=path, query=q,
                max_results=int(params.get("max_results") or 200)))))
        if op == "grep":
            pat = str(params.get("pattern") or "").strip()
            if not pat:
                return _err("pattern is required for grep")
            exts = params.get("exts") or []
            if isinstance(exts, str):
                exts = [e.strip() for e in exts.split(",") if e.strip()]
            return reported(op, **_body(files.grep_content(files.GrepIn(
                root=path, pattern=pat, exts=exts,
                max_hits=int(params.get("max_hits") or 200)))))
        return _err(f"unknown op '{op}' — use roots, list, stat, read, write, "
                    "mkdir, delete, search or grep")
    except Exception as e:  # noqa: BLE001
        msg = _refusal_text(e)
        if msg:
            return _err(msg, refused=True)
        return _err(str(e))


def handle_space(params, **kwargs):
    """Disk state, the shared model store, and the engine's dedupe actions."""
    del kwargs
    try:
        mod = _load_api()
        action = str(params.get("action") or "").strip().lower()
        if not action:
            out: Dict[str, Any] = {"disk": _body(mod.disk_status())}
            try:
                out["shared"] = _body(mod.shared_status())
            except Exception:  # noqa: BLE001
                pass
            return _ok(**out)
        routes = {
            "scan": mod.disk_scan,
            "dedupe": mod.disk_dedupe,
            "free": mod.disk_free,
            "separate": mod.disk_separate,
        }
        if action not in routes:
            return _err(f"unknown action '{action}' — use scan, dedupe, free or separate")
        res = _body(routes[action]())
        if res.get("error") and not res.get("job"):
            return _err(str(res.get("error")))
        snap = _wait_job(mod, res.get("job"), params.get("wait", 20))
        return _ok(action=action, job=res.get("job"), started=res,
                   state=snap.get("state"), log_tail=snap.get("log_tail"),
                   result=snap.get("result"))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def handle_job(params, **kwargs):
    """Where a long Athena action got to — the log tail is usually the answer."""
    del kwargs
    try:
        mod = _load_api()
        jid = str(params.get("job") or params.get("job_id") or "").strip()
        if not jid:
            return _err("job is required")
        snap = _body(mod.job_status(jid))
        log = snap.get("log")
        if isinstance(log, list):
            snap["log_tail"] = log[-int(params.get("lines") or 40):]
            snap.pop("log", None)
        return _ok(job=jid, **snap)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


def _appcall(mod):
    """`dashboard/appcall.py` — the capability probe and caller.

    Loaded by path and cached on the engine module, exactly like the engine
    itself, so there is one copy. It is optional on purpose: if it will not
    import, the two tools that need it say so instead of taking the whole
    plugin down with them.
    """
    existing = getattr(mod, "athena_appcall", None)
    if existing is not None:
        return existing
    try:
        spec = importlib.util.spec_from_file_location(
            "athena_appcall", str(_HERE / "dashboard" / "appcall.py"))
        mod2 = importlib.util.module_from_spec(spec)
        sys.modules["athena_appcall"] = mod2
        spec.loader.exec_module(mod2)
        mod.athena_appcall = mod2
        return mod2
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return None


def _app_row(mod, app: str) -> Optional[Dict[str, Any]]:
    """The installed app a name or id refers to. Exact first, then substring."""
    want = str(app or "").strip().lower().replace(".git", "")
    if not want:
        return None
    rows = _installed(mod)
    for row in rows:
        if want == row["id"].lower().replace(".git", "") or want == row["name"].lower():
            return row
    for row in rows:
        if want in row["id"].lower() or want in row["name"].lower():
            return row
    return None


def _ensure_app(mod, app_id: str, seconds: float = 90.0) -> Dict[str, Any]:
    """Have the app running, and know the URL it serves on.

    An app picks its own port a few seconds after launch, and the engine lists
    nothing until then, so this waits for the URL rather than assuming one.
    """
    live = _live(mod)
    url = (live.get(app_id) or {}).get("url") or ""
    if url:
        _idle_use(mod, app_id, "an agent call")
        return {"url": url, "note": "already running"}
    rt = mod.athena_runtime.Runtime(mod.engine.port, mod.engine.home)
    res = rt.start(app_id)
    if res.get("ok"):
        # Hermes started this one, so it is the kind that gets stopped when it
        # goes idle. The "already running" branch above deliberately does not:
        # that app was somebody else's, and it stays theirs.
        _idle_use(mod, app_id, "an agent call", watch=True)
    if not res.get("ok"):
        if res.get("reason") == "needs_choice":
            return {"url": "", "note": "this app has no default action — use "
                                       "athena_launch to pick one"}
        return {"url": "", "note": str(res.get("error") or res)}
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        url = (_live(mod).get(app_id) or {}).get("url") or ""
        if url:
            _idle_use(mod, app_id, "an agent call", watch=True)
            return {"url": url, "note": "started"}
        time.sleep(2.0)
    return {"url": "", "note": "started, but it has not bound a port yet — ask "
                               "athena_apps for the URL in a moment"}


def _idle(mod):
    """The shared idle record, through the one loader that defines it.

    Not an import of its own: the backend and this half are two different module
    objects, so state has to be fetched through `plugin_api._idle_module()` or
    the two halves would each track a different world.
    """
    fn = getattr(mod, "_idle_module", None)
    return fn() if callable(fn) else None


def _idle_use(mod, app_id: str, what: str = "", watch: Optional[bool] = None) -> None:
    """Note a use of an app — and, when Hermes is the one that started it,
    arm it. `watch` is None for "leave that decision alone", which is what every
    call against an app someone else started wants."""
    fn = getattr(mod, "_idle_note", None)
    if callable(fn):
        try:
            fn(app_id, what, watch)
        except TypeError:  # a module loaded before this signature existed
            fn(app_id, what)


def _cap_summary(caps) -> List[Dict[str, Any]]:
    """Capabilities as a model needs them: what to ask for, what to pass."""
    out = []
    for c in caps or []:
        out.append({
            "capability": c.get("name"),
            "intent": c.get("intent"),
            "does": c.get("description"),
            "media_in": c.get("media_in"),
            "media_out": c.get("media_out"),
            "takes_a_local_file": c.get("accepts_local_files"),
            "inputs": [{"name": i.get("name"), "component": i.get("component"),
                        "required": i.get("required"),
                        "takes_a_local_file": i.get("accepts_local_path")}
                       for i in (c.get("inputs") or [])],
        })
    return out


def handle_app_verify(params, **kwargs):
    """Ask an installed app what it can actually do — answered by the app."""
    del kwargs
    try:
        mod = _load_api()
        ac = _appcall(mod)
        if ac is None:
            return _err("the capability probe is unavailable in this build")
        app = str(params.get("app") or params.get("id") or "").strip()
        refresh = bool(params.get("refresh"))
        if app:
            row = _app_row(mod, app)
            if row is None:
                return _err(f"no installed app matches {app!r}",
                            installed=[r["id"] for r in _installed(mod)][:40])
            rows = [row]
        else:
            rows = _installed(mod)
        if not rows:
            return _err("no apps are installed yet — athena_search then athena_install")
        out: List[Dict[str, Any]] = []
        for row in rows:
            entry: Dict[str, Any] = {"app": row["id"], "name": row["name"],
                                     "kind": _app_kind(row["folder"])}
            man = None if refresh else ac.load(mod.engine.home, row["folder"])
            if man is not None and not man.get("probed"):
                # A guide written when the app installed: the app itself has not
                # been asked yet, so ask now rather than reporting a blank list.
                man = None
            if man is not None:
                entry["manifest"] = man.get("manifest_path")
                entry["probed"] = man.get("probed")
            else:
                started = _ensure_app(mod, row["id"])
                if not started.get("url"):
                    entry["state"] = "not running"
                    entry["note"] = started.get("note")
                    entry["hint"] = ("start it with athena_launch, or ask again "
                                     "once it is up — the probe needs the app running")
                    out.append(entry)
                    continue
                entry["started_by"] = started.get("note")
                man = ac.probe(mod.engine.home, row["folder"], started["url"])
            if man.get("error"):
                entry["error"] = man["error"]
            entry["url"] = man.get("url")
            entry["capabilities"] = _cap_summary(man.get("capabilities"))
            if man.get("note"):
                # Why the list is empty matters as much as the list: an app that
                # answers but describes nothing is a UI to open, not a failure.
                entry["note"] = man["note"]
            entry["how_to_run"] = ("pass the intent as the request to athena_generate, "
                                   "or pass this app and a capability with inputs in order")
            if man.get("kind") == "comfyui":
                entry["models"] = man.get("models")
                entry["note"] = ("ComfyUI capabilities are saved workflows, not named "
                                 "endpoints — it has no plain call path")
            out.append(entry)
        return _ok(count=len(out), apps=out)
    except Exception as e:  # noqa: BLE001
        return _err(str(e), trace=traceback.format_exc()[-800:])


def handle_generate(params, **kwargs):
    """Have an installed app make something, and hand back the files."""
    del kwargs
    try:
        mod = _load_api()
        ac = _appcall(mod)
        if ac is None:
            return _err("the capability probe is unavailable in this build")
        request = str(params.get("request") or params.get("prompt") or "").strip()
        app = str(params.get("app") or "").strip()
        capability = str(params.get("capability") or "").strip()
        inputs = params.get("inputs")
        if inputs is None:
            inputs = []
        if not isinstance(inputs, list):
            inputs = [inputs]
        if not request and not (app and capability):
            return _err("say what should be done (request), or name both an app "
                        "and a capability")
        if not mod.engine.probe():
            started = mod.engine.start()
            if not started.get("ok"):
                return _err("engine unavailable: " + str(started.get("error")))

        # Candidates are the installed apps that already have a manifest — the
        # probe's answer is what an intent is matched against, so an unprobed app
        # has nothing to match on and is listed as such instead of being guessed at.
        cands: List[Dict[str, Any]] = []
        unprobed: List[str] = []
        wanted = _app_row(mod, app) if app else None
        if app and wanted is None:
            return _err(f"no installed app matches {app!r}")
        for row in ([wanted] if wanted else _installed(mod)):
            man = ac.load(mod.engine.home, row["folder"])
            if man and row["folder"] in str(man.get("manifest_path") or row["folder"]):
                cands.append({"id": row["id"], "name": row["name"], "path": row["folder"],
                              "manifest": man, "home": mod.engine.home})
            else:
                unprobed.append(row["id"])
        if not cands:
            return _err("no app has been probed yet, so there is nothing to match "
                        "against — run athena_capabilities on an app first",
                        unprobed=unprobed[:40])

        if app and capability:
            man = cands[0]["manifest"]
            cap = next((c for c in man.get("capabilities") or []
                        if capability in (c.get("name"), c.get("intent"))), None)
            if cap is None:
                return _err(f"{cands[0]['id']} has no capability {capability!r}",
                    available=[c.get("intent") for c in man.get("capabilities") or []])
            found = {"app": cands[0]["id"], "app_path": cands[0]["path"],
                     "capability": cap.get("name"), "intent": cap.get("intent"),
                     "inputs": cap.get("inputs"),
                     "accepts_local_files": cap.get("accepts_local_files")}
        else:
            found = ac.find_capability(cands, request)
            if not found:
                return _err(f"no installed app's capabilities match {request!r}",
                            try_instead=sorted({c.get("intent") for d in cands
                                                for c in (d["manifest"].get("capabilities") or [])})[:40],
                            note="run athena_capabilities to see what an app can do")
        app_id = str(found.get("app"))
        folder = str(found.get("app_path"))
        man = next((d["manifest"] for d in cands if d["id"] == app_id), None)
        cap_spec = next((c for c in (man or {}).get("capabilities") or []
                         if c.get("name") == found.get("capability")), None) or {}
        required = [i for i in (cap_spec.get("inputs") or []) if i.get("required")]
        if not inputs and required:
            return _err(f"{found.get('capability')} needs {len(required)} input(s) — "
                        "pass them in order, local file paths for the media ones",
                        app=app_id, capability=found.get("capability"),
                        inputs=[{"name": i.get("name"), "component": i.get("component"),
                                 "takes_a_local_file": i.get("accepts_local_path")}
                                for i in required])
        stages, stage_err = mod._then_stages(params.get("then"))
        if stage_err:
            return _err(stage_err)
        if stages:
            # The chain is checked here with the same rule the route uses, before
            # anything starts: every stage installed, holding that capability, and
            # able to take what the stage before declares it makes.
            chain_err = mod._chain_check(ac, cap_spec, stages)
            if chain_err:
                extra = {k: v for k, v in chain_err.items() if k != "error"}
                return _err(chain_err.get("error") or "that chain does not fit", **extra)
        # Marked busy BEFORE the app is brought up. A generation can run for
        # twenty minutes on a loaded model, and booting one is work too: a big
        # 3D or video app spends minutes loading weights before it answers on a
        # port, and the idle reaper used to see an armed app with no connection
        # and stop it at 120 s — before the call had even started. The `end` is
        # in a `finally` so a failed bring-up cannot leave the app on a clock.
        idle = _idle(mod)
        if idle is not None:
            idle.begin(app_id)
        try:
            started = _ensure_app(mod, app_id)
            if not started.get("url"):
                return _err(f"{app_id} is not answering: {started.get('note')}",
                            app=app_id)
            if man and man.get("url") != started["url"]:
                # an app that restarted owns a new port; the manifest's url is stale
                man = ac.probe(mod.engine.home, folder, started["url"])
            timeout = float(params.get("timeout") or 900)
            if stages:
                # A chain is not a second implementation here: `run_one` is the same
                # runner a scheduled run calls, which is the same runner the route's job
                # body calls. It brings each app up, re-probes a moved url and holds the
                # app busy for the stage it is running, so the only difference from the
                # route is that the caller waits instead of watching a bar.
                res = mod.run_one(app_id, str(found.get("capability")), inputs, timeout,
                                  stages=stages)
            else:
                res = ac.call(mod.engine.home, folder, str(found.get("capability")),
                              inputs, timeout=timeout, manifest=man)
        finally:
            if idle is not None:
                idle.end(app_id)
        if not res.get("ok"):
            return _err(res.get("error") or "the app did not return a result",
                        app=app_id, capability=res.get("capability"),
                        intent=res.get("intent"), log_tail=(res.get("log") or [])[-12:],
                        app_says=res.get("app_says"))
        return _ok(app=app_id, capability=res.get("capability"), intent=res.get("intent"),
                   files=res.get("files"), run_dir=res.get("run_dir"),
                   recipe=res.get("recipe"), note=res.get("note"),
                   stages=res.get("stages"),
                   log_tail=(res.get("log") or [])[-8:])
    except Exception as e:  # noqa: BLE001
        return _err(str(e), trace=traceback.format_exc()[-800:])


def handle_schedule(params, **kwargs):
    """Arrange for an app to run again on its own, or see what already does.

    One implementation of what a repeat is: the route that validates and stores it
    is the same route the panel and the desktop use, so a repeat asked for in chat
    and a repeat asked for in the UI are the same object. A refusal here is the
    route's refusal, in the route's words.
    """
    del kwargs
    try:
        mod = _load_api()
        action = str(params.get("action") or "create").strip().lower()
        if action in ("", "add", "save", "set"):
            action = "create"
        if action in ("list", "show", "schedules"):
            resp = mod.schedules_index(app=str(params.get("app") or ""))
        elif action in ("stop", "remove", "delete", "cancel", "unschedule"):
            sid = str(params.get("id") or params.get("schedule") or "").strip()
            if not sid:
                return _err("say which schedule to stop — the id comes from action=list")
            resp = mod.schedule_delete(sid)
        elif action == "create":
            app = str(params.get("app") or "").strip()
            capability = str(params.get("capability") or "").strip()
            if not (app and capability):
                return _err("a repeat has to name what to run: pass both app and "
                            "capability (athena_capabilities lists them)",
                            app=app or None, capability=capability or None)
            row = _app_row(mod, app)
            if row is None:
                return _err(f"no installed app matches {app!r}")
            inputs = params.get("inputs") or []
            if not isinstance(inputs, list):
                inputs = [inputs]
            try:
                timeout = float(params.get("timeout") or 900)
            except (TypeError, ValueError):
                timeout = 900.0
            body = mod.ScheduleBody(capability=capability, inputs=inputs,
                                    schedule=str(params.get("schedule") or ""),
                                    name=params.get("name") or None, timeout=timeout,
                                    then=params.get("then") or None)
            resp = mod.app_schedule(str(row["id"]), body)
        else:
            return _err(f"unknown action {action!r} — create, list or stop")
        out = json.loads(bytes(resp.body).decode("utf-8"))
        if resp.status_code != 200:
            extra = {k: v for k, v in out.items() if k not in ("ok", "error")}
            return _err(out.get("error") or "the schedule was refused", **extra)
        return _ok(**{k: v for k, v in out.items() if k != "ok"})
    except Exception as e:  # noqa: BLE001
        return _err(str(e), trace=traceback.format_exc()[-800:])


def handle_build_app(params, **kwargs):
    """Turn a repository into an Athena app — scaffold it, then hand over the parts
    that need reading code.

    Athena has no model of its own: the agent is the generator. This lays down the
    launcher skeleton (clone step, venv wiring, update/reset, metadata) and the
    analysis copy of the repository, then returns everything needed to finish the
    job by hand — the guide to follow, the SPEC the scanner wrote, and what it
    found. After this, edit the launcher files, validate them, install, launch and
    fetch the URL to prove it works.
    """
    del kwargs
    action = str(params.get("action") or "scaffold").strip().lower()
    if action in ("auto", "automatic", "ladder"):
        url = str(params.get("url") or "").strip()
        if not url:
            return _err("no repository given")
        mod = _load_api()
        fn = getattr(mod, "auto_build", None)
        if not callable(fn):
            return _err("the automatic build is not in this build — restart Hermes so "
                        "the backend reloads")
        try:
            wait = float(params.get("wait") or 90.0)
        except (TypeError, ValueError):
            wait = 90.0
        r = fn(url, name=(params.get("name") or None), force=bool(params.get("force")), wait=wait)
        if not r.get("ok"):
            return _err(r.get("error") or "the build did not start",
                        **{k: v for k, v in r.items() if k != "ok"})
        return _ok(**r, note="This runs as a job. Poll it with athena_job: the result carries "
                             "verified=true when the app was seen answering on a port, or "
                             "escalate=true with the stage, reason and SPEC to satisfy.")
    if action in ("smoke", "prove", "verify_run"):
        app = str(params.get("app") or params.get("name") or "").strip()
        if not app:
            return _err("no app given — pass the app's name to smoke-test it")
        try:
            wait = float(params.get("wait") or 75.0)
        except (TypeError, ValueError):
            wait = 75.0
        mod = _load_api()
        fn = getattr(mod, "smoke_app", None)
        if not callable(fn):
            return _err("the smoke test is not in this build — restart Hermes so "
                        "the backend reloads")
        res = fn(app, wait=wait)
        if not res.get("ok"):
            return _err(res.get("error") or res.get("note") or "the app did not come up",
                        **{k: v for k, v in res.items() if k != "ok"})
        return _ok(**res)
    url = str(params.get("url") or "").strip()
    if not url:
        return _err("no repository given")
    try:
        mod = _load_api()
        builder = getattr(mod, "athena_buildapp", None)
        if builder is None:
            return _err("the app builder is unavailable in this build")
        res = builder.scaffold(Path(mod.engine.home), url,
                               name=params.get("name"),
                               force=bool(params.get("force")))
        if not res.get("ok"):
            return _err(res.get("error") or "could not build an app from that repository",
                        app=res.get("app"), already_an_app=bool(res.get("already_an_app")))
        out = {k: v for k, v in res.items() if k != "readme_head"}
        out["next_steps"] = [
            f"Read {res['guide']} — it is the contract for the launcher files.",
            f"Read {Path(res['path']) / 'SPEC.md'} — what the scanner found and what it could not decide.",
            "Fix install.js and start.js for this project (the launch command and the URL event).",
            "Validate every script with node --check, then install, launch, and fetch the URL.",
        ]
        return _ok(**out)
    except Exception as e:  # noqa: BLE001
        return _err(str(e), trace=traceback.format_exc()[-800:])


# ------------------------------------------------------------------ schemas

_STR = {"type": "string"}
_OBJ = {"type": "object"}


def _schema(name: str, desc: str, props: Dict[str, Any], required=None) -> Dict[str, Any]:
    return {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props,
                           "required": required or []}}


SCHEMAS = {
    "status": _schema(
        "athena_status",
        "Check Athena's local app-store engine: whether it is running, its port, "
        "home folder on disk, and which credentials (GitHub, HuggingFace) are inherited from Hermes.",
        {},
    ),
    "apps": _schema(
        "athena_apps",
        "List every AI app installed in Athena (the local Hermes app store): whether "
        "each is running, the URL its web UI serves on, what kind of app it is "
        "(gradio or comfyui), and the folder it lives in. This is the inventory to "
        "check before launching, generating or deleting anything.",
        {},
    ),
    "search": _schema(
        "athena_search",
        "Search the AI app store for installable apps. Returns each app's name, repo, version "
        "and an install_with handle. Pass that handle straight to athena_install.",
        {"query": {**_STR, "description": "Search terms, e.g. 'image upscaler', 'voice clone', 'whisper'. Searches the whole store catalogue, not just installed apps."}},
        ["query"],
    ),
    "install": _schema(
        "athena_install",
        "Install an AI app into Athena from a git repository URL (or catalog app name). "
        "The app installs locally inside Hermes and becomes available as a running app.",
        {"url": {**_STR, "description": "Git repo URL or catalog app name to install"}},
        ["url"],
    ),
    "launch": _schema(
        "athena_launch",
        "Start an installed Athena app so its local web UI becomes reachable, and return "
        "the URL. A cold start binds its port about ten seconds in, so a missing URL "
        "means 'still booting', not 'failed'.",
        {"app": {**_STR, "description": "App id/name as returned by athena_apps"},
         "script": {**_STR, "description": "Which of the app's menu entries to run — the text "
                    "from `choices` (e.g. 'Start'). An app that marks no default returns "
                    "`needs_choice` with the list; call again with one of them."},
         "wait": {"type": "integer", "description": "Seconds to wait for the URL (default 0, max 20)"}},
        ["app"],
    ),
    "stop": _schema(
        "athena_stop",
        "Stop a running Athena app and free its port.",
        {"app": {**_STR, "description": "App id/name as returned by athena_apps"}},
        ["app"],
    ),
    "logs": _schema(
        "athena_logs",
        "Read an installed Athena app's output — and what its log says went wrong. Returns "
        "`reading`: the named cause (out of memory on the card or in RAM, a missing weight, "
        "a port already taken, a missing package, a gated repository, a build that does not "
        "match the card, the network) with the action that answers it. `why=1` returns only "
        "the reading, which is the fastest way to answer \"why did this fail\".",
        {"app": {**_STR, "description": "App id/name as returned by athena_apps"},
         "lines": {"type": "integer", "description": "How many trailing lines to return (default 120)"},
         "why": {"type": "boolean",
                 "description": "Return only the named cause and its fix, without the log lines"}},
        ["app"],
    ),
    "store_detail": _schema(
        "athena_store_detail",
        "The full store record for one app — description, repo, version, tags, install "
        "size — before installing it.",
        {"ref": {**_STR, "description": "Store slug, store page URL, or owner/repo"}},
        ["ref"],
    ),
    "delete": _schema(
        "athena_delete",
        "Uninstall an Athena app: stop whatever it is running and delete its folder from "
        "disk, freeing the app's whole install size. Runs as a job and reports what it "
        "actually removed.",
        {"app": {**_STR, "description": "App id/name as returned by athena_apps"},
         "wait": {"type": "integer", "description": "Seconds to follow the job before returning (default 45)"}},
        ["app"],
    ),
    "fix": _schema(
        "athena_fix",
        "Repair an installed Athena app in place: verify its structure, repoint "
        "environments copied from another install, repair submodules and cache settings, "
        "and re-run the app's own install script when it has never run in this copy. Use "
        "this instead of reinstalling. Runs as a job; returns the health check afterwards.",
        {"app": {**_STR, "description": "App id/name as returned by athena_apps"},
         "provision": {"type": "boolean", "description": "Also run the app's install script when needed (default true)"},
         "wait": {"type": "integer", "description": "Seconds to follow the job before returning (default 45)"}},
        ["app"],
    ),
    "report": _schema(
        "athena_report",
        "What needs you right now, in one call: apps that cannot run (with the check "
        "that failed and the step that fixes it), apps that stopped recently and are not "
        "running (with the log's own reading of why), updates already known to be waiting, "
        "weights no app reads, and launches queued for the graphics card. `headline` is "
        "the one-line answer; every read is local and no update check happens here, so "
        "apps that were never checked are reported as unchecked rather than as up to date.",
        {"limit": {"type": "integer",
                   "description": "How many app folders to read (default 200)"}},
    ),
    "app_health": _schema(
        "athena_app_health",
        "Check an installed app's structure and report each check without changing anything "
        "— the diagnosis half of athena_fix. `failure` carries the log's own account of why "
        "it last stopped, when the log names one, with the action that answers it.",
        {"app": {**_STR, "description": "App id/name as returned by athena_apps"},
         "deep": {"type": "boolean", "description": "Also start its environments to check packages (slower)"}},
        ["app"],
    ),
    "models": _schema(
        "athena_models",
        "Weights: what this machine can generate with, and getting what it lacks. Call "
        "with no arguments at all for the shelf — every app's verdict, every weight "
        "library with what it holds and what it costs on disk, what nothing reads any "
        "more, and which HuggingFace repositories are already cached. Call with `app` "
        "for one app's verdict and the folders that would fix it; with `app` plus "
        "`folder` to point that app at them (the answer is the re-checked verdict, so "
        "no second call is needed). Call with `repo` (`owner/name`) to ask HuggingFace "
        "what a repository holds and what it costs before anything is fetched — the "
        "reply says whether a copy is already on this disk, which is a reason not to "
        "download at all. Add `download: true` to actually pull it: that starts a job "
        "and answers with the id to watch, and with `app` named the file lands in the "
        "library that app already reads, so an app that could not generate can after "
        "one call. Downloads run one at a time and resume where they stopped. ComfyUI "
        "apps only: a clean checkout's own models/ is empty, which is why a machine "
        "whose weights live elsewhere reads as 'cannot generate'.",
        {"app": {**_STR, "description": "App id/name as returned by athena_apps (omit for the whole shelf)"},
         "folder": {**_STR, "description": "Folder to point the app at (omit to just ask)"},
         "force": {"type": "boolean", "description": "Replace an existing mapping instead of refusing"},
         "repo": {**_STR, "description": "HuggingFace repository, `owner/name`, to ask about or pull"},
         "download": {"type": "boolean", "description": "Actually pull `repo` (default is to just report what it holds)"},
         "include": {**_STR, "description": "Only these files from `repo`, as a glob (e.g. '*.safetensors')"},
         "key": {**_STR, "description": "Which folder inside the library the file belongs in (default checkpoints)"},
         "revision": {**_STR, "description": "Branch, tag or commit of `repo` to take"}},
        [],
    ),
    "settings": _schema(
        "athena_settings",
        "Read Athena's settings (home folder, engine port, autostart, shared model cache), "
        "or change them by passing any of those fields. Calling it with no fields only reads.",
        {"home": {**_STR, "description": "Athena's home folder (new value)"},
         "port": {"type": "integer", "description": "Engine port"},
         "autostart": {"type": "boolean", "description": "Start the engine when Hermes opens"},
         "share_caches": {"type": "boolean", "description": "Share one model cache across apps"}},
    ),
    "files": _schema(
        "athena_files",
        "Browse and edit files — Athena's folders, and the person's own machine. "
        "Operations: roots (where to start from), list, stat, read, "
        "write, mkdir, delete (to the recycle bin), search (by name), grep (by content). "
        "Reading reaches Athena's home and every fixed disk; writing reaches Athena's home "
        "and the person's own folder. Credential stores (~/.ssh, browser password stores, "
        "the Windows hives) are refused wherever they are. A refusal names what is allowed. "
        "Returns per op — list: entries; stat: size/mtime; read: text/is_binary; write: "
        "ok/size; mkdir: path; delete: done/failed; search: entries; grep: hits.",
        {"op": {**_STR, "description": "roots | list | stat | read | write | mkdir | delete | search | grep"},
         "path": {**_STR, "description": "Folder or file to act on (absolute, or a path a previous call returned)"},
         "paths": {"type": "array", "items": _STR, "description": "Several paths, for delete"},
         "name": {**_STR, "description": "New folder name, for mkdir"},
         "text": {**_STR, "description": "Content to write (creates the file if it is absent)"},
         "query": {**_STR, "description": "Name fragment, for search"},
         "pattern": {**_STR, "description": "Regex, for grep"},
         "exts": {"type": "array", "items": _STR, "description": "Limit grep to these extensions, e.g. ['py','json']"},
         "max_kb": {"type": "integer", "description": "Read/write cap in KB"},
         "show_hidden": {"type": "boolean", "description": "Include dotfiles in a listing"},
         "dir_size": {"type": "boolean", "description": "Total the folder size in stat (slow)"},
         "max_results": {"type": "integer", "description": "Cap for search"},
         "max_hits": {"type": "integer", "description": "Cap for grep"}},
        ["op"],
    ),
    "space": _schema(
        "athena_space",
        "Disk space and the shared model store for Athena: every drive with free space, how "
        "much Athena's apps use, and the engine's duplicate-file state. Actions: scan "
        "(find duplicates), dedupe (hardlink them), free (reclaim the anchor copies), "
        "separate (undo — restore real copies). Actions run as jobs.",
        {"action": {**_STR, "description": "scan | dedupe | free | separate — omit to just read"},
         "wait": {"type": "integer", "description": "Seconds to follow the job before returning (default 20)"}},
    ),
    "job": _schema(
        "athena_job",
        "Where a long Athena action got to — installs, deletes, fixes, disk actions. Returns "
        "the state and the tail of the job's log, which is where a failure explains itself.",
        {"job": {**_STR, "description": "Job id returned by another Athena tool"},
         "lines": {"type": "integer", "description": "How many trailing log lines (default 40)"}},
        ["job"],
    ),
    "app_verify": _schema(
        "athena_capabilities",
        "Ask an installed Athena app what it can actually do, and get back the "
        "capabilities to run — each with an intent to ask for, the inputs it needs in "
        "order, and whether it accepts a file from this machine. The app answers for "
        "itself: it is started if needed and probed live, and the answer is saved beside "
        "it so later calls are instant. ComfyUI apps report saved workflows and whether "
        "any model is present, because they have no plain call path. Use this before "
        "asking an app to make something you have not made with it before.",
        {"app": {**_STR, "description": "One app id/name, or omit to check every installed app"},
         "refresh": {"type": "boolean", "description": "Probe again even if an answer is saved (default false)"}},
    ),
    "history": _schema(
        "athena_runs",
        "What has already been made here — every job an Athena app has run, newest "
        "first, with what was asked for, what went in, what came out and where it "
        "landed on disk. Use it before making something similar again, or to point at "
        "an earlier result; the recipe and the output files are both on disk under the "
        "run's folder. A run marked 'running' is happening now.",
        {"limit": {"type": "integer", "description": "How many runs (default 20)"},
         "app": {**_STR, "description": "Only this app's runs"}},
    ),
    "schedule": _schema(
        "athena_schedule",
        "Arrange for one of an installed app's capabilities to run again on its own — "
        "'run this every morning', 'every 6 hours' — or see what already does, or stop "
        "one. A repeat is stored in Hermes' own cron as a script job, NOT an agent "
        "turn: the script calls Athena directly, so a scheduled run costs no model "
        "call to start, and it is refused here for the same reasons a call is refused "
        "(app not installed, capability not in the manifest, required inputs not "
        "given, a time Hermes cannot read). The run lands in Athena's Runs tab beside "
        "the ones started by hand.",
        {"action": {**_STR, "description": "create (default) | list | stop"},
         "app": {**_STR, "description": "An app id/name (create, or to filter list)"},
         "capability": {**_STR, "description": "A capability name or intent from athena_capabilities"},
         "inputs": {"type": "array", "description": "Inputs in order — local file paths for media inputs, plain values otherwise"},
         "schedule": {**_STR, "description": "How often, in Hermes' words — 'every day at 8am', 'every 6 hours', or a cron expression like '0 7 * * 1'"},
         "name": {**_STR, "description": "A label for the repeat, for the runs list"},
         "timeout": {"type": "integer", "description": "Seconds to allow each run (default 900)"},
         "then": {"type": "array", "description": "Optional chain: [{app, capability}, …] run on what this one makes"},
         "id": {**_STR, "description": "For action=stop: the schedule id from action=list"}},
    ),
    "generate": _schema(
        "athena_generate",
        "Have an installed Athena app make something — remove a background, generate an "
        "image, video, song or 3D model — and get the files back. Pass a plain request "
        "like 'remove the background from this image' together with inputs (local file "
        "paths, in the order the capability wants them), and this picks the app and "
        "capability that fit, starts the app if it is not running, uploads the files, "
        "runs the job, downloads every output into Athena's runs folder and writes a "
        "recipe of exactly what was done. Or name an app and a capability directly. The "
        "app must have been probed first (athena_capabilities); otherwise there is "
        "nothing to match a request against.",
        {"request": {**_STR, "description": "What to do, in plain words — e.g. 'remove the background from this image'"},
         "app": {**_STR, "description": "An app id/name, to skip the search"},
         "capability": {**_STR, "description": "A capability name or intent from athena_capabilities"},
         "inputs": {"type": "array", "description": "Inputs in order — local file paths for media inputs, plain values otherwise"},
         "timeout": {"type": "integer", "description": "Seconds to allow the app (default 900)"},
         "then": {"type": "array", "description": "Optional chain of stages — [{app, capability}, …] — each run on what the stage before it made. One call, one result: the whole chain is checked before anything starts, and a stage that makes nothing stops the chain rather than pretending."}},
    ),
    "build_app": _schema(
        "athena_build_app",
        "Turn a git repository into an Athena app. Use this when the user wants an app that "
        "is not in the store, or when installing a repo failed because it has no launcher. "
        "It clones the repository into Athena's analysis cache, reports what kind of project "
        "it is (stack, entry point, install commands, whether it needs the AI bundle), and "
        "writes a launcher skeleton into Athena's apps folder along with a SPEC.md of what "
        "the scanner found. It does NOT finish the job: you then read "
        "plugins/athena/docs/build-an-app.md, fix install.js and start.js for this project, "
        "validate with node --check, and use athena_install / athena_launch to prove it "
        "actually runs. If the repository is not a runnable app, say so instead of inventing "
        "one.",
        {"action": {**_STR, "description": "\"scaffold\" (default) to lay down a launcher from a "
                                          "repo, \"auto\" to build it unattended (scaffold, install, "
                                          "prove) and get a verdict, or \"smoke\" to launch an "
                                          "existing app and prove it serves"},
         "url": {**_STR, "description": "Repository URL, owner/repo, or a store branch URL"},
         "name": {**_STR, "description": "Folder name for the app (default: derived from the repo)"},
         "app": {**_STR, "description": "For action=\"smoke\": the app to launch and prove"},
         "wait": {"type": "integer", "description": "For action=\"smoke\": seconds to wait for a port (default 75)"},
         "force": {"type": "boolean", "description": "Rebuild even if a launcher already exists"}},
        [],
    ),
    "publish": _schema(
        "athena_publish",
        "Publish this machine's installed apps to the Athena Store — one branch per app "
        "in the store repository, plus the index on main — using Hermes' own GitHub "
        "login. Starts a job and returns its id; every app is republished, because the "
        "store is a mirror of this machine.",
        {"app": {**_STR, "description": "The installed app whose card the publish is "
                                        "for. Every app is republished; this names the "
                                        "one that prompted it."},
         "wait": {"type": "integer", "description": "Seconds to watch the job before "
                                                    "returning (default 45)"}},
        ["app"],
    ),
    "machines": _schema(
        "athena_machines",
        "A real OS Athena can start, stop, run commands in, snapshot and show you: a WSL "
        "distro, for testing something that only installs on Linux, or checking an app on "
        "another OS. Operations: list (machines, plus distros not yet claimed by one), "
        "setup (what is installed here and the question to ask before installing the rest), "
        "isos (the image folder and everything inside its subfolders), folders (what is in one "
        "folder, for picking one), set_iso_dir, fit (the size this computer "
        "can give a guest), create, start, stop, exec, snapshot, console (what the guest "
        "itself has said), screen (what a QEMU guest is showing right now, saved as a picture "
        "beside the machine), type (a keystroke, a click or a line of text sent into a guest "
        "\u2014 the installer's questions can be answered without a person at the panel; refused "
        "with human_has_control while somebody is using that screen themselves, unless force), "
        "paste (text into the guest's own clipboard, which is how a URL, a key or a block of "
        "config gets in when typing it a character at a time would outlast the install), "
        "lease (who has that screen's keyboard right now, and handing it back), display "
        "(where that screen lives: in the panel, or in its own window), edit (change a "
        "machine's settings: cores, memory, disk size, screen, what it boots, its image, its "
        "ports, its notes \u2014 a running machine is restarted when a change needs it), delete. "
        "Nothing is ever "
        "installed for you — a "
        "missing WSL or QEMU comes back as needs_setup carrying the exact command, and you "
        "ask the person before running it. Machines are Athena-owned by default; attach "
        "points one at a distro of theirs, which is the one case where a command can touch "
        "their own work.",
        {"op": {**_STR, "description": "list | setup | isos | folders | set_iso_dir | fit | create | start | stop | exec | snapshot | console | screen | type | lease | display | boot | edit | forgotten | restore | delete"},
         "changes": {**_OBJ, "description": "For op=edit: any of cpus, memory_mb, disk_gb, display, boot, image, ports, ssh, notes \u2014 a subset is enough. A field the engine does not know comes back refused by name."},
         "name": {**_STR, "description": "Machine name: letters, digits, dots, dashes or underscores"},
         "preset": {**_STR, "description": "ubuntu-24.04 (the default) | debian | arch | custom-iso"},
         "provider": {**_STR, "description": "wsl (a distro, the default) | qemu (boot an ISO or disk image). custom-iso implies qemu."},
         "boot": {**_STR, "description": "For op=boot on a QEMU machine with an ISO attached: system (start on what its disk holds \u2014 the default) | medium (start on the ISO again, for a live or rescue medium). Changing it restarts a running machine."},
         "display": {**_STR, "description": "For a QEMU machine: panel (the default \u2014 its screen is read and shown in the panel) | window (a real QEMU window of its own) | none (headless and unwatched). For op=display: the same three, to move a screen between the panel and a window."},
         "image_path": {**_STR, "description": "A rootfs tarball, ISO or disk image already on this machine"},
         "image_url": {**_STR, "description": "Or a URL to fetch one from, once, into the machine's own folder"},
         "image_kind": {**_STR, "description": "What that image is: rootfs (a WSL tarball) | iso (install media) | disk (a qcow2/raw disk to boot as itself). Defaults to iso for a QEMU machine, rootfs otherwise."},
         "os": {**_STR, "description": "What will boot: linux | windows | anything. A Windows guest is given IDE disks and an e1000 NIC, because an installer has no virtio driver and would report a machine with no hard drive."},
         "ports": {"type": "array", "description": "Ports to forward into the guest — \"8080:80\" and {\"host\": 8080, \"guest\": 80} mean the same thing. A QEMU guest does NOT share your localhost, so a port that is not listed here is unreachable."},
         "vnc_port": {"type": "integer", "description": "Display number (not a port number) for the guest's VNC screen, 0 to let QEMU pick. Loopback only."},
         "ssh_port": {"type": "integer", "description": "Host port to forward to the guest's 22, so ssh -p <port> reaches it once it has an sshd"},
         "cpus": {"type": "integer", "description": "Virtual cores. Omit it and the machine is sized for this computer (half the cores, 2..8, always leaving the host at least one) — see op fit for the numbers and the reasoning."},
         "memory_mb": {"type": "integer", "description": "Memory in MB. Omit it and it is a quarter of this computer's RAM, 2 GB..16 GB, never more than half."},
         "disk_gb": {"type": "integer", "description": "Disk to create in GB. Omit it and it is 40 GB when this computer has room, less when the disk is nearly full; a machine booting its own disk image gets none."},
         "dir": {**_STR, "description": "For isos/set_iso_dir: the folder to look in. Omit it and you get the folder in use. Changing it is the user's call — ask, never point it somewhere on your own."},
         "path": {**_STR, "description": "For folders: one folder to list the folders inside. Omit it and you get a drive to start from. Read-only — this lists a folder somebody asked about, it never walks a drive."},
         "make": {"type": "boolean", "description": "For set_iso_dir: create the folder if it does not exist yet. Without it a missing path comes back as a question, not a mkdir."},
         "limit": {"type": "integer", "description": "For console: how many bytes of the end of the guest's console to read. Defaults to 8000."},
         "distro": {**_STR, "description": "The WSL distro to create, or with attach, the one to use"},
         "attach": {"type": "boolean", "description": "Point the machine at an existing distro of the user's instead of creating one. Never the default: an agent shell would then share their environment."},
         "command": {**_STR, "description": "For exec: what to run inside the machine"},
         "user": {**_STR, "description": "For exec: which user to run as — root by default in an Athena-owned distro"},
         "cwd": {**_STR, "description": "For exec: the directory to start in"},
         "timeout": {"type": "integer", "description": "For exec: seconds to allow (default 300, max 3600)"},
         "text": {**_STR, "description": "For type: a line of text to type into the guest. This is how the installer's questions get answered from here."},
         "key": {**_STR, "description": "For type: one key to press instead of text \u2014 Enter, Tab, Escape, Down, or a single letter."},
         "events": {"type": "array", "description": "For type: raw input events, for anything text and key do not cover. Each is {\"kind\": \"key\", \"key\": \"Enter\"} or \"Control+c\", {\"kind\": \"text\", \"text\": \"...\"} or {\"kind\": \"pointer\", \"x\": 0, \"y\": 0, \"buttons\": [\"left\"]} \u2014 the panel's own shape."},
         "force": {"type": "boolean", "description": "For type: take the keyboard from a person who is using the screen right now. Without it, typing into a screen somebody else is driving is refused with human_has_control \u2014 which is the honest answer, not an obstacle: two hands on one guest's keyboard means the guest acts on a combination neither of you meant. Only pass it when the person asked you to."},
         "release": {"type": "boolean", "description": "For lease: hand the keyboard back. Leave it off to just ask who has it."},
         "unregister": {"type": "boolean", "description": "For delete: destroy the distro and its disk as well as the record. Without it the machine is only forgotten: for a QEMU machine its folder is moved to forgotten/ with the disk intact, and op=forgotten lists those. A QEMU machine's distro lives in that disk file, so forgetting it is how you keep one."}},
        ["op"],
    ),
}


def handle_machines(params, **kwargs):
    """A real OS Athena can start, stop, run commands in, and show you.

    Every op here is a call into the engine's MachineHost, so the panel and an
    agent take the same path and cannot disagree about what a machine is; the only
    thing this adds is turning the JSON the host returns into the success/error
    shape the agent reads, and keeping the sentences that matter (the ones about
    installing something) intact.
    """
    del kwargs
    try:
        mod = _load_api()
        host = getattr(mod, "machines_host", None)
        if host is None:
            return _err("machines are unavailable in this build")
        op = str(params.get("op") or "list").strip().lower()
        name = str(params.get("name") or "").strip()
        known = ("list", "setup", "isos", "folders", "set_iso_dir", "fit", "create", "start", "stop",
                 "exec", "snapshot", "console", "screen", "type", "paste", "lease", "display", "boot", "edit",
                 "delete", "forgotten", "restore")
        # Validated first: an op nobody knows is not a missing machine name, and
        # answering the wrong question is how an agent learns to ignore the answer.
        if op not in known:
            return _err(f"unknown machines op {op!r}; try " + ", ".join(known[:-1])
                        + " or " + known[-1])

        if op in ("list", "setup"):
            res = host.list() if op == "list" else host.setup()
            return _ok(op=op, **{k: v for k, v in res.items() if k != "ok"})

        if op == "edit":
            # The rules are the engine's (`machines.apply_patch`), so an agent gets the same
            # sentence a person would see in the panel. What an agent may *not* do is send a
            # field the engine does not know: `changes` is read as written and refused by
            # name rather than dropped, because a dropped setting is a machine that quietly
            # did not change.
            if not name:
                return _err("name the machine to change")
            changes = params.get("changes")
            if not isinstance(changes, dict) or not changes:
                fields = {k: params[k] for k in
                          ("cpus", "memory_mb", "disk_gb", "display", "boot", "image",
                           "ports", "ssh", "notes") if k in params}
                changes = fields
            if not changes:
                return _err("say what to change: " + ", ".join(
                    ("cpus", "memory_mb", "disk_gb", "display", "boot", "image", "ports",
                     "ssh", "notes")))
            res = host.edit(name, changes)
            if not res.get("ok"):
                return _err(res.get("error") or "the machine was not changed",
                            **{k: v for k, v in res.items() if k not in ("ok", "error")})
            return _ok(op=op, **{k: v for k, v in res.items() if k != "ok"})

        if op == "fit":
            res = host.fit(str(params.get("provider") or "qemu"))
            if not res.get("ok"):
                return _err(res.get("error") or "could not size a guest for this computer")
            return _ok(op=op, **{k: v for k, v in res.items() if k != "ok"})

        if op == "folders":
            # Read-only, and dirs-only: an agent helping somebody find their image
            # folder looks at one folder they named, and lists what folders are in it.
            res = host.folders(str(params.get("path") or ""))
            if res.get("ok") is False:
                return _err(res.get("error") or "could not read that folder")
            return _ok(op=op, **{k: v for k, v in res.items() if k != "ok"})

        if op in ("isos", "set_iso_dir"):
            # A read for `isos`; `set_iso_dir` writes the user's chosen folder and is
            # never something an agent does on its own behalf: the folder is where the
            # person keeps their ISOs, and moving it silently would make the menu a
            # lie about their own disk.
            if op == "isos":
                res = host.isos(str(params.get("dir") or ""))
            else:
                if not (params.get("dir") is not None or params.get("make")):
                    return _err("set_iso_dir needs a folder to point at (dir), or an empty "
                                "dir to go back to Athena's own isos folder")
                res = host.set_iso_dir(str(params.get("dir") or ""), create=bool(params.get("make")))
            if not res.get("ok"):
                return _err(res.get("error") or f"{op} failed",
                            **{k: v for k, v in res.items() if k not in ("ok", "error")})
            return _ok(op=op, **{k: v for k, v in res.items() if k != "ok"})

        if op == "create":
            if not name:
                return _err("a machine needs a name")
            m = mod.athena_machines
            preset = m.PRESETS.get(str(params.get("preset") or "")) or {}
            provider = str(params.get("provider") or preset.get("provider") or "wsl")
            image = dict(preset.get("image") or {})
            # The kind is never guessed from the filename: a .iso and a WSL rootfs
            # tarball are both "a path someone typed", and guessing right four times
            # out of five is how a machine ends up booting nothing.
            kind = str(params.get("image_kind") or image.get("kind")
                       or ("iso" if provider == "qemu" else "rootfs"))
            if params.get("image_path"):
                image.update({"kind": kind, "path": str(params["image_path"])})
            if params.get("image_url"):
                image.update({"kind": kind, "url": str(params["image_url"])})
            # A machine nobody sized is sized for *this computer* rather than for the
            # defaults somebody else had in mind: the fit is read from the host's own
            # cores, RAM and free space, and the reasoning is in op fit.
            fit = host.fit(provider)
            if not fit.get("ok"):
                return _err(fit.get("error") or "could not size a guest for this computer")
            try:
                spec = m.MachineSpec(
                    name=name, provider=provider,
                    os=str(params.get("os") or preset.get("os") or ""), image=image,
                    distro=str(params.get("distro") or ""), attach=bool(params.get("attach")),
                    display=str(params.get("display") or "panel"),
                    cpus=int(params.get("cpus") or fit["cpus"]),
                    memory_mb=int(params.get("memory_mb") or fit["memory_mb"]),
                    disk_gb=int(params.get("disk_gb") or fit["disk_gb"]),
                    # "8080:80" and {"host": 8080, "guest": 80} are the same port pair.
                    ports=[m.parse_port_pair(x) for x in (params.get("ports") or [])],
                    vnc_port=int(params.get("vnc_port") or 0),
                    ssh={"port": int(params["ssh_port"])} if params.get("ssh_port") else {},
                )
                spec.validate()
            except m.SpecError as e:
                return _err(str(e))
            except (TypeError, ValueError) as e:
                return _err(f"a machine field is not the shape it needs to be: {e}")
            res = host.create(spec)
            if not res.get("ok"):
                return _err(res.get("error") or "could not create the machine")
            return _ok(op=op, **{k: v for k, v in res.items() if k != "ok"})

        if not name:
            return _err(f"{op} needs a machine name")

        if op == "start":
            # `watched=True` here and nowhere else: this is the agent's tool, so a guest
            # it starts goes on the idle clock and is stopped once nothing is using it.
            # The panel's Start button takes the default and is never on the clock.
            res = host.start(name, watched=True)
        elif op == "stop":
            res = host.stop(name)
        elif op == "exec":
            if not str(params.get("command") or "").strip():
                return _err("exec needs a command to run")
            # The schema advertises "default 300, max 3600" and nothing enforced
            # it in either direction: a call could pin the tool for a day, and a
            # value that is not a number came back as a bare int() traceback out
            # of the outer handler. The documented bound is the real one now.
            try:
                seconds = int(float(params.get("timeout") or 300))
            except (TypeError, ValueError):
                return _err("timeout must be a number of seconds (default 300, max 3600)")
            res = host.exec_(name, str(params["command"]), user=str(params.get("user") or ""),
                             cwd=str(params.get("cwd") or ""),
                             timeout=min(max(1, seconds), 3600))
        elif op == "snapshot":
            res = host.snapshot(name)
        elif op == "console":
            # A QEMU guest has no shell to type into, so its serial console is the only
            # part of it an agent can read: the boot loader, the installer's first screen,
            # a kernel panic. Read from the end, and it says where the screen is too.
            res = host.console(name, limit=int(params.get("limit") or 8000))
        elif op == "screen":
            # A guest's screen, saved beside the machine, because a picture is the one
            # thing an agent cannot be handed as text: the reply says where it landed and
            # how big the guest's screen currently is.
            res = host.screen(name, save=True)
        elif op == "type":
            # What an installer asks for next is usually a keystroke or a click, so an
            # agent can answer it without a person sitting at the panel.
            events = params.get("events")
            if events is None and params.get("text"):
                events = [{"kind": "text", "text": str(params.get("text"))}]
            if events is None and params.get("key"):
                events = [{"kind": "key", "key": str(params.get("key"))}]
            # `who="agent"` is not a label: it is what lets a person at the panel keep the
            # keyboard. Their input always goes through and takes the hold, and while they
            # hold it this call is refused with `human_has_control` rather than sent — two
            # hands on one guest's keyboard is a guest acting on a combination neither of
            # them meant. `force` takes the keyboard back from a person, which is the
            # person's own tool doing what they asked, and the answer says so.
            res = host.screen_input(name, events or [], who="agent",
                                    force=bool(params.get("force")))
        elif op == "paste":
            # Text into the guest's clipboard, which is how a URL, a key or a block of
            # config gets in. `type` is still the right call for a keystroke — this is
            # for the things that are not a keystroke, and for the ones long enough that
            # typing them a character at a time would take longer than the install.
            body = str(params.get("paste") or params.get("text") or "")
            if not body:
                return _err("paste needs the text to paste")
            # `who=\"agent\"` for the same reason `type` passes it: a paste from an agent
            # must not land on a guest whose keyboard a person is holding.
            res = host.screen_input(name, [{"kind": "clipboard", "text": body}],
                                    who="agent", force=bool(params.get("force")))
        elif op == "lease":
            # Who has this guest's keyboard, and the way to hand it back. Asked before
            # typing rather than discovered by being refused: an agent that checks first
            # never sends a keystroke a person's installer did not need.
            res = host.screen_lease(name, release=bool(params.get("release")))
        elif op == "display":
            res = host.set_display(name, str(params.get("display") or "panel"))
        elif op == "boot":
            # What the machine starts on: `system` (its own disk) or `medium` (the ISO again).
            res = host.set_boot(name, str(params.get("boot") or "system"))
        elif op == "forgotten":
            res = host.forgotten()
        elif op == "restore":
            # The other half of "forget": the disk was kept, so it can come back whole.
            res = host.restore(name)
        elif op == "delete":
            res = host.delete(name, unregister=bool(params.get("unregister")))
        else:  # unreachable: the op was validated above
            return _err(f"unknown machines op {op!r}")

        if not res.get("ok"):
            # The verdict travels with the error: `needs_setup` is how the agent
            # learns that something has to be asked for rather than retried.
            extra = {k: v for k, v in res.items() if k not in ("ok", "error")}
            return _err(res.get("error") or f"{op} failed", **extra)
        # `name` is taken out of the passthrough as well as `ok`: the host reports the
        # machine it acted on, and passing that through next to the name this call
        # already sends is a duplicate keyword — an error raised by a *successful*
        # delete, which reads as the machine not having been deleted at all.
        return _ok(op=op, name=name,
                   **{k: v for k, v in res.items() if k not in ("ok", "name")})
    except Exception as e:  # noqa: BLE001
        return _err(f"machines failed: {e}", traceback=traceback.format_exc()[-600:])


# ---------------------------------------------------------------- the spotlight

# What each tool is *about*, as the panel names it. The key is `kind:name` and both
# sides have to agree on the name, so this mapping lives in exactly one place. A tool
# whose subject has no card in the panel simply has no entry.
_SPOT_SUBJECT = {
    "launch": "app", "stop": "app", "delete": "app", "fix": "app", "logs": "app",
    "app_health": "app", "app_verify": "app", "history": "app", "schedule": "app",
    "generate": "app",
    "install": "store",
    "machines": "machine",
}

# The argument each subject arrives under. Every app-shaped tool spells it `app`, but
# the schema documents `id` as an accepted alias, so both are read.
_SPOT_ARGS = {"app": ("app", "id"), "store": ("url", "name", "slug"), "machine": ("name",)}


def _spot_key(tool: str, params: dict) -> str:
    """`app:wan` — what the panel should reach for, or '' when this call has no target.

    Pure and total: it runs before every tool, so anything it can raise is a tool that
    cannot run.
    """
    kind = _SPOT_SUBJECT.get(tool)
    if not kind:
        return ""
    for arg in _SPOT_ARGS.get(kind, ()):
        val = str((params or {}).get(arg) or "").strip()
        if val:
            return f"{kind}:{val}"
    return ""


def _spot(tool: str, params: dict) -> None:
    """Tell the panel what this call is about, so it can wear the working arc.

    Never raises and never blocks. The arc is how somebody watches an agent work, and a
    tool that failed because the *watching* failed would be a worse trade than no arc.
    """
    try:
        key = _spot_key(tool, params)
        if not key:
            return
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("athena", "spot", {"key": key, "tool": tool, "ms": 4000})
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ register


def register(ctx):
    def _tool(key, handler):
        # Every tool announces what it is about *before* it runs, so the arc is up while
        # the work is happening rather than after it. Wrapping here instead of in each
        # handler is what keeps the two sides from drifting: a new tool with an entry in
        # `_SPOT_SUBJECT` is spotlighted without a second edit.
        def wrapped(params=None, **kwargs):
            _spot(key, params or {})
            return handler(params, **kwargs)

        wrapped.__name__ = getattr(handler, "__name__", "handler")
        try:
            ctx.register_tool(name=SCHEMAS[key]["name"], toolset="athena",
                              schema=SCHEMAS[key], handler=wrapped)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    _tool("status", handle_status)
    _tool("apps", handle_apps)
    _tool("search", handle_search)
    _tool("install", handle_install)
    _tool("launch", handle_launch)
    _tool("stop", handle_stop)
    _tool("logs", handle_logs)
    _tool("store_detail", handle_store_detail)
    _tool("publish", handle_publish)
    _tool("delete", handle_delete)
    _tool("fix", handle_fix)
    _tool("report", handle_report)
    _tool("app_health", handle_app_health)
    _tool("models", handle_models)
    _tool("settings", handle_settings)
    _tool("files", handle_files)
    _tool("space", handle_space)
    _tool("job", handle_job)
    _tool("app_verify", handle_app_verify)
    _tool("generate", handle_generate)
    _tool("history", handle_history)
    _tool("schedule", handle_schedule)
    _tool("build_app", handle_build_app)
    _tool("machines", handle_machines)
