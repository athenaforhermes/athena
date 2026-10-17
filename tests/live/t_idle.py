"""Idle apps: an app nobody is using gets stopped, an app in use does not.

Run: env -u PYTHONPATH python t_idle.py

Two halves, tested separately and honestly:
  * `idle.py` is pure accounting — driven directly, with invented clocks, so the
    rules about busy/quiet/window are proven without waiting on real time.
  * the wiring — the setting, the two routes, the action hook, the agent's busy
    region, and the reaper thread actually calling `stop` — is proven against a
    real module with a REAL stop call, by pointing a fake app at a fake runtime.
"""
import importlib.util, json, sys, time, threading
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PL = Path(str(HERMES_ROOT) + r"/plugins/athena")
sys.path.insert(0, str(PL))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


idle = load("athena_idle", PL / "dashboard" / "idle.py")
api = load("athena_plugin_api", PL / "dashboard" / "plugin_api.py")

fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


print("== the accounting, on an invented clock ==")
idle.reset()
check(idle.due(120, now=1000.0) == [], "nothing tracked, nothing to reap")

# Watching is what arms an app. Being touched only records that someone used it:
# since 0.6.2 the window applies to apps Hermes launched, so that launching an
# app by hand and walking away does not get it killed while it is still open.
idle.watch("wan.git", what="test")
check(idle.tracked() == ["wan"], "the .git suffix is normalised away")
check(idle.due(120, now=time.time() + 60) == [], "60s quiet is inside a 120s window")
check(idle.due(120, now=time.time() + 121) == ["wan"], "121s quiet is not")
check(idle.due(0, now=time.time() + 99999) == [], "window 0 means never reap")
check(idle.due(None) == [], "a None window is not a reap-everything")

idle.begin("wan.git")
check(idle.due(120, now=time.time() + 9999) == [],
      "an app mid-call is never a candidate, however long the call runs")
idle.end("wan.git")
check(idle.due(120, now=time.time() + 121) == ["wan"],
      "and once the call ends, the clock starts again from the end")

# A call is one kind of work. A hold is any other, and the difference matters:
# a 3D or video generation is not one `ac.call` from the panel's point of view
# when the work outlives the request that started it, and booting a large model
# is real work against an app that answers on no port yet.
idle.hold("wan.git", "job")
check(idle.working("wan") is True, "a held app is working")
check(idle.due(120, now=time.time() + 99999) == [],
      "a held app is never due, however long the clock has run")
idle.hold("wan.git", "starting")
idle.release("wan.git", "job")
check(idle.due(120, now=time.time() + 99999) == [],
      "one hold released while another is still held is still work in flight")
idle.release("wan.git", "starting")
check(idle.due(120, now=time.time() + 121) == ["wan"],
      "the last release starts the clock, so an app that finished is reapable")
check(idle.working("wan") is False, "and no hold survives the releases")
check(idle.snapshot()[0]["work"] == [], "the snapshot names what is in flight")

idle.reset()
idle.touch("wan.git", what="a click in the UI")
check(idle.tracked() == ["wan"], "a used app is tracked")
check(idle.due(0.0001, now=time.time() + 9999) == [],
      "but being used never arms it — only Hermes launching it does")

idle.reset()
idle.watch("wan.git")
idle.watch("RMBG-2-Studio.git")
oldest = idle.due(1, now=time.time() + 2000)
check(oldest[0] in ("wan", "RMBG-2-Studio") and len(oldest) == 2,
      f"the longest-quiet app is reaped first: {oldest}")

idle.reset()
idle.begin("busy")
idle.watch("quiet")
time.sleep(0.01)
check(set(idle.due(0.0001, now=time.time() + 10)) == {"quiet"},
      "busy and quiet are sorted out together")

print("\n== the window a user sets is made safe ==")
check(idle.clamp(120) == 120.0, "a sane value passes through")
check(idle.clamp(0) == 0.0, "0 stays 0 — it means 'never', not 'immediately'")
check(idle.clamp(5) == float(idle.MIN_SECONDS), "too short is raised to the floor")
check(idle.clamp(10 ** 9) == float(idle.MAX_SECONDS), "absurd is capped")
check(idle.clamp("nonsense") == float(idle.DEFAULT_SECONDS), "junk falls back to the default")

print("\n== the setting is real, saved, and clamped on the way in ==")
saved = api.engine_module_settings = None
orig_file = api.SETTINGS_FILE
orig_text = orig_file.read_text(encoding="utf-8") if orig_file.is_file() else None
try:
    api._save_settings(dict(api.DEFAULTS))
    check(api._idle_window() == 120.0, "the default window is 120s")
    body = api.SettingIn(idle_stop_seconds=45)
    api.set_settings(body)
    check(json.loads(orig_file.read_text(encoding="utf-8"))["idle_stop_seconds"] == 45.0,
          "a saved window is written to settings.json")
    check(api._idle_window() == 45.0, "and read back")
    api.set_settings(api.SettingIn(idle_stop_seconds=1))
    check(api._idle_window() == float(idle.MIN_SECONDS),
          "a 1-second window is clamped, not obeyed literally")
    api.set_settings(api.SettingIn(idle_stop_seconds=0))
    check(api._idle_window() == 0.0, "0 survives as 'never reap'")
finally:
    if orig_text is not None:
        orig_file.write_text(orig_text, encoding="utf-8")
    else:
        orig_file.unlink(missing_ok=True)

print("\n== one idle record, both doors ==")
check(api._idle_module() is idle, "the backend's loader returns the shared module")
check(api._idle_module() is api._idle_module(), "and the same one every time")
agent = load("athena_agent_half", PL / "__init__.py")
mod2 = agent._load_api()
check(agent._idle(mod2) is idle,
      "the agent half reaches the SAME record through the backend's loader")
_p = Path(str(HERMES_ROOT) + r"/plugins/athena")
check(api.__file__ and str(PL) and mod2 is not api,
      "even though plugin_api is loaded twice, by two names")

print("\n== the reaper really stops an app ==")
stopped, asked = [], []


class FakeRuntime:
    def running(self, app=None, timeout=15.0):
        asked.append(app)
        return {"wan.git": [{"uri": "C:/x/start.js", "url": "http://127.0.0.1:1"}]} if app == "wan" else {}

    def stop(self, app, script=None, timeout=5.0):
        stopped.append(app)
        return {"ok": True}


api._runtime = lambda: FakeRuntime()
api._IDLE_REAPER = False

print("\n== the reaper's decision: work in flight is not idleness ==")
idle.reset()
stopped.clear()
idle.watch("wan.git", what="an agent launch")
idle.hold("wan.git", "job")
check(api._idle_reap("wan") == "kept",
      "a held app is KEPT, not stopped")
check("wan" not in stopped, f"stop was never called: {stopped}")
idle.release("wan.git", "job")
check(api._idle_reap("wan") == "stopped",
      "when the hold ends the same app IS stopped — the memory is still reclaimed")

# The second, independent signal: a running job that names the app. The runner
# holds the app itself, so this is what catches a job that outlives its request
# or a path that forgot to take a hold.
idle.reset()
stopped.clear()
idle.watch("wan.git", what="an agent launch")
jid = api._job_new("call", "a 20-minute render", app="wan.git")
check(api._idle_jobs("wan") == [jid], "a running job that names the app is found")
check(api._idle_reap("wan") == "kept", "a running job keeps its app alive")
check("wan" not in stopped, f"stop was never called: {stopped}")
api._job_end(jid, "done", {"ok": True})
check(api._idle_jobs("wan") == [], "a finished job is not work in flight")
check(api._idle_reap("wan") == "stopped", "and with the job done, the app is stopped")

# The other half, at the reaper's own door: an app with nothing in flight is
# still reclaimed. This is the half that frees the VRAM.
idle.reset()
stopped.clear()
idle.watch("wan.git", what="an agent launch")
check(api._idle_reap("wan") == "stopped", "an idle app is still stopped")
check(stopped == ["wan"], f"and it was one real stop: {stopped}")

real_sleep = time.sleep
ticks = {"n": 0}


def fast_sleep(seconds):
    """The loop sleeps 5s between sweeps; stand it down to milliseconds so the
    test observes the real thread doing the real thing instead of waiting."""
    ticks["n"] += 1
    real_sleep(0.01)
    if ticks["n"] > 400:
        raise SystemExit(0)


time.sleep = fast_sleep
idle.reset()
# The window POLICY (30s floor, clamping, 0 = never) is proven above. Here the
# question is only whether the thread stops what the accounting says is due, so
# stand the window down rather than idling the test for half a minute.
real_window = api._idle_window
api._idle_window = lambda: 0.05
api._save_settings(dict(api.DEFAULTS, idle_stop_seconds=30))
try:
    # The reaper only stops what Hermes armed. `wan` is armed as if Hermes
    # launched it; RMBG is merely touched, the way a click in the UI touches an
    # app — so it has to be left alone however long it sits.
    idle.watch("wan.git", what="an agent launch")
    idle.touch("RMBG-2-Studio.git", what="a click in the UI")
    api._idle_note("wan.git")           # starts the reaper thread
    deadline = time.time() + 20
    while time.time() < deadline and not stopped:
        real_sleep(0.05)
    time.sleep = real_sleep
    check("wan" in stopped, f"the quiet app was stopped: {stopped}")
    check("rmbg-2-studio" not in stopped,
          "the app that was never armed was never stopped")


    log = Path(api.engine.home) / "logs" / "idle.log"
    check(log.is_file() and "stopped wan" in log.read_text(encoding="utf-8", errors="replace"),
          "the reap is written where a user can find it")
finally:
    time.sleep = real_sleep
    api._idle_window = real_window
    check(api._idle_window() == float(idle.MIN_SECONDS),
          "and the real window is still the clamped setting, not the test's")

print("\n== the doors touch the clock ==")
idle.reset()
api._save_settings(dict(api.DEFAULTS))
body = api.ActionBody()
seen = {"started": []}
class R2(FakeRuntime):
    def start(self, app, script=None, wait=25.0, uri=None, input=None):
        seen["started"].append(app)
        return {"ok": True, "state": "running"}
api._runtime = lambda: R2()
api.engine.probe = lambda: True
api._app_menu = lambda app: {"ok": True, "topDefault": {"href": "start.js"}, "leaves": []}
api._resolve_href = lambda app, href: "C:/x/start.js"
api.app_action("wan", "launch", None)
check("wan" in idle.tracked(), f"an app action puts the app on the clock: {idle.tracked()}")
api.app_action("wan", "stop", None)
check("wan" not in idle.tracked(), "and stopping it takes it off")

print("\n== the heartbeat route, for a viewer that is open ==")
idle.reset()
res = api.app_activity("RMBG-2-Studio.git")
check(res.status_code == 200, "POST /apps/<id>/activity answers 200")
check("rmbg-2-studio" in idle.tracked(), "and counts as use")
check(api.idle_state().status_code == 200, "GET /idle answers")
payload = json.loads(api.idle_state().body)
check(payload["window_seconds"] == 120.0 and payload["tracked"],
      f"and reports the window and what is tracked: {payload}")

print("\n== apps already running when Athena starts are not forgotten ==")
idle.reset()


class AdoptRuntime(FakeRuntime):
    def running(self, app=None, timeout=15.0):
        return {"wan.git": [{"uri": "C:/x/start.js", "url": "http://127.0.0.1:1"}],
                "comfy.git": [{"uri": "C:/x/start.js", "url": "http://127.0.0.1:2"}]}


api._runtime = lambda: AdoptRuntime()
api._IDLE_REAPER = False
adopted = api._idle_adopt()
check(sorted(adopted) == ["comfy", "wan"], f"running apps are adopted: {adopted}")
check(sorted(api._idle_module().tracked()) == ["comfy", "wan"], "and tracked")
check(api._idle_adopt() == [], "adopting twice does not reset a clock that is already running")
fresh = api._idle_module().snapshot()
check(all(r["idle_seconds"] < 30 for r in fresh),
      "an app that was already running gets a fresh window, not an instant death")
idle.reset()

print("\n== the agent's busy region ==")
src = (PL / "__init__.py").read_text(encoding="utf-8")
check("idle.begin(app_id)" in src and "idle.end(app_id)" in src,
      "handle_generate marks the app busy around the call")
check(src.index("idle.begin(app_id)") < src.index("res = ac.call(") < src.index("idle.end(app_id)"),
      "begin comes before the call and end after it")
_busy_at = src.index("idle.begin(app_id)")
check(src.index("started = _ensure_app(mod, app_id)", _busy_at) > _busy_at,
      "begin comes before the app is brought up, so a slow boot is not idle time")
check(_busy_at < src.index("finally:", _busy_at) < src.index("idle.end(app_id)", _busy_at),
      "and end runs in a finally, even when the call raises")
runner_src = (PL / "dashboard" / "runner.py").read_text(encoding="utf-8")
check(runner_src.index('idle.hold(app_id, "call")') < runner_src.index("up = bring_up(app_id)"),
      "the runner holds the app before bring_up, not only around ac.call")
check(runner_src.index("up = bring_up(app_id)") < runner_src.index("ac.call(") <
      runner_src.index('idle.release(app_id, "call")'),
      "and releases it after the call, in a finally")
check("_idle_use(mod, app_id" in src, "_ensure_app notes the app as used")

print("\n== nothing polls its way to immortality ==")
# The Apps tab asks after every app every few seconds. If that counted as use,
# no app would ever be idle and this whole feature would be decoration.
eyes = []
api_routes = api.apps
check("_idle_note" not in api.status.__code__.co_names, "/status does not touch the clock")
check("_idle_note" not in api.apps.__code__.co_names, "nor does the app list")
check("_idle_note" not in api.app_health.__code__.co_names, "nor does a health check")

print("\n%d check(s) failed" % len(fails))
for f in fails:
    print("  FAIL", f)
raise SystemExit(1 if fails else 0)
