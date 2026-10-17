"""Prove the reaper can see an app is in use even when Athena's viewer is not it.

Why this exists: an app launched from Athena opened its own page in the user's
browser (Gradio's `inbrowser=True`), the Athena viewer was never the thing on
screen, and 121 seconds later the reaper stopped the app under someone mid-typing.
The reaper's only signal was a heartbeat from a pane that was not open — so it was
stopped for being idle while it was being used.

Two halves:
  1. `_idle_users()` reads a real socket, not a mock: a live connection to the
     app's port is "in use", and it says so.
  2. `_idle_reap()` — the reaper's own decision function — keeps such an app and
     still stops one nobody is connected to.

Run it with `env -u PYTHONPATH` from this directory:

    env -u PYTHONPATH <hermes venv>/Scripts/python.exe t_idle_users.py
"""
import importlib.util
import json
import shutil
import socket
import sys
import time
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}  {detail}")


def load():
    spec = importlib.util.spec_from_file_location("athena_plugin", str(PLUGIN / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["athena_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod, mod._load_api()


class FakeRuntime:
    """Stands in for the engine: remembers what it was asked to stop.

    `running()` answers with the shape the real client answers with — a mapping
    of app folder to its running entries — rather than a bare True. The real
    `Runtime.running()` returns `{folder: [{"uri": ..., "url": ...}]}` whether or
    not an app was named, and code that reads it (the port conflict note, the
    launch gate) unpacks that mapping.
    """

    def __init__(self):
        self.stops = []

    def running(self, app=None):
        return {(app or "zz-any"): [{"uri": "start.js", "url": None}]}

    def stop(self, app, script=None, timeout=5.0):
        self.stops.append(app)
        return {"ok": True, "count": 1}


def listener():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    return srv, srv.getsockname()[1]


def main():
    _, api = load()
    idle = api._idle_module()
    check("the idle module loaded", idle is not None)

    # ---------------------------------------------------------------- sockets
    print("\n1. a real socket, read for real")
    srv, port = listener()
    api._app_port = lambda app_id: port   # the port lookup is the engine's job
    check("nothing connected yet reads as 0", api._idle_users("zz") == 0)
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn, _ = srv.accept()
    check("a live connection is seen", api._idle_users("zz") >= 1,
          f"got {api._idle_users('zz')}")
    client.close()
    conn.close()
    time.sleep(1.5)
    check("and it is gone once they hang up", api._idle_users("zz") == 0,
          f"got {api._idle_users('zz')}")

    # ------------------------------------------------------- the decision
    print("\n2. the reaper's own decision, with a client on the line")
    fake = FakeRuntime()
    api._runtime = lambda: fake

    def arm(name):
        """Track an app and backdate it so it is past the window right now."""
        idle.reset()
        idle.touch(name, what="test")
        idle._state[name]["last"] = time.time() - 999
        fake.stops = []

    url = f"http://127.0.0.1:{port}"
    api._app_port = lambda app_id: port

    arm("zz-idle-decision")
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn, _ = srv.accept()
    verdict = api._idle_reap("zz-idle-decision")
    check("an app someone is connected to is kept", verdict == "kept", f"got {verdict}")
    check("and nothing was stopped", fake.stops == [], f"stopped {fake.stops}")
    check("and its clock was reset, so it is not re-examined on the next tick",
          idle.waiting("zz-idle-decision") < 5.0)
    client.close()
    conn.close()
    time.sleep(1.5)

    print("\n3. the same app, nobody connected")
    arm("zz-idle-decision")
    verdict = api._idle_reap("zz-idle-decision")
    check("it is stopped", verdict == "stopped", f"got {verdict}")
    check("the engine was asked to stop exactly that app",
          fake.stops == ["zz-idle-decision"], f"stopped {fake.stops}")
    srv.close()

    # ------------------------------------------------------------- the shim
    print("\n4. the browser shim")
    shim = PLUGIN / "tools" / "no-browser.cmd"
    check("the shim exists", shim.is_file(), str(shim))
    text = (PLUGIN / "dashboard" / "engine.py").read_text(encoding="utf-8")
    check("the engine's child env carries it", 'env["BROWSER"] = str(_no_browser)' in text)
    check("and it is only set when the file is there",
          "_no_browser.is_file()" in text)

    # No live browser is started here on purpose: `webbrowser.open()` with this
    # shim in BROWSER was checked by process count beforehand — the control run
    # spawned a browser, this one did not.
    print("\n5. the engine hands its env down to the app it runs")
    kernel = PLUGIN / "engine" / "node_modules" / "pinokiod"
    merges = 0
    if kernel.is_dir():
        for js in kernel.rglob("*.js"):
            try:
                body = js.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "process.env" in body and "env:" in body:
                merges += 1
    check("the engine builds app envs from its own process.env", merges >= 1,
          f"{merges} file(s)")

    # ------------------------------------------- only Hermes's apps get a fuse
    print("\n6. the window applies to apps Hermes started, and only those")
    idle.reset()
    idle.touch("zz-mine", what="a click in the UI")
    idle.touch("zz-mine", what="viewer open")
    idle.watch("zz-theirs", what="an agent launch")
    for name in ("zz-mine", "zz-theirs"):
        idle._state[name]["last"] = time.time() - 999
    due = idle.due(120)
    check("an app the user started is never due, however quiet", "zz-mine" not in due,
          f"due={due}")
    check("an app Hermes started is due once it goes quiet", "zz-theirs" in due,
          f"due={due}")
    check("using it does not arm it", not idle.watched("zz-mine"))
    check("and touching an armed app does not disarm it",
          (idle.touch("zz-theirs", what="still being used"), idle.watched("zz-theirs"))[1])
    check("the snapshot says which is which",
          {r["app"]: r["watched"] for r in idle.snapshot()}
          == {"zz-mine": False, "zz-theirs": True},
          json.dumps(idle.snapshot()))

    # The same function serves both doors, so this is where the difference has
    # to be visible: the route's default is the safe one.
    print("\n7. the two doors into app_action, and only the agent's arms")
    import json as _json

    fx = Path(api.engine.home) / "api" / "zz-idle-door"
    (fx).mkdir(parents=True, exist_ok=True)
    (fx / "pinokio.js").write_text(
        'module.exports = { version: "7.0", title: "zz-idle-door", description: "fixture", '
        'menu: async () => [{ default: true, text: "Start", href: "start.js" }] }\n', encoding="utf-8")
    (fx / "start.js").write_text(
        'module.exports = { run: [{ method: "shell.run", params: { message: "echo hi" } }] }\n',
        encoding="utf-8")

    class DoorRuntime(FakeRuntime):
        def start(self, app, script=None, uri=None, input=None):
            return {"ok": True, "state": "running"}

    api._runtime = lambda: DoorRuntime()
    api.engine.probe = lambda *a, **k: True     # the engine is not the subject here
    api.seed_credentials = lambda *a, **k: None

    def launch(**kw):
        resp = api.app_action("zz-idle-door", "launch", **kw)
        return _json.loads(resp.body.decode("utf-8"))

    idle.reset()
    ui = launch()
    check("the UI's launch is not armed", ui.get("idle", {}).get("watched") is False,
          json.dumps(ui.get("idle")))
    check("and the record agrees", not idle.watched("zz-idle-door"))
    idle._state[idle._norm("zz-idle-door")]["last"] = time.time() - 999
    check("so it is never due, however long it sits", idle.due(120) == [], f"{idle.due(120)}")

    idle.reset()
    agent = launch(origin="agent")
    check("the agent's launch is armed", agent.get("idle", {}).get("watched") is True,
          json.dumps(agent.get("idle")))
    check("and the record agrees", idle.watched("zz-idle-door"))
    idle._state[idle._norm("zz-idle-door")]["last"] = time.time() - 999
    check("so it is due once it goes quiet", idle.due(120) == ["zz-idle-door"],
          f"{idle.due(120)}")
    check("and its response carries the window it is on",
          (agent.get("idle") or {}).get("window_seconds") is not None,
          json.dumps(agent.get("idle")))

    shutil.rmtree(fx, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
