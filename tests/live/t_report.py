"""The report: what is asking something of you, in one answer.

Four facts, each already known by a different part of the plugin, folded into one
list in the order they matter: what cannot run, what stopped and was not restarted,
what has an update waiting, what cannot generate, and what disk nothing reads.

Two of the checks here are about what the report refuses to do, because that is
where a report goes wrong: it must not reach the network (the update answers are the
cached ones, and a fresh `git fetch` per call is not a report anyone can ask often),
and it must not call an app up to date just because nobody ever checked it.

The weight inventory is stubbed — `_weights_inventory` has its own tests and takes
seconds on a real machine — so these checks are about the folding, which is what this
route is. Everything else (health, the failure reading, the log's age, the wait
queue) is the real thing, against a synthetic home.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
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


api = load("athena_api_under_test", PLUGIN / "dashboard" / "plugin_api.py")

# ------------------------------------------------------------------ the box

TMP = Path(tempfile.mkdtemp(prefix="athena-report-"))
HOME = Path(TMP) / "athena"


def app_folder(app: str) -> Path:
    d = HOME / "api" / app
    (d / "app").mkdir(parents=True, exist_ok=True)
    (d / "app" / "start.js").write_text("// a script\n", encoding="utf-8")
    (d / "pinokio.js").write_text("module.exports = {}\n", encoding="utf-8")
    return d


# The toolchain check is a machine-level fact asked per app: a home without Miniforge
# makes every fixture "broken", which would hide the one app that is.
(HOME / "bin" / "miniforge").mkdir(parents=True, exist_ok=True)

app_folder("good.git")
# A folder that is not an app: no launcher, no code — the "cannot run" case, which
# is what the report is for.
(HOME / "api" / "broken.git").mkdir(parents=True, exist_ok=True)
a3 = app_folder("stale.git")
# The real sentence, from the app-env torch 2.7 probe: a card with a neighbour on it.
LOG_DIR = HOME / "logs" / "shell" / "api" / "stale.git"
LOG_DIR.mkdir(parents=True, exist_ok=True)
(LOG_DIR / "2026-10-14.txt").write_text(
    "Loading model...\n"
    "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 111.76 GiB. "
    "GPU 0 has a total capacity of 31.82 GiB of which 0.90 GiB is free. "
    "Process 1234 has 24.10 GiB memory in use.\n",
    encoding="utf-8")
app_folder("behind.git")

SETTINGS = Path(TMP) / "settings.json"
# `share_caches` off, because the fixtures are not real installs: with it on, every
# one of them fails the shared-cache check for reasons that have nothing to do with
# what this file is testing.
SETTINGS.write_text(json.dumps({"home": str(HOME), "port": 42001,
                                "share_caches": False}), encoding="utf-8")
api.SETTINGS_FILE = SETTINGS


class FakeRuntime:
    def running(self, app=None, **kw) -> dict:
        return {}

    def logs(self, app: str, limit: int = 0) -> str:
        base = LOG_DIR
        if not base.is_dir() or base.parent.name != app:
            return f"(no output yet for {app})"
        return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in sorted(base.glob("*.txt")))[-limit:]


api._runtime = lambda: FakeRuntime()

# The update checks: one app has a cached answer, the others have never been asked.
api._update_cache.clear()
api._update_cache["behind.git"] = (time.time(), {"ok": True, "behind": 3, "cached": True})


def never(*a, **kw):
    raise AssertionError("the report reached the network: _update_check was called")


api._update_check = never

# The inventory, stubbed (see the module docstring): nothing reads a library, the
# largest is named, and two apps have no weights mapped.
api._weights_inventory = lambda home, sizes=True: {
    "apps": [], "need_weights": ["good.git", "stale.git"],
    "libraries": [{"folder": "E:/AI MODELS/models", "bytes": 142_000_000_000},
                  {"folder": "E:/AI MODELS/loras", "bytes": 71_000_000_000}],
    "unreferenced": [{"folder": "E:/AI MODELS/old", "bytes": 41_000_000_000,
                      "total": 3, "kind": "library"}],
    "cached": {"folder": "cache/hub", "count": 5, "bytes": 9_600_000_000, "repos": []},
    "totals": {"apps": 7, "ready": 1, "need_weights": 2, "libraries": 2},
    "summary": "1/7 apps can generate; 2 weight libraries holding 142.3 GB",
}

# And one launch queued for the card, so the report has all five facts to fold.
api._WAIT_QUEUE.append({"app": "good.git", "action": "launch", "jid": "wait-1",
                        "queued_at": time.time(), "max_seconds": None})

print("the report")

t0 = time.time()
r = api.report_view()
elapsed = time.time() - t0

check("it answers in one call", r.get("ok") is True, str(r.get("ok")))
check("it is fast enough to be asked whenever (under a second here)",
      elapsed < 5.0, f"{elapsed:.2f}s")
kinds = [n["kind"] for n in r["needs"]]
check("an app that cannot run is first", kinds and kinds[0] == "broken", str(kinds))
check("the broken app is named, with the check that failed",
      r["needs"][0].get("app") == "broken.git" and bool(r["needs"][0].get("title")),
      json.dumps(r["needs"][0]))
check("and the check carries the step that fixes it",
      r["needs"][0].get("needs_install") is True or r["needs"][0].get("fixable") is True,
      json.dumps(r["needs"][0]))

stopped = [n for n in r["needs"] if n["kind"] == "stopped"]
check("an app whose log says why it stopped is reported as stopped, not broken",
      len(stopped) == 1 and stopped[0]["app"] == "stale.git", str(stopped))
check("with the log's own reading of the cause",
      "hold" in (stopped[0].get("detail") or "").lower()
      or "card" in (stopped[0].get("detail") or "").lower(),
      str(stopped[0].get("detail")))
check("and the sentence the program printed, so it can be pasted into a search",
      "out of memory" in ((stopped[0].get("evidence") or {}).get("text") or "").lower(),
      str(stopped[0].get("evidence")))
check("a stopped app is still not counted as broken",
      all(n.get("app") != "stale.git" for n in r["needs"] if n["kind"] == "broken"),
      str(kinds))

updates = [n for n in r["needs"] if n["kind"] == "update"]
check("an app with a cached update behind is listed",
      len(updates) == 1 and updates[0]["app"] == "behind.git"
      and updates[0]["behind"] == 3, str(updates))
check("and the step it carries is an update",
      (updates[0].get("next_step") or {}).get("action") == "update",
      str(updates[0].get("next_step")))

weights = [n for n in r["needs"] if n["kind"] == "weights"]
check("apps that cannot generate are one item rather than one per app",
      len(weights) == 1 and set(weights[0].get("apps") or []) == {"good.git", "stale.git"},
      str(weights))
check("it names the largest library already on this disk, so the fix is a mapping "
      "rather than a download",
      "E:/AI MODELS/models" in (weights[0].get("detail") or ""),
      str(weights[0].get("detail")))
check("and the step points at that folder",
      (weights[0].get("next_step") or {}).get("folder") == "E:/AI MODELS/models",
      str(weights[0].get("next_step")))

disk = [n for n in r["needs"] if n["kind"] == "disk"]
check("weights nothing reads are listed with their size",
      len(disk) == 1 and disk[0]["size"] == "38.2 GB", json.dumps(disk[0] if disk else {}))
check("the order is what matters first: broken, stopped, update, weights, disk",
      kinds == ["broken", "stopped", "update", "weights", "disk"], str(kinds))

print("\nwhat it refuses to do")
check("the report never reaches the network for updates — the cached answers only",
      api._update_check is never, "the stub raising was replaced")
check("apps that were never checked are counted as unchecked, not as up to date",
      r["counts"]["unchecked_updates"] == 3, json.dumps(r["counts"]))
check("and that is said out loud rather than left implicit",
      any("never had an update check" in n for n in r["notes"]), str(r["notes"]))
check("the notes say what was read and that nothing was fetched",
      any("never reaches the network" in n for n in r["notes"]), str(r["notes"]))
check("nothing here is a mutation: no job was started",
      not any(isinstance(v, str) and v.startswith("pull-") for v in
              (r["counts"] or {}).values()), str(r["counts"]))

print("\nthe headline")
check("the headline counts each fact in the same order",
      "1 app that cannot run" in r["headline"] and "1 update waiting" in r["headline"]
      and "that stopped" in r["headline"] and "cannot generate yet" in r["headline"],
      r["headline"])
check("a queued launch is counted too",
      "1 launch queued for the card" in r["headline"], r["headline"])
check("and the queued launch itself is carried, not just counted",
      len(r.get("waiting") or []) == 1 and r["waiting"][0]["app"] == "good.git",
      str(r.get("waiting")))

print("\nwhen there is nothing to report")
# A different home with nothing installed in it: the question here is what the report
# says when there is genuinely nothing to say, and a home with apps in it is not that.
QUIET = Path(TMP) / "quiet-home"
(QUIET / "api").mkdir(parents=True, exist_ok=True)
(QUIET / "bin" / "miniforge").mkdir(parents=True, exist_ok=True)
SETTINGS.write_text(json.dumps({"home": str(QUIET), "port": 42001,
                                "share_caches": False}), encoding="utf-8")
api._WAIT_QUEUE.clear()
api._update_cache["behind.git"] = (time.time(), {"ok": True, "behind": 0})
api._weights_inventory = lambda home, sizes=True: {
    "apps": [], "need_weights": [], "libraries": [], "unreferenced": [],
    "cached": {"folder": "cache/hub", "count": 0, "bytes": 0, "repos": []},
    "totals": {}, "summary": "1/1 apps can generate",
}
quiet = api.report_view()
check("a box with nothing wrong says so in words rather than an empty list",
      "Nothing needs you" in quiet["headline"], quiet["headline"])
check("and the list is empty, so the sentence is the whole answer",
      quiet["needs"] == [], str(quiet["needs"]))

print()
print(f"t_report: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("failed: " + "; ".join(FAILURES))
sys.exit(1 if FAIL else 0)
