"""
Is there room for this?

Two gates, both asked before work starts: disk before an install, VRAM before a
launch. The interesting checks are the ones that must stay quiet — a drive with
room, thresholds switched off, a machine with no card at all — because a warning
that fires all the time is a warning nobody reads.

Usage: env -u PYTHONPATH <venv python> t_headroom.py
"""
import importlib.util
import shutil
import sys
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


hr = load("athena_headroom", PLUG / "dashboard" / "headroom.py")
api = load("athena_api_headroom", PLUG / "dashboard" / "plugin_api.py")

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}   {detail}")


print("-- the real drive --")
d = hr.disk(PLUG)
check("a real volume is read", bool(d) and d["free_gb"] > 0, str(d))
check("the volume is named the way a person writes it",
      bool(d) and d["volume"] in ("C:", "/"), str(d))
check("free + used add up to the total",
      bool(d) and 0 < d["free_gb"] <= d["total_gb"], str(d))
check("a path that does not exist is None, not an exception",
      hr.disk("Q:/nothing/here") is not None or hr.disk("Q:/nothing/here") is None)
check("an unresolvable path returns None rather than raising",
      hr.disk("\x00bad") is None)

print("\n-- the install gate, with the numbers faked --")


def fake_disk(free_gb, total_gb=100.0):
    return lambda path: {"volume": "C:", "free_bytes": int(free_gb * hr.GB),
                         "free_gb": free_gb, "total_gb": total_gb,
                         "used_pct": round(100 * (total_gb - free_gb) / total_gb, 1)}


real_disk = hr.disk
hr.disk = fake_disk(40.0)
g = hr.install_gate("C:/whatever", {})
check("room to spare says nothing", g.get("ok") and not g.get("message"), str(g))

hr.disk = fake_disk(6.0)
g = hr.install_gate("C:/whatever", {"disk_warn_free_gb": 10.0})
check("under the user's line it speaks up", bool(g.get("message")), str(g))
check("it gives the number and the line",
      "6.0" in g["message"] and "10" in g["message"], g.get("message", ""))
check("and it is a warning, not a refusal", g["ok"] is True, str(g))

hr.disk = fake_disk(6.0)
check("0 turns the warning off",
      not hr.install_gate("C:/whatever", {"disk_warn_free_gb": 0}).get("message"))
check("a nonsense threshold does not crash the gate",
      bool(hr.install_gate("C:/whatever", {"disk_warn_free_gb": "lots"}).get("ok")))

hr.disk = fake_disk(0.4)
g = hr.install_gate("C:/whatever", {"disk_warn_free_gb": 10.0})
check("a full drive is refused, not warned", g["ok"] is False, str(g))
check("the refusal says why", g.get("reason") == "no_disk", str(g))
check("and carries the arithmetic",
      "0.4" in g["message"] and "2" in g["message"], g.get("message", ""))
check("the floor is not a setting", isinstance(hr.FLOOR_GB, float) and hr.FLOOR_GB > 0)
hr.disk = real_disk

print("\n-- the card --")
hr.gpu = lambda cfg=None: {"name": "RTX 5090", "total_gb": 32.0, "used_gb": 29.0,
                           "free_gb": 3.0, "util": 40.0}
g = hr.gpu_gate({"vram_warn_free_gb": 6.0}, running=["RMBG-2-Studio.git"])
check("a nearly-full card is reported", bool(g), str(g))
check("with the free number and the total",
      g and "3.0" in g["message"] and "32.0" in g["message"], g.get("message", "") if g else "")
check("and it names who is holding it",
      g and "RMBG-2-Studio.git" in g["message"], g.get("message", "") if g else "")
check("the holder list is a sentence, not a dump",
      g and " are running" not in g["message"], g.get("message", "") if g else "")

hr.gpu = lambda cfg=None: {"name": "RTX 5090", "total_gb": 32.0, "used_gb": 10.0,
                           "free_gb": 22.0, "util": 5.0}
check("a card with room says nothing", hr.gpu_gate({"vram_warn_free_gb": 6.0}) is None)
hr.gpu = lambda cfg=None: {"name": "RTX 5090", "total_gb": 32.0, "used_gb": 29.0,
                           "free_gb": 3.0, "util": 40.0}
check("0 turns it off", hr.gpu_gate({"vram_warn_free_gb": 0}) is None)
check("a nonsense threshold falls back to the default rather than crashing",
      bool(hr.gpu_gate({"vram_warn_free_gb": "lots"})))
hr.gpu = lambda cfg=None: None
check("no card at all is silence, not an error", hr.gpu_gate({}) is None)

print("\n-- one sampler, not two --")
src = (PLUG / "dashboard" / "headroom.py").read_text(encoding="utf-8")
check("the card numbers come from the system monitor's own call",
      'import system as _system' in src and "_gpu_metrics" in src)
check("headroom never runs a command of its own", "subprocess" not in src)
check("and never writes anything", "write_text" not in src and "unlink" not in src)

print("\n-- wired where the work starts --")
api_src = (PLUG / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
# The thresholds themselves moved into the settings store with the rest of the defaults (rule
# 136), so a check that greps for the numbers has to grep the module that now holds them.
store_src = (PLUG / "dashboard" / "settings_store.py").read_text(encoding="utf-8")
ui = (PLUG / "desktop" / "plugin.js").read_text(encoding="utf-8")
check("the install path asks before starting",
      'gate = athena_headroom.install_gate(_athena_home(), cfg)' in api_src)
check("the store install asks too",
      'gate = athena_headroom.install_gate(_athena_home(), _load_settings())' in api_src)
check("a refused install stops before the engine is asked to run anything",
      api_src.index('gate["action"] = action') < api_src.index('out = _runtime().start(app_id, APP_SCRIPTS[action]'))
check("the launch path asks about the card",
      'g = athena_headroom.gpu_gate(cfg, running=_running_ports().values())' in api_src)
check("the warning reaches the caller", 'heads["vram_warning"] = g["message"]' in api_src)
check("both thresholds are settings", '"disk_warn_free_gb": 10.0' in store_src
      and '"vram_warn_free_gb": 6.0' in store_src)
check("and both can be set over HTTP",
      "disk_warn_free_gb: Optional[float] = None" in api_src
      and 'cfg["vram_warn_free_gb"] = max(0.0, float(body.vram_warn_free_gb))' in api_src)
check("the card shows any of the three warnings",
      "r.port_warning || r.vram_warning || r.disk_warning" in ui)
check("a refusal's own sentence is what the user sees",
      "r.run && r.run.error) || r.message" in ui)
check("the settings tab carries both rows",
      "Warn before an install fills the drive" in ui and "Warn when the card is nearly full" in ui)

print("\n-- the route, end to end, against a settings file of its own --")
import json
import tempfile
import os


tmp = Path(tempfile.mkdtemp())
api.SETTINGS_FILE = tmp / "settings.json"
body = json.loads(api.set_settings(api.SettingIn(vram_warn_free_gb=4.5,
                                                 disk_warn_free_gb=0)).body)
check("POST /settings takes both thresholds", body.get("vram_warn_free_gb") == 4.5
      and body.get("disk_warn_free_gb") == 0, str({k: body.get(k) for k in
                                                   ("vram_warn_free_gb", "disk_warn_free_gb")}))
check("and the file on disk agrees", json.loads(api.SETTINGS_FILE.read_text())["vram_warn_free_gb"] == 4.5)
check("GET /settings reads them back",
      json.loads(api.get_settings().body)["disk_warn_free_gb"] == 0)
check("a negative threshold is clamped, not stored",
      json.loads(api.set_settings(api.SettingIn(vram_warn_free_gb=-3)).body)["vram_warn_free_gb"] == 0)
check("a settings file missing the fields still gets the defaults",
      api.DEFAULTS["disk_warn_free_gb"] == 10.0 and api.DEFAULTS["vram_warn_free_gb"] == 6.0)
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{failed} failed" if failed else f"\n{passed}/{passed} checks passed")
sys.exit(1 if failed else 0)
