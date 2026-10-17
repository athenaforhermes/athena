"""Athena's view of the public app store.

The store serves a JSON API at ``api.pinokio.co``, so Athena reads the catalogue
as data: no HTML, no page scraping, no flight payloads to unwind. That means
titles, descriptions, icons, tags and platform support all arrive as fields
rather than something to reconstruct out of a rendered page.

The whole store is ~6,400 apps across 64 pages and takes about 40 seconds to
walk, so it is cached on disk and refreshed in the background. Callers never
wait on a cold cache: they get whatever is on disk (or a first page) plus a
``warming`` flag, and the next call has the rest.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

API = "https://api.pinokio.co/apps"      # the global catalogue, as JSON
STORE_ORIGIN = "https://pinokio.co"       # the shopfront, for its tag cloud
UA = "Athena/1.0 (+local Hermes app store)"

# Athena's own store. The global catalogue is a JSON API listing other people's
# repositories; this one is a repository *of apps*: `main` holds an index, every
# other branch holds one app, and an app is installed by cloning its branch.
# Reading it is the same idea as reading the global catalogue — one small index
# instead of 64 pages — so it is fetched whole and filtered here, where the
# global store filters on its own server.
STORE_REPO = "athenaforhermes/athena-store"
STORE_URL = f"https://github.com/{STORE_REPO}"
STORE_BRANCH = "main"
STORE_INDEX = f"https://raw.githubusercontent.com/{STORE_REPO}/{STORE_BRANCH}/index.json"
STORE_TTL = 3600           # an index of installed apps changes when a human changes it

INDEX_TTL = 24 * 3600      # a full crawl is ~40s; daily is plenty
PAGE_SIZE = 100            # the API's ceiling (it clamps anything higher)

# One crawl at a time, process-wide. Without this, a UI that polls while the
# cache is cold starts a fresh 64-request walk on every poll.
_crawl_lock = threading.Lock()
_crawl_thread: Optional[threading.Thread] = None


class CatalogError(RuntimeError):
    """The store could not be read."""


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #

def _cache_dir(home: Path) -> Path:
    d = Path(home) / "cache" / "athena-catalog"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path(home: Path) -> Path:
    return _cache_dir(home) / "index.json"


# The parsed index, held in memory. /apps decorates a handful of installed apps
# with catalogue titles and is polled every 6 seconds; re-reading and re-parsing
# the whole multi-thousand-row index on each call cost ~110ms a time and was most
# of that route. Keyed on mtime_ns + size, so _write_index's atomic replace
# invalidates the memo for free. Callers get a shallow copy: they may add their
# own keys, and none of that belongs in the cache.
_INDEX_MEMO: Dict[str, Any] = {"key": None, "data": None}


def _read_index(home: Path) -> Optional[Dict[str, Any]]:
    path = _index_path(home)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    if _INDEX_MEMO["key"] == key and _INDEX_MEMO["data"] is not None:
        return dict(_INDEX_MEMO["data"])
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("apps"):
            _INDEX_MEMO["key"] = key
            _INDEX_MEMO["data"] = data
            return dict(data)
    except Exception:  # noqa: BLE001 - a bad cache is just a cold cache
        pass
    return None


def _write_index(home: Path, apps: List[Dict[str, Any]]) -> Dict[str, Any]:
    data = {"apps": apps, "count": len(apps), "fetched_at": time.time()}
    path = _index_path(home)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    tmp.replace(path)          # atomic: a killed crawl never leaves a stub
    return data


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #

def _get(url: str, timeout: int = 60, tries: int = 3) -> Dict[str, Any]:
    last: Optional[Exception] = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 - retried below
            last = e
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise CatalogError(f"{url} failed: {last}")


def _page(page: int, per_page: int = PAGE_SIZE, **params: Any) -> Dict[str, Any]:
    qs: Dict[str, Any] = {"page": page, "perPage": per_page}
    for k, v in params.items():
        if v not in (None, "", []):
            qs[k] = v
    # urlencode, not a hand-rolled join: search terms contain spaces, and an
    # unescaped space truncates the query, which silently degrades every
    # multi-word search to the local cache.
    url = API + "?" + urllib.parse.urlencode(qs)
    return _get(url)


def crawl(home: Path, progress: Optional[Callable[[int, int, int], None]] = None,
          per_page: int = PAGE_SIZE) -> List[Dict[str, Any]]:
    """Walk every page and return the de-duplicated catalogue."""
    first = _page(1, per_page)
    pages = int(first.get("totalPages") or 1)
    total = int(first.get("total") or 0)

    apps: List[Dict[str, Any]] = []
    seen = set()

    def absorb(payload: Dict[str, Any]) -> None:
        for item in payload.get("items") or []:
            # The API sometimes wraps each row as {"app": {...}} and sometimes
            # returns the record directly, depending on the endpoint.
            rec = item.get("app") if isinstance(item, dict) and "app" in item else item
            if not isinstance(rec, dict):
                continue
            slug = rec.get("slug")
            if slug and slug not in seen:
                seen.add(slug)
                apps.append(rec)

    absorb(first)
    for p in range(2, pages + 1):
        try:
            absorb(_page(p, per_page))
        except CatalogError:
            # One flaky page should not throw away 60 good ones.
            continue
        if progress:
            progress(p, pages, len(apps))
        time.sleep(0.1)                 # be a good citizen

    if not apps:
        raise CatalogError("the store returned no apps")
    return apps


# --------------------------------------------------------------------------- #
# background warming
# --------------------------------------------------------------------------- #

def _crawl_worker(home: Path) -> None:
    global _crawl_thread
    try:
        with _crawl_lock:
            apps = crawl(home)
            _write_index(home, apps)
    except Exception:  # noqa: BLE001 - warming is best effort
        pass
    finally:
        _crawl_thread = None


def warm(home: Path, force: bool = False) -> bool:
    """Start a background crawl if one is not already running.

    Returns True if a crawl was started.
    """
    global _crawl_thread
    if _crawl_thread is not None and _crawl_thread.is_alive():
        return False
    cached = _read_index(home)
    if cached and not force:
        age = time.time() - float(cached.get("fetched_at") or 0)
        if age < INDEX_TTL:
            return False
    _crawl_thread = threading.Thread(target=_crawl_worker, args=(home,),
                                     daemon=True, name="athena-catalog-warm")
    _crawl_thread.start()
    return True


# --------------------------------------------------------------------------- #
# Athena's own store
# --------------------------------------------------------------------------- #

_SOURCE_MEMO: Dict[str, Any] = {"key": None, "data": None}

# Why the last read of Athena's own index failed, if it did. The store index is
# a placeholder today: the repository exists but its catalogue is empty, so the
# interesting distinction is between "read it, there is nothing in it yet" and
# "could not read it at all". fetch_source already tells the two apart in its
# return value; this remembers which one happened so browse() can pass it on to
# the panel without fetching the index a second time (a stubbed store_apps in
# the paging tests must not turn into a network read).
_SOURCE_STATUS: Dict[str, Any] = {"error": ""}


def _source_path(home: Path) -> Path:
    return _cache_dir(home) / "store.json"


def default_source() -> str:
    return STORE_INDEX


def source_setting_path(home: Path) -> Path:
    """Where the settings owner records which index to read.

    The store source is a setting, but the settings file belongs to the HTTP
    layer and the catalogue is a module that only knows a home directory. Rather
    than have two modules each parse the settings file and each own half of the
    validation, the writer drops the resolved URL here and the reader picks it
    up: one writer, one reader, no import order to depend on.
    """
    return _cache_dir(home) / "source.json"


def set_source(home: Path, index_url: Optional[str]) -> None:
    path = source_setting_path(home)
    payload = {"index": (index_url or "").strip() or default_source()}
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    tmp.replace(path)
    _SOURCE_MEMO["key"] = None


def source_url(home: Path) -> str:
    try:
        raw = json.loads(source_setting_path(home).read_text(encoding="utf-8"))
        return (raw.get("index") or "").strip() or default_source()
    except Exception:  # noqa: BLE001 - no setting written yet is the normal case
        return default_source()


_GH_TOKEN: Dict[str, Any] = {"token": None, "at": 0.0}


def _gh_token() -> str:
    """The GitHub token Hermes already holds, so a private store just works.

    Athena reuses Hermes' login instead of asking for a second one; the token is
    read from the CLI that already owns it and cached briefly, because this runs
    on the browse path.
    """
    if _GH_TOKEN["token"] is not None and time.time() - _GH_TOKEN["at"] < 60:
        return _GH_TOKEN["token"]
    token = ""
    try:
        import shutil as _shutil
        import subprocess as _sp
        gh = _shutil.which("gh")
        if gh:
            out = _sp.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
            token = (out.stdout or "").strip()
    except Exception:  # noqa: BLE001 - an unauthenticated read still works for a public store
        token = ""
    _GH_TOKEN.update({"token": token, "at": time.time()})
    return token


def _to_api_url(url: str) -> Optional[str]:
    """The contents-API equivalent of a raw or web GitHub URL.

    `raw.githubusercontent.com` serves a private file to a token in principle,
    but the API endpoint is the documented way to read one, so a failure on the
    raw route retries here rather than reporting the store as missing.
    """
    pats = (
        r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$",
        r"^https?://github\.com/([^/]+)/([^/]+)/(?:raw|blob)/([^/]+)/(.+)$",
    )
    for pat in pats:
        m = re.match(pat, url)
        if m:
            owner, repo, ref, path = m.groups()
            return (f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
                    f"?ref={urllib.parse.quote(ref)}")
    m = re.match(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?$", url.strip())
    if m:
        owner, repo = m.groups()
        return (f"https://api.github.com/repos/{owner}/{repo}/contents/index.json"
                f"?ref={STORE_BRANCH}")
    return None


def _get_json(url: str, timeout: int = 30, token: str = "") -> Any:
    """Fetch JSON, retrying through the GitHub API for a private store."""
    attempts = [url]
    api = _to_api_url(url)
    if api and api != url:
        attempts.append(api)
    last: Optional[Exception] = None
    for i, target in enumerate(attempts):
        headers = {"User-Agent": UA, "Accept": "application/vnd.github.raw"}
        if token and "github" in target:
            headers["Authorization"] = f"Bearer {token}"
        try:
            req = urllib.request.Request(target, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            return json.loads(body)
        except urllib.error.HTTPError as e:
            last = e
            # A private file answers 404 to an anonymous read; that is the case
            # the API route exists for, so try it before giving up.
            if i == 0 and e.code in (401, 403, 404):
                continue
            break
        except Exception as e:  # noqa: BLE001 - tried again below
            last = e
    raise CatalogError(f"{url} failed: {last}")


def _read_source_cache(home: Path) -> Optional[Dict[str, Any]]:
    path = _source_path(home)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    if _SOURCE_MEMO["key"] == key and _SOURCE_MEMO["data"] is not None:
        return dict(_SOURCE_MEMO["data"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("apps"), list):
            _SOURCE_MEMO.update({"key": key, "data": data})
            return dict(data)
    except Exception:  # noqa: BLE001 - a bad cache is a cold cache
        pass
    return None


def fetch_source(home: Path, force: bool = False) -> Dict[str, Any]:
    """The Athena Store index, cached on disk and refreshed when stale.

    Never raises: a store that cannot be read is a store with no apps in it. The
    global catalogue is still browsable, and an unreachable index must not take
    the Store tab down with it.
    """
    url = source_url(home)
    if not url:
        return {"apps": [], "index": "", "reason": "no store source set"}

    cached = _read_source_cache(home)
    if cached and cached.get("index") == url and not force:
        age = time.time() - float(cached.get("fetched_at") or 0)
        if age < STORE_TTL:
            _SOURCE_STATUS["error"] = ""
            return cached

    try:
        payload = _get_json(url, token=_gh_token())
    except CatalogError as e:
        # Remembered for browse(): the panel says "coming soon" for an
        # unpublished catalogue and must not claim a failure it can explain.
        _SOURCE_STATUS["error"] = str(e)
        if cached:
            out = dict(cached)
            out["stale"] = str(e)
            return out
        return {"apps": [], "index": url, "error": str(e)}

    _SOURCE_STATUS["error"] = ""

    apps = payload.get("apps") if isinstance(payload, dict) else None
    if not isinstance(apps, list):
        apps = payload if isinstance(payload, list) else []
    data = {
        "apps": apps,
        "index": url,
        "repo": (payload.get("repo") if isinstance(payload, dict) else None) or STORE_URL,
        "store": (payload.get("store") if isinstance(payload, dict) else None) or "Athena Store",
        "updated": payload.get("updated") if isinstance(payload, dict) else None,
        "fetched_at": time.time(),
    }
    path = _source_path(home)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    tmp.replace(path)
    _SOURCE_MEMO["key"] = None
    return data


def _is_athena(app: Dict[str, Any]) -> bool:
    return bool(app.get("athena")) or bool(app.get("ref"))


def _match(apps: List[Dict[str, Any]], q: str, tag: str) -> List[Dict[str, Any]]:
    out = apps
    if q:
        terms = [t for t in q.lower().split() if t]

        def hit(a: Dict[str, Any]) -> bool:
            blob = " ".join([a.get("title") or "", a.get("slug") or "",
                             a.get("description") or "",
                             " ".join(_tag_names(a))]).lower()
            return all(t in blob for t in terms)

        out = [a for a in out if hit(a)]
    if tag:
        out = [a for a in out if any(t == tag or t.startswith(tag + "-")
                                     for t in _tag_names(a))]
    return out


def _sort_store(apps: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    if sort == "name":
        return sorted(apps, key=lambda a: (a.get("title") or "").lower())
    if sort == "latest":
        return sorted(apps, key=lambda a: a.get("updated") or "", reverse=True)
    # The store's own apps lead: they are the ones this machine was set up for.
    return sorted(apps, key=lambda a: (0 if _is_athena(a) else 1,
                                       (a.get("title") or "").lower()))


def store_apps(home: Path, q: str = "", tag: str = "", sort: str = "trending",
               force: bool = False) -> List[Dict[str, Any]]:
    data = fetch_source(home, force=force)
    return _sort_store(_match(data.get("apps") or [], q, tag), sort)


# --------------------------------------------------------------------------- #
# public surface
# --------------------------------------------------------------------------- #

def _tag_names(app: Dict[str, Any]) -> List[str]:
    """Tag names from a raw store record.

    The API nests each tag as ``{"tag": "music", "count": 2}`` rather than a
    bare string, so anything working with a cached record has to unwrap it.
    """
    out = []
    for t in app.get("tags") or []:
        if isinstance(t, dict):
            name = t.get("tag")
        else:
            name = t
        if name:
            out.append(str(name))
    return out


def _without_ours(apps: List[Dict[str, Any]], ours: set) -> Any:
    """(the apps that are not ours, how many were).

    The count matters: the global catalogue's total includes our apps under its
    own slugs, so a total that adds both without noticing is inflated by exactly
    the number of apps we recognised as ours on the pages we read.
    """
    if not ours:
        return apps, 0
    kept = [a for a in apps if not (_repo_key(a) and _repo_key(a) in ours)]
    return kept, len(apps) - len(kept)


def _repo_key(app: Dict[str, Any]) -> str:
    """The app behind a record, independent of which catalogue it came from.

    The same app is `athena-comfyui` in our store and
    `github-com-pinokiofactory-comfy` in the global catalogue, with the same
    repository written two ways (`.../comfy.git` and `.../comfy`). The repository
    is the identity; the slug is a per-catalogue naming accident. Empty string
    when nothing identifies it, so callers can leave those alone.
    """
    url = (app.get("repoUrl") or app.get("repo") or "").strip().lower()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    if url.endswith(".git"):
        url = url[:-4]
    return url.rstrip("/")


_ICON_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
              ".ico": "image/x-icon", ".bmp": "image/bmp"}


def _raw_root(url: str) -> Optional[str]:
    """`https://raw.githubusercontent.com/<owner>/<repo>` for any URL of the repo."""
    pats = (
        r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/",
        r"^https?://github\.com/([^/]+)/([^/]+)/(?:raw|blob)/",
        r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?$",
    )
    for pat in pats:
        m = re.match(pat, (url or "").strip())
        if m:
            owner, repo = m.groups()
            return f"https://raw.githubusercontent.com/{owner}/{repo}"
    return None


def _get_bytes(url: str, timeout: int = 30, token: str = "") -> bytes:
    """Fetch bytes, retrying through the GitHub API for a private store."""
    attempts = [url]
    api = _to_api_url(url)
    if api and api != url:
        attempts.append(api)
    last: Optional[Exception] = None
    for i, target in enumerate(attempts):
        headers = {"User-Agent": UA, "Accept": "application/vnd.github.raw"}
        if token and "github" in target:
            headers["Authorization"] = f"Bearer {token}"
        try:
            req = urllib.request.Request(target, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = e
            if i == 0 and e.code in (401, 403, 404):
                continue
            break
        except Exception as e:  # noqa: BLE001 - tried again below
            last = e
    raise CatalogError(f"{url} failed: {last}")


# Inlining an icon is only worth it while it stays small: one of these files is
# 2.3 MB, and a base64 copy of that in every catalogue response is a page nobody
# wants to wait for. Anything bigger than this is downscaled first.
_ICON_INLINE_MAX = 64_000


def _shrink_icon(data: bytes) -> Optional[bytes]:
    """A small PNG of the same picture, or None if it cannot be read."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        im.thumbnail((160, 160))
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - an icon that cannot be read is a letter
        return None


def _icon_of(home: Optional[Path], app: Dict[str, Any]) -> Optional[str]:
    """Where the UI should load this app's icon from.

    Our own store is a private repository, so the raw URL in the index answers
    404 to anyone without a token and the cards fell back to a letter. The icon
    is a file in the app's own branch, and Athena holds the token, so fetch it
    once, cache it beside the store's other state, and hand the UI bytes it can
    draw with no credentials at all. Public catalogue apps stay hotlinked.
    """
    if not _is_athena(app):
        return app.get("iconUrl")
    name = str(app.get("icon") or "").strip()
    if not name or "/" in name or "\\" in name or ":" in name:
        return app.get("iconUrl")          # a URL, or nothing to fetch
    if home is None:
        return app.get("iconUrl")
    slug = re.sub(r"[^\w.-]", "_", str(app.get("slug") or "icon"))[:64]
    ext = Path(name).suffix.lower()
    path = home / "cache" / "store-icons" / f"{slug}{ext}"
    if not path.is_file() or path.stat().st_size == 0:
        root = _raw_root(source_url(home))
        if not root:
            return app.get("iconUrl")
        ref = urllib.parse.quote(str(app.get("ref") or STORE_BRANCH))
        try:
            data = _get_bytes(f"{root}/{ref}/{urllib.parse.quote(name)}",
                              token=_gh_token())
        except CatalogError:
            return app.get("iconUrl")      # unreachable icon is a letter, not an error
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            pass
    try:
        data = path.read_bytes()
        if len(data) > _ICON_INLINE_MAX:
            thumb = path.parent / (path.stem + ".thumb.png")
            if not thumb.is_file():
                small = _shrink_icon(data)
                if small:
                    try:
                        thumb.write_bytes(small)
                    except OSError:
                        pass
            if thumb.is_file():
                return "data:image/png;base64," + base64.b64encode(
                    thumb.read_bytes()).decode("ascii")
        mime = _ICON_MIME.get(ext, "image/png")
        return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
    except OSError:
        return app.get("iconUrl")


def _enrich(app: Dict[str, Any], home: Optional[Path] = None) -> Dict[str, Any]:
    """Normalise one store record into the shape the UI and tools consume."""
    tags = _tag_names(app)
    return {
        "id": app.get("id"),
        "slug": app.get("slug"),
        "name": app.get("title") or app.get("slug"),
        "title": app.get("title") or app.get("slug"),
        "description": (app.get("description") or "").strip(),
        "version": app.get("version"),
        "icon": _icon_of(home, app),
        "icon_pending": bool(app.get("iconPending")),
        "repo": app.get("repoUrl"),
        "tags": tags,
        "listed": app.get("listed"),
        "status": app.get("status"),
        "updated": app.get("repoUpdatedAt"),
        "indexed": app.get("indexedAt"),
        "platforms": app.get("checkinsPlatforms") or {},
        "gpus": app.get("checkinsGpus") or {},
        "owner": (app.get("verifiedOwner") or {}).get("username") if app.get("verifiedOwner") else None,
        "checkins": app.get("checkinsCount") or 0,
        "score": app.get("score"),
        # Which store this came from, and — for ours — the branch to clone. The
        # UI badges our own apps with it; the installer needs the ref, because a
        # global-catalogue app is a whole repository and one of ours is a branch
        # of ours.
        "store": "athena" if _is_athena(app) else "global",
        "ref": app.get("ref"),
        "install": app.get("install"),
        "converted": bool((app.get("athena") or {}).get("converted")),
        "guide": (app.get("athena") or {}).get("guide"),
        "capabilities": (app.get("athena") or {}).get("capabilities"),
    }


def index(home: Path, force: bool = False) -> Dict[str, Any]:
    """The cached catalogue, kicking off a crawl if any of it is missing."""
    cached = _read_index(home)
    if cached and not force:
        started = warm(home)            # refreshes behind the caller if stale
        return {"apps": cached["apps"], "count": len(cached["apps"]),
                "fetched_at": cached.get("fetched_at"), "warming": started}
    if cached and force:
        warm(home, force=True)
        return {"apps": cached["apps"], "count": len(cached["apps"]),
                "fetched_at": cached.get("fetched_at"), "warming": True}

    # Cold: hand back one page so the store is never blank, and crawl the rest.
    warm(home)
    try:
        first = _page(1)
        quick = []
        for item in first.get("items") or []:
            rec = item.get("app") if "app" in item else item
            if isinstance(rec, dict):
                quick.append(rec)
    except CatalogError:
        quick = []
    return {"apps": quick, "count": len(quick), "fetched_at": 0, "warming": True}


def search(home: Path, q: str, limit: int = 60) -> Dict[str, Any]:
    """Free-text search, answered by the store and cached as a fallback."""
    q = (q or "").strip()
    if not q:
        return {"apps": [], "count": 0, "query": q}
    res = browse(home, limit=limit, q=q)
    res["query"] = q
    return res


# Store pages come over the network and the Store tab re-requests its landing
# page on every mount (~0.5-1.1s measured). The catalogue behind it is crawled
# periodically, so a minute of staleness is invisible on a browse page while a
# reopen becomes instant. Only successful payloads are memoised: a store that is
# down has to stay retryable rather than be cached as down.
_BROWSE_MEMO: Dict[Any, tuple] = {}
BROWSE_TTL = 60.0


def _page_cached(page: int, per_page: int, q: str, tag: str, sort: str) -> Dict[str, Any]:
    key = (page, per_page, q or "", tag or "", sort or "")
    hit = _BROWSE_MEMO.get(key)
    if hit is not None and time.time() - hit[0] < BROWSE_TTL:
        return hit[1]
    payload = _page(page, per_page, q=q or None, tag=tag or None, sort=sort or None)
    if len(_BROWSE_MEMO) > 64:      # bounded: this holds whole pages
        _BROWSE_MEMO.clear()
    _BROWSE_MEMO[key] = (time.time(), payload)
    return payload


def browse(home: Path, offset: int = 0, limit: int = 60, q: str = "",
           tag: str = "", sort: str = "trending") -> Dict[str, Any]:
    """A page of the catalogue, optionally searched or filtered by tag.

    This asks the store rather than filtering the cache. The cached records
    carry only each app's leading tags, so a local filter undercounts against
    the store it is meant to mirror, and the store's sorts (trending, popular)
    are its own ranking rather than something to approximate. The cache is the
    fallback for when the store cannot be reached.
    """
    per_page = max(1, min(int(limit or 60), PAGE_SIZE))
    offset = max(0, int(offset or 0))

    # Two catalogues, one page. Athena's own apps come first — they are the ones
    # this machine has chosen — and the global catalogue fills the rest of the
    # page, so paging through the store still walks all of it. Our apps are
    # filtered here because there is no server behind a repository index, and
    # they take part of every page, so the global page to read moves up by
    # however many of ours the caller has not passed yet.
    mine = store_apps(home, q, tag, sort)
    # Empty is the normal state of the published catalogue today, so it is a
    # fact to report, not a failure: no apps plus no recorded error is "nothing
    # published yet", and the panel says so rather than showing a blank shelf.
    athena_error = "" if mine else str(_SOURCE_STATUS.get("error") or "")
    window: List[Dict[str, Any]] = []
    if offset < len(mine):
        window = [_enrich(a, home) for a in mine[offset: offset + per_page]]
    remaining = per_page - len(window)
    global_offset = max(0, offset - len(mine))
    page = global_offset // per_page + 1
    skip = global_offset % per_page

    ours = {_repo_key(a) for a in mine}
    ours.discard("")
    try:
        # Our apps are also in the global catalogue under the global slug, so
        # without this the same app appears twice on the page the user lands on.
        # Skip ours and keep reading pages until the window is full — the page is
        # `per_page` wide, not `per_page` minus however many of ours turned up.
        items: List[Dict[str, Any]] = []
        total = 0
        dropped = 0
        at, start = page, skip
        for _ in range(4):                       # bounded: top up, never crawl
            try:
                payload = _page_cached(at, per_page, q, tag, sort)
            except CatalogError:
                if at == page:
                    raise                        # nothing at all — use the cache
                break                            # ran out of pages to top up from
            total = payload.get("total", total)
            batch: List[Dict[str, Any]] = []
            for item in payload.get("items") or []:
                rec = item.get("app") if isinstance(item, dict) and "app" in item else item
                if isinstance(rec, dict):
                    batch.append(_enrich(rec, home))
            kept, lost = _without_ours(batch, ours)
            dropped += lost
            items.extend(kept[start:])
            start = 0
            at += 1
            if len(items) >= remaining or not batch:
                break
        items = items[:remaining]
        return {
            "apps": window + items,
            "page": page,
            "per_page": per_page,
            "offset": offset,
            "count": len(window) + len(items),
            "total": max(0, (total or 0) - dropped) + len(mine),
            "total_pages": payload.get("totalPages"),
            "tag": tag, "sort": sort, "q": q,
            "athena_store": len(mine),
            "athena_empty": not mine,
            "athena_error": athena_error,
            "source": "athena+store" if mine else "store",
            "warming": False,
        }
    except CatalogError as e:
        # _browse_local already windows the cached catalogue at this offset, so
        # take the head of its page rather than slicing it a second time.
        rest = _browse_local(home, global_offset, max(remaining, 1), q, tag, sort, str(e))
        kept, dropped = _without_ours(rest["apps"], ours)
        rest["apps"] = window + kept[:remaining]
        rest["count"] = len(rest["apps"])
        rest["total"] = max(0, (rest.get("total") or 0) - dropped) + len(mine)
        rest["athena_store"] = len(mine)
        rest["athena_empty"] = not mine
        rest["athena_error"] = athena_error
        if window or mine:
            rest["source"] = "athena+cache"
        return rest


def _browse_local(home: Path, offset: int, limit: int, q: str, tag: str,
                  sort: str, why: str) -> Dict[str, Any]:
    """Serve a page out of the cached catalogue when the store is unreachable."""
    data = index(home)
    apps = data["apps"]

    if q:
        terms = [t for t in q.lower().split() if t]

        def hit(a: Dict[str, Any]) -> bool:
            blob = " ".join([a.get("title") or "", a.get("slug") or "",
                             a.get("description") or "",
                             " ".join(_tag_names(a))]).lower()
            return all(t in blob for t in terms)

        apps = [a for a in apps if hit(a)]

    if tag:
        def matches(a: Dict[str, Any]) -> bool:
            return any(t == tag or t.startswith(tag + "-") for t in _tag_names(a))

        apps = [a for a in apps if matches(a)]

    if sort == "latest":
        apps = sorted(apps, key=lambda a: a.get("repoUpdatedAt") or "", reverse=True)
    elif sort == "popular":
        apps = sorted(apps, key=lambda a: (a.get("checkinsCount") or 0,
                                           a.get("score") or 0), reverse=True)
    elif sort == "name":
        apps = sorted(apps, key=lambda a: (a.get("title") or "").lower())
    else:
        apps = sorted(apps, key=lambda a: (a.get("score") or 0,
                                           a.get("checkinsCount") or 0), reverse=True)

    window = apps[offset: offset + limit]
    return {"apps": [_enrich(a, home) for a in window],
            "offset": offset, "limit": limit,
            "count": len(apps), "total": len(apps),
            "tag": tag, "sort": sort, "q": q,
            "source": "cache", "degraded": why,
            "warming": data.get("warming", False)}


TAG_TTL = 7 * 24 * 3600
# The store's filter chips carry their own counts, e.g. "#voice (16 apps)".
# Match the text rather than the surrounding quoting: inside the flight stream
# the attribute arrives escaped, so anchoring on `title="` never matches.
_TAG_RE = re.compile(r"#([a-z0-9][a-z0-9._-]*) \((\d+) apps?\)")


def tags(home: Path, limit: int = 300) -> Dict[str, Any]:
    """The store's own tag list, with the store's own counts.

    Counts are taken from the store rather than tallied out of the cached
    records: each record carries only its top few tags, so a local tally
    systematically undercounts (voice reads as 9 locally, 16 on the store).
    The store renders its filter cloud with the real numbers in the chip
    titles, so one page read is both cheaper and more correct.
    """
    path = _cache_dir(home) / "tags.json"
    store_counts: Dict[str, int] = {}
    try:
        if path.exists() and time.time() - path.stat().st_mtime < TAG_TTL:
            with open(path, "r", encoding="utf-8") as fh:
                store_counts = dict(json.load(fh).get("counts") or {})
    except Exception:  # noqa: BLE001
        store_counts = {}

    if not store_counts:
        try:
            req = urllib.request.Request(f"{STORE_ORIGIN}/apps",
                                         headers={"User-Agent": UA, "RSC": "1"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read().decode("utf-8", "replace")
            for name, count in _TAG_RE.findall(body):
                if name.isdigit():
                    continue        # a chip reading "#1 (37 apps)" is not a tag
                store_counts.setdefault(name, int(count))
            if store_counts:
                tmp = path.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"counts": store_counts}, fh)
                tmp.replace(path)
        except Exception:  # noqa: BLE001 - the local tally below still applies
            store_counts = {}

    # The shopfront only renders its leading chips, so the store's numbers cover
    # the popular tags and the local tally has to supply the long tail. Store
    # counts win where both know a tag, because the local one undercounts.
    data = index(home)
    counts: Dict[str, int] = {}
    for a in data["apps"]:
        for t in _tag_names(a):
            counts[t] = counts.get(t, 0) + 1
    counts.update(store_counts)

    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"tags": [{"tag": t, "count": c} for t, c in ordered[:limit]],
            "count": len(counts),
            "source": "store+local" if store_counts else "local",
            "warming": data.get("warming", False)}


def detail(home: Path, ref: str) -> Dict[str, Any]:
    """One app, live from the store, falling back to the cached copy."""
    ref = (ref or "").strip()
    if not ref:
        raise CatalogError("no app given")
    low_first = ref.lower()
    for a in fetch_source(home).get("apps") or []:
        if ((a.get("slug") or "").lower() == low_first
                or (a.get("title") or "").lower() == low_first):
            return _enrich(a, home)
    try:
        payload = _get(f"{API}/{urllib.parse.quote(ref)}", timeout=30)
        rec = payload.get("app") if isinstance(payload, dict) else None
        if isinstance(rec, dict) and rec.get("slug"):
            return _enrich(rec, home)
    except Exception:  # noqa: BLE001 - fall through to the cache
        pass

    low = ref.lower()
    for a in index(home)["apps"]:
        if (a.get("slug") or "").lower() == low or f"/{low}" in (a.get("repoUrl") or "").lower():
            return _enrich(a, home)
    raise CatalogError(f"no app called '{ref}'")


def resolve(home: Path, ref: str) -> Dict[str, Any]:
    """Turn anything a user might type into a git URL.

    Accepts a repo URL, a slug, a bare app name, or "owner/repo" — and only
    reports success when it actually has a repository to clone, because a
    resolution that silently yields no repo sends the caller off to `git clone`
    a name that was never a repository.
    """
    ref = (ref or "").strip()
    if not ref:
        return {"error": "no app given"}

    # "owner/repo#branch" — how one of Athena's own apps is named. An app of
    # ours lives on a branch of the store repository, so the branch is what
    # makes it an app rather than the repository it came from. A pasted
    # ".../tree/<branch>" URL means the same thing.
    branchy = re.match(
        r"^(?:https?://github\.com/)?(?P<repo>[\w.-]+/[\w.-]+?)(?:\.git)?"
        r"(?:#|/tree/)(?P<ref>[\w.\-/]+)$", ref)
    if branchy:
        repo = branchy.group("repo")
        branch = branchy.group("ref").strip("/")
        hit = None
        for a in fetch_source(home).get("apps") or []:
            if (a.get("ref") or "") == branch or (a.get("slug") or "") == branch:
                hit = a
                break
        return {
            "slug": (hit or {}).get("slug") or branch,
            "name": _upstream_name(hit) or (hit or {}).get("title") or branch,
            "git": f"https://github.com/{repo}",
            "ref": branch,
            "repo": (hit or {}).get("repoUrl") or f"https://github.com/{repo}",
            "description": (hit or {}).get("description"),
            "icon": (hit or {}).get("iconUrl"),
            "tags": _tag_names(hit or {}),
            "store": "athena",
            "matched": "branch",
        }

    if ref.lower().startswith(("http://", "https://", "git@")):
        return {"slug": None, "name": ref, "git": ref, "repo": ref,
                "source": "url"}

    # Our own apps first: a name that means something in the Athena Store must
    # not be answered by a same-named stranger in the global catalogue.
    mine = fetch_source(home).get("apps") or []
    low_mine = ref.lower().rstrip("/")
    for a in mine:
        if (a.get("slug") or "").lower() == low_mine:
            return _resolved(a, "athena-slug")
    for a in mine:
        if (a.get("title") or "").lower() == low_mine:
            return _resolved(a, "athena-title")

    data = index(home)
    apps = data["apps"]
    low = ref.lower().rstrip("/")

    # slug, then repo URL, then exact title, then a name search
    for a in apps:
        if (a.get("slug") or "").lower() == low:
            return _resolved(a, "slug")

    for a in apps:
        repo = (a.get("repoUrl") or "").lower()
        if repo == low or repo.endswith("/" + low):
            return _resolved(a, "repo")

    for a in apps:
        if (a.get("title") or "").lower() == low:
            return _resolved(a, "title")

    # "owner/repo" against the repo URL
    if "/" in low:
        for a in apps:
            if low in (a.get("repoUrl") or "").lower():
                return _resolved(a, "repo-partial")

    res = search(home, ref, limit=1)
    if res["apps"]:
        found = res["apps"][0]
        for a in apps:
            if a.get("slug") == found["slug"]:
                return _resolved(a, "search")

    return {"slug": None, "name": ref, "git": "", "repo": "",
            "error": f"no app called '{ref}' in the store",
            "warming": data.get("warming", False)}


def _upstream_name(app: Optional[Dict[str, Any]]) -> Optional[str]:
    """The folder an app belongs in, named the way the engine names it.

    Installing from upstream puts an app in `api/<repo>.git`; installing the
    same app from Athena's store must not put it somewhere else, or the same app
    installed twice becomes two apps and neither one notices the other.
    """
    if not app:
        return None
    url = (app.get("repoUrl") or "").rstrip("/")
    if not url:
        return None
    leaf = re.sub(r"[^A-Za-z0-9._-]+", "-", url.split("/")[-1].removesuffix(".git")).strip("-")
    return leaf + ".git" if leaf else None


def _resolved(app: Dict[str, Any], how: str) -> Dict[str, Any]:
    e = _enrich(app)
    out = {"slug": e["slug"], "name": e["name"], "git": e["repo"],
           "repo": e["repo"], "description": e["description"],
           "icon": e["icon"], "tags": e["tags"], "matched": how,
           "store": e["store"], "ref": e.get("ref"),
           "converted": e.get("converted"), "guide": e.get("guide"),
           "capabilities": e.get("capabilities")}
    if e["store"] == "athena":
        # Clone the branch, and name the folder after the app upstream so the
        # same app does not exist twice under two names.
        repo = STORE_URL
        m = re.match(r"^(https?://[^#]+)", (e.get("install") or ""))
        if m:
            repo = m.group(1)
        out["git"] = repo
        out["folder"] = _upstream_name(app) or (str(e["slug"]) + ".git")
        if not e.get("ref"):
            out["error"] = (f"'{e['slug']}' is listed in the Athena Store without "
                            f"a branch, so there is nothing to install")
    return out
