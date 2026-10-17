#!/usr/bin/env python3
"""The same app must not appear twice on one page.

Athena's store and the global catalogue are both indexed by slug, and the two
slugs for one app are different names for it (`athena-comfyui` vs
`github-com-pinokiofactory-comfy`, one repo written `…/comfy.git` and the other
`…/comfy`). Our apps take the head of every page, so without filtering they come
back a second time from the global page below them.

No network here: the two sources are stubbed so the *paging math* is what is
under test — that is the part that silently drops or duplicates apps.

Run with `env -u PYTHONPATH` from this directory.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import os
# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

DASH = Path(str(HERMES_ROOT) + r"/plugins/athena/dashboard")
sys.path.insert(0, str(DASH))
catalog = __import__("catalog")

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
        print(f"  FAIL {name}  {detail}")


def app(slug: str, repo: str, title: str = "") -> dict:
    return {"slug": slug, "title": title or slug, "repoUrl": repo}


HOME = Path(str(HERMES_ROOT) + r"/athena")


def stub(mine: list[dict], pages: dict[int, list[dict]], per_page: int, calls: list):
    """Point browse() at two hand-written catalogues."""
    catalog.store_apps = lambda *a, **k: list(mine)

    def fake_page(page, per_page_, q, tag, sort):
        calls.append(page)
        items = pages.get(page)
        if items is None:
            raise catalog.CatalogError("no more pages")
        return {"items": items, "total": sum(len(v) for v in pages.values()),
                "totalPages": len(pages)}

    catalog._page_cached = fake_page
    catalog.PAGE_SIZE = per_page
    return catalog.browse(HOME, 0, per_page, "", "", "trending")


print("== the duplicate on the head of the global page is skipped ==")
mine = [app("athena-comfyui", "https://github.com/pinokiofactory/comfy.git")]
pages = {1: [app("github-com-pinokiofactory-wan", "https://github.com/pinokiofactory/wan"),
             app("github-com-pinokiofactory-comfy", "https://github.com/pinokiofactory/comfy"),
             app("github-com-blizaine-maestro", "https://github.com/Blizaine/Maestro")],
         # 4 wide, one of the four ours: page 1 is a item short, so filling the
         # window is page 2's job — and that has to be a top-up, not a fallback.
         2: [app("github-com-other-a", "https://github.com/o/a")]}
calls: list = []
r = stub(mine, pages, 4, calls)
got = [a["slug"] for a in r["apps"]]
check("ours is on the page once, from our store", got.count("athena-comfyui") == 1, got)
check("the global copy of our app is dropped", "github-com-pinokiofactory-comfy" not in got, got)
check("the other global apps survive", "github-com-pinokiofactory-wan" in got and "github-com-blizaine-maestro" in got, got)
check("it topped up from page 2, not the cache", calls == [1, 2], calls)
check("and that page is still full width", len(r["apps"]) == 4, [a["slug"] for a in r["apps"]])

print("== a page emptied by duplicates is topped up from the next one ==")
mine = [app("athena-comfyui", "https://github.com/pinokiofactory/comfy.git"),
        app("athena-wan2gp", "https://github.com/pinokiofactory/wan.git")]
pages = {1: [app("github-com-pinokiofactory-comfy", "https://github.com/pinokiofactory/comfy"),
             app("github-com-pinokiofactory-wan", "https://github.com/pinokiofactory/wan"),
             app("github-com-other-a", "https://github.com/o/a")],
         2: [app("github-com-other-b", "https://github.com/o/b"),
             app("github-com-other-c", "https://github.com/o/c")]}
calls = []
r = stub(mine, pages, 4, calls)
got = [a["slug"] for a in r["apps"]]
check("the page is still full width", len(r["apps"]) == 4, got)
check("our two apps lead the page", got[:2] == ["athena-comfyui", "athena-wan2gp"], got)
check("the topped-up app came from page 2", "github-com-other-b" in got, got)
check("it read the next page to fill the gap", calls == [1, 2], calls)
check("no app appears twice", len(set(got)) == len(got), got)

print("== a run of pages that are ALL ours does not loop forever ==")
mine = [app("athena-a", "https://github.com/x/a"), app("athena-b", "https://github.com/x/b")]
pages = {p: [app(f"github-com-x-{p}", f"https://github.com/x/{p}")] for p in range(1, 9)}
calls = []
r = stub(mine, pages, 4, calls)
check("it bounded its page reads", len(calls) <= 4, calls)
check("it returned what it had rather than hanging", isinstance(r["apps"], list))

print("== search goes through the same door ==")
catalog.store_apps = lambda *a, **k: [app("athena-comfyui", "https://github.com/pinokiofactory/comfy.git")]
catalog._page_cached = lambda page, per_page, q, tag, sort: {
    "items": [app("github-com-pinokiofactory-comfy", "https://github.com/pinokiofactory/comfy")],
    "total": 1, "totalPages": 1}
s = catalog.search(HOME, "comfy")
check("search does not show our app twice",
      [a["slug"] for a in s["apps"]] == ["athena-comfyui"], [a["slug"] for a in s["apps"]])

print("== the repository is read back out of any URL we are configured with ==")
check("a raw contents URL",
      catalog._raw_root("https://raw.githubusercontent.com/imzacksong/athena-store/main/index.json")
      == "https://raw.githubusercontent.com/imzacksong/athena-store")
check("a blob URL", catalog._raw_root("https://github.com/imzacksong/athena-store/blob/main/index.json")
      == "https://raw.githubusercontent.com/imzacksong/athena-store")
check("an owner/name shorthand", catalog._raw_root("imzacksong/athena-store")
      == "https://raw.githubusercontent.com/imzacksong/athena-store")
check("something else is not a repository", catalog._raw_root("https://example.com/x.json") is None)

print("== an icon too big to inline is downscaled, not dropped ==")
import io
from PIL import Image
import os

big = io.BytesIO()
# noise, not a flat colour: a solid 1024px square compresses far below any cap
Image.frombytes("RGB", (1024, 1024), __import__("os").urandom(1024 * 1024 * 3)).save(big, "PNG")
b = big.getvalue()
check("the test icon is over the inline cap", len(b) > catalog._ICON_INLINE_MAX, len(b))
small = catalog._shrink_icon(b)
check("it shrinks to something small", small and len(small) < catalog._ICON_INLINE_MAX,
      len(small) if small else None)
check("and it is still a PNG", bool(small) and small[:8] == b"\x89PNG\r\n\x1a\n")
check("unreadable bytes give up instead of raising", catalog._shrink_icon(b"not an image") is None)

print(f"\n{PASS} check(s) passed, {FAIL} failed")
if FAILURES:
    print("failed: " + "; ".join(FAILURES))
    sys.exit(1)