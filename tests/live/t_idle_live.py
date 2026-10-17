"""Live: does the reaper actually stop a running app, through the real engine?"""
import importlib.util, socket, sys, time, urllib.request
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")
PL = Path(str(HERMES_ROOT) + r"/plugins/athena")
spec = importlib.util.spec_from_file_location("athena_plugin_api", PL / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec); sys.modules["athena_plugin_api"] = api
spec.loader.exec_module(api)
idle = api._idle_module()

def live_port():
    """Which port the engine is ACTUALLY on. Settings said 42001 while the
    running engine was on 42002 — probe, do not assume."""
    for p in range(42000, 42012):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/info/scripts", timeout=2) as r:
                if r.status == 200:
                    return p
        except Exception:
            continue
    return None

port = live_port()
print("engine listening on:", port)
if port is None:
    # No engine on any port this harness probes, so there is no live app to
    # watch and nothing to prove about stopping one — the same policy as the
    # "nothing is running" case below, and the same reason: a question about a
    # running app that has no answer is not a failure. Checked explicitly
    # because `Runtime(None, …)` is not a shortcut past it: it raises `int(None)`
    # in its own constructor, which reads as a plugin failure in a suite that
    # was only missing an engine.
    print("SKIPPED — no engine is listening, so there is nothing live to watch")
    raise SystemExit(0)
rt = api.athena_runtime.Runtime(port, api.engine.home)
before = rt.running("wan")
print("wan running before:", {k: [r.get("url") for r in v] for k, v in before.items()})
if not before:
    # Nothing to watch. This probe answers a question about a RUNNING app — with
    # none, there is no answer to give, and "no answer" is not a failure. The
    # window's policy is proven by t_idle.py and t_idle_users.py without a live
    # engine; what only this file can show is a real app being really stopped.
    print("SKIPPED — nothing is running, so there is no stop to prove; launch wan first")
    raise SystemExit(0)

# the window is policy, tested elsewhere; here, is anything reaped at all
api._idle_window = lambda: 0.05
idle.reset()
# Armed, not merely touched: the reaper only stops what Hermes launched.
idle.watch("wan.git", what="a live probe")
print("tracked:", idle.snapshot())
api._runtime = lambda: rt          # the reaper must talk to the live engine
api._idle_reaper_start()
t0 = time.time()
gone_at = None
while time.time() - t0 < 90:
    time.sleep(2.0)
    if not rt.running("wan"):
        gone_at = time.time() - t0
        break
print("wan gone after %.0fs" % gone_at if gone_at is not None else "STILL RUNNING after 90s")
print("tracked after:", idle.snapshot())

def port_open(p):
    with socket.socket() as s:
        s.settimeout(2.0)
        return s.connect_ex(("127.0.0.1", p)) == 0
print("port 42003 still open:", port_open(42003))
print("engine port in settings was:", api.engine.port, "(the drift is why we probed)")
log = Path(api.engine.home) / "logs" / "idle.log"
print("--- idle.log ---")
print(log.read_text(encoding="utf-8", errors="replace").strip() if log.is_file() else "(no log)")
raise SystemExit(0 if gone_at is not None else 1)
