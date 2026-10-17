#!/usr/bin/env python3
"""Athena Store: reading the index, merging it with the global catalogue, and
installing one of its apps for real.

Run with `env -u PYTHONPATH` (the plugin's own venv).

The install checks clone from the real repository over the network with Hermes'
own GitHub token, because the thing being tested is exactly that a private
store installed by a logged-in Hermes works — a mocked clone would test nothing
about the part that can fail.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

DASH = Path(str(HERMES_ROOT) + r"/plugins/athena/dashboard")
PLUG = DASH.parent
HOME = Path(os.environ.get("ATHENA_TEST_HOME") or
            (Path(tempfile.gettempdir()) / "athena-store-test"))

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
        print(f"  FAIL {name} {detail}")


sys.path.insert(0, str(DASH))
import catalog as C  # noqa: E402

print("== the store is a repository, read through Hermes' own login ==")
token = C._gh_token()
check("a GitHub token is available from the existing login (no second sign-in)",
      bool(token))

urlmap = C._to_api_url(C.STORE_INDEX)
check("a raw GitHub URL maps to the contents API for the private route",
      bool(urlmap) and "api.github.com" in urlmap and "index.json" in urlmap, urlmap)
check("an owner/repo shorthand maps to an index read",
      "index.json" in (C._to_api_url("imzacksong/athena-store") or ""))

# The card's rig mark is only as good as these two maps surviving `_enrich`. The
# catalogue declares no VRAM and no disk figure — it never has — so "somebody runs
# this on Windows + NVIDIA" is the only rig-fit fact a store row carries, and a
# row that quietly loses it would make the chip look like a broken filter.
en = C._enrich({"slug": "x", "title": "X", "checkinsCount": 7,
                "checkinsPlatforms": {"windows": True, "linux": True},
                "checkinsGpus": {"nvidia": True}}, HOME)
check("a store row keeps the check-in platforms and GPUs the rig mark reads",
      en.get("platforms", {}).get("windows") is True
      and en.get("gpus", {}).get("nvidia") is True and en.get("checkins") == 7,
      str({k: en.get(k) for k in ("platforms", "gpus", "checkins")}))

anon = False
try:
    C._get_json(C.STORE_INDEX, timeout=20)
except C.CatalogError as e:
    anon = "404" in str(e) or "403" in str(e)
check("the store is private: an anonymous read is refused", anon)

data = C.fetch_source(HOME, force=True)
slugs = [a.get("slug") for a in data.get("apps") or []]
print(f"       fetched {len(slugs)} apps via {data.get('index')}")
check("the index reads through GitHub with the token", len(slugs) == 6, str(data.get("error") or slugs))
check("it records which repository it came from",
      str(data.get("repo") or "").endswith("athena-store"), data.get("repo"))
check("the index is cached on disk next to the catalogue cache",
      C._source_path(HOME).is_file())
recent = time.time() - json.loads(C._source_path(HOME).read_text())["fetched_at"] < 60
check("the cached copy is fresh, not a stale leftover", recent)

print("\n== resolving an app to something installable ==")
r = C.resolve(HOME, "athena-rmbg-2-studio")
check("a store slug resolves", r.get("git") == C.STORE_URL, str(r)[:200])
check("…carrying the branch it lives on", r.get("ref") == "athena-rmbg-2-studio", r.get("ref"))
check("…into the folder the app would occupy from upstream",
      r.get("folder") == "RMBG-2-Studio.git", r.get("folder"))
check("…and it says which store answered", r.get("store") == "athena", r.get("store"))
check("…with the conversion facts along for the ride",
      r.get("converted") is True and r.get("capabilities") == 5, str(r.get("capabilities")))

t2 = C.resolve(HOME, "wan2gp")
check("a title resolves case-insensitively too",
      t2.get("git") == C.STORE_URL and t2.get("ref") == "athena-wan2gp", str(t2)[:160])

b = C.resolve(HOME, "imzacksong/athena-store#athena-comfyui")
check("owner/repo#branch resolves to that branch",
      b.get("git") == C.STORE_URL and b.get("ref") == "athena-comfyui", str(b)[:160])
b2 = C.resolve(HOME, "https://github.com/imzacksong/athena-store/tree/athena-wan2gp")
check("a pasted …/tree/<branch> URL means the same thing",
      b2.get("ref") == "athena-wan2gp", str(b2)[:160])

plain = C.resolve(HOME, "https://github.com/someone/thing.git")
check("a plain repository URL is still itself",
      plain.get("git") == "https://github.com/someone/thing.git", str(plain)[:120])

d = C.detail(HOME, "athena-yue2-groove")
check("detail answers for our own apps without the global store",
      d.get("store") == "athena" and d.get("title"), str(d)[:160])

print("\n== browsed together with the global catalogue ==")
fake_page = {
    "items": [{"app": {"slug": f"global-{i}", "title": f"Global {i}"}} for i in range(60)],
    "page": 1, "perPage": 60, "total": 600, "totalPages": 10,
}
calls: list[tuple] = []


def fake_page_cached(page, per_page, q, tag, sort):
    calls.append((page, per_page, skip_hint))
    rows = [{"slug": f"global-{page}-{i}", "title": f"Global {page}-{i}", "tags": []}
            for i in range(per_page)]
    return {"items": [{"app": r} for r in rows], "page": page, "perPage": per_page,
            "total": 600, "totalPages": 10}


skip_hint = None
C._page_cached = fake_page_cached  # type: ignore[assignment]
C.index = lambda home: {"apps": []}  # type: ignore[assignment]

p0 = C.browse(HOME, offset=0, limit=60)
mine_first = [a for a in p0["apps"][:6] if a.get("store") == "athena"]
check("page one puts the store's own apps first", len(mine_first) == 6, str(len(mine_first)))
check("…and fills the rest of the page from the global catalogue",
      len(p0["apps"]) == 60 and p0["apps"][6]["store"] == "global", str(len(p0["apps"])))
check("…counting both catalogues in the total",
      p0["total"] == 606 and p0["athena_store"] == 6, f"{p0['total']} / {p0['athena_store']}")
check("…and naming both in the source", p0["source"] == "athena+store", p0["source"])

calls.clear()
p1 = C.browse(HOME, offset=60, limit=60)
# The global catalogue is still paged in windows of 60, but our apps now occupy
# the head of the first window, so page two of the store is slot 54 of global
# page one — the window moves, the catalogue's own paging does not.
check("the next page is global-only, from the right global window and slot",
      p1["apps"][0]["slug"] == "global-1-54" and calls and calls[0][0] == 1,
      f"{p1['apps'][0]['slug']} / calls={calls}")

calls.clear()
p2 = C.browse(HOME, offset=6, limit=60)
check("once past our own apps the global catalogue starts from its beginning",
      calls and calls[0][0] == 1 and calls[0][1] == 60, str(calls))

q = C.browse(HOME, offset=0, limit=60, q="background")
check("search reaches our store's apps too",
      any("rmbg" in (a.get("slug") or "") for a in q["apps"]),
      str([a.get("slug") for a in q["apps"]][:3]))

print("\n== installing an app from the store, for real ==")
dest_home = HOME / "home"


def wipe(p: Path) -> None:
    """Remove a previous test install, including git's read-only objects.

    Windows leaves git object files read-only, so a plain rmtree fails on them
    and the next run measures the last run's leftovers.
    """
    for _ in range(3):
        shutil.rmtree(p, ignore_errors=True)
        if not p.exists():
            return
        subprocess.run(["cmd", "/c", "rmdir", "/S", "/Q", str(p)],
                       capture_output=True, text=True)


wipe(dest_home)
(dest_home / "api").mkdir(parents=True, exist_ok=True)

import plugin_api as A  # noqa: E402


plan = C.resolve(HOME, "athena-rmbg-2-studio")
t0 = time.time()
got = A.clone_app(plan["git"], dest_home, ref=plan["ref"], name=plan["folder"])
dt = time.time() - t0
print(f"       cloned {plan['git']}#{plan['ref']} -> api/{plan['folder']} in {dt:.1f}s")
check("the clone succeeded", bool(got.get("ok")), str(got)[:400])

app = dest_home / "api" / "RMBG-2-Studio.git"
check("the app landed in the folder the engine would have used",
      (app / "pinokio.js").is_file(), str(app))
check("it is a real checkout, so the app's own tooling works",
      (app / ".git").is_dir())
check("…on the app's branch of the store",
      subprocess.run(["git", "-C", str(app), "rev-parse", "--abbrev-ref", "HEAD"],
                     capture_output=True, text=True).stdout.strip() == plan["ref"])
files = sorted(p.name for p in app.iterdir() if p.is_file())
check("…carrying the launcher and the conversion output",
      all(f in files for f in ("pinokio.js", "install.js", "start.js", "GUIDE.md", "agent.json")),
      str(files))
# `app/` is the app's own source (two tracked files here), not build output —
# what must not travel is what the publishing machine produced: the venv it
# built, the caches it filled, the logs it wrote, the outputs it made.
built = [d for d in ("venv", "logs", "cache", "output_images") if (app / d).exists()]
inside = sorted(str(p.relative_to(app)).replace("\\", "/")
                for p in (app / "app").rglob("*") if p.is_file())
check("…and none of the publishing machine's own build came with it",
      not built and inside == ["app/app.py", "app/requirements.txt"],
      f"built={built} inside={inside}")

pull = subprocess.run(["git", "-C", str(app), "pull"], capture_output=True, text=True)
check("the app's own `git pull` works — its Update button is alive",
      pull.returncode == 0 and "up to date" in (pull.stdout + pull.stderr).lower(),
      (pull.stdout + pull.stderr).strip()[:200])

hist = subprocess.run(["git", "-C", str(app), "log", "--oneline", "-1"],
                      capture_output=True, text=True).stdout.strip()
check("…with real history behind it, not an empty repository", bool(hist), hist)

again = A.clone_app(plan["git"], dest_home, ref=plan["ref"], name=plan["folder"])
check("installing the same app twice reuses it instead of making a second copy",
      again.get("already") is True, str(again)[:200])

print("\n== the store source is a setting ==")
other = HOME / "home2"
(other / "api").mkdir(parents=True, exist_ok=True)
C.set_source(other, "https://example.com/my-store/index.json")
check("a custom index is remembered", C.source_url(other) == "https://example.com/my-store/index.json")
C.set_source(other, "")
check("clearing it falls back to Athena's own store", C.source_url(other) == C.default_source())
check("a home with no setting at all still reads Athena's store",
      C.source_url(Path(tempfile.gettempdir()) / "definitely-not-here") == C.default_source())

A._apply_store_source({"home": str(other), "store_index": "https://example.com/s/index.json"})
check("the settings layer publishes the source where the catalogue reads it",
      C.source_url(other) == "https://example.com/s/index.json")


print("\n== the apps tab's layout is a setting, and only two things it can be ==")
check("rows is rows, however it was spelled",
      [A.layout_words(w) for w in ("rows", "ROWS", " rows ", "row", "list", "compact")]
      == ["rows"] * 6)
check("anything else is cards, which is the layout that shows the most",
      [A.layout_words(w) for w in ("cards", "", None, "banana", 0, "columns")]
      == ["cards"] * 6)
check("and the settings route stores the normalised word rather than the raw one",
      'cfg["apps_layout"] = layout_words(body.apps_layout)' in Path(
          str(HERMES_ROOT) + r"/plugins/athena/dashboard/plugin_api.py")
      .read_text(encoding="utf-8"),
      "the route keeps no opinion of its own")

print("\n== the global catalogue is untouched ==")
src = Path(str(HERMES_ROOT) + r"/plugins/athena/dashboard/plugin_api.py").read_text(encoding="utf-8")
check("installing without a branch clones the repository as before",
      'if ref:\n        args += ["--branch", ref, "--single-branch"]' in src)
check("an install from the global catalogue still uses the repo basename",
      "name = name or app_folder_name(repo_url)" in src)
check("the agent tool resolves the same way the Store tab does",
      "resolved.get(\"ref\")" in Path(
          str(HERMES_ROOT) + r"/plugins/athena/__init__.py").read_text(encoding="utf-8"))

print(f"\n{PASS} check(s) passed, {FAIL} failed")
if FAILURES:
    print("failed: " + "; ".join(FAILURES))
    sys.exit(1)
