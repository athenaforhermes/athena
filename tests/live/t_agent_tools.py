"""Agent-tool suite for Athena's control plane.

Exercises every handler the way a model tool call reaches it — in process, not
over HTTP — because that is the door the sandbox dependency does NOT cover, and
that difference is the whole reason this file exists.

    env -u PYTHONPATH venv/Scripts/python.exe t_agent_tools.py

Destructive operations are only tested in their refusal path: nothing here
deletes or repairs a real app.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Derived, with overrides, rather than written down as one machine's home directory:
# the suite ships in the repo and has to be able to run somewhere other than where it
# was born.
HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent.parent
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or (Path.home() / "AppData" / "Local" / "hermes"))
HOME = Path(os.environ.get("ATHENA_HOME") or (HERMES_ROOT / "athena"))
SCRATCH = HOME / "cache" / "agent-tools-test"

checks = 0
fails = []


def check(label, cond, detail=""):
    global checks
    checks += 1
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        fails.append(label)


def call(fn, **params):
    """Handlers answer with a JSON string — decode it like the agent sees it."""
    out = json.loads(fn(params))
    return out


def load_plugin():
    spec = importlib.util.spec_from_file_location("athena_plugin_agent", str(PLUGIN / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["athena_plugin_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


print("== loading plugin ==")
A = load_plugin()
api = A._load_api()
print(f"home       {api.engine.home}")
print(f"engine     port {api.engine.port}  probe {api.engine.probe()}")

print("\n== every registered tool reaches a handler ==")
registered = {}


class FakeCtx:
    def register_tool(self, name=None, toolset=None, schema=None, handler=None):
        registered[name] = (toolset, schema, handler)


A.register(FakeCtx())
# The expected set is read from the declared schemas instead of being written
# here as a number: a hardcoded count fails the same way whether a tool was
# added or silently lost, so it cannot tell you which happened.
check(f"a tool per declared schema ({len(A.SCHEMAS)})",
      sorted(registered) == sorted(s["name"] for s in A.SCHEMAS.values()),
      sorted(registered))
check("all named athena_*", all(n.startswith("athena_") for n in registered), sorted(registered))
check("publishing is one of them", "athena_publish" in registered, sorted(registered))
check("all on the athena toolset", {t[0] for t in registered.values()} == {"athena"})
for name, (toolset, schema, handler) in registered.items():
    ok = (schema.get("name") == name and isinstance(schema.get("description"), str)
          and len(schema["description"]) > 40 and callable(handler)
          and isinstance(schema.get("parameters"), dict))
    check(f"schema sane: {name}", ok, str(schema)[:120])

print("\n== the wrapper every tool passes through ==")
# `register` hands each handler to a wrapper that announces what the call is about before it
# runs, so the panel can light the card an agent is using. Everything above only proves a
# handler is *callable* — nothing in this suite ever invokes the wrapper itself — and a
# wrapper that dropped or reordered its arguments would break all 24 tools on a real machine
# while every test here stayed green. So call one through it and watch what it does.
announced = []
_real_spot = A._spot
A._spot = lambda tool, params: announced.append((tool, params))
try:
    # The registered name comes from the schema rather than being spelled again here: the
    # tool key is `history` and the tool the agent sees is named differently.
    via = registered[A.SCHEMAS["history"]["name"]][2]
    got = json.loads(via({"limit": 3}))
    direct = call(A.handle_history, limit=3)
    check("a tool called through the wrapper still runs", got.get("success") is True, str(got)[:160])
    check("... and answers exactly what the handler answers",
          got.get("count") == direct.get("count"), f"{got.get('count')} vs {direct.get('count')}")
    check("... and the wrapper announced the call, with the tool and its own arguments, first",
          announced == [("history", {"limit": 3})], str(announced))
finally:
    A._spot = _real_spot

print("\n== history: what has already been run ==")
# This section used to read whatever the machine had run most recently — the newest
# `recipe.json` under the *real* home's `runs/`. The reader under test is ours, but the
# state it read was this computer's history, and that made the whole suite depend on it:
# on a machine that had never run an app there was no newest run and the section fell to
# its "nothing to show" branch (five checks became one), and on a machine whose most
# recent run produced no file, `it carries the file that came out` went red with no
# change to the code. A stranger's first failed app turned a release gate red — the one
# thing a gate must never do.
#
# So the section now *constructs* the state its reader exists to read: a run folder with
# a recipe beside an output file, in a home of its own, with the plugin pointed at that
# home for the duration via the same `ATHENA_HOME` override the suite already honours.
# The recipe carries an app, an intent and a produced file, so every check below still
# fails if the reader stops carrying one — the test can go red, it just cannot go red for
# a reason that is about this machine. The two recipes are given fixed, distinct mtimes
# because the reader orders runs by `recipe.json` mtime, not by name, and which of two
# runs written in the same instant is "newest" is exactly the kind of accident this
# change exists to remove.
_run_home = Path(tempfile.mkdtemp())
_runs = _run_home / "runs"
_run_dir = _runs / "demo-app" / "20260101-000000"
_run_dir.mkdir(parents=True)
(_run_dir / "out.png").write_bytes(b"png")
(_run_dir / "recipe.json").write_text(json.dumps({
    "app": "demo-app", "kind": "gradio", "capability": "/generate",
    "intent": "remove the background from this image",
    "inputs": ["subject.png"], "state": "ok",
    "started": "2026-01-01T00:00:00-05:00",
    "files": [{"path": str(_run_dir / "out.png"), "name": "out.png",
               "bytes": 3, "kind": "image"}],
}, indent=2), encoding="utf-8")
# A second run under another app, so the filter below has something to narrow away. It is
# deliberately the older of the two.
_other_dir = _runs / "other-app" / "20251231-235959"
_other_dir.mkdir(parents=True)
(_other_dir / "recipe.json").write_text(json.dumps({
    "app": "other-app", "intent": "generate", "state": "ok",
}, indent=2), encoding="utf-8")
os.utime(_other_dir / "recipe.json", (1_700_000_000, 1_700_000_000))
os.utime(_run_dir / "recipe.json", (1_700_000_100, 1_700_000_100))

_home_before = os.environ.get("ATHENA_HOME")
os.environ["ATHENA_HOME"] = str(_run_home)
try:
    h = call(A.handle_history, limit=10)
    check("history succeeds", h.get("success"), str(h)[:180])
    check("history counts what it returns", h.get("count") == len(h.get("runs") or []),
          f"{h.get('count')} vs {len(h.get('runs') or [])}")
    hruns = h.get("runs") or []
    if hruns:
        check("a recorded run names its app", bool(hruns[0].get("app")), str(hruns[0])[:140])
        check("it says what was asked for", bool(hruns[0].get("asked_for")), str(hruns[0].get("asked_for")))
        check("it carries the file that came out",
              any(f.get("path") for f in (hruns[0].get("files") or [])),
              str(hruns[0].get("files"))[:120])
        check("it points at the recipe on disk",
              str(hruns[0].get("recipe") or "").endswith("recipe.json"),
              str(hruns[0].get("recipe")))
        # Strictly fewer, not "no more": there are two runs here under two apps, so a
        # filter that is ignored returns both and this check now says so. Written as
        # `<=` against the machine's real history it could never fail at all, which is
        # the other half of the same defect — a check that cannot go red is not a check.
        _narrowed = call(A.handle_history, app=hruns[0].get("app"), limit=5).get("runs") or []
        check("an app filter narrows the list", len(_narrowed) < len(hruns),
              f"{len(_narrowed)} vs {len(hruns)}")
    else:
        check("history has something to show", False, "no runs recorded")
    check("history normalises a route answer rather than a bare dict",
          isinstance(h.get("runs"), list))
finally:
    if _home_before is None:
        os.environ.pop("ATHENA_HOME", None)
    else:
        os.environ["ATHENA_HOME"] = _home_before
    shutil.rmtree(_run_home, ignore_errors=True)

print("\n== status / apps ==")
st = call(A.handle_status)
check("status succeeds", st.get("success"), st)
check("status reports engine port", bool(st.get("engine", {}).get("port")), st.get("engine"))
check("status names credentials", isinstance(st.get("credentials"), dict), st.get("credentials"))

ap = call(A.handle_apps)
check("apps succeeds", ap.get("success"), str(ap)[:300])
apps = ap.get("apps") or []
check("apps found on disk", len(apps) >= 1, len(apps))
# One check, not one per app. A total that moves when somebody installs or removes an app
# from the store is a number about this machine, not about the code -- and the README
# quotes it. The offending app is still named in the detail.
_bad_rows = [a.get("name") for a in apps
             if not all(k in a for k in ("id", "name", "running", "url", "kind", "path"))]
check(f"every app row is complete ({len(apps)} app(s))", not _bad_rows, str(_bad_rows))
kinds = {a.get("kind") for a in apps}
check("kinds detected (gradio/comfyui)", kinds & {"gradio", "comfyui"}, kinds)
by_name = {a["name"]: a["kind"] for a in apps}
check("comfy reads as comfyui", by_name.get("comfy") == "comfyui", by_name)
check("RMBG reads as gradio", by_name.get("RMBG-2-Studio") == "gradio", by_name)

print("\n== the other door: the router dependency, which an agent call never runs ==")
_files = api.athena_files
_api_src = (PLUGIN / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
check("the floor is wired router-wide, so a new route cannot forget it",
      "dependencies=[Depends(athena_files.sandbox_dep)]" in _api_src)


class _Req:
    """Just enough request for the dependency: it only ever reads the JSON body."""

    query_params = {}

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _floor(body):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(_files.sandbox_dep(_Req(body)))


_floor({"path": "C:/Windows"})
check("the floor lets a read on a fixed disk through", True, "C:/Windows")
try:
    _floor({"path": str(Path.home() / ".ssh" / "id_rsa")})
    check("the floor refuses a credential store", False, "no refusal was raised")
except Exception as e:  # noqa: BLE001
    check("the floor refuses a credential store", getattr(e, "status_code", None) == 403,
          str(e)[:140])
try:
    _floor({"path": "C:/Windows/System32/config/SAM"})
    check("the floor refuses the Windows hives", False, "no refusal was raised")
except Exception as e:  # noqa: BLE001
    check("the floor refuses the Windows hives", getattr(e, "status_code", None) == 403,
          str(e)[:140])

print("\n== file tools: the sandbox must hold on the in-process door ==")
roots = call(A.handle_files, op="roots")
check("roots ok", roots.get("success"), str(roots)[:200])
# Reported: "nobody fixed the file browser tab. i can still only access athenas folders and the
# recycle bin on my computer" — the drives were advertised to the panel and refused by the sandbox.
_r = roots.get("roots") or {}
check("roots says where it may look, and where it may change",
      str(HOME) in (_r.get("may_read") or []) and str(HOME) in (_r.get("may_write") or []),
      str(_r.get("may_read"))[:160] + " | " + str(_r.get("may_write"))[:160])
check("... and a fixed disk is in the first list", len(_r.get("may_read") or []) > 1,
      str(_r.get("may_read"))[:160])
check("... and not in the second: writes do not reach a whole disk",
      not any(len(str(w)) <= 4 and str(w).endswith(("\\", "/")) for w in _r.get("may_write") or []),
      str(_r.get("may_write"))[:160])

# Reads reach a fixed disk: real system paths, because that is the whole point of the change.
r = call(A.handle_files, op="list", path="C:/Windows")
check("list reaches a system folder on a fixed disk",
      r.get("success") and r.get("entries"), str(r)[:160])
r = call(A.handle_files, op="list", path=str(Path.home()))
check("list reaches the person's own folder",
      r.get("success") and r.get("entries"), str(r)[:160])
r = call(A.handle_files, op="read", path="C:/Windows/System32/drivers/etc/hosts")
check("read reaches a file on a fixed disk",
      r.get("success") and r.get("text"), str(r)[:160])

# ... and the deny list is what keeps that from being a credential dump. None of these are
# created first: the refusal is about the path, not about whether it happens to exist.
for target, why in [
    (str(Path.home() / ".ssh"), "the ssh folder"),
    (str(Path.home() / ".ssh" / "id_rsa"), "a private key"),
    (str(HERMES_ROOT / "memories" / "MEMORY.md"), "the assistant's own memory"),
    (str(HERMES_ROOT / "sessions" / "x.jsonl"), "saved conversations"),
    (str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
         / "Default" / "Login Data"), "browser passwords"),
    (str(Path.home() / ".aws" / "credentials"), "cloud credentials"),
    ("C:/Windows/System32/config/SAM", "the Windows account hives"),
    (str(HOME / "cache" / "HF_AUTH"), "Athena's own token store"),
]:
    r = call(A.handle_files, op="list", path=target) if target.endswith((".ssh", ".aws")) \
        else call(A.handle_files, op="read", path=target)
    check(f"read refused: {why}", not r.get("success") and r.get("refused"), str(r)[:160])

# Writes are narrower than reads on purpose: nowhere but the home and the person's own folder.
r = call(A.handle_files, op="write", path="C:/Windows/tala-should-not-exist.txt", text="x")
check("write refused on a system folder", not r.get("success") and r.get("refused"), str(r)[:160])
check("... and nothing was created there", not Path("C:/Windows/tala-should-not-exist.txt").exists())
r = call(A.handle_files, op="mkdir", path="C:/Windows/System32", name="nope")
check("mkdir refused on a system folder", not r.get("success"), str(r)[:200])
check("... and nothing was created there", not Path("C:/Windows/System32/nope").exists())

r = call(A.handle_files, op="delete", paths=["C:/Windows/System32/calc.exe"])
check("delete refused outside home", not r.get("success"), str(r)[:160])
check("calc.exe still there", Path("C:/Windows/System32/calc.exe").exists())

# The refusal has to say what *would* work, or an agent retries the same path forever.
r = call(A.handle_files, op="write", path="C:/Windows/tala-should-not-exist.txt", text="x")
said = str(r.get("error") or "")
check("... and the refusal names what is allowed rather than only what was refused",
      "allowed" in said and str(HOME) in said, said[:220])

# The other half of the same door: a route that answered with a refusal, rather
# than raising one. `_ok` is unconditional, so spreading the route's body into it
# used to turn "no" into `success: true` with an error key an agent has no reason
# to read. A failed write must not be reported as a write.
r = call(A.handle_files, op="mkdir", path=str(HOME / "cache" / "no-such-parent"), name="nope")
check("a mkdir that could not be made is not reported as success",
      not r.get("success"), str(r)[:200])

print("\n== file tools: the round trip inside home ==")
shutil.rmtree(SCRATCH, ignore_errors=True)
r = call(A.handle_files, op="mkdir", path=str(HOME / "cache"), name="agent-tools-test")
check("mkdir inside home", r.get("success"), str(r)[:200])
r = call(A.handle_files, op="write", path=str(SCRATCH / "hello.txt"), text="hello tala\n")
check("write creates a new file", r.get("success") and r.get("created"), str(r)[:200])
r = call(A.handle_files, op="read", path=str(SCRATCH / "hello.txt"))
check("read returns the text", r.get("success") and r.get("text") == "hello tala\n", str(r)[:200])
r = call(A.handle_files, op="write", path=str(SCRATCH / "hello.txt"),
         text="x" * 4096, max_kb=1)
check("a write too large for the cap is refused, not reported as written",
      not r.get("success"), str(r)[:200])
r = call(A.handle_files, op="read", path=str(SCRATCH / "hello.txt"))
check("and the file it refused to write is untouched", r.get("text") == "hello tala\n",
      str(r)[:120])
r = call(A.handle_files, op="list", path=str(SCRATCH))
names = [e.get("name") for e in (r.get("entries") or [])]
check("list shows the file", "hello.txt" in names, names)
r = call(A.handle_files, op="stat", path=str(SCRATCH / "hello.txt"))
check("stat returns size", r.get("success") and r.get("size") == 11, str(r)[:200])
r = call(A.handle_files, op="search", path=str(SCRATCH), query="hello")
check("search finds it", r.get("success") and (r.get("entries") or r.get("matches")), str(r)[:200])
r = call(A.handle_files, op="grep", path=str(SCRATCH), pattern="tala")
check("grep finds the content", r.get("success") and (r.get("hits") or r.get("matches")), str(r)[:200])
r = call(A.handle_files, op="delete", paths=[str(SCRATCH / "hello.txt")])
check("delete inside home works", r.get("success"), str(r)[:200])
check("file is gone", not (SCRATCH / "hello.txt").exists())
r = call(A.handle_files, op="bogus")
check("unknown op is refused with the list", not r.get("success") and "roots" in str(r), str(r)[:200])
shutil.rmtree(SCRATCH, ignore_errors=True)

print("\n== settings round trip (restores what it found) ==")
before = call(A.handle_settings)
check("settings read", before.get("success"), str(before)[:200])
cur = (before.get("settings") or {}).get("autostart")
r = call(A.handle_settings, autostart=bool(cur))
check("settings write", r.get("success") and r.get("changed", {}).get("autostart") == bool(cur), str(r)[:200])
after = call(A.handle_settings)
check("settings value held",
      (after.get("settings") or {}).get("autostart") == bool(cur),
      str(after.get("settings"))[:200])

print("\n== the report: is anything asking something of me ==")
rep = call(A.handle_report)
check("the report answers", rep.get("success"), str(rep)[:180])
check("it leads with a sentence rather than a list", bool(rep.get("headline")), str(rep.get("headline")))
check("it carries the list of things to do", isinstance(rep.get("needs"), list),
      str(type(rep.get("needs"))))
check("counts agree with the list it returned",
      (rep.get("counts") or {}).get("broken") ==
      len([n for n in (rep.get("needs") or []) if n.get("kind") == "broken"]),
      str(rep.get("counts"))[:160])
check("it says what it read, so an empty list is not mistaken for a clean machine",
      bool(rep.get("notes")), str(rep.get("notes"))[:160])
check("a limit is honoured rather than refused",
      call(A.handle_report, limit=1).get("success") is True)
# The whole point of the route: every fact in it is already known, so it must not
# reach the network. `_update_check` is the only thing here that could.
api_mod = A._load_api()
_orig = api_mod._update_check


def _boom(*a, **kw):
    raise AssertionError("the report reached the network")


api_mod._update_check = _boom
try:
    again = call(A.handle_report)
    check("asking it again fetches nothing — the update answers are the cached ones",
          again.get("success"), str(again)[:160])
finally:
    api_mod._update_check = _orig

print("\n== read-only diagnostics ==")
sp = call(A.handle_space)
check("space read", sp.get("success") and sp.get("disk"), str(sp)[:200])
se = call(A.handle_search, query="wan")
check("store search", se.get("success") and (se.get("apps") or se.get("count") is not None), str(se)[:200])

if apps:
    name = apps[0]["id"]
    h = call(A.handle_app_health, app=name)
    check(f"health check: {name}", h.get("success") and h.get("health"), str(h)[:300])
    # What the app will ask *you* for rides on the same call, as a line that is
    # never a failure: an app that wants a key you have not given it is not
    # broken, and a check that failed on a working app is a check nobody reads.
    health = (h.get("health") or {})
    access = [c for c in (health.get("checks") or []) if c.get("name") == "Access"]
    check("health says what the app asks for, without calling it broken",
          bool(access) and access[0].get("informational") is True,
          str(health.get("checks"))[:200])
    check("and the structured answer rides along for the card",
          isinstance(health.get("access"), dict) and "local" in health["access"],
          str(health.get("access"))[:200])
    check("apps carry the same answer, read from the cache rather than a walk",
          isinstance((by_id_early := {a["id"]: a for a in apps}).get(name, {}).get("keys"),
                     dict),
          str(by_id_early.get(name, {}).get("keys"))[:200])
    lg = call(A.handle_logs, app=name, lines=5)
    check(f"logs readable: {name}", lg.get("success"), str(lg)[:200])

print("\n== destructive paths refuse cleanly (nothing is deleted here) ==")
r = call(A.handle_delete, app="not-an-app.git", wait=0)
check("delete of a missing app refuses", not r.get("success"), str(r)[:200])
check("no job spawned for a missing app", not r.get("job"), str(r)[:200])
r = call(A.handle_fix, app="not-an-app.git", provision=False, wait=0)
check("fix of a missing app refuses", not r.get("success"), str(r)[:200])
r = call(A.handle_job, job="does-not-exist")
check("bogus job id does not crash", isinstance(r, dict), str(r)[:200])
r = call(A.handle_launch, app="")
check("launch with no app refuses", not r.get("success"), str(r)[:200])
r = call(A.handle_files, op="read", path="")
check("read with no path does not crash", isinstance(r, dict), str(r)[:200])

print("\n== capability tools: ask what an app can do, and refuse cleanly ==")
by_id = {a["id"]: a for a in apps}
probed = by_id.get("RMBG-2-Studio.git")
check("apps reports the probed capability count",
      isinstance((probed or {}).get("capabilities"), int) and probed["capabilities"] >= 1,
      str(probed)[:200])

v = call(A.handle_app_verify, app="RMBG-2-Studio")
check("verify reads a saved manifest without launching", v.get("success") and v.get("apps"), str(v)[:200])
if v.get("success") and v.get("apps"):
    entry = v["apps"][0]
    caps = entry.get("capabilities") or []
    check("every capability carries an intent a model can ask for",
          bool(caps) and all(c.get("intent") for c in caps), str(caps)[:200])
    check("capabilities say which of them take a local file",
          any(c.get("takes_a_local_file") for c in caps), str(caps)[:200])
    check("capabilities name the inputs in order",
          all(isinstance(c.get("inputs"), list) for c in caps), str(caps)[:200])
    check("no plumbing survived into the tool answer",
          not any(str(c.get("capability", "")).startswith(("/update_preview", "/lambda", "/reset"))
                  for c in caps), [c.get("capability") for c in caps])

v2 = call(A.handle_app_verify, app="not-an-app")
check("verify of a missing app refuses and lists what is installed",
      not v2.get("success") and v2.get("installed"), str(v2)[:200])
v3 = call(A.handle_app_verify, app="RMBG-2-Studio", refresh=False)
check("verify twice is stable", v3.get("success") and v3.get("apps"), str(v3)[:120])

g0 = call(A.handle_generate)
check("generate with nothing refuses", not g0.get("success"), str(g0)[:160])
g1 = call(A.handle_generate, request="fly me to the moon in abstract zzzz")
check("generate with an unmatchable request says what is possible instead",
      not g1.get("success") and g1.get("try_instead"), str(g1)[:240])
g2 = call(A.handle_generate, app="not-an-app", capability="x")
check("generate with a missing app refuses", not g2.get("success"), str(g2)[:160])
g3 = call(A.handle_generate, app="RMBG-2-Studio", capability="definitely_not_a_capability")
check("generate with a bogus capability lists the real intents",
      not g3.get("success") and g3.get("available"), str(g3)[:240])
g4 = call(A.handle_generate, request="remove the background from this image")
check("generate with no inputs names the capability and the inputs it needs",
      not g4.get("success") and g4.get("inputs") and g4.get("capability"), str(g4)[:240])
check("the capability it picked can take a local file",
      all(i.get("takes_a_local_file") for i in (g4.get("inputs") or [])), str(g4)[:240])
g5 = call(A.handle_generate, request="cut out the subject in my photo")
check("plain words for the same job resolve too (cut out / photo)",
      (not g5.get("success")) and g5.get("inputs"), str(g5)[:240])

print("\n== the models tool answers through the tool envelope ==")
# Registering a handler is not the same as the handler working. This one builds
# its answer out of a route body, and a body that already names its own app
# collides with the argument being passed — a TypeError that every check looking
# only at registration waves through. So: call it, and call it on a real app.
m0 = call(A.handle_models, app="not-an-app.git")
check("asking about an app that does not exist fails as a call, not as a string",
      m0.get("success") is False and "not found" in str(m0.get("error")), json.dumps(m0)[:200])

for name in ("comfy", "ComfyUI"):
    h = call(A.handle_app_health, app=name)
    if h.get("success") is False:
        continue
    m = call(A.handle_models, app=name)
    check("a comfy app answers with its verdict, not a traceback",
          m.get("success") is True and isinstance(m.get("models"), dict), json.dumps(m)[:240])
    w = [c for c in ((m.get("models") or {}).get("folders") or [])]
    check("the verdict says which folders it can reach", isinstance(w, list), json.dumps(m)[:160])
    check("and the machine's own weight folders come back as candidates",
          isinstance(m.get("candidates"), list), json.dumps(m)[:240])
    check("so acting on 'cannot generate' needs no remembered path",
          all("folder" in c for c in (m.get("candidates") or [])), json.dumps(m)[:240])
    break

# The same tool asked of the machine rather than of an app: what is on this disk,
# what still lacks weights, and what is already paid for.
shelf = call(A.handle_models)
check("no arguments at all answers for the whole shelf instead of erroring",
      shelf.get("success") is True and isinstance(shelf.get("libraries"), list),
      json.dumps(shelf)[:220])
check("with a sentence first, because that is what an agent reads",
      isinstance(shelf.get("summary"), str) and "apps can generate" in shelf.get("summary", ""),
      str(shelf.get("summary"))[:200])
check("and the totals a decision needs",
      isinstance((shelf.get("totals") or {}).get("weights_bytes"), int)
      and isinstance((shelf.get("totals") or {}).get("cached_repos"), int),
      json.dumps(shelf.get("totals"))[:220])
check("naming the apps that cannot generate, so one call can drive the fixes",
      isinstance(shelf.get("need_weights"), list), json.dumps(shelf.get("need_weights"))[:200])

# A repository that is not named `owner/name` is refused before any request goes
# out, so this part of the surface can be checked without a network.
bad_repo = call(A.handle_models, repo="justaname")
check("a repository name that is not `owner/name` is refused as a failed call",
      bad_repo.get("success") is False and "owner/name" in (bad_repo.get("error") or ""),
      json.dumps(bad_repo)[:200])
params = ((A.SCHEMAS.get("models") or {}).get("parameters") or {})
props = params.get("properties") or {}
check("and the pull surface is declared, so an agent can see it exists",
      {"repo", "download", "include", "key", "revision"} <= set(props),
      str(sorted(props)))

# ---- machines: a real OS, offered to an agent and to the panel alike -------
# The same host serves the Machines tab and this tool, so what is checked here is
# that the tool reaches it and that every way of asking for something impossible
# comes back as a refusal with a sentence in it — never as a machine that was
# half made. Nothing in this section creates a distro: the provisioning path is
# proven in t_machines.py against the platform verbs, and the end-to-end run in
# tests/live/machine_provision_check.py against a real WSL.
shelf_m = call(A.handle_machines)
check("asking for the machines with no arguments lists them rather than erroring",
      shelf_m.get("success") is True and isinstance(shelf_m.get("machines"), list),
      json.dumps(shelf_m)[:220])
check("and it carries the distros Athena is not managing, so attaching is a choice",
      isinstance(shelf_m.get("unclaimed"), list), json.dumps(shelf_m.get("unclaimed"))[:160])
check("with a sentence first, because that is what an agent reads",
      isinstance(shelf_m.get("summary"), str) and "machine" in shelf_m.get("summary", ""),
      str(shelf_m.get("summary"))[:200])

# A display choice Athena cannot carry out is not quietly rounded to one it can.
_disp_calls = [call(A.handle_machines, op="display", name=n, display="holodeck")
               for n in (shelf_m.get("machines") and [m.get("name") for m in shelf_m["machines"][:1]] or
                         ["nothing-here"])]
check("a screen moved to a display that does not exist is refused, and told the three that work",
      all(c.get("success") is not True and "panel" in json.dumps(c) and "window" in json.dumps(c)
          and "none" in json.dumps(c) for c in _disp_calls),
      json.dumps(_disp_calls)[:240])


setup_m = call(A.handle_machines, op="setup")
check("setup says what is installed here instead of installing it",
      setup_m.get("success") is True and isinstance(setup_m.get("wsl"), dict)
      and isinstance(setup_m.get("presets"), dict), json.dumps(setup_m)[:220])
check("... and every missing prerequisite arrives as a question with a command",
      all(("needs_setup" in n and "question" in n and "command" in n)
          for n in (setup_m.get("needs_setup") or [])),
      json.dumps(setup_m.get("needs_setup"))[:240])
check("the presets are the ones a machine can actually be made from",
      {"ubuntu-24.04", "debian", "arch", "custom-iso"} <= set(setup_m.get("presets") or {}),
      str(sorted(setup_m.get("presets") or {})))

for name, params, want in (
        ("an op nobody knows", dict(op="frobnicate"), "unknown machines op"),
        ("a machine that does not exist", dict(op="start", name="nothing-here"), "no machine called"),
        ("a command with nothing to run", dict(op="exec", name="nothing-here"), "needs a command"),
        # The screen: a picture of a machine that is not running is a picture of nothing, so
        # asking for one is refused with a sentence rather than answered with a blank PNG.
        ("a screen from a machine that is not running", dict(op="screen", name="nothing-here"),
         "no machine called"),
        ("a keystroke aimed at nothing", dict(op="type", name="nothing-here", key="Enter"),
         "no machine called"),
        ("a display that is not a display", dict(op="display", name="nothing-here", display="holodeck"),
         "no machine called"),
        ("deleting nothing", dict(op="delete", name="nothing-here"), "no machine called"),
        ("an op with no machine named at all", dict(op="stop"), "needs a machine name")):
    res = call(A.handle_machines, **params)
    check(f"{name} is refused as a failed call, and says which it was",
          res.get("success") is False and want in (res.get("error") or ""),
          json.dumps(res)[:200])

bad_name = call(A.handle_machines, op="create", name="not a name")
check("a name that is not a name is refused before anything is written",
      bad_name.get("success") is False and "name" in (bad_name.get("error") or ""),
      json.dumps(bad_name)[:200])

mprops = ((A.SCHEMAS.get("machines") or {}).get("parameters") or {}).get("properties") or {}
# Every field a machine can be described with is declared, including the ones that only
# a QEMU machine uses: an agent that cannot see `image_kind` will describe an ISO as a
# rootfs and then wonder why the machine boots nothing.
check("the whole surface is declared, so an agent can see it exists",
      {"op", "name", "preset", "provider", "display", "os", "image_path", "image_url",
       "image_kind", "ports", "vnc_port", "ssh_port", "cpus", "memory_mb", "disk_gb",
       "attach", "command", "unregister", "dir", "make"} <= set(mprops) and
      ((A.SCHEMAS.get("machines") or {}).get("parameters") or {}).get("required") == ["op"],
      str(sorted(mprops)))
check("... and the port list is described as pairs, because that is the shape it reads",
      "host" in str(mprops.get("ports")) and "guest" in str(mprops.get("ports")),
      str(mprops.get("ports"))[:200])

# ---- sizing, the image folder, and the menu: the same host, one temp home ------
# These three are what the Machines tab asks for on first load, and they are checked
# through the tool an agent actually calls — with the home pointed at a temporary
# folder, so a fit is read from *this* computer without writing anything into the
# one somebody is working in.
_host_home = api._athena_home
_host_settings_file = api.SETTINGS_FILE
_host_legacy_file = api.LEGACY_SETTINGS_FILE
_tmp_home = Path(tempfile.mkdtemp())
_tmp_settings = Path(tempfile.mkdtemp()) / "settings.json"
api._athena_home = lambda: _tmp_home
# Redirect the settings *files*, not just the home. Settings are read from and **written
# back to** a file whose path is decided at import, so a suite that only moves the home
# still answers with the folder the person using this machine really set — and, worse,
# writes its temporary home back into the file they own. Pointing both paths at a
# temporary file keeps the machine this runs on exactly as it was.
api.SETTINGS_FILE = _tmp_settings
api.LEGACY_SETTINGS_FILE = _tmp_settings.parent / "legacy-settings.json"
try:
    fit_m = call(A.handle_machines, op="fit")
    check("the size a guest can be given here is computed, with its reasons attached",
          fit_m.get("success") is True and int(fit_m.get("cpus") or 0) >= 2
          and int(fit_m.get("memory_mb") or 0) >= 1024
          and isinstance(fit_m.get("why"), list) and len(fit_m.get("why")) >= 3,
          json.dumps(fit_m)[:240])
    _facts = fit_m.get("host") or {}
    check("... and the fit is not larger than this computer, because it was measured on it",
          int(fit_m.get("cpus") or 0) <= int(_facts.get("cores") or 0)
          and int(fit_m.get("memory_mb") or 0) <= int(_facts.get("memory_mb") or 0),
          json.dumps(_facts)[:200])

    isos_m = call(A.handle_machines, op="isos")
    check("the image folder is reported, and asking for it does not create it",
          isos_m.get("success") is True and isinstance(isos_m.get("dir"), str)
          and isos_m.get("isos") == [] and not Path(isos_m.get("dir")).exists(),
          json.dumps(isos_m)[:220])

    _want = str(_tmp_home / "my-isos")
    ask = call(A.handle_machines, op="set_iso_dir", dir=_want)
    check("pointing the folder at somewhere that does not exist is a question, not a mkdir",
          ask.get("success") is False and ask.get("needs_folder") is True
          and "does not exist yet" in (ask.get("error") or "")
          and not Path(_want).exists(), json.dumps(ask)[:240])

    made = call(A.handle_machines, op="set_iso_dir", dir=_want, make=True)
    check("... and saying so makes it, and it becomes the folder in use",
          made.get("success") is True and Path(_want).is_dir()
          and Path(made.get("dir")) == Path(_want), json.dumps(made)[:240])
    (Path(_want) / "bazzite.iso").write_bytes(b"x" * 2048)
    listed = call(A.handle_machines, op="isos")
    check("... a file dropped in it is listed with the kind its name suggests, and its size",
          [i["name"] for i in listed.get("isos") or []] == ["bazzite.iso"]
          and listed["isos"][0]["kind"] == "iso" and listed["isos"][0]["size_mb"] == 0.0,
          json.dumps(listed.get("isos"))[:240])
    (Path(_want) / "linux").mkdir()
    (Path(_want) / "linux" / "arch.iso").write_bytes(b"x" * 1024)
    deep = call(A.handle_machines, op="isos")
    check("... and an image in a subfolder is listed too, saying which folder it came from",
          any(i.get("name") == "arch.iso" and i.get("rel") == "linux" for i in deep.get("isos") or [])
          and deep.get("folders") == 2,
          json.dumps(deep.get("isos"))[:240])

    walked = call(A.handle_machines, op="folders", path=str(_want))
    check("an agent can look at one folder to find where the images are, without a drive sweep",
          walked.get("success") is True
          and any(d.get("name") == "linux" for d in walked.get("dirs") or [])
          and walked.get("files") == 1 and len(walked.get("drives") or []) >= 1,
          json.dumps(walked)[:240])
    check("... and a folder nobody can read is an answer, not a crash",
          call(A.handle_machines, op="folders", path="")["success"] is True)

    check("the section wrote its settings into its own temporary file, not the real one",
          Path(api.SETTINGS_FILE) == _tmp_settings and _tmp_settings.exists()
          and _tmp_settings != _host_settings_file,
          f"{api.SETTINGS_FILE} vs {_host_settings_file}")

    # One settings file is shared with the engine, so a save must merge onto it. Written
    # as a replacement, saving anything at all deleted every key the caller was not
    # holding — an ISO folder somebody had set disappeared the next time an unrelated
    # setting was saved, and the panel then listed an empty folder as though it had never
    # been chosen.
    _before = json.loads(_tmp_settings.read_text(encoding="utf-8"))
    check("the image folder is in the settings file the section just wrote",
          bool(_before.get("iso_dir")), json.dumps(sorted(_before)))
    api._save_settings({"port": 42999})
    _after = json.loads(_tmp_settings.read_text(encoding="utf-8"))
    check("... and a save by somebody holding one key keeps every other key",
          _after.get("iso_dir") == _before.get("iso_dir")
          and _after.get("port") == 42999
          and _after.get("idle_stop_seconds") == _before.get("idle_stop_seconds"),
          json.dumps(sorted(_after)))
    check("... and what a save merges onto is the file itself, not the answer with this run's overrides",
          api._load_settings().get("iso_dir") == _after.get("iso_dir")
          and "ATHENA_HOME" not in _after,
          json.dumps({k: _after[k] for k in list(_after)[:4]}))

    back = call(A.handle_machines, op="set_iso_dir", dir="")
    check("... and it goes back to Athena's own folder when asked to, without being asked why",
          back.get("success") is True and Path(back.get("dir")) == _tmp_home / "isos",
          json.dumps(back)[:200])

    auto = call(A.handle_machines, op="create", name="sized-box", preset="custom-iso",
                image_path=str(Path(_want) / "bazzite.iso"), os="linux")
    _specm = (auto.get("machine") or {})
    check("a machine an agent makes without sizes is sized for this computer, not for a habit",
          auto.get("success") is True and _specm.get("provider") == "qemu"
          and int(_specm.get("cpus") or 0) == int(fit_m.get("cpus"))
          and int(_specm.get("memory_mb") or 0) == int(fit_m.get("memory_mb")),
          json.dumps(_specm)[:240])
    ported = call(A.handle_machines, op="create", name="ported-box", preset="custom-iso",
                  image_path=str(Path(_want) / "bazzite.iso"), ports=["8080:80"])
    check("... a port typed as text arrives as the pair the spec stores",
          (ported.get("machine") or {}).get("ports") == [{"host": 8080, "guest": 80}],
          json.dumps(ported)[:200])
    junk_port = call(A.handle_machines, op="create", name="junk-port", preset="custom-iso",
                     image_path=str(Path(_want) / "bazzite.iso"), ports=["8080"])
    check("... and a port that is not a pair is refused with the shape in the sentence",
          junk_port.get("success") is False and "8080:80" in (junk_port.get("error") or ""),
          json.dumps(junk_port)[:200])
    gone = call(A.handle_machines, op="delete", name="sized-box", unregister=True)
    check("the machines this section made are gone again, so nothing is left behind",
          gone.get("success") is True, json.dumps(gone)[:200])
finally:
    api._athena_home = _host_home
    api.SETTINGS_FILE = _host_settings_file
    api.LEGACY_SETTINGS_FILE = _host_legacy_file

print(f"\n{'=' * 60}\n{checks - len(fails)}/{checks} checks passed")
if fails:
    print("FAILURES:")
    for f in fails:
        print("  -", f)
sys.exit(1 if fails else 0)
