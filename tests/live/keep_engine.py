"""Start the engine with the plugin's own manager and keep the parent alive.

Prints its own pid, the engine child pid and the engine's *actual* OS parent, so
a parent-gone self-stop can be told apart from a spawn the harness broke.
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
spec = importlib.util.spec_from_file_location("athena_api_keep", PLUGIN / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
sys.modules["athena_api_keep"] = api
spec.loader.exec_module(api)

print(f"harness pid {os.getpid()} ppid {os.getppid()} home {api.engine.home} port {api.engine.port}",
      flush=True)
if not api.engine.probe():
    api.engine.start()
    for _ in range(60):
        if api.engine.probe():
            break
        time.sleep(1)
proc = getattr(api.engine, "_proc", None)
print(f"engine up: {api.engine.probe()} child pid {getattr(proc, 'pid', None)}", flush=True)

for i in range(40):  # ~10 minutes, well past any test
    time.sleep(15)
    print(f"  t+{(i + 1) * 15}s  running={api.engine.probe()}", flush=True)
