"""Conversion: an app that lands must be checked and made Athena's own.

Run: python t_convert.py   (env -u PYTHONPATH)
"""
import importlib.util, json, sys, shutil, inspect
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PL = Path(str(HERMES_ROOT) + r"/plugins/athena")
sys.path.insert(0, str(PL))
calls, joins = [], []
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    return m
api = load("athena_api_convert", PL / "dashboard" / "plugin_api.py")
api._job_add = lambda jid, msg: calls.append((jid, msg))
api._job_spawn = lambda kind, label, work: joins.append((kind, label)) or "job-%d" % len(joins)

fails = []
def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok: fails.append(what)

APP = "wan.git"
folder = api._app_folder(APP)
venv = folder / "app" / "venv"
act = venv / "Scripts" / "activate.bat"
manifest = folder / "agent.json"

print("== conversion is a first-class path, wired at every door ==")
src = inspect.getsource(api)
check("def _convert_app(" in src, "_convert_app exists")
check(src.count("_convert_app(") >= 4, "called from install, import and the sweep (%d refs)"
      % src.count("_convert_app("))
check("def _start_sweep(" in src, "sweep entry exists")
check("_start_sweep()" in src.replace("def _start_sweep", ""), "sweep runs after the engine comes up")
check("provision=False" in src, "install/import conversion does not auto-provision an app")

print("\n== put the app back in the state an import leaves it in ==")
act_before = act.read_text(encoding="utf-8")
man_before = manifest.read_text(encoding="utf-8") if manifest.is_file() else "{}"
was_converted = api._conversion(folder) is not None
restore = json.loads(man_before)
restore.pop("athena", None)
manifest.write_text(json.dumps(restore, indent=2), encoding="utf-8")
act.write_text(act_before.replace(str(venv), r"C:\pinokio\api\wan.git\app\venv"), encoding="utf-8")
check(APP in api._pending_conversions(), "an unconverted app is pending conversion")
check(bool(api._stale_activations(venv)), "its activation scripts name another install")
check(not [c for c in api._app_health(APP, deep=True)["checks"]
           if c["name"] == "Environment paths"][0]["ok"],
      "health notices the stale environment")

print("\n== the sweep picks up what is already on disk ==")
joins.clear()
job = api._start_sweep()
check(job == "job-1", f"sweep spawns a job when there is work: {job}")
check(joins and joins[-1][0] == "convert", f"sweep job kind is convert: {joins[-1]}")

print("\n== conversion ==")
calls.clear()
res = api._convert_app(APP, "jid-convert", provision=False)
check(res["ok"] and res["converted"], "conversion reports success")
check(res["health_ok"], "nothing blocking is left")
check(any("virtualenv" in r for r in res["repairs"]), f"repair recorded: {res['repairs']}")
check(res["guide"] and Path(res["guide"]).is_file(), "guide written: %s" % res["guide"])
check(not api._stale_activations(venv), "activation scripts point at this install now")
check(APP not in api._pending_conversions(), "a converted app is no longer pending")
stamp = api._conversion(folder)
check(bool(stamp), "agent.json carries the conversion record")
check(stamp and stamp["converted_at"], "the record is dated")
check(stamp and stamp["repairs"] == res["repairs"], "the record names what was repaired")
check(stamp and stamp["health_ok"] is True, "the record carries the verdict")
check(any("Converting" in m for _, m in calls), "conversion logs what it is doing")
check(any("ready to run" in m for _, m in calls), "conversion says the app is ready")

print("\n== a note is reported as a note, not as a broken app ==")
check("Install history" in (res.get("outstanding") or []),
      "the standing note is recorded: %s" % res.get("outstanding"))
check(not res.get("blocking"), "it is not counted as blocking")
check(any("press Install" in m for _, m in calls), "it names the action that would fix it")
check(not res["install_started"], "conversion itself ran no install")

print("\n== a real defect is reported as one ==")
calls.clear()
venv2 = folder / "app" / "venv"
act.write_text(act_before.replace(str(venv), r"C:\pinokio\api\wan.git\app\venv"), encoding="utf-8")
res2 = api._convert_app(APP, "jid-convert2", provision=False)
check(res2["repairs"], "the stale environment was repaired again: %s" % res2["repairs"])
check(res2["health_ok"], "and the app is clean again")

print("\n== conversion must not make an app forget what it can do ==")
rmbg = api._app_folder("RMBG-2-Studio.git")
mp = rmbg / "agent.json"
saved = json.loads(mp.read_text(encoding="utf-8")) if mp.is_file() else {}
if saved.get("capabilities"):
    fixture = dict(saved)
    fixture["capabilities"] = saved["capabilities"]
    mp.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    api._convert_app("RMBG-2-Studio.git", "jid-convert3", provision=False)
    after = json.loads(mp.read_text(encoding="utf-8"))
    check(after.get("capabilities") == saved["capabilities"],
          "a probed capability list survives conversion (%d caps)"
          % len(after.get("capabilities") or []))
    check(after.get("probed") == saved.get("probed"), "and so does the probe timestamp")
    check(after.get("athena", {}).get("converted_at"), "and the conversion is recorded")
else:
    check(True, "no probed app to check preservation against")

print("\n== the Fix button still provisions: conversion must not take that away ==")
check("provision: bool = True" in src, "Fix defaults to provisioning")
check("_fix_app(app_id, jid, provision)" in src, "Fix calls the shared checker")
check("if want_install:" in src, "Fix still runs the app's own install when needed")

print("\nrestoring the app to its converted state")
manifest.write_text(man_before if was_converted else json.dumps(restore, indent=2), encoding="utf-8")
print("\n%d check(s) failed" % len(fails))
for f in fails: print("  FAIL", f)
raise SystemExit(1 if fails else 0)
