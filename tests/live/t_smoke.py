"""Prove the smoke test both passes a working app and FAILS a broken one.

The failure half is the point: a generated launcher can print a URL and exit a
second later, and a check that cannot fail is not a check. Run it with
`env -u PYTHONPATH` from this directory:

    env -u PYTHONPATH <hermes venv>/Scripts/python.exe t_smoke.py
"""
import importlib.util
import json
import shutil
import sys
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
FIXTURE = "zz-smoke-fail"
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


def make_liar(home: Path) -> Path:
    """An app whose launcher announces a URL and then dies — the exact lie a
    generated launcher tells when a dependency is missing."""
    fx = home / "api" / FIXTURE
    (fx / "app").mkdir(parents=True, exist_ok=True)
    (fx / "pinokio.js").write_text(
        'module.exports = { version: "7.0", title: "zz-smoke-fail", '
        'description: "fixture", menu: async () => '
        '[{ default: true, text: "Start", href: "start.js" }] }\n', encoding="utf-8")
    (fx / "pinokio_meta.json").write_text(
        json.dumps({"title": "zz-smoke-fail", "description": "fixture", "version": "2.0"}),
        encoding="utf-8")
    (fx / "app" / "die.py").write_text(
        "print('Uvicorn running on http://127.0.0.1:42999')\nraise SystemExit(1)\n",
        encoding="utf-8")
    (fx / "start.js").write_text(
        'module.exports = {\n  daemon: true,\n  run: [\n'
        '    { method: "shell.run", params: { path: "app", message: ["python die.py"],\n'
        '      on: [{ event: "/(http:\\\\/\\\\/[0-9.:]+)/", done: true }] } },\n'
        '    { method: "local.set", params: { url: "{{input.event[1]}}" } }\n'
        '  ]\n}\n', encoding="utf-8", newline="\n")
    return fx


def main():
    mod, api = load()
    home = Path(api.engine.home)
    print("routes:")
    check("/apps/{app_id}/smoke is mounted",
          any(r.path.endswith("/smoke") for r in api.router.routes))
    check("smoke_app is callable from the agent half", callable(getattr(api, "smoke_app", None)))

    fx = make_liar(home)
    try:
        print("failure path (a launcher that lies):")
        bad = api.smoke_app(FIXTURE, wait=40)
        check("verdict is not ok", bad.get("ok") is False, json.dumps(bad)[:200])
        check("it kept the URL the app claimed", bool(bad.get("url")), json.dumps(bad)[:200])
        check("nothing answered on it (http is null)", bad.get("http") is None)
        check("evidence is attached for whoever finishes it", bool(bad.get("logs")))
    finally:
        shutil.rmtree(fx, ignore_errors=True)
        if fx.exists():  # git marks pack files read-only; this fixture has none
            print(f"  WARN could not remove {fx}")
        check("fixture cleaned up", not fx.exists())

    apps = [p.name for p in (home / "api").iterdir() if p.is_dir()] if (home / "api").is_dir() else []
    target = next((a for a in apps if a.lower().startswith("datasette")), None)
    if target:
        print(f"success path (a real app: {target}):")
        good = api.smoke_app(target[:-4] if target.lower().endswith(".git") else target, wait=60)
        check("verdict is ok", good.get("ok") is True, json.dumps(good)[:300])
        check("it answered below 400", (good.get("http") or 999) < 400)
        check("a URL was found", bool(good.get("url")))
    else:
        print("success path: skipped (datasette is not installed in this home)")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
