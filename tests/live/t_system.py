"""
One port, two readers.

`plugin_api.py` mounts this module's router, so the direction of an import is a
cycle and the System tab cannot borrow the engine's answer. It read a constant
instead — `http://127.0.0.1:42001` — while the rest of the plugin read the
settings file and `ATHENA_PORT`. Any Athena started on another port showed its
app rows as though the engine were down, which is the worst shape a wrong answer
takes: not an error, just a quieter, emptier screen.

Two resolutions exist because of the cycle. What must not exist is two *different*
answers, so this file is the thing that holds them together: every case below
asks both modules the same question and requires the same number back. Drift fails
here rather than in a tab.

The other half is the typo. `int()` ran at settings load, so `ATHENA_PORT=4200o`
took the whole plugin down — every route raising, nothing in the panel saying why.
A value that is not a port now falls through to the file and then to the default,
and the checks below include the one that used to be fatal.

Run with `env -u PYTHONPATH -u ATHENA_HOME -u ATHENA_PORT` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

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


api = load("athena_api_system_test", PLUGIN / "dashboard" / "plugin_api.py")
sysmod = load("athena_system_under_test", PLUGIN / "dashboard" / "system.py")

# Both sides are pointed at one synthetic settings file. Nothing here touches the
# real plugin's settings.json, and the file is rewritten between cases so the
# "read per call, not frozen at import" claim is exercised rather than assumed.
TMP = Path(tempfile.mkdtemp(prefix="athena-system-")).resolve()
SETTINGS = TMP / "settings.json"
api.SETTINGS_FILE = SETTINGS
api.LEGACY_SETTINGS_FILE = TMP / "absent.json"
sysmod.SETTINGS_FILE = SETTINGS
sysmod.LEGACY_SETTINGS_FILE = TMP / "absent.json"


def both(want: int, label: str, env: str = None, **file_keys) -> None:
    """Ask the engine and the tab what port this machine is on, and compare."""
    SETTINGS.write_text(json.dumps(file_keys), encoding="utf-8")
    if env is None:
        os.environ.pop("ATHENA_PORT", None)
    else:
        os.environ["ATHENA_PORT"] = env
    try:
        api_answer = api._load_settings()["port"]
    except Exception as e:  # noqa: BLE001 — an exception here is the failing case
        check(f"{label}: the engine reads it", False, f"{type(e).__name__}: {e}")
        return
    tab_answer = sysmod.engine_port()
    check(f"{label}: the engine reads it", api_answer == want, f"got {api_answer}, want {want}")
    check(f"{label}: the System tab reads the same", tab_answer == api_answer,
          f"engine {api_answer}, tab {tab_answer}")
    check(f"{label}: the URL it hands out is that port",
          sysmod.engine_url() == f"http://127.0.0.1:{api_answer}",
          f"got {sysmod.engine_url()}")


both(42001, "nothing set anywhere")
both(43000, "a port in the settings file", port=43000)
both(44000, "a port in the environment, which wins", env="44000", port=43000)
both(43000, "an environment value that is not a port falls through", env="not-a-port", port=43000)
both(42001, "not a port there either, so the default", env="42001x")
both(42001, "a file value that is not a port falls through to the default",
     port="forty-three thousand")

# The settings file is read per call, so a port changed while Hermes is running is
# picked up by both readers without a restart. Frozen at import, this is the check
# that would have caught it.
SETTINGS.write_text(json.dumps({"port": 45000}), encoding="utf-8")
sysmod._ENGINE_TARGET.update({"at": 0.0, "url": "", "parts": None})   # the 5s memo, skipped
check("a moved port is read without a restart",
      sysmod.engine_port() == 45000 and api._load_settings()["port"] == 45000,
      f"tab {sysmod.engine_port()}, engine {api._load_settings()['port']}")
url, parts = sysmod._engine_target()
check("the target the calls use is that same port",
      url == "http://127.0.0.1:45000" and parts.port == 45000, f"got {url!r}")

# ------------------------------------------------------- an engine that is down

# A closed port, chosen by opening one and letting it go: this is the reading the
# panel depends on to say "not running", and it has to be None rather than an
# exception, because a monitor that raises is a monitor that shows nothing.
s = socket.socket()
s.bind(("127.0.0.1", 0))
dead = s.getsockname()[1]
s.close()
SETTINGS.write_text(json.dumps({"port": dead}), encoding="utf-8")
sysmod._ENGINE_TARGET.update({"at": 0.0, "url": "", "parts": None})
check("a port with nothing on it reads as down, not as an error",
      sysmod._engine_get("/info/scripts", timeout=0.6) is None)
check("and the liveness probe agrees", sysmod._engine_up() is False)

SETTINGS.unlink()
os.environ.pop("ATHENA_PORT", None)

print(f"\nt_system: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("failed: " + ", ".join(FAILURES))
sys.exit(1 if FAIL else 0)
