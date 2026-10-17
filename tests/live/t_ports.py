"""
Two apps, one socket.

There is no pre-launch check to test, and this file is the record of why: no
app installed here declares a port, so nothing can be known before the launch.
What can be known is what the app's own output says when a bind fails, and which
of Athena's apps is holding that port at that moment. That is what is tested —
plus the negative cases, since a missing module reported as a port clash would
be worse than no message at all.

Usage: env -u PYTHONPATH <venv python> t_ports.py
"""
import importlib.util
import sys
import types
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUG = Path(str(HERMES_ROOT) + r"/plugins/athena")
sys.path.insert(0, str(PLUG / "dashboard"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


api = load("athena_api_ports", PLUG / "dashboard" / "plugin_api.py")

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}   {detail}")


def fake_runtime(rows):
    """Stand in for the engine's book: `{folder: [{'uri','url'}]}`.

    Filters the way the real one does: asked about one app, it answers about
    that app. Without the filter an app looks like it owns the *other* app's
    port, which is the bug this check exists to find.
    """
    def running(app=None):
        if app is None:
            return rows
        want = app[:-4] if app.lower().endswith(".git") else app
        return {k: v for k, v in rows.items()
                if k in (app, want) or (k.lower().endswith(".git") and k[:-4] == want)}
    return types.SimpleNamespace(running=running)


rows = {"RMBG-2-Studio.git": [{"uri": "x", "url": "http://127.0.0.1:7860"}],
        "yue2-groove-pinokio.git": [{"uri": "y", "url": "http://127.0.0.1:7861"},
                                    {"uri": "y2", "url": "http://127.0.0.1:7862"}],
        "no-ui.git": [{"uri": "z", "url": None}],
        "empty.git": []}
api._runtime = lambda: fake_runtime(rows)

print("-- which app is on which port, right now --")
live = api._running_ports()
check("every running app that opened a URL is mapped",
      live == {7860: "RMBG-2-Studio.git", 7861: "yue2-groove-pinokio.git",
               7862: "yue2-groove-pinokio.git"}, str(live))
check("an app with no URL contributes nothing", "no-ui.git" not in live.values())
check("a bind on 0.0.0.0 is read off the URL the engine recorded",
      api._port_of("http://0.0.0.0:8188") == 8188)


def engine_down():
    raise RuntimeError("engine is not running")


api._runtime = engine_down
check("an engine that is down is a quiet zero, not an exception",
      api._running_ports() == {})
api._runtime = lambda: fake_runtime(rows)

print("\n-- the failure the app printed itself --")
note = api.port_failure_note("Error: listen EADDRINUSE: address already in use :::7860")
check("a failed bind is translated", bool(note), note or "")
check("it names the app holding the socket", bool(note) and "RMBG-2-Studio.git" in note, note or "")
check("and the port", bool(note) and "7860" in note, note or "")
check("it says what to do about it",
      bool(note) and ("stop it" in note or "another port" in note), note or "")

check("a missing module is not a port clash",
      api.port_failure_note("ModuleNotFoundError: No module named 'torch'") is None)
check("an empty output is not a port clash", api.port_failure_note("") is None)
check("None is not a port clash", api.port_failure_note(None) is None)
check("a Windows bind error still counts",
      bool(api.port_failure_note("bind() to 0.0.0.0:5555 failed (10048: address already in use)")))
unknown = api.port_failure_note("EADDRINUSE: address already in use :::5555")
check("a clash on a port no app of ours holds still says something", bool(unknown))
check("and it does not invent an owner", "right now" not in (unknown or ""))

print("\n-- the check that is not there --")
api_src = (PLUG / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
ui = (PLUG / "desktop" / "plugin.js").read_text(encoding="utf-8")
check("no pre-launch port check is claimed", "port_conflict" not in api_src)
check("and no declared-port read is left behind", "_declared_ports" not in api_src)
check("the reason is written down where the next person will look",
      "Nothing in the seven" in api_src)
check("_app_port is defined once", api_src.count("def _app_port(") == 1,
      str(api_src.count("def _app_port(")))
check("the launch path translates a failed bind", 'out["port_failure"] = note' in api_src)
check("the smoke verdict carries it too", 'out["port_note"] = port_note' in api_src)
check("the card shows it instead of clearing its message",
      "const head = (r && (r.port_warning || r.vram_warning || r.disk_warning)) || ''" in ui)
check("and shows it on a failure as well",
      "r.port_failure || r.port_warning || r.vram_warning" in ui)

print(f"\n{failed} failed" if failed else f"\n{passed}/{passed} checks passed")
sys.exit(1 if failed else 0)
