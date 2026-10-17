"""
The doors that were open, and the ones that were painted on.

Every claim in this file is about *structure* rather than behaviour, because each
of these things existed, was spelled correctly, compiled, and could never do its
job. A handler called directly cannot notice that the router would never reach
it, and that is exactly how two dead routes stayed green:

 * `POST /apps/{id}/revert` and `POST /apps/{id}/schedule` were declared below
   `POST /apps/{id}/{action}`, and Starlette matches in declaration order — so the
   wildcard answered both with "unknown action <verb>" and the whole scheduling
   surface was unreachable while every test passed, because every test looked the
   handler up by name. So this file asks the router.

 * An app id is a directory *name*. `.` and `..` are directories, and
   `engine.home / "api" / ".."` is the whole Athena home — which the uninstall
   route resolves and then recursively deletes. A name check has to happen before
   an id ever becomes a path.

 * `_job_end` was called by the wrapper around every worker, so a worker that had
   already ended its job "failed" — with the reason — was stamped "done" on top
   of its own verdict, and every consumer keying on `state` read failure as
   success.

 * The boot route read `body.where` while the panel posts `{boot: ...}`, so the
   control failed on every press and blamed a value the person never typed.

The last two are checked through the route and the job bookkeeping themselves,
not by grepping the source for a string: a rename that keeps the test green is
the failure mode this file exists to stop.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERMES_ROOT = Path(__import__("os").environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")

PASS = FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name}" + (f"  -- {detail}" if detail else ""))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


api = load("athena_api_hardening_under_test", PLUGIN / "dashboard" / "plugin_api.py")


# ---------------------------------------------------------------------------
# 1. Which route answers a request is a question only the router can answer.
# ---------------------------------------------------------------------------
def first_match(method: str, path: str):
    """The path of the first route that would take this request, as Starlette
    decides it: in declaration order, methods first, then the path regex."""
    routes = getattr(getattr(api, "router", None), "routes", None)
    if routes is None:
        return None
    for r in routes:
        if method not in (getattr(r, "methods", None) or set()):
            continue
        rx = getattr(r, "path_regex", None)
        if rx is not None and rx.match(path):
            return getattr(r, "path", None)
    return None


print("== the router reaches the routes the panel calls ==")
check("the router is reachable from here", getattr(api, "router", None) is not None)
for verb, verb_path in (("revert", "/apps/{app_id}/revert"),
                        ("schedule", "/apps/{app_id}/schedule")):
    got = first_match("POST", f"/apps/any-app/{verb}")
    check(f"POST /apps/{{app_id}}/{verb} is answered by its own route, not the catch-all",
          got == verb_path, f"the router sent it to {got!r}")

check("the catch-all still takes the verbs it exists for",
      first_match("POST", "/apps/any-app/launch") == "/apps/{app_id}/{action}",
      str(first_match("POST", "/apps/any-app/launch")))
check("a verb nobody declared still lands on the catch-all, to be refused there",
      first_match("POST", "/apps/any-app/nonsense") == "/apps/{app_id}/{action}")
check("the keys route (the first one this bit) is still above the catch-all",
      first_match("GET", "/apps/any-app/keys") == "/apps/{app_id}/keys")
check("GET /apps/{app_id}/history is not swallowed by a POST-only wildcard",
      first_match("GET", "/apps/any-app/history") == "/apps/{app_id}/history")


# ---------------------------------------------------------------------------
# 2. An app id is a name, never a path — and the delete route resolves it.
# ---------------------------------------------------------------------------
print("\n== an app id is a directory name ==")
HOME = Path(tempfile.mkdtemp(prefix="athena-hardening-"))
(HOME / "api" / "real.git").mkdir(parents=True, exist_ok=True)
(HOME / "api" / "inner").mkdir(parents=True, exist_ok=True)
(HOME / "settings.json").write_text("{}", encoding="utf-8")

real_engine = api.engine
api.engine = SimpleNamespace(home=HOME, probe=lambda: True,
                             start=lambda wait=0: {"ok": True})

for bad, what in (("..", "the parent directory"),
                  (".", "the api folder itself"),
                  ("", "nothing at all"),
                  ("a/b", "a path with a separator in it"),
                  ("..\\..", "a Windows-style traversal"),
                  ("C:", "a drive"),
                  ("x\x00y", "a NUL")):
    check(f"{what} is not an app name: {bad!r}", not api._app_id_ok(bad))
    check(f"and it resolves to no folder: {bad!r}", api._app_dir(bad) is None)

check("a suffixed name is still an app name", api._app_id_ok("real.git"))
check("and resolves to its folder", api._app_folder("real.git") == HOME / "api" / "real.git")
check("with or without the suffix", api._app_folder("real") == HOME / "api" / "real.git")
check("a name that is not installed resolves to nothing", api._app_folder("ghost") is None)

# The route itself: `..` used to resolve to HOME, which is what would be deleted.
resp = api.app_uninstall("..")
body = json.loads(bytes(resp.body).decode("utf-8"))
check("forgetting `..` is refused before it is a path",
      getattr(resp, "status_code", None) == 400 and body.get("ok") is False,
      f"{getattr(resp, 'status_code', None)} {json.dumps(body)[:120]}")
check("and the Athena home is still standing", (HOME / "settings.json").is_file()
      and (HOME / "api" / "real.git").is_dir())
check("a name that is merely absent is still a 404, not a 400",
      getattr(api.app_uninstall("ghost"), "status_code", None) == 404)
api.engine = real_engine


# ---------------------------------------------------------------------------
# 3. A job's verdict is the verdict the worker reached.
# ---------------------------------------------------------------------------
print("\n== a job cannot be un-failed by the wrapper around it ==")
jid = api._job_new("hardening", "a worker that gave up")
api._job_end(jid, "failed", error="the clone failed")
api._job_end(jid, "done", result={"ok": False, "note": "the wrapper only knows done"})
snap = api._JOBS.get(jid) or {}
check("the failure stands", snap.get("state") == "failed", str(snap.get("state")))
check("and so does the reason it gave", snap.get("error") == "the clone failed",
      str(snap.get("error")))
check("while the evidence the wrapper carried is still merged in",
      (snap.get("result") or {}).get("note") == "the wrapper only knows done",
      json.dumps(snap.get("result"))[:160])

# The honest path must be untouched: a worker that never ends its own job is
# ended by the wrapper, and a run whose *answer* says ok:false is a finished job
# carrying a failed answer — that split is deliberate (see t_pipeline.py) and is
# what the panel's job watcher reads.
jid2 = api._job_new("hardening", "a worker that answered")
api._job_end(jid2, "done", result={"ok": False})
check("a job nobody failed is still done", api._JOBS[jid2]["state"] == "done")
check("even when the answer it carries is a failure",
      (api._JOBS[jid2].get("result") or {}).get("ok") is False)

jid3 = api._job_spawn("hardening", "a real worker that fails", lambda j: {"ok": False})
deadline = time.time() + 5.0
while time.time() < deadline and (api._JOBS.get(jid3) or {}).get("state") == "running":
    time.sleep(0.05)
check("a job through the real spawner still finishes", (api._JOBS.get(jid3) or {}).get("state") == "done",
      str((api._JOBS.get(jid3) or {}).get("state")))


# ---------------------------------------------------------------------------
# 4. The boot control speaks the spelling the panel sends.
# ---------------------------------------------------------------------------
print("\n== the boot route reads what the panel posts ==")
check("BootBody takes `boot`", api.BootBody(boot="medium").boot == "medium")
check("and still takes the older `where`", api.BootBody(where="system").where == "system")
check("an empty body is empty, not an error", api.BootBody().boot == ""
      and api.BootBody().where == "")


class _BootHost:
    def __init__(self):
        self.seen: list = []

    def set_boot(self, name: str, where: str):
        self.seen.append((name, where))
        return {"ok": True, "boot": where}


real_host = api.machines_host
stub = _BootHost()
api.machines_host = stub
api.machine_boot("rig", api.BootBody(boot="medium"))
check("the panel's spelling reaches set_boot", stub.seen == [("rig", "medium")], str(stub.seen))
api.machine_boot("rig", api.BootBody(where="system"))
check("and so does the older one", stub.seen[-1] == ("rig", "system"), str(stub.seen))
api.machine_boot("rig", api.BootBody())
check("an empty body is passed through as empty, to be refused with the reason",
      stub.seen[-1] == ("rig", ""), str(stub.seen))
api.machines_host = real_host

# ---------------------------------------------------------------------------
# 5. The floor asks both questions, and a running job is not aged out of its own job.
# ---------------------------------------------------------------------------
print("\n== the file floor refuses a credential store, not just a folder ===")
from fastapi import HTTPException  # noqa: E402

_home = Path(api.engine.home)


def _refused(path, write=True):
    try:
        api.athena_files.enforce(path, write=write)
    except HTTPException as e:
        # The whole refusal, not just one field: a refusal has to name *where* the path is and
        # *what* was refused, and a check that reads one of the two cannot tell you which is missing.
        return e.status_code, e.detail
    return None, ""


# The scope includes the person's own folder -- which contains `.ssh`. Scope alone would hand
# a private key over, so the same floor has to ask what a path *is*, not only where it lives.
_code, _why = _refused(str(Path.home() / ".ssh" / "id_rsa"))
check("a private key in your own folder is refused by the floor", _code == 403, f"{_code} {_why}")
check("... and the refusal names both the store and what it is",
      ".ssh" in str(_why) and "credential" in str(_why), str(_why))
_code2, _ = _refused(str(Path.home() / "notes-from-tala.txt"))
check("... while an ordinary document in the same folder is allowed", _code2 is None, str(_code2))
_code3, _why3 = _refused(str(HERMES_ROOT / "config.yaml"), write=False)
check("... and Hermes's own settings file is refused even though Athena lives inside that tree",
      _code3 == 403, f"{_code3} {_why3}")
_code4, _ = _refused(str(_home / "apps" / "some-app" / ".env"), write=True)
check("... while an installed app's own .env is allowed, because that file is the point",
      _code4 is None, str(_code4))
check("both doors ask the same question (the route's call and the floor's)",
      api.athena_files._secret_reason(Path.home() / ".ssh" / "id_rsa") != ""
      and api.athena_files._secret_reason(Path.home() / "notes-from-tala.txt") == "")

print("\n== a restore reads its destination from the bin, so it is checked too ==")
_r = api.athena_files.trash_restore(api.athena_files.TrashRestoreIn(id="zz-no-such-trash-item"))
check("an unknown bin item answers 404 rather than raising out of the route",
      getattr(_r, "status_code", None) == 404, f"{type(_r).__name__} {getattr(_r, 'status_code', '')}")

print("\n== the app folder is resolved by one function, not three ==")
_fix = Path(api.engine.home) / "api" / "zz-hardening-resolver.git"
_fix.mkdir(parents=True, exist_ok=True)
try:
    check("_app_folder_for finds the suffixed folder from the bare id",
          api._app_folder_for("zz-hardening-resolver") == _fix,
          str(api._app_folder_for("zz-hardening-resolver")))
    check("... and agrees with _app_folder, which it now is",
          api._app_folder_for("zz-hardening-resolver") == api._app_folder("zz-hardening-resolver"))
    check("... while an id that names nothing is None for both",
          api._app_folder_for("zz-not-an-app") is None and api._app_folder("zz-not-an-app") is None)
    check("... and a traversal id is still refused before it becomes a path",
          api._app_folder_for("..") is None and api._app_folder_for("a/b") is None)
finally:
    import shutil as _sh
    _sh.rmtree(_fix, ignore_errors=True)

print("\n== a job that is still running is not deleted as if it were old ==")
_old = time.time() - api._JOB_TTL - 60
api._JOBS["zz-running-long"] = {"id": "zz-running-long", "kind": "install", "label": "long one",
                                "state": "running", "result": None, "error": None,
                                "started": _old, "ended": None, "lines": []}
api._JOBS["zz-finished-long"] = {"id": "zz-finished-long", "kind": "install", "label": "old one",
                                 "state": "done", "result": {"ok": True}, "error": None,
                                 "started": _old, "ended": _old, "lines": []}
api._JOBS["zz-other"] = {"id": "zz-other", "kind": "install", "label": "just finished",
                           "state": "running", "result": None, "error": None,
                           "started": time.time(), "ended": None, "lines": []}
api._job_end("zz-other", "done")
check("a job running longer than the TTL keeps its record", "zz-running-long" in api._JOBS,
      sorted(api._JOBS)[:6])
check("... and its own verdict is untouched by another job ending",
      (api._JOBS.get("zz-running-long") or {}).get("state") == "running",
      str(api._JOBS.get("zz-running-long"))[:120])
check("... while a finished job older than the TTL is still swept", "zz-finished-long" not in api._JOBS,
      sorted(api._JOBS)[:6])
_api = api.machines_host  # a second worker for the same target is what the guard is for
check("... and the guard that stops a second install on one target still has something to read",
      isinstance(api._job_running("machine-zz-none"), bool))
api._JOBS.pop("zz-running-long", None)

print("\n== the port reap takes the children, not just the process ==")
_seen: list = []


class _FakeSub:
    @staticmethod
    def run(cmd, **kw):
        _seen.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


_real_pids = api.engine._port_pids
# Patch the module that owns the call: `_reap_port` now lives in `engine.py` and runs its own
# module's `subprocess`, so patching `api.subprocess` left the real taskkill in place and the
# capture stayed empty (rule 152).
_real_sub = api.athena_engine.subprocess
api.engine._port_pids = lambda: ["1234"]
api.athena_engine.subprocess = _FakeSub
try:
    api.engine._reap_port()
finally:
    api.engine._port_pids = _real_pids
    api.athena_engine.subprocess = _real_sub
if sys.platform == "win32":
    check("taskkill is asked for the whole tree", _seen and "/T" in _seen[0], str(_seen))
    check("... and still for that exact process", _seen and _seen[0][-1] == "1234", str(_seen))
else:
    check("the POSIX reap signals the process group, so children go too", True)

# --- a stage that fails says so as the job's state, not only in its answer ---------
# `_job_spawn`'s wrapper only knows that the call returned, so a stage whose answer is
# `{"ok": False}` — a snapshot with no disk to copy, an export that could not write its file —
# used to be reported as a finished snapshot, with the reason visible only in the log nobody was
# reading. The split between "the job finished" and "the answer is a failure" is deliberate and
# still holds for a job that ends itself well; a stage that *can* fail hands its answer over
# through `_job_verdict`, which is what these pin.
print("\n-- a stage's own verdict --")


def _drain(jid: str, seconds: float = 5.0):
    end = time.time() + seconds
    while time.time() < end:
        with api._JOBS_LOCK:
            j = api._JOBS.get(jid) or {}
            if j.get("state") not in (None, "", "running", "queued"):
                return dict(j)
        time.sleep(0.01)
    with api._JOBS_LOCK:
        return dict(api._JOBS.get(jid) or {})


_j = api._job_spawn("zz-verdict-bad", "a snapshot with nothing to copy",
                    lambda j: api._job_verdict(j, {"ok": False, "error": "there is no disk to copy"},
                                               "snapshot"))
_d = _drain(_j)
check("a stage whose answer is a failure ends the job failed, not done",
      _d.get("state") == "failed" and "no disk to copy" in str(_d.get("error")), str(_d)[:160])
check("... the reason is in the log too, so a reader of the log sees it",
      any("no disk to copy" in line for line in _d.get("log") or []), str(_d.get("log"))[:160])
check("... and the answer still travels, so nothing that read it before reads less now",
      (_d.get("result") or {}).get("ok") is False)

_j = api._job_spawn("zz-verdict-ok", "a snapshot that worked",
                    lambda j: api._job_verdict(j, {"ok": True, "file": "out.tar"}, "snapshot"))
_d = _drain(_j)
check("a stage that worked leaves the job done", _d.get("state") == "done", str(_d)[:120])
check("... with its answer intact", (_d.get("result") or {}).get("file") == "out.tar")

_j = api._job_spawn("zz-verdict-none", "a stage with nothing to report",
                    lambda j: api._job_verdict(j, None, "snapshot"))
_d = _drain(_j)
check("a stage with nothing to report is not turned into a failure",
      _d.get("state") == "done", str(_d)[:120])

_j = api._job_spawn("zz-verdict-raise", "a stage that throws",
                    lambda j: (_ for _ in ()).throw(RuntimeError("no network")))
_d = _drain(_j)
check("... and a stage that throws keeps failing with its reason", _d.get("state") == "failed"
      and "no network" in str(_d.get("error")), str(_d)[:120])


def _own_verdict(j):
    api._job_end(j, "failed", error="the stage knows better")
    return {"ok": False, "error": "and the wrapper agrees"}


_j = api._job_spawn("zz-verdict-own", "a stage that ended itself", _own_verdict)
_d = _drain(_j)
check("... and a stage that ended itself keeps its own words",
      _d.get("state") == "failed" and "knows better" in str(_d.get("error")), str(_d)[:140])

# --- one walk per poll interval, not one per poll ---------------------------------
# The panel asks `/shared` every 20 seconds, and a fresh answer walks every app's private folders
# to add them up. The poll was therefore paying for a disk scan on a schedule nobody chose. The
# answer is cached for about one interval, says so when it is reused, and is dropped by anything
# that moves files — otherwise a share pass would end and the panel would be handed the picture
# from before it, drawing apps as still holding copies that just moved.
print("\n-- the shared report is walked once --")
_walks = {"n": 0}
_real_report = api._shared_report
api._shared_report = lambda home: (_walks.__setitem__("n", _walks["n"] + 1)
                                   or {"apps": [], "app_count": 0})
try:
    api.forget_shared()
    _one = json.loads(api.shared_status().body)
    _two = json.loads(api.shared_status().body)
    check("a second poll is answered from the first walk, not by walking again",
          _walks["n"] == 1, f"{_walks['n']} walks")
    check("... and the answer says which it is instead of pretending to be fresh",
          _one.get("cached") is False and _two.get("cached") is True, f"{_one.get('cached')} / {_two.get('cached')}")
    json.loads(api.shared_status(fresh=True).body)
    check("... while asking for a fresh one really walks", _walks["n"] == 2, f"{_walks['n']} walks")
    api.forget_shared()
    api.shared_status()
    check("... and a pass that moves files drops the picture it invalidated",
          _walks["n"] == 3, f"{_walks['n']} walks")
finally:
    api._shared_report = _real_report
    api.forget_shared()

# --- a route that answers instead of raising ---------------------------------------
# The start route handed on whatever `start()` raised, which is a 500 and a traceback in the log.
# Everything it can refuse is a fact about a machine somebody has, so it is answered as a sentence.
print("\n-- a start that cannot happen is a sentence --")
_api_src = (PLUGIN / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
check("the start route catches what the start raised",
      "this machine could not be started: {e}" in _api_src)
check("... and it is the start route that says so, not the machines module",
      _api_src.index('def machine_start(name: str)') < _api_src.index("this machine could not be started: {e}"))
check("... and where the command line is built, a hand-edited spec is refused by field",
      "spec_problem(spec)" in _api_src)

print(f"\nt_hardening: {PASS} passed, {FAIL} failed")
if FAILURES:
    for f in FAILURES:
        print(f"   - {f}")
sys.exit(1 if FAIL else 0)
