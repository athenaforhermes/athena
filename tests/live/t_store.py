#!/usr/bin/env python3
"""Athena Store: reading the placeholder index, degrading calmly when it cannot
be read, and browsing it alongside the global catalogue.

Run with `env -u PYTHONPATH` (the plugin's own venv).

The store repository is private and its catalogue is a deliberate placeholder
with an empty `apps` list, so the two states this file keeps apart are:

* empty but readable — the index was fetched and says there are no apps yet;
* unreachable — the index could not be read at all (404, no token, offline).

The panel says "coming soon" for the first and "could not be read just now" for
the second; neither is an error, a traceback, or a spinner that never resolves.
"""
from __future__ import annotations

import json
import os
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

print("== no store until a person names one ==")
token = C._gh_token()
# Fail closed: with no secret scope active there is no token to hand out, and the
# read has to say so rather than blow up a caller who has not signed in. This is
# the property the review asks for — a GitHub credential is either the one Hermes
# already holds for you, or nothing at all.
check("a token read with no scope active fails closed instead of crashing",
      token == "", repr(token))

check("there is no default store: the product never picks a repository for you",
      C.STORE_REPO is None and C.STORE_URL == "" and C.STORE_INDEX == "",
      f"{C.STORE_REPO!r} / {C.STORE_URL!r} / {C.STORE_INDEX!r}")
check("…so with nothing named, the default source is empty rather than Athena's own",
      C.default_source() == "", C.default_source())

urlmap = C._to_api_url("https://raw.githubusercontent.com/someone/"
                       "some-store/main/index.json")
check("a raw GitHub URL maps to the contents API for the private route",
      bool(urlmap) and "api.github.com" in urlmap and "index.json" in urlmap, urlmap)
check("an owner/repo shorthand maps to an index read",
      "index.json" in (C._to_api_url("someone/some-store") or ""))

# The chip's filter is only as good as these two maps surviving `_enrich`. The
# catalogue declares no VRAM and no disk figure — it never has — so "somebody runs
# this on Windows + NVIDIA" is the only rig-fit fact a store row carries, and a
# row that quietly loses it would make the chip look like a broken filter. The
# per-card badge that used to read the same maps is gone; the chip is opt-in.
en = C._enrich({"slug": "x", "title": "X", "checkinsCount": 7,
                "checkinsPlatforms": {"windows": True, "linux": True},
                "checkinsGpus": {"nvidia": True}}, HOME)
check("a store row keeps the check-in platforms and GPUs the chip filter reads",
      en.get("platforms", {}).get("windows") is True
      and en.get("gpus", {}).get("nvidia") is True and en.get("checkins") == 7,
      str({k: en.get(k) for k in ("platforms", "gpus", "checkins")}))

# The review's rule is that a private store is never read without the credential
# Hermes holds for you — so with no store named there is no read to make, and
# nothing goes out first. A request that is made "anonymously" is exactly what
# must not happen.
sent: list = []
real_json = C._get_json
C._get_json = lambda url, *a, **k: (sent.append(url) or {})
plain = C.fetch_source(HOME, force=True)
C._get_json = real_json
check("with no store named, Athena sends no request at all", sent == [], str(sent))

apps = plain.get("apps") or []
check("nothing named means nothing is fetched",
      isinstance(apps, list) and apps == [] and not plain.get("index"),
      str(plain)[:200])
check("…and no failure is recorded against it", not plain.get("error"), str(plain)[:200])
check("…so nothing is cached as though a store had been read",
      not C._source_path(HOME).is_file())

# …and the panel's own reading of that state: empty, with nothing broken.
_real_page, _real_index = C._page_cached, C.index
C._page_cached = lambda *a, **k: (_ for _ in ()).throw(C.CatalogError("offline"))
C.index = lambda home: {"apps": [], "warming": False}  # type: ignore[assignment]
empty = C.browse(HOME, 0, 60)
C._page_cached, C.index = _real_page, _real_index
check("the panel reports an unnamed store as empty, not as an error",
      empty.get("athena_empty") is True, str(empty)[:200])
check("…with no error attached to it",
      empty.get("athena_error") == "", empty.get("athena_error"))

# A store a person names is remembered, and read from where they pointed it.
named = HOME / "named"
(named / "api").mkdir(parents=True, exist_ok=True)
named_index = "https://raw.githubusercontent.com/someone/some-store/main/index.json"
C.set_source(named, named_index)
check("a store a person names is remembered", C.source_url(named) == named_index,
      C.source_url(named))

seen: list = []
real_get = C._get_json
C._get_json = lambda url, *a, **k: (seen.append(url) or {
    "repo": "someone/some-store",
    "apps": [{"slug": "named-app", "title": "Named", "ref": "athena-named"}]})
read = C.fetch_source(named, force=True)
C._get_json = real_get
check("…and it is the store Athena then reads", seen == [named_index], str(seen))
check("…so the apps it carries are the store's apps",
      [a.get("slug") for a in read.get("apps") or []] == ["named-app"],
      str(read.get("apps"))[:200])
check("…and the read is cached beside the catalogue, marked fresh",
      C._source_path(named).is_file()
      and time.time() - json.loads(C._source_path(named).read_text())["fetched_at"] < 60)

print("\n== an empty catalogue is a state, not a failure ==")
old_page, old_index, old_get = C._page_cached, C.index, C._get_json
C._page_cached = lambda *a, **k: (_ for _ in ()).throw(C.CatalogError("offline"))
C.index = lambda home: {"apps": [], "warming": False}  # type: ignore[assignment]
b = C.browse(HOME, 0, 60)
check("browsing an empty published catalogue does not raise", isinstance(b, dict))
check("…it reports the store as empty", b.get("athena_empty") is True, str(b)[:200])
check("…with no failure attached to it", b.get("athena_error") == "", b.get("athena_error"))
check("…and hands back no store apps", b.get("athena_store") == 0, b.get("athena_store"))

print("\n== an unreachable index is reported as unreachable, not as empty ==")
# A store that IS named but cannot be read is a different state from one that was
# never named: the panel has to be able to tell "the store is empty" from "the
# store is there and we could not reach it".
dead = HOME / "unreachable"
(dead / "api").mkdir(parents=True, exist_ok=True)
C.set_source(dead, "https://raw.githubusercontent.com/someone/some-store/main/index.json")
C._get_json = lambda *a, **k: (_ for _ in ()).throw(C.CatalogError("404 Not Found"))
raw = C.fetch_source(dead, force=True)
check("fetch_source returns an empty list and the reason instead of raising",
      raw.get("apps") == [] and "404" in str(raw.get("error") or ""), str(raw)[:200])
check("…and the reason is kept for the panel",
      "404" in str(C._SOURCE_STATUS.get("error") or ""), C._SOURCE_STATUS.get("error"))
u = C.browse(dead, 0, 60)
check("browse does not raise when the index cannot be read", isinstance(u, dict))
check("…and says the store is empty", u.get("athena_empty") is True, str(u)[:200])
check("…while carrying the reason, so it is not mistaken for a published empty store",
      "404" in str(u.get("athena_error") or ""), u.get("athena_error"))
check("…and hands back no apps at all", u.get("apps") == [] and u.get("athena_store") == 0,
      str(u.get("apps"))[:120])

# The panel's sentence and the fields it is conditioned on. This is a build
# artefact check on purpose: the empty state ships, so it is asserted in the
# shipped copy rather than in a component the suite cannot mount.
ui = (PLUG / "desktop" / "plugin.js").read_text(encoding="utf-8")
check("the panel has a calm 'coming soon' empty state for an empty catalogue",
      "catalogue is coming soon" in ui)
check("…driven by athena_empty / athena_error rather than by an error banner",
      "athenaSoon" in ui and "athena_empty" in ui and "athena_error" in ui)
check("…and it is not shown while a search is running",
      "athenaSoon && !active" in ui)

C._get_json, C._page_cached, C.index = old_get, old_page, old_index

print("\n== resolving an app without a catalogue ==")
r = C.resolve(HOME, "athena-does-not-exist")
check("an unknown store slug answers an error object, not an exception",
      isinstance(r, dict) and not r.get("git") and r.get("error"), str(r)[:200])
plain = C.resolve(HOME, "https://github.com/someone/thing.git")
check("a plain repository URL is still itself",
      plain.get("git") == "https://github.com/someone/thing.git", str(plain)[:120])

print("\n== browsed together with the global catalogue ==")
calls: list[tuple] = []


def fake_page_cached(page, per_page, q, tag, sort):
    calls.append((page, per_page, per_page))
    rows = [{"slug": f"global-{page}-{i}", "title": f"Global {page}-{i}", "tags": []}
            for i in range(per_page)]
    return {"items": [{"app": r} for r in rows], "page": page, "perPage": per_page,
            "total": 600, "totalPages": 10}


C._page_cached = fake_page_cached  # type: ignore[assignment]
C.index = lambda home: {"apps": []}  # type: ignore[assignment]

p0 = C.browse(HOME, offset=0, limit=60)
check("a page is filled from the global catalogue when the store is empty",
      len(p0["apps"]) == 60 and p0["apps"][0]["store"] == "global", str(len(p0["apps"])))
check("…counting only the global catalogue in the total",
      p0["total"] == 600 and p0["athena_store"] == 0, f"{p0['total']} / {p0['athena_store']}")
check("…and naming it as the only source", p0["source"] == "store", p0["source"])

calls.clear()
p1 = C.browse(HOME, offset=60, limit=60)
check("the next page is the global catalogue's own second page",
      p1["apps"][0]["slug"] == "global-2-0" and calls and calls[0][0] == 2,
      f"{p1['apps'][0]['slug']} / calls={calls}")

# Store apps still lead the page when there are any: that ordering is what the
# Store tab depends on, and it is exercised here with a stub because nothing is
# published yet.
C.store_apps = lambda *a, **k: [{"slug": "athena-stub", "title": "Stub",
                                 "repoUrl": "https://github.com/x/stub", "ref": "athena-stub"}]
calls.clear()
p2 = C.browse(HOME, offset=0, limit=60)
check("a store app still leads the page and counts in the total",
      p2["apps"][0]["slug"] == "athena-stub" and p2["athena_store"] == 1
      and p2["total"] == 601, f"{p2['apps'][0]['slug']} / {p2['total']}")
check("…and the global page below it is still full width",
      len(p2["apps"]) == 60 and p2["apps"][1]["store"] == "global", str(len(p2["apps"])))

print("\n== the store source is a setting ==")
other = HOME / "home2"
(other / "api").mkdir(parents=True, exist_ok=True)
C.set_source(other, "https://example.com/my-store/index.json")
check("a custom index is remembered", C.source_url(other) == "https://example.com/my-store/index.json")
C.set_source(other, "")
check("clearing it leaves no store named, not a product-chosen one",
      C.source_url(other) == C.default_source())
check("a home with no setting at all reads no store either",
      C.source_url(Path(tempfile.gettempdir()) / "definitely-not-here") == C.default_source())

import plugin_api as A  # noqa: E402

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
