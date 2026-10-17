#!/usr/bin/env python3
"""Nothing a user or the agent can see says the vendor name.

The point of the whole Option A job, expressed as a test. It is deliberately not
a source grep: it scans *outputs* — the files a scaffold writes, the guide Athena
generates, the words the store publishes, the strings the UI and the agent tools
carry — because those are the surfaces a person actually reads. A rename that
misses one of them is invisible to a grep of the source tree.

Allowed, and only in the listed place:
  * the import label, which names the source system the way "Import from
    ComfyUI" would (a feature name, not Athena's brand)
  * docs/NAMING.md and docs/SPIKE-VERDICT.md (the mapping table and a historical
    record — both are *about* the rename)
  * upstream repo URLs inside store records (provenance, install source)

Run with `env -u PYTHONPATH` from this directory.
"""
from __future__ import annotations

import json
import re
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
sys.path.insert(0, str(PLUGIN / "dashboard"))
sys.path.insert(0, str(PLUGIN / "tools"))

WORD = re.compile(r"pinokio", re.I)
URL = re.compile(r"https?://\S+")
IMPORT_LABEL = "Import from a Pinokio install"

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


def mentions(text: str, allow: tuple[str, ...] = ()) -> int:
    """Count vendor mentions, ignoring URLs and the allowed substrings.

    A URL is a fact, not a brand: it points at something real, and the store's
    records and the engine's upstream link both have to keep theirs. The same
    rule the boundary scrub follows.
    """
    text = URL.sub(" ", text)
    for a in allow:
        text = text.replace(a, " ")
    return len(WORD.findall(text))


def is_shim(p: Path) -> bool:
    """The legacy-named launcher shim is the one filename that keeps the name —
    the engine resolves that literal string, and the shim exists for it alone."""
    if p.name != "pinokio.js":
        return False
    try:
        return 'require("./athena.js")' in p.read_text(encoding="utf-8")
    except OSError:
        return False


def scannable(p: Path) -> bool:
    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".bmp"):
        return False
    try:
        p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return True


# ---------------------------------------------------------------- surfaces
SURFACES = [
    ("the desktop UI", PLUGIN / "desktop" / "plugin.js", (IMPORT_LABEL, "Pinokio install")),
    ("the agent's tool descriptions", PLUGIN / "__init__.py", (IMPORT_LABEL,)),
    ("the plugin README", PLUGIN / "README.md", ()),
    ("the build guide", PLUGIN / "docs" / "build-an-app.md", ()),
    ("the store's README", PLUGIN / "tools" / "store-README.md", ()),
]


def test_surfaces() -> None:
    print("\n-- strings the user and the agent read --")
    for label, path, allow in SURFACES:
        if not path.is_file():
            check(f"{label} exists", False, str(path))
            continue
        n = mentions(path.read_text(encoding="utf-8", errors="replace"), allow)
        check(f"{label} is clean", n == 0, f"{n} mention(s) in {path.name}")
    # The examples ship inside docs/ and get copied into real apps, filenames
    # included — so names count here too.
    print("\n-- the examples an author copies --")
    bad_name: list[str] = []
    bad_text: list[str] = []
    for p in sorted((PLUGIN / "docs" / "examples").rglob("*")):
        if not p.is_file():
            continue
        if mentions(p.name) and not is_shim(p):
            bad_name.append(p.name)
        if scannable(p) and mentions(p.read_text(encoding="utf-8", errors="replace")):
            bad_text.append(p.relative_to(PLUGIN).as_posix())
    check("no example file name mentions the vendor", not bad_name, str(bad_name))
    check("no example file mentions the vendor", not bad_text, str(bad_text))


def test_scaffold_output() -> None:
    """A scaffolded app is Athena's, on disk as well as in the UI."""
    print("\n-- what a scaffold writes to disk --")
    import buildapp

    url = "https://github.com/example/demo-app"
    info = {
        "ok": True,
        "repo": buildapp.repo_parts(url),
        "branch": "main",
        "head": "deadbeef add demo",
        "analysis_dir": "",
        "readme_head": "# Demo app",
        "tree_top": ["app.py", "requirements.txt"],
        "manifests_present": ["requirements.txt"],
        "github": {"description": "A demo app", "stars": 3, "license": "MIT",
                   "avatar": "", "default_branch": "main", "private": False},
        **buildapp.classify(["app.py", "requirements.txt"], "# Demo app",
                            {"requirements.txt": "gradio\n"}),
    }
    buildapp.recon = lambda url, cache_dir, timeout=600: info          # offline
    buildapp._download_icon = lambda url, dest: False                   # offline

    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        res = buildapp.scaffold(home, url, "demo-app", force=True)
        check("scaffold succeeds", bool(res.get("ok")), str(res)[:200])
        app = home / "api" / "demo-app"
        check("the launcher is athena.js", (app / "athena.js").is_file())
        shim = app / "pinokio.js"
        check("a legacy shim keeps the engine able to resolve it", shim.is_file())
        if shim.is_file():
            body = shim.read_text(encoding="utf-8")
            check("the shim does nothing but re-export the launcher",
                  "athena.js" in body and len(body.splitlines()) <= 4, body.strip()[:120])

        bad_name = [p.name for p in app.rglob("*")
                    if p.is_file() and mentions(p.name) and not is_shim(p)]
        check("no file name mentions the vendor", not bad_name, str(bad_name))

        bad_text: list[str] = []
        for p in sorted(app.rglob("*")):
            if p.is_file() and scannable(p) and mentions(p.read_text(encoding="utf-8")):
                bad_text.append(p.relative_to(app).as_posix())
        check("no generated file mentions the vendor", not bad_text, str(bad_text))

        v = buildapp.validate(home, "demo-app")
        check("validate() accepts the scaffold", bool(v.get("ok")),
              str(v.get("errors") or v.get("notes"))[:200])


def test_agent_prompt() -> None:
    """The instruction the agent follows when it builds an app.

    If this says the wrong filename, every app the agent builds comes out wrong,
    which no amount of string-cleaning elsewhere makes up for.
    """
    print("\n-- the prompt the agent is handed --")
    import buildapp

    url = "https://github.com/example/demo-app"
    info = {
        "ok": True, "repo": buildapp.repo_parts(url), "branch": "main",
        "head": "deadbeef add demo", "analysis_dir": "",
        "readme_head": "# Demo app", "tree_top": ["app.py", "requirements.txt"],
        "manifests_present": ["requirements.txt"],
        "github": {"description": "A demo app", "stars": 3, "license": "MIT",
                   "avatar": "", "default_branch": "main", "private": False},
        **buildapp.classify(["app.py", "requirements.txt"], "# Demo app",
                            {"requirements.txt": "gradio\n"}),
    }
    buildapp.recon = lambda url, cache_dir, timeout=600: info
    buildapp._download_icon = lambda url, dest: False
    with tempfile.TemporaryDirectory() as td:
        res = buildapp.build_prompt(Path(td), url, "demo-app")
        txt = res.get("prompt") or ""
        check("build_prompt returns a prompt", bool(txt), str(res)[:200])
        check("the prompt names athena.js", "athena.js" in txt, txt[:200])
        bad = [l.strip() for l in txt.split("\n") if mentions(l) and "shim" not in l]
        check("the prompt names the vendor only to protect the shim", not bad, str(bad)[:200])


def test_generated_guide() -> None:
    print("\n-- the guide Athena writes into an app --")
    import appcall

    with tempfile.TemporaryDirectory() as td:
        app = Path(td) / "api" / "demo"
        app.mkdir(parents=True)
        man = {"title": "Demo", "description": "A demo app", "version": "1.0",
               "capabilities": [], "entry": None, "install": {"kind": "python"},
               "athena": {"converted_at": 0}}
        try:
            path = appcall.write_guide(Path(td), app, man)
        except Exception as e:                                    # noqa: BLE001
            check("write_guide runs", False, f"{type(e).__name__}: {e}")
            return
        text = Path(path).read_text(encoding="utf-8")
        n = mentions(text)
        check("the generated guide is clean", n == 0, f"{n} mention(s) in {Path(path).name}")


def test_store_records() -> None:
    print("\n-- the words the store publishes --")
    import publish_store

    home = Path(str(HERMES_ROOT) + r"/athena")
    apps = publish_store.apps_in(home) if home.is_dir() else []
    if not apps:
        check("a local store app to describe", False, "none found — nothing measured")
        return
    # One check rather than one per app, for the same reason as the app rows next door: the
    # number this file reports is quoted in the README, and an install or an uninstall must
    # not move it. The apps that fail are named.
    _dirty: list[str] = []
    for app in apps:
        files = publish_store.app_files(app)
        rec = publish_store.describe(app, publish_store.slug_for(app.name), app.name,
                                     "", files)
        shown = {k: v for k, v in rec.items()
                 if k not in ("repo", "repoUrl", "origin", "branch", "files")}
        if mentions(json.dumps(shown, ensure_ascii=False)):
            _dirty.append(app.name)
    check(f"every local store app's description is clean ({len(apps)} app(s))",
          not _dirty, str(_dirty))


SAMPLE = ('[pinokiod] pinokio://run  starting pinokio.js for Maestro\n'
          'Pinokio Home: ' + str(Path.home()) + '/.pinokio\n'
          'warning: pinokio_meta.json could not be parsed\n'
          'run app.py in ./app  (http://127.0.0.1:7860)')


def test_boundary() -> None:
    """Engine text reaches the user through Athena — it is scrubbed on the way."""
    print("\n-- engine output crossing into the UI --")
    import naming
    import runtime as athena_runtime

    fn = getattr(naming, "scrub_engine_text", None)
    if fn is None:
        check("the engine-text scrub exists", False, "naming.scrub_engine_text")
        return
    out = fn(SAMPLE)
    n = mentions(out)
    check("engine text arrives clean", n == 0, out[:200])
    check("the local URL survives the scrub", "http://127.0.0.1:7860" in out, out[:200])
    # A URL points at something real; rewriting it hands out a dead link.
    ext = fn("see https://pinokio.co/docs for usage")
    check("an external URL is left alone", "https://pinokio.co/docs" in ext, ext[:160])

    # And the surface a person actually reads — the log view — must be clean.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "logs" / "shell" / "api" / "demo"
        d.mkdir(parents=True)
        (d / "today.txt").write_text(SAMPLE, encoding="utf-8")
        rt = athena_runtime.Runtime(42001, home=td)
        text = rt.logs("demo")
        k = mentions(text)
        check("the log view shows no vendor name", k == 0, f"{k} mention(s): {text[:160]}")


def test_naming_helpers() -> None:
    """One place answers 'where is this app's launcher'."""
    print("\n-- the compat reader --")
    sys.path.insert(0, str(PLUGIN / "dashboard"))
    try:
        import naming
    except ImportError:
        check("dashboard/naming.py exists", False, "no module")
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "pinokio.js").write_text("// legacy\n", encoding="utf-8")
        check("legacy-only folder resolves to the legacy launcher",
              naming.launcher_of(d) == d / "pinokio.js")
        (d / "athena.js").write_text("// ours\n", encoding="utf-8")
        check("athena.js wins when both are present",
              naming.launcher_of(d) == d / "athena.js")
        check("a folder with neither yields None", naming.launcher_of(Path(td) / "nope") is None)
        (d / "athena_meta.json").write_text("{}", encoding="utf-8")
        (d / "pinokio_meta.json").write_text("{}", encoding="utf-8")
        check("the meta reader prefers the Athena name",
              naming.meta_of(d) == d / "athena_meta.json")
        check("the naming table agrees with the helpers",
              "athena.js" in naming.LAUNCHER_NAMES and "pinokio.js" in naming.LAUNCHER_NAMES)


def test_installed_apps() -> None:
    """The apps on this machine: a project's own launcher stays, ours moves."""
    print("\n-- the apps installed here --")
    import buildapp

    home = Path(str(HERMES_ROOT) + r"/athena/api")
    if not home.is_dir():
        check("an app folder to inspect", False, str(home))
        return
    left_behind: list[str] = []
    theirs = 0
    for app in sorted(p for p in home.iterdir() if p.is_dir()):
        legacy = app / "pinokio.js"
        if not legacy.is_file() or (app / "athena.js").is_file() or is_shim(legacy):
            continue
        if buildapp.is_ours(legacy):
            left_behind.append(app.name)
        else:
            theirs += 1
    print(f"       ({theirs} project-owned launcher(s) left untouched)")
    check("no launcher Athena wrote is still on the legacy name", not left_behind,
          str(left_behind))

    # An app transferred from another install keeps its own files, and saying
    # where it came from is honest. An app built here from a repository has no
    # such history: nothing of its own may name the vendor but the shim the
    # engine insists on.
    built_here_bad: dict[str, list[str]] = {}
    for app in sorted(p for p in home.iterdir() if p.is_dir()):
        if (app / ".git").exists():      # a transferred app is a checkout
            continue
        bad = []
        for f in sorted(app.iterdir()):
            if not f.is_file() or f.name == "pinokio.js" or f.name.endswith((".png", ".jpg", ".ico")):
                continue
            if mentions(f.name):
                bad.append(f.name + " (filename)")
                continue
            try:
                txt = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if mentions(txt):
                bad.append(f.name)
        if bad:
            built_here_bad[app.name] = bad
    check("an app built here names the vendor nowhere but the shim",
          not built_here_bad, str(built_here_bad))


def test_shim_is_still_required() -> None:
    """The legacy-named file beside a launcher Athena wrote is the engine's own filename.

    Measured, not remembered: the engine resolves the launcher through its own
    literal, with no constant and no config knob, and lists apps by that same
    filename — so a folder carrying only `athena.js` is a folder the engine
    cannot see or start. That is why Phase 4's "drop the shim" is not a rename
    but an engine patch, and the reason is re-checked here instead of trusted to
    a comment. A bump that changes it is not a break — it is the day the shim
    becomes dead weight — so the evidence is printed every run, and the alarm is
    a line of text rather than a red check that trains people to ignore red.
    """
    print("\n-- the shim the engine insists on --")
    import naming
    engine = PLUGIN / "engine" / "node_modules" / "pinokiod"
    sites = ["kernel/api/index.js", "server/index.js", "server/autolaunch.js"]
    if not engine.is_dir():
        check("the vendored engine is there to inspect", False, str(engine))
        return
    literal, ours, seen = [], [], []
    for rel in sites:
        f = engine / rel
        if not f.is_file():
            continue
        seen.append(rel)
        txt = f.read_text(encoding="utf-8", errors="ignore")
        if f'"{naming.LEGACY_LAUNCHER}"' in txt or f"'{naming.LEGACY_LAUNCHER}'" in txt:
            literal.append(rel)
        if naming.LAUNCHER in txt:
            ours.append(rel)
    check("the engine still names the legacy launcher itself, so the shim has to exist",
          len(literal) == len(seen) and bool(seen), f"named in {literal} of {seen}")
    if ours:
        print(f"       ACTION: {ours} now read {naming.LAUNCHER} — the shim can be dropped; "
              "see docs/NAMING.md, 'The shim, and when it can go'")

    code = [l for l in naming.SHIM_BODY.splitlines() if l.strip() and not l.lstrip().startswith("//")]
    check("the shim is a re-export and nothing else",
          code == [f'module.exports = require("./{naming.LAUNCHER}");'], repr(code))

    home = Path(str(HERMES_ROOT) + r"/athena/api")
    missing: list[str] = []
    stale: list[str] = []
    if home.is_dir():
        for app in sorted(p for p in home.iterdir() if p.is_dir()):
            if (app / ".git").exists() or not (app / naming.LAUNCHER).is_file():
                continue            # an imported app keeps upstream's own file
            shim = app / naming.LEGACY_LAUNCHER
            if not shim.is_file():
                missing.append(app.name)
            elif shim.read_text(encoding="utf-8").strip() != naming.SHIM_BODY.strip():
                stale.append(app.name)
    check("an app built here carries the shim the engine will look for", not missing, str(missing))
    check("and it is the generated re-export, not a stale copy", not stale, str(stale))


def main() -> int:
    for fn in (test_surfaces, test_scaffold_output, test_agent_prompt, test_generated_guide,
               test_installed_apps,
               test_store_records, test_boundary, test_naming_helpers,
               test_shim_is_still_required):
        try:
            fn()
        except Exception as e:                                     # noqa: BLE001
            check(f"{fn.__name__} ran", False, f"{type(e).__name__}: {e}")
    print(f"\n{PASS} check(s) passed, {FAIL} failed")
    if FAILURES:
        print("failing: " + "; ".join(FAILURES[:12]))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
