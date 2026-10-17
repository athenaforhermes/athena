"""Finding Node, and finding the settings file — two things that must not know
which machine they are on.

Athena runs its engine on a Node that Hermes downloaded, and only on a version
whose ABI pinokiod's native addons have prebuilds for. That search used to be two
hard-coded paths, one of them naming the author's own home directory: on any other
machine — and on any Mac or Linux — Node was simply not found, and the engine could
not start. The search is now derived (per-platform tool roots, an override, then
PATH), and these checks are about what it will and will not accept.

The settings file has the same shape of problem in reverse: it used to live beside
the source, in the plugin directory. A plugin directory is code. `hermes plugins
update` replaces it wholesale, so a setting written there is a setting that
disappears on the next update — and one that travelled with the repository besides.
It now lives in Athena's home, with the old location read once so an existing
install keeps what it had.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent.parent

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

print("\n== where Node might be ==")

roots = api._tool_roots()
# The engine supervisor's code lives in `engine.py` since the split, so a check that greps for
# its wiring greps both files. Presence checks are indifferent to which side holds it.
_source = "".join((PLUGIN / "dashboard" / f).read_text(encoding="utf-8")
                  for f in ("plugin_api.py", "engine.py"))
check("no user's name is written into the search — it is derived or it is not searched",
      "C:/Users/" not in _source and "C:\\Users\\" not in _source,
      "a hard-coded home directory would only ever find the author's Node")
check("the override comes first, so a machine with its own toolchain says so",
      (lambda: (os.environ.__setitem__("HERMES_TOOLS", "X:/tools"),
                api._tool_roots()[0] == Path("X:/tools"))[1])(), str(api._tool_roots()[:1]))
os.environ.pop("HERMES_TOOLS", None)
check("the Hermes data directory's own tools folder is on the list",
      any(r.parts[-2:] == ("hermes", "tools") for r in roots), str(roots))
check("... and the three per-platform locations are all covered",
      any(".local" in r.parts and "share" in r.parts for r in roots)
      and any("Library" in r.parts for r in roots)
      and any(".hermes" in r.parts for r in roots),
      str(roots))

print("\n== which Node is accepted ==")

with tempfile.TemporaryDirectory() as tmp:
    box = Path(tmp) / "tools"
    for name in ("node-22.1.0-test", "node-26.0.0-test", "not-a-node"):
        d = box / name
        (d / "bin").mkdir(parents=True)
        (d / "node.exe").write_text("stub", encoding="utf-8")
        (d / "bin" / "node").write_text("stub", encoding="utf-8")

    os.environ["HERMES_TOOLS"] = str(box)
    found = api._bundled_nodes()
    mine = [p for p in found if str(p).startswith(str(box))]
    check("a tools folder is walked for node directories, not for one pinned name",
          len(mine) == 2 and len({p.parent for p in mine}) == 2, str(mine))
    check("the override is looked at in addition to the real locations, not instead of",
          mine and any(not str(p).startswith(str(box)) for p in found), str(found))
    check("the highest version is offered first, so the ABI check is what decides",
          "node-26" in str(mine[0]), str(mine[:2]))
    check("the executable is the one this operating system actually uses",
          all(str(p).endswith("node.exe") if os.name == "nt" else str(p).endswith("bin/node")
              for p in mine), str(mine[:2]))
    check("a directory whose name is not a Node is not offered at all",
          not any("not-a-node" in str(p) for p in found), str(found))
    check("... and a file that only pretends to be Node is refused on its ABI",
          api._valid_node(mine[0]) is None, str(api._valid_node(mine[0])))
    os.environ.pop("HERMES_TOOLS", None)

real = api._find_node()
if real:
    abi = api._valid_node(real)
    check("the machine this suite runs on finds a Node, and it is an accepted one",
          abi is not None and abi == real, real)
    check("... and it is one pinokiod's native addons have a prebuild for",
          abi is not None, real)
else:
    print("  skip  no Node on this machine, so nothing to assert about the real one")

print("\n== stopping the engine on a POSIX host ==")
# Windows reaps a stale engine with `netstat` + `taskkill`. Neither exists on Linux
# or macOS, so the port sweep reads `lsof` or `ss` instead — and reading their output
# is the part worth checking, because a parser that quietly returns nothing looks
# exactly like "nothing is holding the port".
check("`lsof -ti` output becomes pids",
      api._pids_from_lsof("1234\n5678\n") == {"1234", "5678"},
      str(api._pids_from_lsof("1234\n5678\n")))
check("... and blank lines are not pids",
      api._pids_from_lsof("\n  \n") == set(), str(api._pids_from_lsof("\n  \n")))
_ss = ("LISTEN 0  4096  127.0.0.1:42001  0.0.0.0:*  "
       "users:(('node',pid=4242,fd=23),('node',pid=4243,fd=24))")
check("`ss -ltnpH` output becomes pids",
      api._pids_from_ss(_ss) == {"4242", "4243"}, str(api._pids_from_ss(_ss)))
check("... and an ss line with no owner leaves no pid behind",
      api._pids_from_ss("LISTEN 0 4096 127.0.0.1:42001 0.0.0.0:*") == set(),
      str(api._pids_from_ss("LISTEN 0 4096 127.0.0.1:42001 0.0.0.0:*")))
check("the sweep is wired to them rather than to a Windows-only command",
      "_pids_from_lsof" in _source and "_pids_from_ss" in _source
      and "taskkill" in _source)

print("\n== where the settings file lives ==")

check("settings are not written into the plugin directory",
      PLUGIN not in api.SETTINGS_FILE.resolve().parents
      and api.SETTINGS_FILE.resolve() != (PLUGIN / "settings.json").resolve(),
      str(api.SETTINGS_FILE))
check("... and the plugin directory is still named, as the legacy location",
      api.LEGACY_SETTINGS_FILE == PLUGIN / "settings.json", str(api.LEGACY_SETTINGS_FILE))

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    original = api.SETTINGS_FILE, api.LEGACY_SETTINGS_FILE
    api.SETTINGS_FILE = tmp / "home" / "settings.json"
    api.LEGACY_SETTINGS_FILE = tmp / "plugin" / "settings.json"

    saved = dict(api.DEFAULTS)
    saved["vram_warn_free_gb"] = 3.5
    api._save_settings(saved)
    check("saving creates the folder it needs rather than failing on a fresh install",
          api.SETTINGS_FILE.exists(), str(api.SETTINGS_FILE))
    check("... and the value comes back",
          api._load_settings()["vram_warn_free_gb"] == 3.5,
          str(api._load_settings().get("vram_warn_free_gb")))

    api.SETTINGS_FILE.unlink()
    api.LEGACY_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    legacy = dict(api.DEFAULTS)
    legacy["idle_stop_seconds"] = 900
    api.LEGACY_SETTINGS_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    check("an install that predates the move keeps the settings it had",
          api._load_settings()["idle_stop_seconds"] == 900,
          str(api._load_settings().get("idle_stop_seconds")))
    check("... and the legacy file is only a fallback, not a second source of truth",
          not api.SETTINGS_FILE.exists())

    api.SETTINGS_FILE, api.LEGACY_SETTINGS_FILE = original

print("\n== what a look at a stopped engine costs ==")

# The panel looks at /status on a clock, and every app health check asks whether
# the engine is running. On a host that DROPS the SYN of a port with nothing
# behind it instead of refusing it — measured 2.0s against 127.0.0.1 on this
# machine, 4.0s where the name resolves to ::1 first — each of those looks used
# to spend its whole timeout failing, which is most of what "Athena takes 10-30
# seconds to start" was made of. So: a probe of a port nobody holds must answer
# fast, and a port that accepts but says nothing must read as an engine coming
# up rather than keep the caller waiting.


def a_port_nobody_holds() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


dead = api._new_engine()  # the manager now takes its dirs and settings from the API module (rule 153)
dead._port_override = a_port_nobody_holds()

began = time.time()
check("a port nobody holds is reported as nothing listening",
      dead.listening() is False)
took = time.time() - began
check("... and answering that does not wait out a connection that never lands",
      took < 1.0, f"{took:.2f}s against a {api._STARTING_PROBE}s budget")

began = time.time()
check("... while a repeated look is free, because the engine stays down for a while",
      dead.listening() is False and time.time() - began < 0.1,
      f"{time.time() - began:.2f}s")

began = time.time()
probed = dead.probe(timeout=2.0, ttl=0)
took = time.time() - began
check("a probe of a stopped engine still says the engine is not up", probed is False)
check("... in a fraction of the timeout it was given, never all of it",
      took < 1.0, f"{took:.2f}s of a 2.0s timeout")

began = time.time()
state = dead._identify_if_taken()
took = time.time() - began
check("asking who holds a port nobody holds is answered, not waited on",
      state == "none", f"{state} in {took:.2f}s")

# The other half of the same coin: a port that ACCEPTS but has not started
# answering is a cold engine, and the panel has to be able to say so instead of
# sitting on a spinner that only resolves after the whole boot.
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(8)
try:
    booting = api._new_engine()
    booting._port_override = srv.getsockname()[1]
    booting.port_owner = lambda ttl=15.0: 4242       # who owns it is another test's job

    check("a port that accepts and stays silent still counts as something there",
          booting.listening() is True)
    check("... but it is not an engine that is up",
          booting.probe(timeout=api._STARTING_PROBE, ttl=0) is False)
    st = booting.status()
    check("... so the answer says the engine is starting", st["engine_state"] == "starting",
          str(st.get("engine_state")))
    check("... as a field of its own, not as the absence of two others",
          st["starting"] is True and st["ready"] is False, str(st.get("starting")))
    check("... and the holder is named in the same answer", st.get("pid") == 4242,
          str(st.get("pid")))
    check("... besides saying how long the wait has been, because 'a while' is not a number",
          isinstance(st.get("starting_ms"), int) and st["starting_ms"] >= 0,
          str(st.get("starting_ms")))
    first_ms = st["starting_ms"]
    time.sleep(0.4)
    again = booting.status()["starting_ms"]
    check("... counting up rather than answering zero on every poll", again > first_ms,
          f"{first_ms} -> {again}")
    # Ours is measured from the spawn, which is the number the person waiting lived
    # through; an engine this process did not start has no spawn time to offer.
    ours = api._new_engine()
    ours._port_override = srv.getsockname()[1]
    ours.port_owner = lambda ttl=15.0: 4242
    ours._started_at = time.time() - 7.0
    check("... measured from the moment the engine was spawned, not from the first look",
          6500 <= ours.status()["starting_ms"] < 9000, str(ours.status()["starting_ms"]))
finally:
    srv.close()

check("... while an engine that is simply not up has no wait to report",
      dead.status()["starting_ms"] is None, str(dead.status()["starting_ms"]))

check("a URL whose port was never resolved is not an engine, and not a crash",
      api.athena_runtime._port_open("http://127.0.0.1:None") is False)
check("... nor is a URL that cannot be read at all",
      api.athena_runtime._port_open("not a url at all") is False)
check("... because the gate sits outside the try that used to make a bad address harmless",
      "ValueError, TypeError" in (PLUGIN / "dashboard" / "runtime.py").read_text(encoding="utf-8"))

print("\n== finding Node does not repeat the whole search on every look ==")

searches = []
# Patch the module that owns the search, not the API's re-export of it: an alias is a
# name for a function, not a seam. `find_node` calls its own module's `find_node_uncached`,
# so patching `api._find_node_uncached` left the real search in place and the count stayed
# at zero -- which is how this check failed, honestly, on the extraction that moved it.
_node_mod = api.athena_node
real_search = _node_mod.find_node_uncached


def counted_search():
    searches.append(1)
    return real_search()


_node_mod.find_node_uncached = counted_search
try:
    api._NODE_MEMO.update({"key": None, "at": 0.0, "path": None})
    first = api._find_node()
    api._find_node()
    check("the search behind a Node lookup runs once, not once per look",
          len(searches) == 1, f"ran {len(searches)} time(s)")
    check("... and the remembered answer is the one that was handed out",
          first == api._NODE_MEMO["path"], f"{first} vs {api._NODE_MEMO['path']}")

    before = api._NODE_MEMO["key"]
    os.environ["ATHENA_NODE"] = str(PLUGIN / "engine" / "no-such-node.exe")
    api._find_node()
    check("... while an override in the environment is picked up at once, "
          "without waiting for the memory to expire",
          api._NODE_MEMO["key"] != before,
          f"{before} -> {api._NODE_MEMO['key']}")
finally:
    _node_mod.find_node_uncached = real_search
    api._NODE_MEMO.update({"key": None, "at": 0.0, "path": None})
    os.environ.pop("ATHENA_NODE", None)

print("\n== one app's bad day is not the whole report ==")

seen = []
real_health = api._app_health


def fake_health(app_id: str, deep: bool = False):
    seen.append(app_id)
    if app_id == "b":
        raise RuntimeError("this app's check blew up")
    return {"app": app_id, "checks": []}


api._app_health = fake_health
try:
    sweep = api._health_sweep(["a", "b", "c"])
    check("the sweep answers for every app it was asked about",
          sorted(sweep) == ["a", "b", "c"], str(sorted(sweep)))
    check("... in the order it was asked", list(sweep) == ["a", "b", "c"], str(list(sweep)))
    check("... and hands back the reason when one app's check raises instead of raising",
          isinstance(sweep["b"], str) and "blew up" in sweep["b"], str(sweep.get("b")))
    check("... asking each app exactly once", sorted(seen) == ["a", "b", "c"], str(sorted(seen)))
    check("a sweep of nothing is empty rather than an error", api._health_sweep([]) == {})
finally:
    api._app_health = real_health

print(f"\n{PASS} check(s) passed, {FAIL} failed")
if FAILURES:
    print("failed: " + ", ".join(FAILURES))
sys.exit(1 if FAIL else 0)
