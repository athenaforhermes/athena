
import importlib.util, sys, json
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")
P = Path(str(HERMES_ROOT) + r"/plugins/athena/dashboard")
sys.path.insert(0, str(P))
spec = importlib.util.spec_from_file_location("athena_api_live", P/"plugin_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["athena_api_live"] = m; spec.loader.exec_module(m)
try:
    print("engine up:", m.engine.probe())
except Exception as e:
    print("engine probe failed:", e)
live = m._running_ports()
print("running ports:", json.dumps(live))
if live:
    port, app = next(iter(live.items()))
    print("live translation:", m.port_failure_note(f"Error: listen EADDRINUSE: address already in use :::%d" % port))
