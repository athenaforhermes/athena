"""Build-an-app suite: repository -> Athena app.

Covers the mechanical half (fetch, classify, scaffold, validate) and both doors
into it — the HTTP route and the agent tool — because they must do the same
thing. Uses real public repositories, so it needs the network; it writes only
under `zz-buildtest-*` names and removes everything it created.

    env -u PYTHONPATH venv/Scripts/python.exe t_build.py
"""
from __future__ import annotations

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
HOME = Path(str(HERMES_ROOT) + r"/athena")
NODE = None
for c in sorted((Path(str(HERMES_ROOT) + r"/node")).glob("node-*/node.exe"), reverse=True):
    NODE = str(c)
    break

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


def load_plugin():
    spec = importlib.util.spec_from_file_location("athena_plugin_build", str(PLUGIN / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["athena_plugin_build"] = mod
    spec.loader.exec_module(mod)
    return mod


print("== loading plugin ==")
A = load_plugin()
api = A._load_api()
sys.path.insert(0, str(PLUGIN / "dashboard"))
import buildapp  # noqa: E402
import os


created = []

print("\n== the guide and the examples the agent reads ==")
guide = PLUGIN / "docs" / "build-an-app.md"
check("guide exists", guide.is_file())
text = guide.read_text(encoding="utf-8") if guide.is_file() else ""
check("guide is substantial", len(text) > 6000, f"{len(text)} bytes")
check("guide covers the file set", "athena_meta.json" in text and "start.js" in text and "install.js" in text)
check("guide says the agent is the generator", "You are the generator" in text)
check("guide has no vendor branding", "Pinokio" not in text)
ex = PLUGIN / "docs" / "examples"
check("examples exist", ex.is_dir() and len(list(ex.iterdir())) >= 5, sorted(p.name for p in ex.iterdir()) if ex.is_dir() else "")
for name in ("python-gradio-server", "ai-python-server", "node-server", "cli-app", "static-site"):
    check(f"example {name}", (ex / name).is_dir() and (ex / name / "athena_meta.json").is_file())
branded = [str(f.relative_to(ex)) for f in ex.rglob("*")
           if f.is_file() and f.suffix in (".js", ".json", ".md", ".html")
           and "Pinokio" in f.read_text(encoding="utf-8", errors="replace")]
check("examples carry no vendor branding", not branded, branded)
for js in ex.rglob("*.js"):
    if NODE:
        import subprocess
        rc = subprocess.run([NODE, "--check", str(js)], capture_output=True).returncode
        check(f"example parses: {js.relative_to(ex)}", rc == 0)

print("\n== parsing repository references ==")
for ref, want in [("https://github.com/SillyTavern/SillyTavern", ("SillyTavern", "SillyTavern")),
                  ("SillyTavern/SillyTavern", ("SillyTavern", "SillyTavern")),
                  ("https://github.com/octocat/Spoon-Knife.git", ("octocat", "Spoon-Knife")),
                  ("github.com/octocat/Spoon-Knife/tree/main", ("octocat", "Spoon-Knife"))]:
    got = buildapp.repo_parts(ref)
    check(f"parses {ref}", got.get("owner") == want[0] and got.get("repo") == want[1], got)
check("rejects nonsense", not buildapp.repo_parts("not a repo"))
check("slug is filesystem-safe", buildapp.slug_for("a b/c:d*e") == "a-b-c-d-e", buildapp.slug_for("a b/c:d*e"))

print("\n== classification against real repositories ==")
cases = [
    ("https://github.com/octocat/Spoon-Knife", {"stack": ["static"], "kind": "static"}),
    ("https://github.com/psf/requests", {"stack": ["python"]}),
    ("https://github.com/SillyTavern/SillyTavern", {"stack": ["node"], "kind": "server"}),
]
for url, want in cases:
    r = buildapp.recon(url, HOME / "cache", timeout=600)
    if not r.get("ok"):
        check(f"recon {url}", False, r.get("error"))
        continue
    ok = all((not isinstance(v, list) or v == r.get(k)) if isinstance(v, list) else r.get(k) == v
             for k, v in want.items())
    check(f"recon {url} -> {want}", ok, {k: r.get(k) for k in want})
    if "SillyTavern" in url:
        check("node app does not claim the AI bundle", r.get("needs_ai_bundle") is False, r.get("ai_markers"))
        check("node server finds a launch command", r.get("launch") == "npm start", r.get("launch"))
    if "requests" in url:
        check("a library is not dressed up as a server", r.get("kind") in ("cli", "unknown"), r.get("kind"))

print("\n== scaffolding ==")
res = buildapp.scaffold(HOME, "https://github.com/octocat/Spoon-Knife", name="zz-buildtest-static")
created.append(HOME / "api" / "zz-buildtest-static")
check("scaffold ok", res.get("ok"), res.get("error"))
if res.get("ok"):
    d = Path(res["path"])
    for f in ("athena.js", "pinokio.js", "athena_meta.json", "install.js", "start.js", "update.js", "reset.js",
              "README.md", ".gitignore", "SPEC.md"):
        check(f"wrote {f}", (d / f).is_file())
    check("analysis copy is separate from the app", Path(res["analysis_dir"]).is_dir()
          and "scaffold" in res["analysis_dir"] and not str(res["analysis_dir"]).startswith(str(d)))
    check("no app/ folder invented", not (d / "app").exists())
    spec = (d / "SPEC.md").read_text(encoding="utf-8")
    check("SPEC names the repo", "Spoon-Knife" in spec)
    check("SPEC tells the agent what to decide", "must decide" in spec)
    meta = json.loads((d / "athena_meta.json").read_text(encoding="utf-8"))
    check("metadata has the four fields", all(k in meta for k in ("title", "description", "icon", "version")))
    check("metadata keeps the schema version", meta["version"] == "2.0")
    # The guard has to name .git, not the folder: the venv lives inside app/, so
    # "app exists" is true after install and would skip the clone forever.
    check("clone step is guarded (idempotent)",
          "{{!exists('app/.git')}}" in (d / "install.js").read_text(encoding="utf-8"))
    check("start is a daemon", "daemon: true" in (d / "start.js").read_text(encoding="utf-8"))
    check("start publishes the url", "local.set" in (d / "start.js").read_text(encoding="utf-8"))
    v = buildapp.validate(HOME, "zz-buildtest-static")
    check("scaffold validates clean", v.get("ok"), v.get("errors"))

print("\n== refusals ==")
again = buildapp.scaffold(HOME, "https://github.com/octocat/Spoon-Knife", name="zz-buildtest-static")
check("will not clobber an existing launcher", not again.get("ok") and again.get("already_an_app"), again)
forced = buildapp.scaffold(HOME, "https://github.com/octocat/Spoon-Knife", name="zz-buildtest-static", force=True)
check("force rebuilds", forced.get("ok"), forced.get("error"))
bad = buildapp.scaffold(HOME, "this is not a repository")
check("refuses an unparseable repo", not bad.get("ok"), bad)
missing = buildapp.recon("https://github.com/imzacksong/definitely-not-a-real-repo-xyz", HOME / "cache", timeout=120)
check("reports a clone failure instead of pretending", not missing.get("ok"), missing)

print("\n== validation catches real breakage ==")
d = HOME / "api" / "zz-buildtest-static"
good = (d / "start.js").read_text(encoding="utf-8")
(d / "start.js").write_text("module.exports = { run: [ }\n", encoding="utf-8")
v = buildapp.validate(HOME, "zz-buildtest-static")
check("syntax error is caught", not v.get("ok") and any("syntax" in e for e in v["errors"]), v.get("errors"))
(d / "start.js").write_text(good, encoding="utf-8")
(d / "athena_meta.json").write_text("{ not json", encoding="utf-8")
v = buildapp.validate(HOME, "zz-buildtest-static")
check("broken metadata is caught", not v.get("ok"), v.get("errors"))
(d / "athena_meta.json").write_text(json.dumps({"title": "x", "path": "somewhere-else"}), encoding="utf-8")
v = buildapp.validate(HOME, "zz-buildtest-static")
check("wrong path declaration is caught", not v.get("ok"), v.get("errors"))
(d / "athena_meta.json").write_text(json.dumps({"title": "x", "description": "y", "icon": "icon.png", "version": "2.0"}), encoding="utf-8")
v = buildapp.validate(HOME, "zz-buildtest-static")
check("repaired launcher validates again", v.get("ok"), v.get("errors"))

print("\n== the agent tool ==")
out = json.loads(A.handle_build_app({"url": "https://github.com/octocat/Spoon-Knife",
                                     "name": "zz-buildtest-tool", "force": True}))
created.append(HOME / "api" / "zz-buildtest-tool")
check("tool reports ok", out.get("ok"), out)
check("tool names the app", out.get("app") == "zz-buildtest-tool", out.get("app"))
check("tool points at the guide", str(guide) == out.get("guide"), out.get("guide"))
check("tool returns the recon", isinstance(out.get("recon"), dict) and out["recon"].get("stack"), out.get("recon"))
check("tool hands over next steps", len(out.get("next_steps") or []) >= 4, out.get("next_steps"))
check("tool refuses an empty url", not json.loads(A.handle_build_app({})).get("ok"))
check("tool refuses an unparseable repo", not json.loads(A.handle_build_app({"url": "??"})).get("ok"))

print("\n== the route does the same thing ==")
r = json.loads(api.catalog_scaffold(api.BuildIn(url="https://github.com/octocat/Spoon-Knife",
                                                name="zz-buildtest-route", force=True)).body.decode())
created.append(HOME / "api" / "zz-buildtest-route")
check("route reports ok", r.get("ok"), r)
check("route agrees with the module", r.get("app") == "zz-buildtest-route" and Path(r["path"]).is_dir(), r.get("path"))
check("route refuses an empty url", api.catalog_scaffold(api.BuildIn(url="")).status_code == 400)
p = json.loads(api.catalog_build_prompt(api.BuildIn(url="https://github.com/octocat/Spoon-Knife",
                                                    name="zz-buildtest-prompt", force=True)).body.decode())
created.append(HOME / "api" / "zz-buildtest-prompt")
check("prompt route returns a prompt", p.get("ok") and len(p.get("prompt") or "") > 200, p.get("error"))
if p.get("ok"):
    prompt = p["prompt"]
    check("prompt names the repo", "Spoon-Knife" in prompt)
    check("prompt points at the guide", str(guide) in prompt)
    check("prompt points at the SPEC", "SPEC.md" in prompt)
    check("prompt points at the analysis copy", str(p["analysis_dir"]) in prompt)
    check("prompt tells the agent to prove it runs", "fetch" in prompt.lower())
g = json.loads(api.catalog_guide().body.decode())
check("guide route serves the guide", g.get("text", "").startswith("# Building an Athena app"))

print("\n== registration ==")
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
check("build tool is registered", "athena_build_app" in registered)
check("and so is the publisher", "athena_publish" in registered, sorted(registered))
sch = registered["athena_build_app"][1]
# A build needs a repository, a smoke needs an app — neither input can be
# required unconditionally, so the tool enforces it itself and says what is
# missing. Assert the description offers both, not a schema the tool cannot have.
check("build tool offers both actions",
      "scaffold" in sch["parameters"]["properties"]["action"]["description"]
      and "smoke" in sch["parameters"]["properties"]["action"]["description"])
check("build tool does not pretend url is always required",
      "url" not in (sch["parameters"].get("required") or []), sch["parameters"].get("required"))
check("build tool is on the athena toolset", registered["athena_build_app"][0] == "athena")

print("\n== a manifest bigger than a reading window is still a manifest ==")
# 9 KB of real package.json: past the 8 KB window manifests used to be read through, where the
# truncated text was a JSONDecodeError and the whole build died with a traceback.
_filler = {"pad": "x" * 200}
_big_pkg = json.dumps({"name": "big-app", "version": "1.0.0",
                       "scripts": {"start": "node server.js"},
                       "dependencies": {"express": "^4.18.0"},
                       "notes": [_filler] * 45})
check("the fixture really is bigger than the old window", len(_big_pkg) > 8000, str(len(_big_pkg)))
check("the manifest cap is larger than the window that used to truncate it",
      buildapp.JSON_MANIFEST_LIMIT > 8000, str(buildapp.JSON_MANIFEST_LIMIT))
_cls = buildapp.classify(["package.json", "server.js"], "", {"package.json": _big_pkg})
check("a package.json that ran past the old window is read with its scripts intact",
      _cls["package_scripts"].get("start") == "node server.js", str(_cls["package_scripts"]))
check("... and the app is recognised as something to run, from that manifest",
      "node" in _cls["stack"] and _cls["launch"] == "npm start",
      f"{_cls['stack']} {_cls['launch']}")
check("... with nothing to report about it", _cls["manifest_notes"] == [], str(_cls["manifest_notes"]))

_bad = buildapp.classify(["package.json", "x.js"], "", {"package.json": '{"name": "half, tru'})
check("a package.json that cannot be parsed does not end the build",
      isinstance(_bad, dict) and "manifest_notes" in _bad)
check("... it is reported instead of being silently ignored",
      _bad["manifest_notes"] and "package.json" in _bad["manifest_notes"][0], str(_bad["manifest_notes"]))
check("... and the rest of the scan still happened", _bad["total_files"] == 2, str(_bad["total_files"]))
_notdict = buildapp.classify(["package.json"], "", {"package.json": "[1, 2, 3]"})
check("a package.json that is not an object is reported too",
      _notdict["manifest_notes"] and "not a JSON object" in _notdict["manifest_notes"][0],
      str(_notdict["manifest_notes"]))
check("... where a missing manifest is simply no manifest, with nothing to report",
      buildapp.classify(["app.py"], "", {})["manifest_notes"] == [])

print("\n== cleanup ==")
for d in created:
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    print(f"  removed {d.name}: {not d.exists()}")
check("nothing left behind", all(not d.exists() for d in created))

print(f"\n{'='*60}\n{checks - len(fails)}/{checks} checks passed")
if fails:
    print("FAILURES:")
    for f in fails:
        print("  -", f)
sys.exit(1 if fails else 0)
