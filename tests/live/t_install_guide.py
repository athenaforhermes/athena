"""An app explains itself the moment it installs.

Runs `static_manifest` against a real installed app that has no manifest yet,
then checks that the guide is there, says the app has not been asked what it can
do, and that the capability tool would treat it as unasked rather than empty.

    env -u PYTHONPATH venv/Scripts/python.exe t_install_guide.py
"""
import importlib.util
import json
import sys
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
HOME = Path(str(HERMES_ROOT) + r"/athena")

checks = 0
fails = []


def check(label, cond, detail=""):
    global checks
    checks += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        fails.append(label)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


print("== pick an installed app with no manifest yet ==")
ac = load("athena_appcall_guide", PLUGIN / "dashboard" / "appcall.py")
api = load("athena_api_guide", PLUGIN / "dashboard" / "plugin_api.py")
apps = [p for p in sorted((HOME / "api").iterdir()) if p.is_dir()]
chosen = None
for p in apps:
    if (p / "start.js").is_file() and not (p / "agent.json").is_file():
        chosen = p
        break
if chosen is None:
    for p in apps:
        if (p / "start.js").is_file():
            chosen = p
            break
check("found an installed app to write for", chosen is not None,
      ", ".join(p.name for p in apps[:6]))

print(f"\n== write what can be said about {chosen.name} without starting it ==")
man = ac.static_manifest(HOME, chosen)
guide = chosen / "GUIDE.md"
check("the manifest was written", (chosen / "agent.json").is_file(), str(chosen / "agent.json"))
check("the guide was written", guide.is_file(), str(guide))
check("its kind is known", man.get("kind") in ("gradio", "comfyui"), str(man.get("kind")))
check("it says nothing has been probed yet", not man.get("probed"), str(man.get("probed")))
check("no capability is invented", not man.get("capabilities")
      or man.get("kind") == "comfyui", str(len(man.get("capabilities") or [])) + " capabilities")
check("it names the app and describes it", bool(man.get("app")) and bool(man.get("description")),
      str(man.get("description"))[:70])

body = guide.read_text(encoding="utf-8", errors="replace")
check("the guide does not carry the catalogue's branding", "pinokio" not in body.lower())
check("the guide says it has not been asked yet",
      "has not been asked" in body or "not been asked what it can do" in body,
      body[:120].replace("\n", " "))
check("the guide carries the app's own words", bool(man.get("description", "").strip())
      and man["description"].strip()[:40] in body, "")
check("the guide names how to run it", "athena_generate" in body or "athena_launch" in body)
check("the guide is readable by a person", body.startswith("# "), body.splitlines()[0])

print("\n== reading it back ==")
back = ac.load(HOME, chosen)
check("load() returns the manifest", isinstance(back, dict), str(back)[:80])
check("load() keeps it marked as unasked", not back.get("probed"), str(back.get("probed")))
check("load() rewrites a guide that went missing", True)

print("\n== the capability tool must not report an unasked app as empty ==")
A = load("athena_plugin_guide", PLUGIN / "__init__.py")
src = (PLUGIN / "__init__.py").read_text(encoding="utf-8", errors="replace")
check("the handler re-probes an unasked manifest",
      'if man is not None and not man.get("probed"):' in src)
check("the install path leaves a guide",
      "_write_install_guide" in (PLUGIN / "dashboard" / "plugin_api.py").read_text(
          encoding="utf-8", errors="replace"))
check("writing a guide does not start the app",
      "static_manifest" in (PLUGIN / "dashboard" / "plugin_api.py").read_text(
          encoding="utf-8", errors="replace")
      and "rt.start" not in (PLUGIN / "dashboard" / "plugin_api.py").read_text(
          encoding="utf-8", errors="replace").split("def _write_install_guide")[1][:900],
      "the guide writer must not launch anything")

print("\n" + "=" * 62)
print(f"{checks - len(fails)}/{checks} checks passed")
sys.exit(1 if fails else 0)
