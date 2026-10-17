"""The unattended ladder: a repo becomes a verified app with no LLM, and the
cases a script cannot decide come back as an escalation with evidence.

Two real repositories, both public:
  * octocat/Spoon-Knife  — static html: the scanner must run it and PROVE it
  * simonw/datasette     — no entry point: the scanner must refuse, not guess

Run with `env -u PYTHONPATH` from this directory (see the athena-dev skill):
    env -u PYTHONPATH <hermes venv>/Scripts/python.exe t_ladder.py
"""
import importlib.util
import json
import shutil
import subprocess
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
STATIC_REPO = "https://github.com/octocat/Spoon-Knife"
UNDECIDABLE_REPO = "https://github.com/simonw/datasette"
STATIC_APP = "zz-ladder-static"
ESCALATE_APP = "zz-ladder-esc"
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


def wait(api, jid, budget=300):
    t0 = time.time()
    while time.time() - t0 < budget:
        j = api._JOBS.get(jid) or {}
        if j.get("state") != "running":
            return j
        time.sleep(3)
    return api._JOBS.get(jid) or {}


def rmtree(path: Path):
    """Windows: git leaves read-only objects behind and rmtree gives up on them."""
    if path.exists():
        subprocess.run(f'cmd /c rmdir /S /Q "{path}"', shell=True,
                       capture_output=True, text=True)


def main():
    mod, api = load()
    home = Path(api.engine.home)
    print("routes:")
    check("/catalog/auto is mounted", any(r.path == "/catalog/auto" for r in api.router.routes))
    check("auto_build is callable from the agent half", callable(getattr(api, "auto_build", None)))

    for app in (STATIC_APP, ESCALATE_APP):
        rmtree(home / "api" / app)
    try:
        print("1. unattended, a static site (nothing to install, no entry point):")
        started = api.auto_build(STATIC_REPO, name=STATIC_APP, force=True, wait=90)
        check("the ladder accepted the job", started.get("ok") is True, json.dumps(started)[:200])
        j = wait(api, started.get("job"))
        res = j.get("result") or {}
        check("the job finished", j.get("state") == "done", j.get("error") or j.get("state"))
        check("VERIFIED — it came up and answered", res.get("verified") is True,
              json.dumps({k: v for k, v in res.items() if k != "logs"})[:300])
        check("it was proved on a real URL", bool(res.get("proved")), json.dumps(res)[:200])
        check("no escalation", not res.get("escalate"), json.dumps(res)[:200])
        print("       " + " | ".join((j.get("log") or [])[-3:])[:220])

        print("2. undecidable, no entry point anywhere in the repository:")
        started = api.auto_build(UNDECIDABLE_REPO, name=ESCALATE_APP, force=True, wait=90)
        check("the ladder accepted the job", started.get("ok") is True, json.dumps(started)[:200])
        j = wait(api, started.get("job"))
        res = j.get("result") or {}
        check("the job finished (a refusal is an outcome, not a crash)",
              j.get("state") == "done", j.get("error") or j.get("state"))
        check("ESCALATED rather than fabricated a launch command",
              res.get("escalate") is True and res.get("verified") is False,
              json.dumps({k: v for k, v in res.items() if k != "logs"})[:300])
        check("it names the stage it died at", res.get("stage") == "launcher", res.get("stage"))
        check("it hands over the SPEC to satisfy", str(res.get("spec") or "").endswith("SPEC.md"))
        check("it hands over the authoring guide", str(res.get("guide") or "").endswith("build-an-app.md"))
        check("it says what to do next", "smoke" in str(res.get("ask") or ""))
        check("nothing was installed while refusing", not (home / "api" / ESCALATE_APP / "app").exists())
    finally:
        for app in (STATIC_APP, ESCALATE_APP):
            rmtree(home / "api" / app)
        for slug in ("Spoon-Knife", "datasette"):
            rmtree(home / "cache" / "scaffold" / slug)
        check("fixtures cleaned up",
              not (home / "api" / STATIC_APP).exists() and not (home / "api" / ESCALATE_APP).exists())

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())