"""Waiting for the card: queueing a launch instead of retrying it by hand.

The VRAM gate warns and lets a launch through, because no app here declares what
it needs. That leaves one case with no answer — the card is full *now* and the work
is wanted *later* — and this is the answer: a queued launch, which is a job, so the
Terminal already shows what it is waiting for.

What matters here is that the queue cannot lie about three things, and each has a
check below:

  * it starts the app when the card frees, and *only* then — a wait that fires while
    the card is still full is worse than no wait at all;
  * it ends honestly when it gives up, saying nothing was started;
  * it is in memory, and says so, because a queue that outlived the dashboard would
    start apps nobody is watching.

The gate is stubbed rather than the real sampler, so these checks are about the
queue's own decisions. What the sampler *returns* is headroom.py's business, and the
same `gpu_gate` the launch path uses is what the pump asks.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
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


api = load("athena_api_under_test", PLUGIN / "dashboard" / "plugin_api.py")

# --------------------------------------------------------------- the synthetic box

TMP = Path(tempfile.mkdtemp(prefix="athena-wait-"))
HOME = TMP / "athena"
(HOME / "api" / "box.git" / "app").mkdir(parents=True)
(HOME / "api" / "box.git" / "app" / "start.js").write_text("// a script\n", encoding="utf-8")
(HOME / "api" / "box.git" / "pinokio.js").write_text("module.exports = {}\n", encoding="utf-8")

# `engine.home` is built from settings, so the whole module agrees about the home
# once the settings file says where it is — no per-accessor patching, which is how a
# test ends up half in one home and half in another.
SETTINGS = TMP / "settings.json"
SETTINGS.write_text(json.dumps({"home": str(HOME), "port": 42001}), encoding="utf-8")
api.SETTINGS_FILE = SETTINGS


class FakeRuntime:
    """Records launches instead of starting anything, and reports no live URLs."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    def start(self, app: str, script: str | None = None, **kw) -> dict:
        self.started.append((app, script))
        return dict(self.next_response)

    next_response: dict = {"ok": True}

    def running(self, app: str | None = None, **kw) -> dict:
        return {}


class FakeHeadroom:
    """A card with numbers we choose. `gpu_gate` is None when the card is fine."""

    def __init__(self) -> None:
        self.answer: dict | None = None
        self.asked = 0

    def gpu_gate(self, cfg, running=None) -> dict | None:
        self.asked += 1
        return self.answer


RT, HR = FakeRuntime(), FakeHeadroom()
api._runtime = lambda: RT
api.athena_headroom = HR

BUSY = {"name": "NVIDIA GeForce RTX 5090", "total_gb": 31.82, "used_gb": 29.4,
        "free_gb": 2.4, "warn_gb": 8.0, "holders": ["wan.git"],
        "message": "only 2.4 GB of the card's 31.82 GB is free right now "
                   "(wan.git is running) — an app that loads a model may fail to load"}

print("waiting for the card")

# --------------------------------------------------------------- what a wait is

HR.answer = dict(BUSY)
r1 = api.app_wait("box.git", api.WaitIn())
body1 = json.loads(r1.body)
check("a launch can be queued while the card is full", body1.get("ok") is True, str(body1))
check("the queue hands back the job to watch it by", isinstance(body1.get("job"), str))
check("and where in line it is", body1.get("position") == 1, str(body1))
jit = api._JOBS[body1["job"]]
check("the job is named for the app, not for the waiting",
      jit["label"].startswith("box.git"), jit["label"])
check("its log says what it is waiting to do",
      any("queued: start box.git" in l for l in jit["log"]), str(jit["log"]))

r2 = api.app_wait("box.git", api.WaitIn())
body2 = json.loads(r2.body)
check("asking twice is one intention, not two places in line",
      body2.get("already") is True and body2.get("position") == 1, str(body2))
check("and it is the same job, so nothing is duplicated",
      body2.get("job") == body1.get("job") and len(api._WAIT_QUEUE) == 1,
      str(api._WAIT_QUEUE))

view = json.loads(api.waiting().body)
check("/waiting shows the queue", view.get("count") == 1, str(view))
check("it carries the card's own numbers, not a second sampling",
      (view.get("card") or {}).get("free_gb") == 2.4, str(view.get("card")))
check("and it says the queue lives in memory — the one thing a person must know",
      "memory" in (view.get("note") or "").lower(), str(view.get("note")))

bad = json.loads(api.app_wait("nope.git", api.WaitIn()).body)
check("an app that does not exist cannot be queued", bad.get("ok") is False, str(bad))
bad2 = json.loads(api.app_wait("box.git", api.WaitIn(action="teleport")).body)
check("and neither can an action that is not an action", bad2.get("ok") is False, str(bad2))

# --------------------------------------------------------------- the pump

# The route spawns the pump on a thread, which this test cannot wait on and should
# not race with: the thread is replaced with a stub, and the pump is driven by hand
# below. Everything the pump does is a plain function call, so nothing is lost.
_real_thread = threading.Thread


class NoThread:
    """A thread that never runs. `start()` does nothing, so the test owns the clock."""

    def __init__(self, target=None, args=(), daemon=None, **kw) -> None:
        self.target, self.args = target, args

    def start(self) -> None:
        pass


threading.Thread = NoThread

# Still full: the pump looks, logs the gate's own sentence, and waits — and the
# clock is ours, so a deadline passes in no real time at all.
entry = api._WAIT_QUEUE[0]
entry["max_seconds"] = 25
HR.answer = dict(BUSY)
_real_sleep, _real_time = time.sleep, time.time
OFF = {"s": 0.0}


def fast_forward(sec: float) -> None:
    OFF["s"] += 10.0


time.sleep = fast_forward
time.time = lambda: _real_time() + OFF["s"]
try:
    api._wait_pump(entry)
finally:
    time.sleep, time.time = _real_sleep, _real_time

jit = api._JOBS[body1["job"]]
check("a pump that never sees room does not start anything", RT.started == [], str(RT.started))
check("it says the card is still held, in the gate's own words",
      any("only 2.4 GB" in l for l in jit["log"]), str(jit["log"]))
check("and it reports the card's numbers as progress rather than as text",
      (jit.get("progress") or {}).get("total") == 8.0, str(jit.get("progress")))
check("giving up ends the job as a failure, not as a success",
      jit["state"] == "failed", jit["state"])
check("and it says plainly that nothing was started",
      any("nothing was started" in l for l in jit["log"]), str(jit["log"]))
check("the queue is empty afterwards — a given-up intent is not left in line",
      api._WAIT_QUEUE == [], str(api._WAIT_QUEUE))

# Now the card frees. The gate answers None, which is what it does when there is
# nothing to warn about — so the pump must read that as "go", not as "unknown".
HR.answer = None
r3 = api.app_wait("box.git", api.WaitIn())
job3 = json.loads(r3.body)["job"]
api._wait_pump(api._WAIT_QUEUE[0])
jit = api._JOBS[job3]
check("when the card frees, the queued launch actually runs",
      RT.started == [("box.git", api.APP_SCRIPTS["launch"])], str(RT.started))
check("and it runs the app's own default script — Athena does not invent one",
      RT.started and RT.started[0][1] is None, str(RT.started))
check("the job ends done, carrying what was started",
      jit["state"] == "done" and (jit.get("result") or {}).get("app") == "box.git",
      str(jit.get("result")))
check("and the queue is empty once it has run", api._WAIT_QUEUE == [], str(api._WAIT_QUEUE))

# A launch the engine refuses is reported as the engine's own refusal.
RT.next_response = {"ok": False, "error": "the app's launcher refused to start"}
r4 = api.app_wait("box.git", api.WaitIn())
job4 = json.loads(r4.body)["job"]
api._wait_pump(api._WAIT_QUEUE[0])
jit = api._JOBS[job4]
check("a refused launch inside the queue is a failed job, with the refusal quoted",
      jit["state"] == "failed" and "refused" in (jit.get("error") or ""),
      str(jit.get("error")))
RT.next_response = {"ok": True}

# --------------------------------------------------------------- taking it back

HR.answer = dict(BUSY)
r5 = api.app_wait("box.git", api.WaitIn())
job5 = json.loads(r5.body)["job"]
gone = json.loads(api.app_unwait("box.git").body)
check("a waiting launch can be taken back", gone.get("removed") == 1, str(gone))
check("and taking it back when there is none is not an error",
      json.loads(api.app_unwait("box.git").body).get("removed") == 0)
api._wait_pump(api._WAIT_QUEUE[0] if api._WAIT_QUEUE else
               {"app": "box.git", "jid": job5, "action": "launch"})
jit = api._JOBS[job5]
check("the pump ends the job the moment the entry is gone, without starting it",
      jit["state"] == "done" and (jit.get("result") or {}).get("cancelled") is True,
      str(jit.get("result")))
check("and it says the work was withdrawn rather than done",
      any("taken off the queue" in l for l in jit["log"]), str(jit["log"]))
# Exactly two launches in this whole file, both of them asked for: the one that ran
# when the card freed, and the one the engine then refused. Taking an entry back
# must add none, which is what this counts.
check("taking one back launches nothing — two launches, both of them asked for",
      len(RT.started) == 2, str(RT.started))

threading.Thread = _real_thread   # put the real one back for anything after this file

print()
print(f"t_wait: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("failed: " + "; ".join(FAILURES))
sys.exit(1 if FAIL else 0)
