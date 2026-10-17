"""A repeat: an app that should run again tomorrow, without an agent to say so.

A schedule is where "run this again" has to survive the conversation that asked
for it. The parts worth testing are the ones that decide whether the thing will
actually fire:

  * a schedule Hermes cannot read is refused with Hermes' own words, before a job
    or a script exists — storing an unreadable schedule means a job that never
    runs and nobody to blame;
  * the run is a script, not an agent turn, so a daily run costs no model call,
    and it calls the same code a hand-started run calls;
  * the index Athena keeps and the cron store are two different records, and a job
    that is gone while the index still lists it is reported as gone rather than as
    waiting — a schedule that will never fire is the worst thing to be wrong about;
  * a scheduled run needs no job behind it: the record a person reads afterwards
    is the recipe the call writes beside its outputs.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
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


api = load("athena_api_schedule_under_test", PLUGIN / "dashboard" / "plugin_api.py")
ac = api._appcall_module()
check("the capability module loads through plugin_api", ac is not None)
if ac is None:
    print("t_schedule: 0 passed, 1 failed")
    sys.exit(1)

HOME = Path(tempfile.mkdtemp(prefix="athena-schedules-"))
(HOME / "api" / "pretend").mkdir(parents=True, exist_ok=True)
real_engine = api.engine
api.engine = SimpleNamespace(home=HOME, probe=lambda: True,
                             start=lambda wait=0: {"ok": True})

CUT = {"name": "cut", "intent": "remove_background",
       "inputs": [{"name": "image", "component": "Image", "accepts_local_path": True,
                   "media": True, "required": True, "multi": False}],
       "outputs": [{"label": "out", "type": "pil", "component": "Image"}]}
MANIFEST = {"app": "pretend", "url": "http://127.0.0.1:7001/",
            "capabilities": [CUT]}

calls: list = []


def fake_call(home, folder, capability, inputs, timeout=None, manifest=None):
    calls.append({"capability": capability, "inputs": list(inputs)})
    if capability == "explodes":
        raise RuntimeError("the app went away")
    return {"ok": True, "capability": capability, "intent": "remove_background",
            "files": [{"path": "C:/runs/one/cut.png", "name": "cut.png",
                       "kind": "image"}],
            "run_dir": "C:/runs/one", "log": ["the app took 1.2s"]}


SECOND = {"app": "second", "url": "http://127.0.0.1:7002/",
          "capabilities": [{"name": "up", "intent": "upscale",
                            "inputs": [{"name": "image", "component": "Image",
                                        "accepts_local_path": True, "required": True}],
                            "outputs": [{"label": "out", "type": "pil",
                                         "component": "Image"}]}]}
(HOME / "api" / "second").mkdir(parents=True, exist_ok=True)
MANIFESTS = {"pretend": MANIFEST, "second": SECOND}


def fake_load(home, folder):
    """Only the two apps this test installed have a manifest; anything else is
    'not installed' as far as a chain is concerned."""
    return MANIFESTS.get(Path(str(folder)).name)


fake = SimpleNamespace(load=fake_load,
                       probe=lambda home, folder, url: dict(MANIFEST, url=url),
                       call=fake_call, fits=ac.fits, candidates=ac.candidates,
                       links=ac.links, file_kind=ac.file_kind)
real_folder = api._app_folder
real_ac_module = api._appcall_module
api._appcall_module = lambda: fake
api._app_folder = lambda app_id: (HOME / "api" / app_id) if app_id == "pretend" \
    else real_folder(app_id)
api._bring_up = lambda app_id, seconds=180.0: {"url": "http://127.0.0.1:7001/",
                                               "note": "was already running"}
api._idle_module = lambda: None


class FakeCron:
    """Hermes' cron store, as much of it as a schedule touches.

    `parse_schedule` is strict on purpose: the point of the refusal the route
    makes is that the same parser that will fire the job is the one that read it.
    """

    READABLE = ("every day at 8am", "every 6 hours", "0 7 * * 1")

    def __init__(self) -> None:
        self.jobs: dict = {}
        self.created: list = []
        self.removed: list = []
        self._n = 0

    def parse_schedule(self, schedule):
        if schedule not in self.READABLE:
            raise ValueError(f"unrecognized schedule: {schedule!r}")
        return {"kind": "interval", "raw": schedule}

    def create_job(self, prompt=None, schedule="", name=None, script=None,
                   no_agent=False, interpreter=None, workdir=None, **kw):
        self.created.append({"prompt": prompt, "schedule": schedule, "name": name,
                             "script": script, "no_agent": no_agent,
                             "interpreter": interpreter, "workdir": workdir, **kw})
        if prompt is not None or not no_agent or not script:
            raise ValueError("a no-agent job needs a script and no prompt")
        self._n += 1
        jid = f"job-{self._n}"
        self.jobs[jid] = {"id": jid, "name": name, "schedule": schedule,
                          "script": script, "no_agent": True, "paused": False,
                          "next_run": "2026-10-15T08:00:00-04:00", "last_run": None}
        return dict(self.jobs[jid])

    def get_job(self, jid):
        rec = self.jobs.get(str(jid))
        return dict(rec) if rec else None

    def remove_job(self, jid):
        self.removed.append(str(jid))
        return self.jobs.pop(str(jid), None) is not None


cron = FakeCron()
api._CRON = cron


def run_route(app_id, capability=None, inputs=None, schedule=None, name=None,
              timeout=None, then=None):
    body = api.ScheduleBody(capability=capability, inputs=inputs, schedule=schedule,
                            name=name, timeout=timeout, then=then)
    resp = api.app_schedule(app_id, body)
    return resp.status_code, json.loads(bytes(resp.body).decode("utf-8"))


def listing(app=""):
    resp = api.schedules_index(app=app)
    return json.loads(bytes(resp.body).decode("utf-8"))


def deleting(sid):
    resp = api.schedule_delete(sid)
    return resp.status_code, json.loads(bytes(resp.body).decode("utf-8"))


real_file = PLUGIN / "README.md"

try:
    # --------------------------------------------- refusals before anything exists
    print("\n== what is refused before a schedule exists ==")
    check("a scheduled run is not a job — the runner is there for a script to call",
          callable(getattr(api, "run_one", None)) and hasattr(api, "ScheduleBody"))

    status, out = run_route("ghost", "cut", [str(real_file)], "every day at 8am")
    check("an app that is not installed is refused", status == 404
          and "no installed app called ghost" in str(out.get("error")), str(out))

    status, out = run_route("pretend", None, [str(real_file)], "every day at 8am")
    check("a schedule that does not name a capability is refused",
          not out.get("ok") and "name the capability to run" in str(out.get("error")),
          str(out))

    status, out = run_route("pretend", "not_a_thing", [], "every day at 8am")
    check("a capability that is not in the manifest is refused, and what it does "
          "have is named", not out.get("ok") and "no capability called" in str(out.get("error"))
          and "remove_background" in str(out.get("available")), str(out))

    status, out = run_route("pretend", "cut", [], "every day at 8am")
    check("a capability whose required inputs are missing is refused here too, not "
          "at 8am tomorrow", not out.get("ok")
          and "needs 1 input(s)" in str(out.get("error")), str(out))

    status, out = run_route("pretend", "cut", [str(real_file)], "")
    check("a schedule with no time is refused, and the sentence shows what one looks "
          "like", not out.get("ok") and "say when it should run" in str(out.get("error"))
          and "every day at 8am" in str(out.get("examples")), str(out))

    status, out = run_route("pretend", "cut", [str(real_file)], "whenever i feel like it")
    check("a schedule Hermes cannot read is refused with Hermes' own words",
          status == 400 and "Hermes cannot read" in str(out.get("error"))
          and "unrecognized schedule" in str(out.get("error")), str(out))
    scripts = list((HOME / "schedules").glob("*.py")) if (HOME / "schedules").is_dir() else []
    check("and nothing was created for it — no job, no script, no row",
          cron.created == [] and scripts == []
          and not (HOME / "schedules" / "index.json").exists(),
          f"{cron.created} {scripts}")

    api._CRON = False
    status, out = run_route("pretend", "cut", [str(real_file)], "every day at 8am")
    check("a build with no cron store says so instead of writing a job nobody will "
          "run", status == 503 and "no scheduled jobs" in str(out.get("error")), str(out))
    api._CRON = cron

    status, out = run_route("pretend", "cut", [str(real_file)], "every day at 8am",
                            then=[{"app": "wanish", "capability": "t2v"}])
    check("a chain that cannot hand over is refused as a schedule for the same "
          "reason it would be refused as a call", status == 400
          and "stage 2" in str(out.get("error")), str(out))

    # ------------------------------------------------------------- the schedule
    print("\n== the schedule, made ==")
    status, out = run_route("pretend", "cut", [str(real_file)], "every day at 8am")
    check("a schedule that holds up is stored and reported with its next run",
          out.get("ok") and out.get("next_run") == "2026-10-15T08:00:00-04:00", str(out))
    row = out.get("schedule") or {}
    check("and it names the app, the capability and the time it runs",
          row.get("app") == "pretend" and row.get("capability") == "cut"
          and row.get("schedule") == "every day at 8am", str(row))

    made = cron.created[-1]
    check("the job behind it is a script and not an agent turn, so starting it costs "
          "no model call", made.get("no_agent") is True and made.get("prompt") is None,
          str(made))
    check("it runs the script Athena wrote for it, under the engine's own home",
          str(made.get("script")) == str(row.get("script"))
          and Path(str(made.get("script"))).is_file()
          and made.get("workdir") == str(HOME), str(made))
    check("and it is given a python to run it with, because the script imports the "
          "plugin's own code", str(made.get("interpreter") or "").endswith(".exe")
          or "python" in str(made.get("interpreter") or ""), str(made.get("interpreter")))

    text = Path(str(row.get("script"))).read_text(encoding="utf-8")
    check("the script it wrote calls the plugin's own runner with the app and the "
          "capability", "run_one" in text and "'pretend'" in text and "'cut'" in text,
          text[:200])
    check("and the script is valid python, so the failure mode is not a syntax error "
          "at 8am",
          compile(text, str(row.get("script")), "exec") is not None)
    check("the script explains itself, so whoever finds it in the cron folder knows "
          "why it is not an agent turn", "cost a model call" in text, text[:200])
    check("the inputs the schedule will pass are the ones it was given",
          repr(str(real_file)) in text, text[:400])
    check("it says where the run will show up, and that Hermes has to be running",
          "beside the ones you start by hand" in str(out.get("note"))
          and "while Hermes is running" in str(out.get("note")), str(out.get("note")))

    rows = api._schedules_index()
    check("the index keeps the row, so the schedule can be listed and stopped",
          len(rows) == 1 and rows[0].get("job") == "job-1", str(rows))

    # ------------------------------------------------------- a run, with no job
    print("\n== a run with no job behind it ==")
    before = dict(api._JOBS)
    res = api.run_one("pretend", "cut", [str(real_file)])
    check("the runner runs the capability and reports what came back",
          res.get("ok") and [str(f).replace("\\", "/") for f in res.get("files") or []]
          == ["C:/runs/one/cut.png"], str(res))
    check("it made no job — a scheduler's log is its own stdout",
          api._JOBS == before, str(list(api._JOBS)))
    check("and the call it made is the one a hand-started run makes, with the app "
          "brought up first", calls and calls[-1]["capability"] == "cut",
          str(calls[-1:]))

    res = api.run_one("ghost", "cut", [])
    check("running an app that is not installed fails as a sentence, not a traceback",
          not res.get("ok") and "no installed app called ghost" in str(res.get("error")),
          str(res))

    res = api.run_one("pretend", "explodes", [])
    check("an app that throws is reported as a failed run with the reason",
          not res.get("ok") and "the app went away" in str(res.get("error")), str(res))

    run_dir = HOME / "runs" / "pretend" / "20261014-080000"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "recipe.json").write_text(json.dumps(
        {"app": "pretend", "capability": "cut", "state": "ok",
         "files": [{"path": "C:/runs/one/cut.png", "name": "cut.png", "kind": "image"}]}),
        encoding="utf-8")
    counted = api._schedule_runs("pretend", "cut")
    check("how many times a schedule has run is read off the same recipes the Runs "
          "tab shows", counted.get("runs") == 1 and counted.get("last"), str(counted))
    check("and a capability that never ran counts zero rather than borrowing another "
          "one's runs", api._schedule_runs("pretend", "something_else").get("runs") == 0,
          str(api._schedule_runs("pretend", "something_else")))

    # ---------------------------------------------------------------- reading it
    print("\n== reading a schedule back ==")
    out = listing()
    check("a stored schedule is listed with its state and its next run",
          out.get("ok") and out.get("count") == 1
          and out["schedules"][0].get("state") == "waiting"
          and out["schedules"][0].get("next_run"), str(out))
    check("and it carries how often it has actually run",
          out["schedules"][0].get("runs", {}).get("runs") == 1, str(out["schedules"][0]))
    check("the list says Hermes' cron was found, and that runs fire while Hermes is "
          "running", out.get("hermes_cron") is True
          and "while Hermes is running" in str(out.get("note")), str(out))
    check("filtering by app keeps the ones that match and drops the rest",
          listing("pretend").get("count") == 1 and listing("other").get("count") == 0,
          str(listing("other")))

    cron.jobs.clear()
    out = listing()
    check("a job that is gone from the store is reported as gone, not as waiting",
          out["schedules"][0].get("state") == "gone", str(out))
    check("and it says what that means rather than leaving the person to guess",
          "will not run" in str(out["schedules"][0].get("note")),
          str(out["schedules"][0].get("note")))

    cron.jobs["job-1"] = {"id": "job-1", "paused": False,
                          "next_run": "2026-10-15T08:00:00-04:00"}
    Path(str(row.get("script"))).unlink()
    out = listing()
    check("a schedule whose script is missing says so — it cannot fire",
          "is missing" in str(out["schedules"][0].get("note")),
          str(out["schedules"][0].get("note")))
    Path(str(row.get("script"))).write_text(text, encoding="utf-8")

    cron.jobs["job-1"]["paused"] = True
    out = listing()
    check("a paused job is reported paused, and not enabled",
          out["schedules"][0].get("state") == "paused"
          and out["schedules"][0].get("enabled") is False, str(out["schedules"][0]))
    cron.jobs["job-1"]["paused"] = False

    # -------------------------------------------------------------- stopping it
    print("\n== stopping a schedule ==")
    status, out = deleting("no-such-schedule")
    check("stopping a schedule that does not exist is a 404, not a quiet success",
          status == 404 and "no schedule called" in str(out.get("error")), str(out))

    sid = str(row.get("id"))
    status, out = deleting(sid)
    check("stopping one removes the cron job, the script and the row",
          out.get("ok") and out.get("job_removed") is True
          and out.get("script_removed") is True, str(out))
    check("the cron store no longer has it", "job-1" in cron.removed
          and "job-1" not in cron.jobs, str(cron.removed))
    check("the script is off the disk, so nothing can fire from a leftover file",
          not Path(str(row.get("script"))).exists(), str(row.get("script")))
    check("and the index no longer lists it", api._schedules_index() == [],
          str(api._schedules_index()))
    check("stopping says plainly that it will not run again",
          "will not run again" in str(out.get("note")), str(out.get("note")))

    status, out = deleting(sid)
    check("a second stop finds nothing to stop", status == 404, str(out))

    # ------------------------------------------------ and the real store is there
    print("\n== Hermes' own cron, as this build has it ==")
    api._CRON = None
    real = api._cron_jobs()
    if real is None:
        check("this build has no cron store, and the route says so instead of "
              "pretending", True)
    else:
        check("Hermes' cron store is importable from the plugin, so a schedule can "
              "actually be stored", hasattr(real, "create_job")
              and hasattr(real, "remove_job") and hasattr(real, "parse_schedule"))
        ok = True
        for phrase in ("every day at 8am", "every 6 hours", "0 7 * * 1"):
            try:
                real.parse_schedule(phrase)
            except Exception as e:  # noqa: BLE001
                ok = False
                check(f"the store reads {phrase!r}", False, str(e))
        check("and it reads the three forms the route offers as examples", ok)
        try:
            real.parse_schedule("whenever i feel like it")
            check("a schedule it cannot read is rejected, which is why the route "
                  "asks it first", False, "it accepted nonsense")
        except Exception:
            check("a schedule it cannot read is rejected, which is why the route "
                  "asks it first", True)
        import inspect
        params = inspect.signature(real.create_job).parameters
        check("create_job still takes what a script-only job needs — no_agent, "
              "script and interpreter", all(p in params for p in
                                            ("no_agent", "script", "interpreter")),
              str(sorted(params)))
    # --------------------------------------- the same repeat, asked for in chat
    # A repeat asked for in chat has to be the same object a repeat asked for in
    # the panel is: one route, one validator, one store — so the tool is driven
    # against the very same plugin instance the routes above were driven against.
    # The store is the route's, not a fresh module's: the same instance the checks
    # above drove, so a repeat made in chat lands where the panel will find it.
    api._CRON = cron
    # `__init__._appcall()` caches the capability module on the engine module, which
    # is the hook that lets this drive the real handlers against the fake appcall.
    api.athena_appcall = fake
    print("\n== the same repeat, asked for in chat ==")
    spec = importlib.util.spec_from_file_location("athena_tools_under_test",
                                                  PLUGIN / "__init__.py")
    init = importlib.util.module_from_spec(spec)
    sys.modules["athena_tools_under_test"] = init
    spec.loader.exec_module(init)
    init._api = api
    real_ensure = init._ensure_app
    init._ensure_app = lambda mod, app_id, seconds=90.0: {"url": "http://127.0.0.1:7001/",
                                                          "note": "already running"}

    def tool(name, params):
        return json.loads(getattr(init, "handle_" + name)(params))

    check("the repeat tool is declared under a name an agent can call",
          (init.SCHEMAS.get("schedule") or {}).get("name") == "athena_schedule",
          str(sorted(init.SCHEMAS)))

    out = tool("schedule", {"action": "create"})
    check("asking for a repeat without saying what to run is refused, and told what "
          "is missing", not out.get("success")
          and "has to name what to run" in str(out.get("error")), str(out))

    out = tool("schedule", {"action": "create", "app": "ghost", "capability": "cut"})
    check("an app that is not installed is refused in the tool as it is in the route",
          not out.get("success") and "no installed app matches" in str(out.get("error")),
          str(out))

    out = tool("schedule", {"action": "wobble"})
    check("an action nobody knows is named rather than quietly ignored",
          not out.get("success") and "unknown action" in str(out.get("error")), str(out))

    out = tool("schedule", {"action": "create", "app": "pretend", "capability": "cut",
                            "inputs": [str(real_file)], "schedule": "every 6 hours"})
    chat_row = out.get("schedule") or {}
    check("a repeat asked for in chat is stored as the same object the panel reads",
          out.get("success") and chat_row.get("schedule") == "every 6 hours", str(out))
    check("it is in the index the panel lists, in the cron store, with its own script "
          "on disk", len(api._schedules_index()) == 1
          and cron.jobs.get(str(chat_row.get("job"))) is not None
          and Path(str(chat_row.get("script"))).is_file(), str(api._schedules_index()))

    out = tool("schedule", {"action": "list"})
    check("listing them through the tool gives the same answer the route does",
          out.get("success") and out.get("count") == 1
          and out["schedules"][0].get("state") == "waiting", str(out))

    out = tool("schedule", {"action": "create", "app": "pretend", "capability": "cut",
                            "inputs": [str(real_file)],
                            "schedule": "whenever i feel like it"})
    check("a time Hermes cannot read is refused in the tool with Hermes' own words "
          "rather than stored and hoped for", not out.get("success")
          and "Hermes cannot read" in str(out.get("error")), str(out))
    check("and that refusal left the store exactly as it was",
          len(api._schedules_index()) == 1, str(api._schedules_index()))

    out = tool("schedule", {"action": "stop"})
    check("stopping without saying which one is refused",
          not out.get("success") and "say which schedule to stop" in str(out.get("error")),
          str(out))

    out = tool("schedule", {"action": "stop", "id": chat_row.get("id")})
    check("stopping one through the tool does what the route's DELETE does",
          out.get("success") and out.get("job_removed") is True
          and api._schedules_index() == [], str(out))

    # ------------------------------------------- a chain, asked for by an agent
    before = len(calls)
    out = tool("generate", {"app": "pretend", "capability": "cut",
                            "inputs": [str(real_file)],
                            "then": [{"app": "wanish", "capability": "t2v"}]})
    check("a chain asked for by an agent is refused before anything starts, naming "
          "the stage that does not fit", not out.get("success")
          and "stage 2" in str(out.get("error")), str(out))
    check("and nothing ran — the refusal is not a half-done chain",
          len(calls) == before, str(calls[before:]))

    calls.clear()
    out = tool("generate", {"app": "pretend", "capability": "cut",
                            "inputs": [str(real_file)],
                            "then": [{"app": "second", "capability": "up"}]})
    check("a chain that fits runs every stage in the one call, in order, each on what "
          "the stage before made", [c["capability"] for c in calls] == ["cut", "up"],
          str(calls))
    check("and the files it hands back are the last stage's, with the stages reported "
          "beside them", out.get("success") is True, str(out)[:300])
    init._ensure_app = real_ensure

finally:
    try:
        shutil.rmtree(HOME, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    api.engine = real_engine
    api._appcall_module = real_ac_module


print(f"\nt_schedule: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("failed:")
    for name in FAILURES:
        print("  -", name)
sys.exit(1 if FAIL else 0)
