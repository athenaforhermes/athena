"""Does the browser shim actually reach an app the engine runs?

The shim is only worth wiring if the env Athena gives the engine survives into
the process the app runs. This drops a one-line fixture app into the home, runs
it through the engine exactly as Athena runs a real one, and reads what the app
saw. Cleans up after itself.

    env -u PYTHONPATH <hermes venv>/Scripts/python.exe probe_browser_env.py
"""
import importlib.util
import json
import shutil
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
FIX = "zz-env-probe"


def load():
    spec = importlib.util.spec_from_file_location("athena_plugin", str(PLUGIN / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["athena_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod._load_api()


def main() -> int:
    api = load()
    home = Path(api.engine.home)
    print("home:", home)

    # Always a fresh engine: the env under test is set when Athena spawns it, so
    # an engine that predates the change can only tell us about the old world.
    if api.engine.probe():
        print("restarting the engine so it carries the env under test")
        api.engine.stop()
        time.sleep(2)
    else:
        print("engine is down — starting it")
    api.engine.start()
    deadline = time.time() + 150
    while time.time() < deadline and not api.engine.probe():
        time.sleep(2)
    if not api.engine.probe():
        print("FAIL: the engine would not start")
        return 1
    print("engine: up")

    fx = home / "api" / FIX
    (fx / "app").mkdir(parents=True, exist_ok=True)
    (fx / "probe.py").write_text(
        'import os\nprint("BROWSER=" + str(os.environ.get("BROWSER")))\n', encoding="utf-8")
    (fx / "pinokio.js").write_text(
        'module.exports = { version: "7.0", title: "%s", description: "fixture", '
        'menu: async () => [{ default: true, text: "Probe", href: "probe.js" }] }\n' % FIX,
        encoding="utf-8")
    (fx / "pinokio_meta.json").write_text(
        json.dumps({"title": FIX, "description": "fixture", "version": "2.0"}), encoding="utf-8")
    (fx / "probe.js").write_text(
        'module.exports = { run: [{ method: "shell.run", params: '
        '{ message: "python probe.py" } }] }\n', encoding="utf-8")

    rt = api._runtime()
    lines = []
    res = rt.run(FIX, uri=rt.script_path(FIX, "probe.js"), on_line=lambda ln: lines.append(ln),
                 timeout=180.0, idle_timeout=60.0)
    print("run verdict:", json.dumps(res)[:240] if isinstance(res, dict) else str(res)[:240])

    bodies = [str(x) for x in lines]
    logs = fx / "logs"
    if logs.is_dir():
        for p in sorted(logs.rglob("*")):
            if p.is_file():
                bodies.append(p.read_text(encoding="utf-8", errors="replace"))
    found = [ln.strip() for blob in bodies for ln in blob.splitlines() if "BROWSER=" in ln]
    print("what the app saw:", found or "nothing — the probe never ran")
    ok = any("no-browser.cmd" in f for f in found)

    try:
        rt.stop(FIX)
    except Exception as exc:  # noqa: BLE001
        print("stop:", exc)
    time.sleep(1)
    shutil.rmtree(fx, ignore_errors=True)
    print("fixture removed:", not fx.exists())

    print("\nRESULT:", "the shim reaches the app" if ok else "the shim did NOT reach the app")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
