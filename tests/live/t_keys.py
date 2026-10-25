"""
What will this ask me for — wired into the plugin.

`t_keyscan.py` proves the reader; this proves the wiring, and the wiring is where
a read becomes a claim on a screen. Three things have to stay true:

 * A card for an app nobody has scanned says **unknown**, never "runs locally".
   `local: None` and `local: False` are different answers and the UI must be able
   to tell them apart.
 * The app list never walks a folder. It is a polled route; the scan is cached and
   the list only reads the cache — so this file replaces `scan` with one that
   raises and checks the cached path still answers.
 * No value is ever in the payload. The fixture script contains a fake key, and the
   scan output must not contain it.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
import os

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


api = load("athena_api_keys", PLUGIN / "dashboard" / "plugin_api.py")

TMP = Path(tempfile.mkdtemp(prefix="athena-keys-"))
HOME = Path(TMP) / "athena"


def app_folder(app: str, script: str) -> Path:
    d = HOME / "api" / app
    (d / "app").mkdir(parents=True, exist_ok=True)
    (d / "app" / "run.py").write_text(script, encoding="utf-8")
    (d / "pinokio.js").write_text("module.exports = {}\n", encoding="utf-8")
    return d


(HOME / "bin" / "miniforge").mkdir(parents=True, exist_ok=True)

SECRET = "sk-live-do-not-copy-0123456789"
app_folder("wan.git",
           "import os\n"
           f"key = os.environ['OPENAI_API_KEY']  # {SECRET}\n"
           "print('hi')\n")
app_folder("local.git",
           "import os\nmodel = os.environ.get('MODEL_PATH', './models')\n")
# A folder with no launcher and no code: the health fold must still answer.
(HOME / "api" / "empty.git").mkdir(parents=True, exist_ok=True)

SETTINGS = Path(TMP) / "settings.json"
SETTINGS.write_text(json.dumps({"home": str(HOME), "port": 42001,
                                "share_caches": False}), encoding="utf-8")
api.SETTINGS_FILE = SETTINGS

# Credentials: a token *file* is the thing this needs to see. `_held_providers`
# reads the standard Hugging Face locations under the user's home — a badge must
# not open Hermes' own auth.json — so point pathlib's home at the sandbox and
# write the token there. The real user profile is never touched.
import pathlib
_real_home = pathlib.Path.home
pathlib.Path.home = staticmethod(lambda: HOME)
(HOME / ".cache" / "huggingface").mkdir(parents=True, exist_ok=True)
(HOME / ".cache" / "huggingface" / "token").write_text("hf_fake\n", encoding="utf-8")

# The collector is stubbed out to prove the badge degrades instead of failing:
# `collect_credentials` now reads Hermes' secret routes and no longer shells out,
# but a badge must still answer when it cannot reach them at all.
COLLECTOR_CALLS: list[int] = []


def no_subprocess(*a, **kw):
    COLLECTOR_CALLS.append(1)
    raise AssertionError("collect_credentials was called")


api.collect_credentials = no_subprocess

print("what Hermes already holds, without asking anybody")
try:
    held = api._held_providers()
finally:
    pathlib.Path.home = _real_home
check("the badge consults Hermes' credential collector first", bool(COLLECTOR_CALLS))
check("a failing collector does not break the badge", "huggingface" in held, str(held))
check("a token file at the standard location counts as held", "huggingface" in held, str(held))
check("a provider confirmed nowhere is not claimed",
      "replicate" not in held, str(held))

print("\nreading an app that was never read")
never = api._keys_cached("local.git")
check("an app nobody scanned reads as unknown, not as local",
      never["scanned"] is False and never["local"] is None, json.dumps(never))
check("the unknown shape is complete, so a card needs no special case",
      set(never) == {"scanned", "local", "keys", "keys_total", "held_count",
                     "cloud_total", "complete"}, str(sorted(never)))

print("\nreading it on request")
ran = api.keys_of("wan.git")
check("the scan runs and answers", ran.get("ok") is True and ran["keys_total"] == 1,
      json.dumps(ran)[:200])
check("the key it wants is named, with the line it was read from",
      ran["keys"][0]["name"] == "OPENAI_API_KEY" and bool(ran["keys"][0]["line"]),
      json.dumps(ran["keys"][0]))
cache_file = api._keyscan_module().cache_path(HOME, "wan.git")
check("the answer is cached beside the plugin's home, not in the app's folder",
      cache_file.is_file() and HOME in cache_file.parents, str(cache_file))
blob = cache_file.read_text(encoding="utf-8")
check("and the cache holds no value from the file it read",
      SECRET not in blob and "OPENAI_API_KEY" in blob, blob[:120])

# The app list is polled: from here on, a walk must be impossible.
ks = api._keyscan_module()
real_scan = ks.scan


def no_walk(*a, **kw):
    raise AssertionError("a polled route walked the folder: scan was called")


ks.scan = no_walk
try:
    cached = api._keys_cached("wan.git")
    check("a polled read answers from the cache alone",
          cached["scanned"] is True and cached["keys_total"] == 1, json.dumps(cached))
    check("and it is a summary, not the whole reading — no evidence lines in the list",
          "line" not in (cached["keys"][0] if cached["keys"] else {}), json.dumps(cached))
    again = api.keys_of("wan.git")
    check("asking again does not read the folder a second time",
          again["keys_total"] == 1 and again.get("scanned_at") == ran.get("scanned_at"),
          json.dumps(again)[:120])
    try:
        api.keys_of("wan.git", refresh=True)
        check("refresh=1 reads the folder again", False, "the walk was never called")
    except AssertionError:
        check("refresh=1 reads the folder again", True)
finally:
    ks.scan = real_scan

print("\nthe app list carries it")
apps = json.loads(bytes(api.apps().body).decode("utf-8"))
rows = {a["id"]: a for a in apps["apps"]}
check("every app in the list carries a keys answer",
      all("keys" in a for a in apps["apps"]), str([a["id"] for a in apps["apps"]]))
check("the scanned app's row carries its real answer",
      rows.get("wan.git", {}).get("keys", {}).get("keys_total") == 1,
      json.dumps(rows.get("wan.git", {}).get("keys")))
check("the unscanned app's row carries unknown rather than local",
      rows.get("local.git", {}).get("keys", {}).get("local") is None,
      json.dumps(rows.get("local.git", {}).get("keys")))

print("\nthe health panel says it in words")
api.keys_of("local.git")
local_health = api._app_health("local.git")
access_checks = [c for c in local_health["checks"] if c["name"] == "Access"]
check("health carries one Access line", len(access_checks) == 1,
      str([c["name"] for c in local_health["checks"]]))
check("it is informational, so a working app is not called broken",
      access_checks and access_checks[0]["informational"] is True
      and local_health["ok"] is True, json.dumps(access_checks))
check("an app that runs on this box says so",
      "runs on this box" in (access_checks[0]["detail"] if access_checks else ""),
      str(access_checks))
check("and the structured answer rides along for the card",
      local_health.get("access", {}).get("scanned") is True,
      json.dumps(local_health.get("access")))

wan_health = api._app_health("wan.git")
wan_access = [c for c in wan_health["checks"] if c["name"] == "Access"]
check("an app that wants a key nobody holds says which one",
      wan_access and "OPENAI_API_KEY" in wan_access[0]["detail"],
      str(wan_access))
check("every app gets an Access line, even one with no code at all",
      any(c["name"] == "Access" for c in api._app_health("empty.git")["checks"]),
      "no Access check for a bare folder")

print("\nthe route, above the catch-all")
route_src = (PLUGIN / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
at_keys = route_src.find('@router.get("/apps/{app_id}/keys")')
at_catch = route_src.find('@router.post("/apps/{app_id}/{action}")')
check("the keys route is declared", at_keys > 0, str(at_keys))
check("and above the per-app action catch-all, or it would never be reached",
      at_keys < at_catch, f"{at_keys} vs {at_catch}")
resp = api.app_keys("wan.git")
payload = json.loads(bytes(resp.body).decode("utf-8"))
check("the route answers with the reading", payload.get("keys_total") == 1,
      json.dumps(payload)[:160])
check("a refresh flag is accepted rather than refused",
      json.loads(bytes(api.app_keys("wan.git", refresh=1).body).decode("utf-8")).get("ok")
      is True)
check("an app that does not exist is a refusal, not a traceback",
      api.keys_of("nope.git").get("ok") is False, json.dumps(api.keys_of("nope.git")))

print(f"\nt_keys: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
